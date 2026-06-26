"""
jarvis_core.py — JARVIS Core Agent Runtime (production)
=========================================================
CofCITIP — College of Charleston IT Infrastructure Platform

Architecture:
  GLaDOS (voice in)
    -> jarvis_core.py  (this file — the brain)
      -> ChromaDB      (memory: context + behavioral collections)
      -> Ollama        (local inference + native tool routing)
      -> tools/        (intune, jamf, taegis, servicenow, briefing, ...)
    -> GLaDOS (voice out)

Zero-egress design:
  All sensitive ops data (fleet, compliance, SIEM) stays local via Ollama.
  Claude API is only ever used for non-sensitive research queries when
  EGRESS_MODE=auto and no ops tool was called. Any query that touched a
  tool in TOOL_DATA_LOCAL is hard-blocked from egress at summarize time.
  FERPA is a standing requirement — no student/institutional data leaves campus.

Run modes:
  Production (systemd):  python3 jarvis_core.py
  CLI testing:           python3 jarvis_core.py --cli
  Mock mode:             JARVIS_MOCK=true python3 jarvis_core.py --cli

Config: /etc/cofc-itip/config.env (chmod 600 — never committed to git)
Repo:   github.com/scc81/cofc-it-ops

Phase 2 / Session 3 (OpenJarvis selective borrowing):
  1. EventBus (event_bus.py) — side effects (audit, learning, Teams notify,
     trace assembly) decouple onto a synchronous in-process bus. SAFETY GATES
     DO NOT GO ON THE BUS: the FERPA guard, egress routing decision, and the
     human confirmation surfacing stay inline as hard checkpoints.
  2. InferenceEngine abstraction — OllamaEngine / AnthropicEngine behind one
     generate() interface. Engine SELECTION still happens AFTER the egress
     router has already decided local-vs-external; an engine can never route
     around the FERPA firewall because it's only ever handed the decision,
     never makes it.
  3. Trace learning loop — every query gets a uuid4 trace_id; metadata-only
     trace.step events publish as the query flows; a subscriber assembles one
     trace record per query into the existing behavioral collection to give
     the feedback loop the path, not just the final signal.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

from event_bus import EventBus

# Session B — optional SQLite inference/perf log (desktop/monitor/stress_log.py).
# GUARDED import: a headless install that doesn't deploy the desktop/ package
# must still run jarvis_core.py fine. If the import fails we log ONCE, set a flag,
# and no-op every hook call below — stress logging is observability, never a hard
# dependency on the query path. stress_log itself has no Qt dependency (that's
# why it lives in its own module and not in the Qt-bound telemetry_bridge), so
# importing it here doesn't drag PySide6 into the core runtime.
try:
    from desktop.monitor import stress_log  # type: ignore
    _STRESS_LOG_AVAILABLE = True
except Exception as _stress_import_err:  # ImportError, or desktop/ not deployed
    stress_log = None  # type: ignore
    _STRESS_LOG_AVAILABLE = False

load_dotenv(os.getenv("JARVIS_CONFIG", "/etc/cofc-itip/config.env"))

# ── STRUCTURED LOGGING ────────────────────────────────────────────────────────
# DECISION: JSON renderer to stdout. systemd/journald captures it; Promtail or
# a simple shipper can forward to the monitoring node later. ISO timestamps in
# UTC so logs correlate cleanly with Graph/Jamf API timestamps.
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger("jarvis.core")

# ── CONFIG ────────────────────────────────────────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Session 4: LiteLLM proxy in front of BOTH backends. Engines now POST to this
# OpenAI-compatible endpoint instead of Ollama's native API / api.anthropic.com
# directly. OLLAMA_HOST is still read (LiteLLM's config points at it, and
# embedding.py / health checks use it), but jarvis_core inference flows through
# LITELLM_HOST. This decouples engines from backend transport so Node 2 is a
# LiteLLM config edit, not a code change here.
LITELLM_HOST = os.getenv("LITELLM_HOST", "http://localhost:4000")
# Optional admin/master key for the proxy. Sent as a bearer token if set; the
# proxy accepts any key on loopback when no master_key is configured, so this
# stays blank-safe. Never hardcoded — env only.
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# DECISION: BB is the confirmed 6GB A2000 — defaults sized for 6GB VRAM.
#   FAST    = mistral:7b (q4)   — voice-latency tier
#   PRIMARY = llama3:8b  (q4)   — general reasoning / summarization
#   HEAVY   = gemma2:9b  (q4)   — best quality that still fits 6GB quantized
# llama3.1:70b removed — does not fit BB even quantized. Override in config.env
# if hardware changes (e.g. R760 consolidation).
FAST_MODEL    = os.getenv("FAST_MODEL", "mistral")
PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "llama3:8b")
HEAVY_MODEL   = os.getenv("HEAVY_MODEL", "gemma2:9b")

CHROMA_PATH    = os.getenv("CHROMA_PATH", "/var/lib/cofc-itip/chroma")
AUDIT_DB       = os.getenv("AUDIT_DB", "/var/lib/cofc-itip/audit.db")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))
MOCK_MODE      = os.getenv("JARVIS_MOCK", "false").lower() == "true"

# ── EGRESS CONFIG ─────────────────────────────────────────────────────────────
# Master switch — 'local' forces everything on-box regardless of per-capability
# flags. 'auto' respects EGRESS_INFERENCE / EGRESS_RESEARCH individually.
EGRESS_MODE       = os.getenv("EGRESS_MODE", "local")
EGRESS_INFERENCE  = os.getenv("EGRESS_INFERENCE", "local")
EGRESS_RESEARCH   = os.getenv("EGRESS_RESEARCH", "local")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")  # Session 5: updated stale model string
BRAVE_SEARCH_KEY  = os.getenv("BRAVE_SEARCH_API_KEY", "")

# ── OBSERVABILITY (Session 5 — LangFuse) ──────────────────────────────────────
# Self-hosted LangFuse trace export. ALL OPTIONAL: if any of the three is blank
# the integration no-ops cleanly (logged once at startup). This is best-effort
# observability — it must NEVER affect a query response. No keys = disabled.
LANGFUSE_HOST       = os.getenv("LANGFUSE_HOST", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

# ── EGRESS SAFETY ─────────────────────────────────────────────────────────────
# Tool names whose output MUST stay local — no exceptions, ever.
# Any query that called one of these tools is blocked from egress at
# summarize time. This set is the FERPA firewall. When adding a new tool,
# it goes in this set UNLESS it provably returns zero institutional data.
TOOL_DATA_LOCAL = {
    "query_intune_compliance",
    "query_intune_device_detail",
    "query_intune_app_deployments",
    "query_jamf_fleet",
    "query_jamf_device_detail",
    "query_jamf_patch_status",
    # Session E: MCM/PDQ connectors + the authority-routing entry point all
    # touch device data — FERPA-local, never egress (CLAUDE.md non-negotiable #1).
    "query_mcm_devices",
    "query_mcm_patch_status",
    "query_mcm_device_detail",
    "query_pdq_devices",
    "query_pdq_deployment_status",
    "query_pdq_device_detail",
    "query_device_auto",
    "query_taegis_alerts",
    "query_device_detail",
    "morning_briefing",
    "search_knowledge_base",   # Session 5: KB articles are institutional content
    "send_teams_alert",        # Session 5: alert bodies carry ops data
    "send_teams_briefing",     # Session 5: briefing content is ops data
    "store_memory",  # memories may contain environment facts — keep local
    # Phase 2: ServiceNow ticket/incident content carries user + device data
    # (caller IDs, hostnames, descriptions) — FERPA-local, never egress.
    "servicenow_get_ticket",
    "servicenow_list_my_tickets",
    "servicenow_create_ticket",
    "servicenow_update_ticket",
    "servicenow_create_incident",
}

# Tools that mutate a system of record and therefore require the human
# confirmation gate before a LIVE (non-mock) execution. The connector itself
# enforces the gate structurally (a live write needs confirmed=true and is
# audited); this set lets jarvis_core / the UI know which tool calls should
# surface a confirmation prompt rather than firing silently. Package-pipeline
# Stage 5 has its own token gate and isn't an LLM-registered tool, so it isn't
# listed here.
TOOLS_REQUIRING_CONFIRMATION = {
    "servicenow_create_ticket",
    "servicenow_update_ticket",
    "servicenow_create_incident",
}

RESEARCH_TRIGGERS = [
    "what is", "explain", "how does", "research", "find out",
    "look up", "search for", "what are best practices", "industry standard",
    "latest news", "recent", "cve", "vulnerability advisory",
]

MEMORY_TRIGGERS  = ["remember that", "don't forget", "note that", "save that"]
FEEDBACK_GOOD    = ["that's right", "correct", "good job", "exactly"]
FEEDBACK_BAD     = ["that's wrong", "incorrect", "no that's not right", "wrong"]
CORRECTION_START = ["actually", "no,", "correction:"]
HEAVY_TRIGGERS   = ["compare", "analyze", "summarize", "why", "trend", "last week"]


# ── EVENT TYPES (Session 3 — EventBus) ────────────────────────────────────────
# String constants so subscribers and publishers can't drift on a typo. These
# carry SIDE EFFECTS only — never gate decisions.
EVT_QUERY_COMPLETED   = "query.completed"      # -> audit + learning-loop log
EVT_ACTION_CONFIRMED  = "action.confirmed"     # -> audit of a confirmed write
EVT_PIPELINE_STAGE    = "pipeline.stage_complete"  # -> Teams notify on a stage
EVT_TRACE_STEP        = "trace.step"           # -> trace assembler (metadata only)


# ── AUDIT LOG ─────────────────────────────────────────────────────────────────
# Append-only SQLite. Required before any write operations go live; wired now
# so store_memory (the only current state-changing action) is covered from
# day one. Shipped to the monitoring node nightly (rsync in cron, see runbook).
def _init_audit() -> None:
    Path(AUDIT_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT NOT NULL,
            action       TEXT NOT NULL,
            tool         TEXT NOT NULL,
            params       TEXT NOT NULL,
            result       TEXT,
            confirmed_by TEXT DEFAULT 'human'
        )
        """
    )
    conn.commit()
    conn.close()


