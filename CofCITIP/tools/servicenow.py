"""
tools/servicenow.py — ServiceNow Connector (KB + Ticket/Incident/Task CRUD)
===========================================================================
CofCITIP — Full ServiceNow integration for the CofC instance.

REPLACES tools/servicenow_kb.py. Once this is merged and reviewed, delete
servicenow_kb.py from the repo and repoint any remaining imports here. (That
deletion is a git operation — not done in code.)

This file is the Phase-2 merge the old servicenow_kb.py was structured for:
ALL auth and HTTP plumbing still lives in _client() / _sn_get() / _sn_post() /
_sn_patch(), and every public function keeps the (params: dict) -> dict
contract with its own tool schema in jarvis_core.TOOL_SCHEMAS.

Auth:      Basic auth. SN_INSTANCE/SN_USER non-secret (env); SN_PASS via
           tools.secrets (LastPass CLI -> env fallback).
Read:      kb_knowledge (search_kb), incident/sc_task (get/list).
Write:     incident + ticket/task create + update. WRITES GO THROUGH THE
           HUMAN CONFIRMATION GATE — same pattern as package_pipeline.py:
           a live (non-mock) write does NOT execute until a human confirms.
           Confirmation is structural, audited, and cannot be skipped by a
           flag. In MOCK_MODE writes simulate + log only.

Tool functions (all (params: dict) -> dict):
  search_kb(params)        — KB article search (read)
  create_ticket(params)    — open a catalog/request task (WRITE, gated)
  get_ticket(params)       — fetch one ticket/incident by number (read)
  update_ticket(params)    — work notes / state change (WRITE, gated)
  list_my_tickets(params)  — tickets assigned to a user (read)
  create_incident(params)  — open an incident (WRITE, gated)
  health_check()           — connector health for briefing + monitoring

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
"""

from __future__ import annotations

import getpass
import html
import os
import re
import time
from datetime import datetime, timezone

import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from tools.secrets import get_secret  # Phase 2: creds via LastPass CLI -> env

log = structlog.get_logger("jarvis.tools.servicenow")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SN_INSTANCE = os.getenv("SN_INSTANCE", "")  # non-secret — e.g. "cofc" -> cofc.service-now.com
SN_USER     = os.getenv("SN_USER", "")      # non-secret — env only
SN_PASS     = get_secret("SN_PASS")         # secret — LastPass/env
MOCK_MODE   = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES = int(os.getenv("SN_MAX_RETRIES", "3"))

# Deliberate switch for live ServiceNow WRITES (mirrors package_pipeline's
# PIPELINE_LIVE_ENABLED). Even with this true, a write still needs human
# confirmation — this only gates whether the live branch is reachable at all.
SN_WRITE_ENABLED = os.getenv("SN_WRITE_ENABLED", "false").lower() == "true"

# ServiceNow limits vary by instance — start conservative at 20 req/min
# (per platform_engineering_guide.md) until IT confirms real quotas.
SN_CALLS_PER_MINUTE = int(os.getenv("SN_RATE_LIMIT_RPM", "20"))

sn_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60,
                                      name="servicenow")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


sn_breaker.add_listener(_BreakerLogger())


# ── AUDIT (write actions) ─────────────────────────────────────────────────────
# Same shape as package_pipeline.py's audit: who, action, target, outcome,
# when. structlog only (no separate JSONL here — the SN instance itself is the
# system of record; this is the JARVIS-side trail).
def _audit(action: str, target: str, params: dict, result: dict) -> None:
    log.info(
        "audit.write",
        action=action,
        actor=os.getenv("JARVIS_ACTOR", getpass.getuser()),
        target=target,
        params={k: params.get(k) for k in
                ("short_description", "state", "category", "urgency",
                 "assigned_to") if k in params},
        result_summary={k: result.get(k) for k in
                        ("ticket_number", "sys_id", "state", "status")
                        if k in result},
        mock=MOCK_MODE,
    )


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class KBSearchParams(BaseModel):
    query: str
    limit: int = 5

    @field_validator("query")
    @classmethod
    def query_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query cannot be empty")
        if len(v) > 200:
            raise ValueError("query too long")
        return v

    @field_validator("limit")
    @classmethod
    def limit_ok(cls, v: int) -> int:
        return max(1, min(v, 20))  # clamp rather than error — voice-friendly