def audit(action: str, tool: str, params: dict, result: dict | str | None = None) -> None:
    """Immutable record of every state-changing action."""
    try:
        conn = sqlite3.connect(AUDIT_DB)
        conn.execute(
            "INSERT INTO audit_log (timestamp, action, tool, params, result) "
            "VALUES (?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                action,
                tool,
                json.dumps(params, default=str),
                json.dumps(result, default=str) if result is not None else None,
            ),
        )
        conn.commit()
        conn.close()
        log.info("audit.recorded", action=action, tool=tool)
    except Exception as e:  # audit failure must never crash the pipeline
        log.error("audit.failed", action=action, tool=tool, error=str(e))


# ── MEMORY LAYER (ChromaDB) ───────────────────────────────────────────────────
class JarvisMemory:
    """
    Two collections:
      - 'context'    : facts, corrections, environment knowledge
      - 'behavioral' : past interactions, outcomes, feedback scores
    """

    def __init__(self):
        import chromadb  # DECISION: lazy import — keeps /health fast on cold start

        from embedding import get_embedding_function

        Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=CHROMA_PATH)
        # FIX: pass an explicit Ollama-backed embedding_function. Without it
        # Chroma falls back to ONNXMiniLM_L6_V2, which downloads a model into
        # the service account's (non-writable) home dir at query time and
        # crashes /query with PermissionError. This embedder stays on
        # localhost:11434 and never writes to $HOME.
        embed_fn = get_embedding_function()
        self.context = self._open_collection("context", embed_fn)
        self.behavioral = self._open_collection("behavioral", embed_fn)
        log.info(
            "memory.init",
            path=CHROMA_PATH,
            context_count=self.context.count(),
            behavioral_count=self.behavioral.count(),
        )

    def _open_collection(self, name: str, embed_fn):
        """Open (or create) a Chroma collection, healing an embedding-function
        mismatch instead of crashing on it.

        BUG 3: when a collection was first created with a DIFFERENT embedding
        function than the one we now pass (the classic case: an old reinstall
        that ran before the Ollama-backed embedder existed, so the collection is
        tagged with Chroma's default ONNX embedder), get_or_create_collection
        raises a ValueError on reopen. Rather than push version-drift detection
        onto the installer, we self-heal here: log old vs. new function + the
        collection name, DROP the stale collection, and recreate it with the
        correct embedder. Stored docs are re-seedable (seed_context.py /
        behavioral logs rebuild over time); a hard crash on every /query is not
        an acceptable alternative."""
        try:
            return self.client.get_or_create_collection(
                name, embedding_function=embed_fn)
        except ValueError as e:
            new_name = getattr(embed_fn, "name", lambda: str(embed_fn))()
            log.warning(
                "memory.embed_fn_mismatch",
                collection=name,
                new_embed_fn=new_name,
                error=str(e),  # carries Chroma's stored-vs-provided detail
                action="recreating collection with current embedder",
            )
            # Drop the stale collection and recreate. delete_collection is a
            # no-op-safe call here because we only reach it on a confirmed
            # mismatch for an existing collection.
            self.client.delete_collection(name)
            return self.client.get_or_create_collection(
                name, embedding_function=embed_fn)

    def recall(self, query: str, n: int = 5) -> list[str]:
        """Pull relevant memories before sending query to Ollama."""
        memories: list[str] = []
        try:
            # Chroma errors if n_results > collection size — clamp both queries.
            ctx_n = min(n, self.context.count())
            if ctx_n:
                ctx = self.context.query(query_texts=[query], n_results=ctx_n)
                memories += [f"[MEMORY] {d}" for d in ctx["documents"][0]]

            beh_n = min(3, self.behavioral.count())
            if beh_n:
                beh = self.behavioral.query(
                    query_texts=[query],
                    n_results=beh_n,
                    where={"feedback_score": {"$gte": 1}},  # only good outcomes
                )
                memories += [f"[PAST INTERACTION] {d}" for d in beh["documents"][0]]
        except Exception as e:
            # Memory recall is best-effort — never block a query on it.
            log.warning("memory.recall_failed", error=str(e))
        log.debug("memory.recall", query=query, hits=len(memories))
        return memories

    def store_fact(self, fact: str, source: str = "user") -> None:
        doc_id = f"fact_{datetime.now(timezone.utc).timestamp()}"
        self.context.add(
            documents=[fact],
            metadatas=[{"source": source, "timestamp": datetime.now(timezone.utc).isoformat()}],
            ids=[doc_id],
        )
        log.info("memory.fact_stored", fact=fact[:120], source=source)

    def log_interaction(self, query: str, tool_called: str, response: str,
                        feedback_score: int = 0) -> None:
        doc_id = f"interaction_{datetime.now(timezone.utc).timestamp()}"
        summary = f"Query: {query} | Tool: {tool_called} | Response: {response[:200]}"
        self.behavioral.add(
            documents=[summary],
            metadatas=[{
                "query": query,
                "tool_called": tool_called,
                "feedback_score": feedback_score,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            ids=[doc_id],
        )
        log.info("memory.interaction_logged", tool=tool_called, score=feedback_score)

    def log_trace(self, trace_id: str, query: str, steps: list[dict]) -> None:
        """Session 3 — store an assembled per-query trace as a behavioral
        entry. METADATA ONLY: the document is a compact step summary, never the
        raw prompt/response text, so we don't bloat Chroma with conversation
        content. Reuses the behavioral collection (+ its embedding_function
        from the Session 1 fix). feedback_score starts at 0; the existing
        feedback loop updates the matching interaction record on signal — the
        trace is the richer context that sits alongside it."""
        try:
            path = " -> ".join(
                f"{s.get('step')}({s.get('outcome')})" for s in steps)
            summary = f"TRACE {trace_id} | Query: {query[:120]} | Path: {path}"
            self.behavioral.add(
                documents=[summary],
                metadatas=[{
                    "kind": "trace",
                    "trace_id": trace_id,
                    "query": query[:200],
                    "step_count": len(steps),
                    "feedback_score": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }],
                ids=[f"trace_{trace_id}"],
            )
            log.info("memory.trace_logged", trace_id=trace_id, steps=len(steps))
        except Exception as e:
            # Trace storage is observability — never block or crash a query on it.
            log.warning("memory.trace_failed", trace_id=trace_id, error=str(e))

    def apply_correction(self, original_query: str, correction: str) -> None:
        self.store_fact(
            f"CORRECTION: When asked '{original_query}', the right answer is: {correction}",
            source="correction",
        )
        log.info("memory.correction_stored", query=original_query[:120])


# ── TOOL REGISTRY ─────────────────────────────────────────────────────────────
# Each tool = a Python function in tools/ with signature (params: dict) -> dict
# plus a JSON schema entry here. Ollama's native tool-calling reads the
# description and decides when to call it. Add a tool: add function + schema.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_intune_compliance",
            "description": "Get device compliance stats from Microsoft Intune. Use for questions about compliant/non-compliant devices, enrollment counts, or Windows fleet health.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "'non-compliant', 'compliant', or 'all'",
                    },
                    "platform": {
                        "type": "string",
                        "description": "Optional platform filter, e.g. 'Windows', 'macOS'",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_intune_device_detail",
            "description": "Look up a specific Intune-managed device by hostname or user principal name. Returns the full managed device record.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Hostname or UPN"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_intune_app_deployments",
            "description": "Get app deployment status from Intune. Use for questions about app install success/failure rates or whether a specific app is deploying correctly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "Optional app name filter"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_jamf_fleet",
            "description": "Get Mac fleet health and compliance data from Jamf Pro. Use for questions about Mac devices, macOS versions, or Mac compliance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional filter: 'all', 'stale', or a macOS version"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_jamf_device_detail",
            "description": "Look up a specific Mac by hostname, serial number, or username in Jamf Pro.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Hostname, serial, or username"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_jamf_patch_status",
            "description": "Get macOS patch management status from Jamf Pro. Use for questions about Mac patch levels or outdated software.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Optional software title filter"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            # Session E: ambiguous-query entry point. Use when a device is named
            # but the platform/management system is NOT specified — resolves the
            # authoritative source first, then dispatches to that one connector.
            "name": "query_device_auto",
            "description": "Look up a device when the platform or management system is NOT specified (e.g. 'patch status of laptop X', 'who owns CHEM-LAB-14'). Automatically resolves which system is authoritative (Intune, MCM, Jamf, or PDQ) and queries that one — do NOT use this when the user already named the platform (use the explicit query_intune_*/query_jamf_* tools then).",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Device hostname, serial, or username"},
                    "platform": {"type": "string", "description": "Optional hint if known: 'windows' or 'mac'"},
                    "enrollment": {"type": "string", "description": "Optional hint if known: e.g. 'cloud-native', 'domain-joined', 'co-managed'"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_mcm_devices",
            "description": "List or filter Windows devices managed by MCM/SCCM (on-prem, domain-joined). Use for questions about MCM client health, domain-joined workstations, or lab/classroom PCs. (Mock-mode only — MCM not yet live.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "'all', 'inactive'/'unhealthy', or an OS substring"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_mcm_patch_status",
            "description": "Get software-update (patch) compliance from MCM/SCCM for a single device or the MCM fleet. (Mock-mode only — MCM not yet live.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Optional device hostname; omit for a fleet summary"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_mcm_device_detail",
            "description": "Look up a specific MCM/SCCM-managed Windows device by hostname or username. (Mock-mode only — MCM not yet live.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Hostname or COFC\\username"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_pdq_devices",
            "description": "List or filter Windows devices in PDQ Inventory — the gap-fill tier for kiosks, signage, lab spares, and shared PCs that Intune/MCM/Jamf don't manage. Use 'stale' to find devices not scanned recently. (Mock-mode only — PDQ not yet live.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "'all', 'stale', or a device-type/hostname substring"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_pdq_deployment_status",
            "description": "Get PDQ Deploy package deployment status (succeeded/failed/running) across the gap-fill fleet. Use for questions about PDQ software pushes. (Mock-mode only — PDQ not yet live.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "Optional package name filter"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_pdq_device_detail",
            "description": "Look up a specific PDQ-inventoried device by hostname or serial. (Mock-mode only — PDQ not yet live.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Hostname or serial number"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_taegis_alerts",
            "description": "Get recent security alerts from Taegis SIEM. Use for threat questions, security events, or unusual activity. (Available Saturday build.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "description": "'critical', 'high', or 'all'"},
                    "hours": {"type": "integer", "description": "Hours back to look. Default 24."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "morning_briefing",
            "description": "Generate a full morning briefing — overnight alerts, fleet health, patch status, anything needing attention today.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search the CofC ServiceNow knowledge base for help articles. Use for how-to questions, procedures, troubleshooting steps, or 'is there a KB on X'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms"},
                    "limit": {"type": "integer", "description": "Max articles. Default 5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "servicenow_get_ticket",
            "description": "Fetch one ServiceNow ticket or incident by its number (e.g. INC0012345 or TASK0012345). Use when asked about the status or details of a specific ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_number": {"type": "string", "description": "Ticket/incident number, e.g. INC0012345"},
                },
                "required": ["ticket_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "servicenow_list_my_tickets",
            "description": "List ServiceNow tickets assigned to a user, optionally filtered by state. Use for 'what's assigned to me/X' or 'open tickets for X'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assigned_to": {"type": "string", "description": "ServiceNow username the tickets are assigned to"},
                    "state": {"type": "string", "description": "'open' (default), 'all', or a specific state like 'in_progress', 'resolved'"},
                },
                "required": ["assigned_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "servicenow_create_ticket",
            "description": "Open a ServiceNow request task (sc_task). WRITE ACTION — requires human confirmation before it executes against a live instance. Use only when the user explicitly asks to open/create a ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "short_description": {"type": "string", "description": "One-line summary of the request"},
                    "description": {"type": "string", "description": "Full details"},
                    "category": {"type": "string", "description": "Optional category, e.g. 'software', 'network'"},
                    "urgency": {"type": "string", "description": "'1' high, '2' medium, '3' low (default)"},
                    "caller_id": {"type": "string", "description": "Optional requesting user"},
                    "confirmed": {"type": "boolean", "description": "Set true only after a human has confirmed this write"},
                },
                "required": ["short_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "servicenow_update_ticket",
            "description": "Add work notes and/or change the state of a ServiceNow ticket. WRITE ACTION — requires human confirmation before it executes against a live instance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_number": {"type": "string", "description": "Ticket/incident number to update"},
                    "work_notes": {"type": "string", "description": "Work notes to append"},
                    "state": {"type": "string", "description": "New state, e.g. 'in_progress', 'on_hold', 'resolved'"},
                    "confirmed": {"type": "boolean", "description": "Set true only after a human has confirmed this write"},
                },
                "required": ["ticket_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "servicenow_create_incident",
            "description": "Open a ServiceNow incident. WRITE ACTION — requires human confirmation before it executes against a live instance. Use only when the user explicitly asks to open/log an incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "short_description": {"type": "string", "description": "One-line summary of the incident"},
                    "description": {"type": "string", "description": "Full details"},
                    "category": {"type": "string", "description": "Optional category"},
                    "urgency": {"type": "string", "description": "'1' high, '2' medium, '3' low (default)"},
                    "caller_id": {"type": "string", "description": "Optional affected user"},
                    "confirmed": {"type": "boolean", "description": "Set true only after a human has confirmed this write"},
                },
                "required": ["short_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_teams_alert",
            "description": "Post an alert message to the IT Teams channel. Use ONLY when the user explicitly asks to notify, alert, or message the team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short alert title"},
                    "body": {"type": "string", "description": "Alert body text"},
                    "severity": {"type": "string", "description": "critical/high/medium/low/info"},
                },
                "required": ["title", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_teams_briefing",
            "description": "Generate the morning briefing and post it to the IT Teams channel. Use when the user asks to send/post/share the briefing with the team.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "store_memory",
            "description": "Store a fact for future recall. Use when the user explicitly asks JARVIS to remember something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "The fact to remember"},
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            # Session 5: opt-in web research via SearXNG. NOT an ops-data tool —
            # governed by EGRESS_RESEARCH, not the FERPA local-only set. Returns
            # {"results": [...], "blocked": bool} and self-blocks when research
            # egress is disabled, so it's safe to register unconditionally.
            "name": "web_research",
            "description": "Search the public web for general/background research via the self-hosted SearXNG instance. Use for 'look up', 'what is', 'best practices', CVE/advisory lookups, and other non-sensitive external questions. Does NOT touch Intune/Jamf/Taegis or any institutional data. Only works when web research has been enabled by an operator; otherwise it returns no results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms"},
                    "max_results": {"type": "integer", "description": "Max results. Default 5."},
                },
                "required": ["query"],
            },
        },
    },
]

# Set of every tool name the model is allowed to call, derived from the schema
# list above so the two can't drift. Used by the BUG 1 leak guard below to tell
# a real (but text-emitted) tool call apart from ordinary bracketed prose.
_VALID_TOOL_NAMES: set[str] = {
    s["function"]["name"] for s in TOOL_SCHEMAS if s.get("function", {}).get("name")
}

# ── TOOL-CALL LEAK GUARD (BUG 1) ──────────────────────────────────────────────
# Root cause: with Ollama's smaller models (mistral 7B, llama3 8B) behind the
# LiteLLM proxy, the model does NOT reliably populate the structured
# `tool_calls` field. It frequently emits the call as PLAIN TEXT in
# message.content instead — e.g. the literal string
#   [query_intune_compliance filter='compliant']
# When that happens chat_with_tools used to return {"type":"text", ...} and the
# raw bracket string flowed straight to the user (and to GLaDOS, spoken aloud).
#
# Two-layer fix:
#   1. RECOVER: if the model emitted a bracketed call as text and the name
#      matches a registered tool, parse it back into a real tool_call dict so
#      the EXISTING dispatch path in handle() executes it normally. This makes
#      the common failure mode actually work instead of leaking.
#   2. SUPPRESS: a final guard in handle() catches any bracketed
#      tool-call-shaped string that still slipped through (e.g. a malformed or
#      unknown-tool bracket) before it reaches the user, replacing it with a
#      graceful spoken fallback rather than reading raw syntax aloud.
#
# The pattern is deliberately ANCHORED and NAME-GATED so it can't false-positive
# on legitimate bracketed prose. It only matches when the ENTIRE trimmed string
# is a single [name ...] / [name(...)] token whose name is a known tool — normal
# sentences containing brackets (e.g. "[see the runbook]") never match because
# the first token won't be a registered tool name.
_TOOLCALL_TEXT_RE = re.compile(
    r"""^\s*\[\s*
        (?P<name>[a-zA-Z_][a-zA-Z0-9_]*)   # leading token = candidate tool name
        \s*[\s(]?                            # optional ( or whitespace before args
        (?P<args>.*?)                        # arg blob (k='v', k=v, or JSON-ish)
        \)?\s*\]\s*$                          # optional ) then closing bracket
    """,
    re.VERBOSE | re.DOTALL,
)