class CreateTicketParams(BaseModel):
    short_description: str
    description: str = ""
    category: str = "inquiry"
    urgency: str = "3"           # ServiceNow 1=high, 2=medium, 3=low
    caller_id: str = ""
    confirmed: bool = False      # human confirmation gate (live writes)

    @field_validator("short_description")
    @classmethod
    def sd_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("short_description cannot be empty")
        return v[:160]

    @field_validator("urgency")
    @classmethod
    def urgency_ok(cls, v: str) -> str:
        v = (str(v) or "3").strip()
        return v if v in ("1", "2", "3") else "3"


class GetTicketParams(BaseModel):
    ticket_number: str

    @field_validator("ticket_number")
    @classmethod
    def num_ok(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticket_number cannot be empty")
        return v


class UpdateTicketParams(BaseModel):
    ticket_number: str
    work_notes: str = ""
    state: str = ""
    confirmed: bool = False      # human confirmation gate (live writes)

    @field_validator("ticket_number")
    @classmethod
    def num_ok(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticket_number cannot be empty")
        return v


class ListMyTicketsParams(BaseModel):
    assigned_to: str
    state: str = "open"

    @field_validator("assigned_to")
    @classmethod
    def at_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("assigned_to cannot be empty")
        return v


class CreateIncidentParams(BaseModel):
    short_description: str
    description: str = ""
    category: str = "inquiry"
    urgency: str = "3"
    caller_id: str = ""
    confirmed: bool = False      # human confirmation gate (live writes)

    @field_validator("short_description")
    @classmethod
    def sd_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("short_description cannot be empty")
        return v[:160]

    @field_validator("urgency")
    @classmethod
    def urgency_ok(cls, v: str) -> str:
        v = (str(v) or "3").strip()
        return v if v in ("1", "2", "3") else "3"


# ── SHARED CLIENT (auth/HTTP plumbing — the only place creds touch the wire) ──
_client_cache: dict = {"client": None}


def _client() -> httpx.Client:
    """
    Single shared HTTP client for ALL ServiceNow calls — KB read AND ticket
    CRUD. Auth, base URL, headers, and timeouts live here and only here.
    """
    if _client_cache["client"] is not None:
        return _client_cache["client"]

    if not (SN_INSTANCE and SN_USER and SN_PASS):
        raise RuntimeError(
            "ServiceNow credentials missing — set SN_INSTANCE, SN_USER, "
            "SN_PASS in /etc/cofc-itip/config.env (or LastPass)"
        )

    _client_cache["client"] = httpx.Client(
        base_url=f"https://{SN_INSTANCE}.service-now.com",
        auth=(SN_USER, SN_PASS),
        headers={"Accept": "application/json",
                 "Content-Type": "application/json"},
        timeout=30,
    )
    log.info("client.created", instance=SN_INSTANCE)
    return _client_cache["client"]


# ── HTTP LAYER (rate limit -> retry -> breaker) ───────────────────────────────
@sn_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=SN_CALLS_PER_MINUTE, period=60)
def _sn_get(path: str, params: dict) -> dict:
    started = time.monotonic()
    resp = _client().get(path, params=params)
    resp.raise_for_status()
    log.debug("sn.get", path=path,
              duration_ms=int((time.monotonic() - started) * 1000))
    return resp.json()


@sn_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=SN_CALLS_PER_MINUTE, period=60)
def _sn_post(path: str, body: dict) -> dict:
    started = time.monotonic()
    resp = _client().post(path, json=body)
    resp.raise_for_status()
    log.debug("sn.post", path=path,
              duration_ms=int((time.monotonic() - started) * 1000))
    return resp.json()


@sn_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=SN_CALLS_PER_MINUTE, period=60)
def _sn_patch(path: str, body: dict) -> dict:
    started = time.monotonic()
    resp = _client().patch(path, json=body)
    resp.raise_for_status()
    log.debug("sn.patch", path=path,
              duration_ms=int((time.monotonic() - started) * 1000))
    return resp.json()


# ── HELPERS ───────────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")