# A looser detector used ONLY for the suppress layer: does this text CONTAIN a
# bracketed token whose first word is a known tool name? Used to catch a leak
# we couldn't cleanly re-dispatch, so we never speak raw tool syntax.
_TOOLCALL_CONTAINS_RE = re.compile(
    r"\[\s*(" + "|".join(re.escape(n) for n in sorted(_VALID_TOOL_NAMES)) + r")\b",
    re.IGNORECASE,
) if _VALID_TOOL_NAMES else None


def _parse_text_tool_call(content: str) -> dict | None:
    """Layer 1 (recover). If `content` is ENTIRELY a single bracketed call whose
    name is a registered tool, parse it into {"tool": name, "args": {...}} so the
    normal dispatch path can run it. Returns None if it isn't a clean,
    known-tool call — in which case it's treated as ordinary text.

    Arg parsing is best-effort and tolerant: it accepts a JSON object, or simple
    key='value' / key="value" / key=value pairs. Unparseable args -> {} (the
    connectors validate / default their own params), never a crash."""
    if not content:
        return None
    m = _TOOLCALL_TEXT_RE.match(content.strip())
    if not m:
        return None
    name = m.group("name")
    if name not in _VALID_TOOL_NAMES:
        return None  # bracketed prose that merely looks call-shaped — leave it
    raw_args = (m.group("args") or "").strip()
    args: dict = {}
    if raw_args:
        # Try JSON first (model sometimes emits [name {"k": "v"}]).
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                return {"tool": name, "args": parsed}
        except (json.JSONDecodeError, ValueError):
            pass
        # Fall back to key=value pair extraction (quoted or bare).
        for key, q1, q2, bare in re.findall(
            r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s,]+))",
            raw_args,
        ):
            args[key] = q1 or q2 or bare
    return {"tool": name, "args": args}


def _contains_leaked_tool_call(text: str) -> bool:
    """Layer 2 (suppress). True if `text` still contains a bracketed token whose
    first word is a known tool name — i.e. a leak we did NOT re-dispatch. Kept
    name-gated so ordinary bracketed prose ('[draft]', '[see KB]') is never
    flagged."""
    if not text or _TOOLCALL_CONTAINS_RE is None:
        return False
    return bool(_TOOLCALL_CONTAINS_RE.search(text))


# ── TOOL EXECUTOR ─────────────────────────────────────────────────────────────
def execute_tool(tool_name: str, tool_args: dict, memory: JarvisMemory) -> str:
    """
    Routes tool calls to the right connector module.
    All connectors take (params: dict) and return dict; we JSON-encode for
    the local summarization pass. Graceful degradation: a missing or failing
    connector returns a spoken-friendly error instead of crashing the loop.
    """
    started = time.monotonic()
    log.info("tool.start", tool=tool_name, params=tool_args)

    try:
        if tool_name == "query_intune_compliance":
            from tools import intune
            result = intune.query_compliance(tool_args)

        elif tool_name == "query_intune_device_detail":
            from tools import intune
            result = intune.query_device_detail(tool_args)

        elif tool_name == "query_intune_app_deployments":
            from tools import intune
            result = intune.query_app_deployments(tool_args)

        elif tool_name == "query_jamf_fleet":
            from tools import jamf
            result = jamf.query_fleet(tool_args)

        elif tool_name == "query_jamf_device_detail":
            from tools import jamf
            result = jamf.query_device_detail(tool_args)

        elif tool_name == "query_jamf_patch_status":
            from tools import jamf
            result = jamf.query_patch_status(tool_args)

        # ── Session E: MCM / PDQ connectors (mock-mode only) ──────────────────
        elif tool_name == "query_mcm_devices":
            from tools import mcm
            result = mcm.query_devices(tool_args)

        elif tool_name == "query_mcm_patch_status":
            from tools import mcm
            result = mcm.query_patch_status(tool_args)

        elif tool_name == "query_mcm_device_detail":
            from tools import mcm
            result = mcm.query_device_detail(tool_args)

        elif tool_name == "query_pdq_devices":
            from tools import pdq
            result = pdq.query_devices(tool_args)

        elif tool_name == "query_pdq_deployment_status":
            from tools import pdq
            result = pdq.query_deployment_status(tool_args)

        elif tool_name == "query_pdq_device_detail":
            from tools import pdq
            result = pdq.query_device_detail(tool_args)

        elif tool_name == "query_device_auto":
            # Session E: AMBIGUOUS-query path — identifier given, platform/system
            # NOT specified. Resolve the authoritative source FIRST (no live
            # probe), then dispatch device_detail to exactly that connector —
            # never fan out to all four and guess. Additive to the explicit
            # query_intune_*/query_jamf_* tools, which remain for platform-known
            # calls. The routing decision (source, confidence, reason, fallbacks)
            # is returned alongside the result so the spoken answer can say WHY.
            from tools import device_authority
            identifier = (tool_args or {}).get("identifier", "")
            hint = {}
            if tool_args.get("platform"):
                hint["platform"] = tool_args["platform"]
            if tool_args.get("enrollment"):
                hint["enrollment"] = tool_args["enrollment"]
            routing = device_authority.resolve_authority(identifier, hint or None)
            src = routing.get("authoritative_source", "unknown")
            _detail_dispatch = {
                "intune": ("tools.intune", "query_device_detail"),
                "mcm":    ("tools.mcm", "query_device_detail"),
                "jamf":   ("tools.jamf", "query_device_detail"),
                "pdq":    ("tools.pdq", "query_device_detail"),
            }
            if src in _detail_dispatch:
                import importlib
                mod_name, fn_name = _detail_dispatch[src]
                _mod = importlib.import_module(mod_name)
                device_result = getattr(_mod, fn_name)({"identifier": identifier})
            else:
                device_result = {"found": False,
                                 "message": f"No authoritative source resolved for "
                                            f"'{identifier}'"}
            result = {"source": "device_authority", "mock": MOCK_MODE,
                      "routing": routing, "authoritative_source": src,
                      "result": device_result}

        elif tool_name == "query_taegis_alerts":
            from tools import taegis  # Saturday build
            result = taegis.query_alerts(tool_args)

        elif tool_name == "morning_briefing":
            from tools import briefing  # Saturday build
            result = briefing.generate(tool_args)

        elif tool_name == "search_knowledge_base":
            from tools import servicenow  # Phase 2: servicenow_kb merged into servicenow
            result = servicenow.search_kb(tool_args)

        elif tool_name == "servicenow_get_ticket":
            from tools import servicenow
            result = servicenow.get_ticket(tool_args)

        elif tool_name == "servicenow_list_my_tickets":
            from tools import servicenow
            result = servicenow.list_my_tickets(tool_args)

        elif tool_name == "servicenow_create_ticket":
            # WRITE — gated. The connector enforces the human confirmation gate
            # itself (live writes require confirmed=true); core marks it in
            # TOOLS_REQUIRING_CONFIRMATION so callers/UI can surface the prompt.
            from tools import servicenow
            result = servicenow.create_ticket(tool_args)

        elif tool_name == "servicenow_update_ticket":
            from tools import servicenow  # WRITE — gated (see connector)
            result = servicenow.update_ticket(tool_args)

        elif tool_name == "servicenow_create_incident":
            from tools import servicenow  # WRITE — gated (see connector)
            result = servicenow.create_incident(tool_args)

        elif tool_name == "send_teams_alert":
            from tools import teams  # Session 5 registration
            tool_args.setdefault("source", "jarvis-voice")
            result = teams.send_alert(tool_args)

        elif tool_name == "send_teams_briefing":
            # Session 5 registration. Composition: generate fresh briefing,
            # then deliver. Posting a card is a notification, not a production
            # action — no human gate needed (gate covers MDM writes only).
            from tools import briefing as _briefing
            from tools import teams
            b = _briefing.generate({})
            result = teams.send_briefing({"briefing": b})

        elif tool_name == "store_memory":
            memory.store_fact(tool_args["fact"], source="explicit_user_request")
            audit("store_memory", "jarvis_core.store_memory",
                  tool_args, {"status": "stored"})
            result = {"status": "stored", "spoken": "Got it, I'll remember that."}

        elif tool_name == "web_research":
            # Session 5: SearXNG opt-in research. The connector self-gates on
            # EGRESS_RESEARCH and returns {"blocked": True, ...} (not an
            # exception) when research egress is off — no special handling here.
            from tools import websearch
            result = websearch.search(tool_args)

        else:
            log.warning("tool.unknown", tool=tool_name)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        log.info("tool.success", tool=tool_name,
                 duration_ms=int((time.monotonic() - started) * 1000))
        return json.dumps(result, default=str)

    except ImportError:
        log.warning("tool.not_built_yet", tool=tool_name)
        return json.dumps({"error": f"The {tool_name} capability isn't installed yet."})
    except Exception as e:
        # pybreaker.CircuitBreakerError lands here too — connector modules
        # raise it when their circuit is open.
        log.error("tool.error", tool=tool_name, error=str(e),
                  duration_ms=int((time.monotonic() - started) * 1000))
        return json.dumps({"error": f"{tool_name} is currently unreachable: {e}"})


# ── MODEL SELECTOR ────────────────────────────────────────────────────────────
def pick_model(query: str) -> str:
    """Fast model for simple voice queries; heavy for complex/comparative."""
    q = query.lower()
    if any(trigger in q for trigger in HEAVY_TRIGGERS):
        return HEAVY_MODEL
    return FAST_MODEL


# ── FEEDBACK DETECTOR ─────────────────────────────────────────────────────────
def detect_feedback(text: str) -> tuple[str, str]:
    """
    Returns (feedback_type, cleaned_text).
    feedback_type: 'good' | 'bad' | 'correction' | 'memory' | 'none'
    """
    t = text.lower().strip()

    if any(t.startswith(trigger) for trigger in MEMORY_TRIGGERS):
        # Strip the trigger phrase itself so we store only the fact.
        for trigger in MEMORY_TRIGGERS:
            if t.startswith(trigger):
                fact = text[len(trigger):].lstrip(" ,:")
                return "memory", fact or text
        return "memory", text

    if any(trigger in t for trigger in FEEDBACK_BAD):
        return "bad", text  # check bad before good — "no that's not right" contains "right"

    if any(trigger in t for trigger in FEEDBACK_GOOD):
        return "good", text

    if any(t.startswith(c) for c in CORRECTION_START):
        return "correction", text

    return "none", text


# ── EGRESS ROUTER ─────────────────────────────────────────────────────────────
class EgressRouter:
    """
    Decides where a query goes — local Ollama or external API.

    Rules (in order of precedence):
      1. EGRESS_MODE=local -> always local, full stop
      2. Query touched a tool with real ops data -> always local, full stop
      3. Research query + EGRESS_RESEARCH=claude/web -> external
      4. EGRESS_INFERENCE=claude -> external for pure reasoning queries
      5. Default -> local
    """

    def __init__(self):
        self.forced_local = (EGRESS_MODE == "local")
        # Session 3: engine instances. The router still DECIDES local-vs-
        # external exactly as before (route_inference / route_research below,
        # unchanged); it just delegates the actual call mechanics to an engine.
        # Constructing them here does not change any decision — selection still
        # happens per-call after the routing decision is made.
        self.ollama = OllamaEngine()
        self.anthropic = AnthropicEngine(self.ollama)

    def route_inference(self, query: str, tool_was_called: bool, tool_name: str = "") -> str:
        # Rule 1 — master switch
        if self.forced_local:
            return "local"
        # Rule 2 — ops data touched -> hard local. This is the FERPA firewall.
        # DECISION: default-deny — ANY tool call routes local unless the tool
        # is explicitly known-safe. Unknown/new tools cannot leak by omission.
        if tool_was_called:
            if tool_name in TOOL_DATA_LOCAL or tool_name not in _KNOWN_SAFE_TOOLS:
                return "local"
        # Rule 4 — external inference enabled
        if EGRESS_INFERENCE == "claude" and ANTHROPIC_API_KEY:
            return "claude"
        return "local"

    def route_research(self, query: str) -> str:
        if self.forced_local:
            return "local"
        q = query.lower()
        if any(trigger in q for trigger in RESEARCH_TRIGGERS):
            if EGRESS_RESEARCH == "claude" and ANTHROPIC_API_KEY:
                return "claude"
            if EGRESS_RESEARCH == "web" and BRAVE_SEARCH_KEY:
                return "web"
        return "local"

    def call_claude(self, query: str, system: str) -> str:
        """Pure reasoning to Claude API. NEVER called with ops tool output.
        Session 3: mechanics now live in AnthropicEngine.generate(); this
        stays as the call site so the routing-decision -> engine-call split is
        explicit. Behavior (incl. local fallback on failure) is unchanged."""
        return self.anthropic.generate(query, ANTHROPIC_MODEL, system=system)

    def call_web_search(self, query: str) -> str:
        """Brave Search — raw results come back for LOCAL summarization."""
        if not BRAVE_SEARCH_KEY:
            return self._fallback_local(query, "")
        log.info("egress.web_search", query=query)
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": BRAVE_SEARCH_KEY,
                },
                params={"q": query, "count": 5},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("web", {}).get("results", [])
            snippets = [f"- {r['title']}: {r['description']}" for r in results[:5]]
            return "\n".join(snippets) if snippets else "No results found."
        except Exception as e:
            log.warning("egress.web_failed", error=str(e))
            return "Web search unavailable."

    def _fallback_local(self, query: str, system: str) -> str:
        # Session 3: delegates to the local engine — same payload, same host,
        # same timeout as the former inline block; behavior unchanged.
        return self.ollama.generate(query, PRIMARY_MODEL, system=system)

    @staticmethod
    def egress_status() -> str:
        if EGRESS_MODE == "local":
            return "Egress: fully local — all inference on-box"
        return (
            f"Egress: mode={EGRESS_MODE} | inference={EGRESS_INFERENCE} | "
            f"research={EGRESS_RESEARCH} | "
            f"claude={'configured' if ANTHROPIC_API_KEY else 'no key'} | "
            f"web={'configured' if BRAVE_SEARCH_KEY else 'no key'}"
        )


# Tools provably free of institutional data. Currently empty on purpose —
# every existing tool returns ops data. A tool must be added here EXPLICITLY
# (code review + Philip sign-off) before its output may ever leave the box.
_KNOWN_SAFE_TOOLS: set[str] = set()