def _snippet(text: str, length: int = 240) -> str:
    """Strip HTML from kb body text and trim to a spoken-friendly snippet."""
    clean = html.unescape(_TAG_RE.sub(" ", text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:length] + ("…" if len(clean) > length else "")


def _article_url(sys_id: str) -> str:
    return (f"https://{SN_INSTANCE or 'INSTANCE'}.service-now.com"
            f"/kb_view.do?sys_kb_id={sys_id}")


def _ticket_url(table: str, sys_id: str) -> str:
    return (f"https://{SN_INSTANCE or 'INSTANCE'}.service-now.com"
            f"/nav_to.do?uri={table}.do?sys_id={sys_id}")


# ServiceNow incident state codes (numeric) <-> words, for friendly I/O.
_STATE_WORD = {"1": "new", "2": "in_progress", "3": "on_hold",
               "6": "resolved", "7": "closed", "8": "canceled"}
_STATE_CODE = {v: k for k, v in _STATE_WORD.items()}


def _state_to_code(state: str) -> str:
    """Accept a word or a code; return the numeric code SN expects."""
    s = (state or "").strip().lower()
    if s in _STATE_CODE:
        return _STATE_CODE[s]
    if s in _STATE_WORD:   # already a code
        return s
    return s  # pass through unknown values; SN validates server-side


# ── MOCK DATA — KB ─────────────────────────────────────────────────────────────
# Realistic CofC IT KB shaped like real kb_knowledge rows. Topic-keyed so the
# returned articles actually match the query subject in demos.
_MOCK_KB = [
    {
        "topics": ["bitlocker", "encrypt", "recovery key", "compliance"],
        "number": "KB0010234",
        "short_description": "Retrieving a BitLocker recovery key from Entra ID",
        "text": "When a Windows device prompts for a BitLocker recovery key, locate "
                "the key in the Entra admin center under Devices > All devices > "
                "select device > Recovery keys. Field techs may also use the Intune "
                "console device blade. Verify the user's identity per the standard "
                "verification checklist before reading any key aloud.",
        "sys_id": "mock-sys-0234",
    },
    {
        "topics": ["intune", "enroll", "autopilot", "windows"],
        "number": "KB0010418",
        "short_description": "Windows Autopilot enrollment troubleshooting",
        "text": "If a device stalls during Autopilot ESP: confirm the hardware hash "
                "was imported, the device is assigned to the CofC-Autopilot profile, "
                "and the network allows access to the Microsoft endpoints listed in "
                "the firewall appendix. Common fix: delete the Entra device object "
                "and re-register the hash.",
        "sys_id": "mock-sys-0418",
    },
    {
        "topics": ["jamf", "mac", "macos", "ventura", "sonoma", "enrollment"],
        "number": "KB0010522",
        "short_description": "Re-enrolling a Mac in Jamf Pro after OS reinstall",
        "text": "After erasing or reinstalling macOS, devices purchased through "
                "Apple School Manager re-enroll automatically at Setup Assistant. "
                "For manual enrollment, direct the user to the enrollment URL and "
                "approve the MDM profile in System Settings > Privacy & Security > "
                "Profiles. Confirm check-in in Jamf within 15 minutes.",
        "sys_id": "mock-sys-0522",
    },
    {
        "topics": ["vpn", "remote", "globalprotect", "network"],
        "number": "KB0010611",
        "short_description": "GlobalProtect VPN setup for faculty and staff",
        "text": "Install GlobalProtect from the software portal, connect to "
                "vpn.cofc.edu, and authenticate with campus credentials plus Duo. "
                "Split tunneling is enabled — only campus resources route through "
                "the tunnel. For 'gateway unreachable' errors, verify the client "
                "version is 6.x or later.",
        "sys_id": "mock-sys-0611",
    },
    {
        "topics": ["password", "reset", "account", "duo", "mfa", "lockout"],
        "number": "KB0010702",
        "short_description": "Campus account password reset and Duo re-activation",
        "text": "Users reset passwords at password.cofc.edu after verifying with "
                "Duo. If the user lost their Duo device, Service Desk verifies "
                "identity per the callback procedure, then issues a bypass code "
                "valid for 10 minutes. New phones re-activate via the Duo portal.",
        "sys_id": "mock-sys-0702",
    },
    {
        "topics": ["printer", "papercut", "print", "queue"],
        "number": "KB0010815",
        "short_description": "Adding campus printers via PaperCut Mobility Print",
        "text": "Faculty/staff devices discover printers automatically through "
                "Mobility Print when on the campus network. For manual setup, "
                "install the Mobility Print client and sign in with campus "
                "credentials. Lab and classroom printers are deployed via Intune "
                "and Jamf policy — do not add them manually.",
        "sys_id": "mock-sys-0815",
    },
    {
        "topics": ["sentinelone", "antivirus", "security", "agent", "malware"],
        "number": "KB0010903",
        "short_description": "SentinelOne agent health checks and reinstallation",
        "text": "Verify agent status with 'sentinelctl status' (macOS/Linux) or the "
                "S1 tray icon (Windows). Devices showing offline more than 7 days "
                "in the console need agent reinstall via Intune/Jamf policy. "
                "Never uninstall without the passphrase from InfoSec — contact "
                "the security team, do not attempt removal tools.",
        "sys_id": "mock-sys-0903",
    },
]


def _mock_search(query: str, limit: int) -> list[dict]:
    q_words = set(query.lower().split())
    scored = []
    for art in _MOCK_KB:
        topic_hits = sum(1 for t in art["topics"]
                         if any(w in t or t in w for w in q_words))
        text_hits = sum(1 for w in q_words
                        if w in art["short_description"].lower()
                        or w in art["text"].lower())
        score = topic_hits * 2 + text_hits
        if score > 0:
            scored.append((score, art))
    scored.sort(key=lambda x: -x[0])
    # Always return at least 3 in mock mode so demos never look empty.
    hits = [a for _, a in scored[:limit]]
    if len(hits) < 3:
        seen = {a["number"] for a in hits}
        hits += [a for a in _MOCK_KB if a["number"] not in seen][: 3 - len(hits)]
    return hits[:limit]


# ── MOCK DATA — TICKETS / INCIDENTS ───────────────────────────────────────────
# Realistic CofC-shaped endpoint-team work. Numbers follow SN conventions:
# INC######## for incidents, TASK######## / RITM######## for requests.
# In-process store so create -> get -> update flows are coherent in a demo.
_MOCK_TICKETS: dict[str, dict] = {
    "INC0012345": {
        "number": "INC0012345", "sys_id": "mock-inc-12345", "table": "incident",
        "short_description": "Laptop won't connect to campus wifi",
        "description": "Faculty laptop in HIST fails to join eduroam after the "
                       "summer reimage. Other devices on the same SSID are fine.",
        "category": "network", "urgency": "2", "state": "in_progress",
        "assigned_to": "sgray", "caller_id": "mthompson",
        "opened_at": "2026-06-18T13:20:00Z",
    },
    "INC0012346": {
        "number": "INC0012346", "sys_id": "mock-inc-12346", "table": "incident",
        "short_description": "BitLocker recovery prompt after firmware update",
        "description": "Dell Latitude prompting for BitLocker key after a BIOS "
                       "update pushed via Intune. Key retrieved from Entra.",
        "category": "endpoint", "urgency": "2", "state": "resolved",
        "assigned_to": "mversoza", "caller_id": "jlee",
        "opened_at": "2026-06-17T09:05:00Z",
    },
    "TASK0012345": {
        "number": "TASK0012345", "sys_id": "mock-task-12345", "table": "sc_task",
        "short_description": "Need software install - Adobe Creative Cloud",
        "description": "New design hire in COMM needs Adobe CC deployed to "
                       "COMM-MAC-214 via Jamf Self Service.",
        "category": "software", "urgency": "3", "state": "new",
        "assigned_to": "magostosaviado", "caller_id": "rkim",
        "opened_at": "2026-06-19T15:40:00Z",
    },
}


def _next_mock_number(table: str) -> str:
    prefix = {"incident": "INC", "sc_task": "TASK"}.get(table, "INC")
    existing = [int(re.sub(r"\D", "", n)) for n in _MOCK_TICKETS
                if n.startswith(prefix)]
    nxt = (max(existing) + 1) if existing else 12347
    return f"{prefix}{nxt:07d}"


def _public_ticket(t: dict) -> dict:
    """Shape a stored/live ticket row into the response dict."""
    return {
        "ticket_number": t.get("number", ""),
        "sys_id": t.get("sys_id", ""),
        "table": t.get("table", "incident"),
        "short_description": t.get("short_description", ""),
        "description": t.get("description", ""),
        "category": t.get("category", ""),
        "urgency": t.get("urgency", ""),
        "state": _STATE_WORD.get(str(t.get("state", "")), t.get("state", "")),
        "assigned_to": t.get("assigned_to", ""),
        "caller_id": t.get("caller_id", ""),
        "opened_at": t.get("opened_at", ""),
        "url": _ticket_url(t.get("table", "incident"), t.get("sys_id", "")),
    }


# ── CONFIRMATION GATE (writes) ────────────────────────────────────────────────
# Mirrors package_pipeline.py's gate intent for the simpler ticket-write case:
# a LIVE write must carry an explicit human confirmation (confirmed=true) or it
# does NOT execute. There is no env flag that bypasses the human confirmation —
# SN_WRITE_ENABLED only controls whether the live code path exists at all.
def _gate_blocked(action: str, target: str, confirmed: bool,
                  params: dict) -> dict | None:
    """Return a 'confirmation_required' dict if a live write must be held,
    else None (clear to proceed). MOCK_MODE never blocks — it simulates."""
    if MOCK_MODE:
        return None
    if not confirmed:
        _audit(action, target, params, {"status": "confirmation_required"})
        log.warning("gate.confirmation_required", action=action, target=target)
        return {
            "source": "servicenow",
            "status": "confirmation_required",
            "action": action,
            "target": target,
            "mock": False,
            "spoken": (f"This will {action.replace('_', ' ')} in ServiceNow. "
                       f"I need explicit confirmation before I make that "
                       f"change. Re-issue with confirmation to proceed."),
        }
    if not SN_WRITE_ENABLED:
        _audit(action, target, params, {"status": "writes_disabled"})
        raise RuntimeError(
            "live ServiceNow write blocked: SN_WRITE_ENABLED is not set. "
            "Enable deliberately after write-scope credential approval.")
    return None


# ── TOOL FUNCTIONS — READ ─────────────────────────────────────────────────────
def search_kb(raw_params: dict) -> dict:
    """Search the ServiceNow knowledge base for articles matching a query."""
    params = KBSearchParams(**(raw_params or {}))
    log.info("tool.start", tool="search_kb", query=params.query,
             limit=params.limit)

    if MOCK_MODE:
        rows = _mock_search(params.query, params.limit)
    else:
        # sysparm_query: published articles, latest version, text match via
        # 123TEXTQUERY321 (SN's full-text search operator for Table API).
        data = _sn_get(
            "/api/now/table/kb_knowledge",
            {
                "sysparm_query": (
                    f"workflow_state=published^latest=true"
                    f"^123TEXTQUERY321{params.query}"
                ),
                "sysparm_fields": "number,short_description,text,sys_id",
                "sysparm_limit": params.limit,
            },
        )
        rows = data.get("result", [])

    articles = [
        {
            "title": r.get("short_description", "Untitled"),
            "number": r.get("number", ""),
            "snippet": _snippet(r.get("text", "")),
            "url": _article_url(r.get("sys_id", "")),
        }
        for r in rows
    ]

    result = {
        "source": "servicenow",
        "mock": MOCK_MODE,
        "query": params.query,
        "article_count": len(articles),
        "articles": articles,
    }
    log.info("tool.success", tool="search_kb", hits=len(articles))
    return result


def get_ticket(raw_params: dict) -> dict:
    """Fetch one ticket/incident by its number."""
    params = GetTicketParams(**(raw_params or {}))
    log.info("tool.start", tool="get_ticket", ticket_number=params.ticket_number)

    if MOCK_MODE:
        t = _MOCK_TICKETS.get(params.ticket_number)
        if not t:
            return {"source": "servicenow", "mock": True, "found": False,
                    "ticket_number": params.ticket_number,
                    "spoken": f"No ticket found with number "
                              f"{params.ticket_number}."}
        return {"source": "servicenow", "mock": True, "found": True,
                "ticket": _public_ticket(t)}

    # Live: incident table first, then sc_task. Number is unique per table.
    for table in ("incident", "sc_task"):
        data = _sn_get(f"/api/now/table/{table}",
                       {"sysparm_query": f"number={params.ticket_number}",
                        "sysparm_limit": 1})
        rows = data.get("result", [])
        if rows:
            row = rows[0]
            row["table"] = table
            return {"source": "servicenow", "mock": False, "found": True,
                    "ticket": _public_ticket(row)}

    return {"source": "servicenow", "mock": False, "found": False,
            "ticket_number": params.ticket_number,
            "spoken": f"No ticket found with number {params.ticket_number}."}


def list_my_tickets(raw_params: dict) -> dict:
    """Tickets assigned to a user, optionally filtered by state."""
    params = ListMyTicketsParams(**(raw_params or {}))
    log.info("tool.start", tool="list_my_tickets",
             assigned_to=params.assigned_to, state=params.state)

    if MOCK_MODE:
        rows = [t for t in _MOCK_TICKETS.values()
                if t.get("assigned_to") == params.assigned_to]
        if params.state != "all":
            open_states = {"new", "in_progress", "on_hold"}
            if params.state == "open":
                rows = [t for t in rows
                        if _STATE_WORD.get(str(t["state"]), t["state"])
                        in open_states]
            else:
                rows = [t for t in rows
                        if _STATE_WORD.get(str(t["state"]), t["state"])
                        == params.state]
        tickets = [_public_ticket(t) for t in rows]
    else:
        # active=true approximates "open"; a named state maps to its code.
        if params.state == "open":
            q = f"assigned_to={params.assigned_to}^active=true"
        elif params.state == "all":
            q = f"assigned_to={params.assigned_to}"
        else:
            q = (f"assigned_to={params.assigned_to}"
                 f"^state={_state_to_code(params.state)}")
        data = _sn_get("/api/now/table/incident",
                       {"sysparm_query": q, "sysparm_limit": 50})
        rows = data.get("result", [])
        for r in rows:
            r["table"] = "incident"
        tickets = [_public_ticket(r) for r in rows]

    result = {
        "source": "servicenow",
        "mock": MOCK_MODE,
        "assigned_to": params.assigned_to,
        "state_filter": params.state,
        "ticket_count": len(tickets),
        "tickets": tickets,
    }
    log.info("tool.success", tool="list_my_tickets", hits=len(tickets))
    return result


# ── TOOL FUNCTIONS — WRITE (gated) ────────────────────────────────────────────
def _create(table: str, sd: str, desc: str, category: str, urgency: str,
            caller_id: str) -> dict:
    """Shared create path for incident + sc_task."""
    if MOCK_MODE:
        number = _next_mock_number(table)
        row = {
            "number": number, "sys_id": f"mock-{number.lower()}",
            "table": table, "short_description": sd, "description": desc,
            "category": category, "urgency": urgency, "state": "1",
            "assigned_to": "", "caller_id": caller_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        _MOCK_TICKETS[number] = row
        return _public_ticket(row)

    body = {"short_description": sd, "description": desc,
            "category": category, "urgency": urgency}
    if caller_id:
        body["caller_id"] = caller_id
    data = _sn_post(f"/api/now/table/{table}", body)
    row = data.get("result", {})
    row["table"] = table
    return _public_ticket(row)


def create_ticket(raw_params: dict) -> dict:
    """Open a request task (sc_task). WRITE — human confirmation gated."""
    params = CreateTicketParams(**(raw_params or {}))
    log.info("tool.start", tool="create_ticket",
             short_description=params.short_description)

    blocked = _gate_blocked("create_ticket", "sc_task", params.confirmed,
                            raw_params or {})
    if blocked:
        return blocked

    ticket = _create("sc_task", params.short_description, params.description,
                     params.category, params.urgency, params.caller_id)
    _audit("create_ticket", "sc_task", raw_params or {},
           {"ticket_number": ticket["ticket_number"],
            "sys_id": ticket["sys_id"], "state": ticket["state"]})
    result = {"source": "servicenow", "mock": MOCK_MODE, "created": True,
              **ticket}
    log.info("tool.success", tool="create_ticket",
             ticket_number=ticket["ticket_number"])
    return result


def create_incident(raw_params: dict) -> dict:
    """Open an incident. WRITE — human confirmation gated."""
    params = CreateIncidentParams(**(raw_params or {}))
    log.info("tool.start", tool="create_incident",
             short_description=params.short_description)

    blocked = _gate_blocked("create_incident", "incident", params.confirmed,
                            raw_params or {})
    if blocked:
        return blocked

    ticket = _create("incident", params.short_description, params.description,
                     params.category, params.urgency, params.caller_id)
    _audit("create_incident", "incident", raw_params or {},
           {"ticket_number": ticket["ticket_number"],
            "sys_id": ticket["sys_id"], "state": ticket["state"]})
    result = {"source": "servicenow", "mock": MOCK_MODE, "created": True,
              **ticket}
    log.info("tool.success", tool="create_incident",
             ticket_number=ticket["ticket_number"])
    return result


def update_ticket(raw_params: dict) -> dict:
    """Add work notes and/or change state on a ticket. WRITE — this is the
    action the spec singles out for the human confirmation gate: a live update
    does NOT execute until a human confirms (confirmed=true)."""
    params = UpdateTicketParams(**(raw_params or {}))
    log.info("tool.start", tool="update_ticket",
             ticket_number=params.ticket_number, state=params.state)

    if not (params.work_notes or params.state):
        return {"source": "servicenow", "mock": MOCK_MODE, "updated": False,
                "ticket_number": params.ticket_number,
                "spoken": "Nothing to update — provide work notes or a new "
                          "state."}

    blocked = _gate_blocked("update_ticket", params.ticket_number,
                            params.confirmed, raw_params or {})
    if blocked:
        return blocked

    if MOCK_MODE:
        t = _MOCK_TICKETS.get(params.ticket_number)
        if not t:
            return {"source": "servicenow", "mock": True, "updated": False,
                    "ticket_number": params.ticket_number,
                    "spoken": f"No ticket found with number "
                              f"{params.ticket_number}."}
        if params.state:
            t["state"] = _state_to_code(params.state)
        if params.work_notes:
            t.setdefault("work_notes_log", []).append(params.work_notes)
        ticket = _public_ticket(t)
    else:
        # Live: resolve number -> sys_id (table-aware), then PATCH.
        located = get_ticket({"ticket_number": params.ticket_number})
        if not located.get("found"):
            return {"source": "servicenow", "mock": False, "updated": False,
                    "ticket_number": params.ticket_number,
                    "spoken": f"No ticket found with number "
                              f"{params.ticket_number}."}
        tk = located["ticket"]
        body: dict = {}
        if params.work_notes:
            body["work_notes"] = params.work_notes
        if params.state:
            body["state"] = _state_to_code(params.state)
        data = _sn_patch(f"/api/now/table/{tk['table']}/{tk['sys_id']}", body)
        row = data.get("result", {})
        row["table"] = tk["table"]
        ticket = _public_ticket(row)

    _audit("update_ticket", params.ticket_number, raw_params or {},
           {"ticket_number": ticket["ticket_number"], "state": ticket["state"],
            "status": "updated"})
    result = {"source": "servicenow", "mock": MOCK_MODE, "updated": True,
              **ticket}
    log.info("tool.success", tool="update_ticket",
             ticket_number=ticket["ticket_number"])
    return result


# ── HEALTH CHECK ──────────────────────────────────────────────────────────────
def health_check() -> dict:
    """Connector health for briefing + monitoring (Part 3 contract).
    Returns {"status": "ok"|"degraded"|"down", "detail": str}.
    No network probe — creds-present + breaker state — so polling never burns
    API rate limit. Extra keys (source/breaker/mock) retained for callers that
    used the Session-5 shape."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not (SN_INSTANCE and SN_USER and SN_PASS):
        status, detail = "down", "credentials missing (SN_INSTANCE/USER/PASS)"
    elif sn_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {sn_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "servicenow", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": sn_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.servicenow --function search_kb --params '{"query":"bitlocker"}' --mock
# python -m tools.servicenow --function create_incident --params '{"short_description":"wifi down","confirmed":true}' --mock
# python -m tools.servicenow --function update_ticket --params '{"ticket_number":"INC0012345","state":"resolved","confirmed":true}' --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="ServiceNow connector harness")
    parser.add_argument("--function", default="search_kb",
                        choices=["search_kb", "create_ticket", "get_ticket",
                                 "update_ticket", "list_my_tickets",
                                 "create_incident", "health_check"])
    parser.add_argument("--params", default='{"query": "password reset"}')
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    if args.function == "health_check":
        print(_json.dumps(health_check(), indent=2, default=str))
    else:
        fn = {"search_kb": search_kb, "create_ticket": create_ticket,
              "get_ticket": get_ticket, "update_ticket": update_ticket,
              "list_my_tickets": list_my_tickets,
              "create_incident": create_incident}[args.function]
        print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