# ── INFERENCE ENGINE ABSTRACTION (Session 3) ──────────────────────────────────
# One consistent interface over the two inference backends that already exist
# today: local Ollama and the external Claude API. This is PURELY an interface
# unification — it does NOT decide local-vs-external. That decision is made by
# EgressRouter BEFORE an engine is ever selected, and an engine is only ever
# HANDED the chosen route; it can't route around the FERPA firewall because it
# never makes the routing call. Adding a third backend later (Node 2, a
# different model host) means writing one more class, not touching call sites.
class InferenceEngine:
    """Single-method interface: generate(prompt, model, **kwargs) -> str."""

    name = "base"

    def generate(self, prompt: str, model: str, **kwargs) -> str:
        raise NotImplementedError


class OllamaEngine(InferenceEngine):
    """Local Ollama backend, now reached THROUGH the LiteLLM proxy (Session 4).
    Same host-of-record (Ollama on BB) — LiteLLM just sits in front so the call
    site is one consistent OpenAI-compatible endpoint. generate()/chat_with_tools
    ()/summarize_tool_result() signatures are UNCHANGED; only the transport inside
    moved from {OLLAMA_HOST}/api/chat to {LITELLM_HOST}/v1/chat/completions. Stays
    local: these model names route to ollama/* in litellm_config.yaml and cross-
    provider fallback is disabled there, so a local call can never promote to
    Claude."""

    name = "ollama"

    @staticmethod
    def _headers() -> dict:
        h = {"content-type": "application/json"}
        if LITELLM_API_KEY:  # bearer only if a proxy master key is configured
            h["Authorization"] = f"Bearer {LITELLM_API_KEY}"
        return h

    def generate(self, prompt: str, model: str, *, system: str = "",
                 **kwargs) -> str:
        """Plain local completion via the proxy. Same logical request as the
        former direct /api/chat call; OpenAI chat-completions shape now."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        resp = requests.post(f"{LITELLM_HOST}/v1/chat/completions",
                             headers=self._headers(), json=payload,
                             timeout=OLLAMA_TIMEOUT)
        return resp.json()["choices"][0]["message"]["content"]

    def chat_with_tools(self, query: str, system: str, model: str) -> dict:
        """Local tool-routing chat via the proxy. Behavior identical to the
        former JarvisCore._call_ollama — returns a tool_call dict or text dict.
        OpenAI tool-call schema (TOOL_SCHEMAS is already OpenAI function format),
        which LiteLLM passes through to Ollama's tool calling."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": query},
            ],
            "tools": TOOL_SCHEMAS,
            "stream": False,
        }
        started = time.monotonic()
        try:
            resp = requests.post(f"{LITELLM_HOST}/v1/chat/completions",
                                 headers=self._headers(), json=payload,
                                 timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("ollama.unreachable", error=str(e))
            return {"type": "text",
                    "content": "I can't reach my local inference engine right now. "
                               "Check that the LiteLLM proxy and Ollama are running."}
        data = resp.json()
        log.info("ollama.chat", model=model,
                 duration_ms=int((time.monotonic() - started) * 1000))

        # OpenAI-compatible response: choices[0].message{.tool_calls|.content}.
        message = (data.get("choices") or [{}])[0].get("message", {})
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # OpenAI returns arguments as a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            return {"type": "tool_call", "tool": fn.get("name", ""), "args": args}

        # BUG 1 (layer 1 — recover): no structured tool_calls came back. Smaller
        # Ollama models often emit the call as PLAIN TEXT instead. If the content
        # is entirely a single bracketed call for a KNOWN tool, parse it back into
        # a real tool_call so the normal dispatch path runs it rather than leaking
        # the raw string to the user/voice.
        content = message.get("content", "")
        recovered = _parse_text_tool_call(content)
        if recovered is not None:
            log.warning("ollama.toolcall_text_recovered",
                        tool=recovered["tool"], model=model)
            return {"type": "tool_call",
                    "tool": recovered["tool"], "args": recovered["args"]}

        return {"type": "text", "content": content}

    def summarize_tool_result(self, original_query: str, tool_name: str,
                              tool_output: str, system: str, model: str) -> str:
        """Local summarization of raw tool output via the proxy. Identical
        behavior to the former JarvisCore._summarize_tool_result. This is the
        path ops-data tool results ALWAYS take — local by construction (model
        routes to ollama/* with no fallback)."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": original_query},
                {"role": "assistant", "content": f"[Tool: {tool_name}]"},
                {"role": "tool", "content": tool_output},
            ],
            "stream": False,
        }
        try:
            resp = requests.post(f"{LITELLM_HOST}/v1/chat/completions",
                                 headers=self._headers(), json=payload,
                                 timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            log.error("ollama.summarize_failed", tool=tool_name, error=str(e))
            # Degrade gracefully — return the raw data rather than nothing.
            return f"I pulled the data but couldn't summarize it. Raw result: {tool_output[:500]}"


class AnthropicEngine(InferenceEngine):
    """External Claude backend, now reached THROUGH the LiteLLM proxy (Session
    4) at the claude-sonnet-4-6 model_name. CRITICAL: this engine is ONLY ever
    reached after EgressRouter returned 'claude' for a NON-ops query — never
    called with ops tool output. The router enforces that; routing through the
    proxy does not relax it (proxy fallbacks are disabled, so a Claude error
    can't bounce to a local model and vice-versa — but here the gate already
    decided egress is allowed). The no-key short-circuit and local fallback on
    failure are unchanged from before."""

    name = "anthropic"

    def __init__(self, local: OllamaEngine):
        # Hold the local engine for graceful fallback — preserves the old
        # _fallback_local behavior without re-deciding routing.
        self._local = local

    def generate(self, prompt: str, model: str, *, system: str = "",
                 **kwargs) -> str:
        # No key configured -> never attempt egress; answer locally. Same guard
        # as before; the proxy would also reject, but we keep the explicit gate.
        if not ANTHROPIC_API_KEY:
            return self._local.generate(prompt, PRIMARY_MODEL, system=system)
        log.info("egress.claude", chars=len(prompt))
        try:
            # OpenAI-compatible call to the proxy; LiteLLM maps it to Anthropic
            # and injects the key from its own env (os.environ/ANTHROPIC_API_KEY
            # in litellm_config.yaml) — the key is NOT sent from here.
            resp = requests.post(
                f"{LITELLM_HOST}/v1/chat/completions",
                headers=self._local._headers(),
                json={
                    "model": model,
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("egress.claude_failed", error=str(e))
            return self._local.generate(prompt, PRIMARY_MODEL, system=system)


# ── OBSERVABILITY EXPORTER (Session 5 — LangFuse) ─────────────────────────────
# Wraps the LangFuse client so jarvis_core never has to care whether it's
# configured or reachable. enabled is False unless all three creds are present
# AND the SDK imports AND the client constructs — any miss logs once and the
# whole thing becomes a no-op. export_trace() is fire-and-forget: it catches
# everything, so an observability failure can never touch a query response.
class LangFuseObserver:
    def __init__(self):
        self.enabled = False
        self._client = None
        if not (LANGFUSE_HOST and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
            log.info("observability.disabled",
                     reason="LangFuse creds not fully set — running without "
                            "trace export")
            return
        try:
            from langfuse import Langfuse
            self._client = Langfuse(
                host=LANGFUSE_HOST,
                public_key=LANGFUSE_PUBLIC_KEY,
                secret_key=LANGFUSE_SECRET_KEY,
            )
            self.enabled = True
            log.info("observability.enabled", host=LANGFUSE_HOST)
        except Exception as e:
            # Missing SDK, bad creds, unreachable host — disable, don't crash.
            log.warning("observability.init_failed", error=str(e))
            self.enabled = False
            self._client = None

    def export_trace(self, trace_id: str, query: str, steps: list[dict],
                     tool: str, response: str,
                     feedback_score: int | None = None) -> None:
        """Fan the SAME assembled trace data that goes to ChromaDB out to
        LangFuse. Best-effort: any failure logs a warning and returns. Uses the
        current OTel-based context-manager SDK API; wrapped broadly so SDK
        version drift degrades to 'no trace' rather than an error in the loop.
        Metadata-only on the path; query/response included as the trace's
        input/output for observability (LangFuse is on-box, same trust
        boundary as ChromaDB — not an egress destination)."""
        if not self.enabled or self._client is None:
            return
        try:
            # Derive model + latency-ish summary from the already-assembled
            # steps (no second data-gathering path). engine_select / *_route
            # outcomes carry the model and routing decision.
            model = "local"
            for s in steps:
                if s.get("step") in ("engine_select", "research_route",
                                     "egress_route") and s.get("outcome"):
                    model = str(s["outcome"])
                    break
            metadata = {
                "trace_id": trace_id,
                "tool": tool,
                "step_count": len(steps),
                "path": " -> ".join(
                    f"{s.get('step')}({s.get('outcome')})" for s in steps),
            }
            if feedback_score is not None:
                metadata["feedback_score"] = feedback_score

            # start_as_current_observation is the documented v3 entry point.
            with self._client.start_as_current_observation(
                    as_type="generation", name="jarvis_query",
                    model=model) as gen:
                gen.update(input=query[:2000], output=response[:2000],
                           metadata=metadata)
                if feedback_score is not None:
                    # Map the learning-loop signal onto a LangFuse score.
                    try:
                        gen.score(name="user_feedback", value=feedback_score)
                    except Exception:
                        pass  # scoring API optional; never fail the export on it
            # Non-blocking flush so short voice turns don't drop the event.
            self._client.flush()
            log.debug("observability.trace_exported", trace_id=trace_id)
        except Exception as e:
            log.warning("observability.export_failed", trace_id=trace_id,
                        error=str(e))


# ── MAIN QUERY HANDLER ────────────────────────────────────────────────────────
class JarvisCore:

    def __init__(self):
        self.memory = JarvisMemory()
        self.egress = EgressRouter()
        self.last_query: str | None = None
        self.last_tool: str | None = None
        self.last_response: str | None = None
        _init_audit()
        # Session B — initialize the stress/inference log DB once at startup
        # (idempotent). Guarded: if the desktop/ package isn't deployed the
        # import above already no-op'd, and we just record that once here so the
        # operator can see in the logs why no stress rows are being written.
        if _STRESS_LOG_AVAILABLE:
            stress_log.init_db()
        else:
            log.info("stress_log.unavailable",
                     reason="desktop.monitor.stress_log not importable — "
                            "inference perf logging disabled (headless install?)")
        # Session 3 — EventBus for SIDE EFFECTS ONLY. Gate/FERPA/egress
        # decisions are NOT published here; they stay inline below.
        self.bus = EventBus()
        # In-flight trace step buffers, keyed by trace_id. The trace subscriber
        # appends steps here and the query.completed handler flushes one record.
        self._traces: dict[str, list[dict]] = {}
        # Session 5: optional LangFuse exporter. No-ops cleanly if unconfigured.
        self.observer = LangFuseObserver()
        self._wire_subscribers()
        log.info("core.init", egress=EgressRouter.egress_status(), mock=MOCK_MODE)

    # ── EVENT SUBSCRIBERS (Session 3) ────────────────────────────────────────
    # Each handler is a pure side effect: logging, learning, notification, or
    # trace assembly. None of them gate execution; the publisher does not
    # depend on their return value. A raising handler is isolated by the bus.
    def _wire_subscribers(self) -> None:
        self.bus.subscribe(EVT_TRACE_STEP, self._on_trace_step)
        self.bus.subscribe(EVT_QUERY_COMPLETED, self._on_query_completed)
        self.bus.subscribe(EVT_ACTION_CONFIRMED, self._on_action_confirmed)
        self.bus.subscribe(EVT_PIPELINE_STAGE, self._on_pipeline_stage)

    def _on_trace_step(self, payload: dict) -> None:
        """Accumulate one metadata-only trace step under its trace_id."""
        tid = payload.get("trace_id")
        if not tid:
            return
        self._traces.setdefault(tid, []).append({
            "step": payload.get("step"),
            "outcome": payload.get("outcome"),
            "timestamp": payload.get("timestamp"),
        })

    def _on_query_completed(self, payload: dict) -> None:
        """Side effects at end of a query: behavioral interaction log (the
        existing learning-loop feed) + flush the assembled trace record. This
        was formerly the inline _finish() body; it now rides the bus.

        Session 5: the SAME assembled trace data is also fanned out to LangFuse
        (fire-and-forget). We read the steps ONCE and send them to both ChromaDB
        and LangFuse — no second data-gathering path."""
        query = payload.get("query", "")
        tool = payload.get("tool", "none")
        response = payload.get("response", "")
        self.memory.log_interaction(query, tool, response, feedback_score=0)
        log.info("query.answered", tool=tool, chars=len(response))
        tid = payload.get("trace_id")
        if tid:
            steps = self._traces.pop(tid, [])
            self.memory.log_trace(tid, query, steps)
            # Fan the identical trace data to LangFuse. Best-effort; the
            # exporter swallows all errors so this can't affect the response.
            self.observer.export_trace(tid, query, steps, tool, response)

    def _on_action_confirmed(self, payload: dict) -> None:
        """Audit a confirmed state-changing action (e.g. store_memory).
        Audit is a side effect, so it moves to the bus — but note the CONFIRM
        DECISION itself is NOT here. The gate (which tool requires confirmation,
        and the connector's structural enforcement) stays inline in the call
        path; this only records that a confirmed action happened."""
        audit(payload.get("action", ""), payload.get("tool", ""),
              payload.get("params", {}), payload.get("result"))

    def _on_pipeline_stage(self, payload: dict) -> None:
        """Teams notification on a completed package-pipeline stage. Pure
        notification — posting a card mutates nothing in CofC systems and is
        not the human gate (that lives in package_pipeline Stage 4/5). Best
        effort; failures are isolated by the bus."""
        from tools import teams
        teams.send_alert({
            "title": f"Pipeline stage complete: {payload.get('stage', '?')}",
            "body": payload.get("summary", ""),
            "severity": payload.get("severity", "info"),
            "source": "jarvis-pipeline",
        })

    def handle(self, raw_text: str) -> str:
        """Main entry point. GLaDOS posts transcribed text; returns speakable text."""
        log.info("query.received", chars=len(raw_text))

        # Session 3 — one trace_id per query, generated at request start. Trace
        # steps are metadata-only (step name + short outcome), published to the
        # bus as the query flows; a subscriber assembles them into one record.
        trace_id = str(uuid.uuid4())

        # ── Step 1: Feedback / memory commands ───────────────────────────────
        feedback_type, text = detect_feedback(raw_text)
        self._trace(trace_id, "feedback_detect", feedback_type)

        if feedback_type == "memory":
            self.memory.store_fact(text)
            # Audit is a side effect -> bus (action.confirmed). The decision
            # that this is an explicit, confirmed store stays inline (the
            # detect_feedback classification above); only the record rides out.
            self.bus.publish(EVT_ACTION_CONFIRMED, {
                "action": "store_memory", "tool": "jarvis_core.store_memory",
                "params": {"fact": text}, "result": {"status": "stored"},
            })
            return "Got it, I'll remember that."

        if feedback_type == "good" and self.last_query:
            self.memory.log_interaction(self.last_query, self.last_tool,
                                        self.last_response, feedback_score=1)
            return "Good to know, I'll use that as a reference going forward."

        if feedback_type == "bad" and self.last_query:
            self.memory.log_interaction(self.last_query, self.last_tool,
                                        self.last_response, feedback_score=-1)
            return "Got it. What's the right answer so I can correct myself?"

        if feedback_type == "correction" and self.last_query:
            self.memory.apply_correction(self.last_query, text)
            self.memory.log_interaction(self.last_query, self.last_tool,
                                        self.last_response, feedback_score=-1)
            return "Understood. I've stored the correction."

        # ── Step 2: Recall relevant memories ─────────────────────────────────
        memories = self.memory.recall(text)
        memory_block = "\n".join(memories) if memories else ""
        self._trace(trace_id, "memory_recall", f"{len(memories)} hits")

        # ── Step 3: Build system prompt ───────────────────────────────────────
        system_prompt = (
            "You are JARVIS, the IT operations assistant for the College of "
            "Charleston endpoint team. You have tools for Intune, Jamf Pro, "
            "Taegis SIEM, and device records. Responses are spoken aloud — keep "
            "them concise and conversational. No markdown. If you need data, "
            "call the appropriate tool. Don't guess at numbers.\n"
            + (f"\nRelevant context from memory:\n{memory_block}\n" if memory_block else "")
        )

        # ── Step 4: Pure research queries (may egress per config) ─────────────
        # GATE (inline, not an event): the egress routing decision is made here
        # by EgressRouter and acted on directly. No event handler participates
        # in deciding local-vs-external.
        research_route = self.egress.route_research(text)
        self._trace(trace_id, "research_route", research_route)
        if research_route == "claude":
            answer = self.egress.call_claude(text, system_prompt)
            self._finish(text, "external:claude", answer, trace_id)
            return answer
        if research_route == "web":
            raw = self.egress.call_web_search(text)
            # Raw web results are summarized LOCALLY — they never round-trip out.
            answer = self._summarize_tool_result(text, "web_search", raw,
                                                 system_prompt, pick_model(text))
            self._finish(text, "external:web", answer, trace_id)
            return answer

        # ── Step 5: Local inference with tool routing ─────────────────────────
        # Session B: stamp the start of the local-inference path so the stress
        # log can record end-to-end duration_ms. One assignment, no control-flow
        # change — the research/claude/web paths return before here and are out
        # of scope for the perf log (they're egress paths, not local inference).
        _inference_started = time.monotonic()
        model = pick_model(text)
        self._trace(trace_id, "engine_select", f"ollama:{model}")
        result = self._call_ollama(text, system_prompt, model)

        tool_called = "none"
        answer = result.get("content", "")

        # ── Step 6: Execute tool if the model asked for one ───────────────────
        if result.get("type") == "tool_call":
            tool_called = result["tool"]
            self._trace(trace_id, "tool_call", tool_called)
            tool_output = execute_tool(tool_called, result["args"], self.memory)

            # GATE (inline, not an event): the FERPA firewall. Tools in
            # TOOL_DATA_LOCAL — and any tool not explicitly whitelisted —
            # ALWAYS summarize locally. route_inference makes this decision
            # synchronously here, BEFORE any engine is chosen; an engine never
            # gets to re-decide. This is the hard egress checkpoint.
            route = self.egress.route_inference(text, True, tool_called)
            self._trace(trace_id, "egress_route", route)
            if route == "claude":
                answer = self.egress.call_claude(
                    f"Summarize this tool result for spoken audio, concisely:\n\n"
                    f"Original question: {text}\nTool: {tool_called}\n"
                    f"Result: {tool_output}",
                    system_prompt,
                )
            else:
                answer = self._summarize_tool_result(
                    text, tool_called, tool_output, system_prompt, model
                )

        # ── Step 7: Finish — side effects (log + trace) flushed via the bus ───
        # BUG 1 (layer 2 — suppress): final backstop. If a bracketed tool-call
        # string for a known tool STILL made it into the answer (e.g. the model
        # buried it mid-sentence so layer 1's whole-string match didn't fire, or
        # a summarization pass echoed it), never speak/display raw call syntax.
        # Replace with a graceful spoken fallback. Name-gated, so ordinary
        # bracketed prose is untouched.
        if _contains_leaked_tool_call(answer):
            log.warning("query.toolcall_leak_suppressed",
                        tool=tool_called, trace_id=trace_id)
            answer = ("I started to pull that data but the request didn't "
                      "complete cleanly — could you ask me again?")

        # Session B — fire-and-forget perf log. Records only what's already in
        # scope on this path (model, end-to-end duration, tool); tokens/VRAM/
        # spillover aren't measured in core (no NVML here, summarize path returns
        # no token counts) so they stay NULL rather than restructuring the flow
        # to manufacture them. Fully guarded — see _log_stress.
        self._log_stress(text, model, tool_called, _inference_started)

        self._finish(text, tool_called, answer, trace_id)
        return answer

    def _log_stress(self, query: str, model: str, tool_called: str,
                    started_monotonic: float) -> None:
        """Fire-and-forget bridge to stress_log.log_inference. DOUBLE-guarded:
        no-ops if the module isn't available, and wraps the call in try/except so
        a logging failure can never propagate onto the user query path (it must
        not change handle()'s behavior or return value). stress_log itself is
        already non-raising by contract; this is belt-and-suspenders."""
        if not _STRESS_LOG_AVAILABLE:
            return
        try:
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            stress_log.log_inference({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "query_preview": query,            # stress_log truncates to ~80
                "duration_ms": duration_ms,
                "tool_called": None if tool_called == "none" else tool_called,
                # tokens_generated / tokens_per_second / vram_peak_mb /
                # cpu_spillover: not available on this path -> NULL by omission.
            })
        except Exception as e:
            # Never let observability touch the response path.
            log.error("stress_log.hook_failed", error=str(e))

    def _trace(self, trace_id: str, step: str, outcome: str) -> None:
        """Publish a single metadata-only trace step. Helper so the call sites
        in handle() stay terse. Never carries prompt/response text."""
        self.bus.publish(EVT_TRACE_STEP, {
            "trace_id": trace_id, "step": step, "outcome": outcome,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _finish(self, query: str, tool: str, response: str,
                trace_id: str | None = None) -> None:
        """End-of-query bookkeeping. The behavioral interaction log (learning-
        loop feed) and trace flush are SIDE EFFECTS, so they ride the bus via
        query.completed. last_* state is set inline because the very next
        feedback turn reads it synchronously."""
        self.last_query, self.last_tool, self.last_response = query, tool, response
        self._trace(trace_id or "", "finish", tool)
        self.bus.publish(EVT_QUERY_COMPLETED, {
            "query": query, "tool": tool, "response": response,
            "trace_id": trace_id,
        })

    def _call_ollama(self, query: str, system: str, model: str) -> dict:
        """Local tool-routing chat. Session 3: mechanics moved into
        OllamaEngine.chat_with_tools (behavior unchanged); this delegates so
        there's one Ollama implementation. Stays LOCAL by construction."""
        return self.egress.ollama.chat_with_tools(query, system, model)

    def _summarize_tool_result(self, original_query: str, tool_name: str,
                               tool_output: str, system: str, model: str) -> str:
        """Turn raw tool output into a natural spoken response — locally.
        Session 3: delegates to OllamaEngine.summarize_tool_result; identical
        behavior. This is the path ops-data tool results always take and it
        never leaves the box."""
        return self.egress.ollama.summarize_tool_result(
            original_query, tool_name, tool_output, system, model)


# ── HTTP SERVER (FastAPI) ─────────────────────────────────────────────────────
# voice_listener.py POSTs transcribed speech to /query.
# Uptime Kuma polls /health — degraded/down states alert via the monitoring node.
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

_jarvis: JarvisCore | None = None


def get_jarvis() -> JarvisCore:
    global _jarvis
    if _jarvis is None:
        _jarvis = JarvisCore()
    return _jarvis


class QueryRequest(BaseModel):
    query: str

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query cannot be empty")
        if len(v) > 4000:
            raise ValueError("query too long")
        return v.strip()


def _check_ollama() -> str:
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
        return "ok" if r.status_code == 200 else "degraded"
    except Exception:
        return "down"


def _check_chroma() -> str:
    try:
        import chromadb
        chromadb.PersistentClient(path=CHROMA_PATH).heartbeat()
        return "ok"
    except Exception:
        return "down"


def build_app() -> FastAPI:
    app = FastAPI(title="JARVIS Core", version="1.0")

    @app.on_event("startup")
    def startup():
        get_jarvis()  # warm: loads ChromaDB, inits audit, verifies config
        log.info("server.ready")

    @app.post("/query")
    def query(req: QueryRequest):
        return {"response": get_jarvis().handle(req.query)}

    @app.get("/health")
    def health():
        checks = {"ollama": _check_ollama(), "chromadb": _check_chroma()}
        status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
        code = 200 if status == "ok" else 503
        return JSONResponse(status_code=code, content={
            "status": status,
            "checks": checks,
            "egress": EgressRouter.egress_status(),
            "mock_mode": MOCK_MODE,
        })

    return app


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


def run_cli() -> None:
    jarvis = get_jarvis()
    print("[JARVIS] Ready. (CLI mode — type 'exit' to quit)")
    print(f"[JARVIS] {EgressRouter.egress_status()}")
    if MOCK_MODE:
        print("[JARVIS] MOCK MODE — all connectors returning synthetic data")
    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[JARVIS] Signing off.")
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            print("[JARVIS] Signing off.")
            break
        print(f"JARVIS: {jarvis.handle(text)}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
# systemd unit (etc/jarvis-core.service):
#   ExecStart=/usr/bin/python3 /opt/cofc-itip/jarvis_core.py --host 127.0.0.1
#   Environment=JARVIS_CONFIG=/etc/cofc-itip/config.env
#   Restart=on-failure
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="JARVIS Core Agent")
    parser.add_argument("--cli", action="store_true", help="Interactive CLI mode")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8081, type=int)  # Session 5: 8080 belongs to the mobile UI
    args = parser.parse_args()

    if args.cli:
        run_cli()
    else:
        run_server(host=args.host, port=args.port)
