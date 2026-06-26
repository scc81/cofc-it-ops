"""
tools/servicenow_kb.py — ServiceNow Knowledge Base Connector
=============================================================
CofCITIP — KB search against the CofC ServiceNow instance.

REFACTOR NOTE (Phase 2):
  This module will be merged into tools/servicenow.py when ticket/incident/
  task CRUD is added. ALL auth and HTTP plumbing lives in _client() so that
  refactor is a simple file merge — the future servicenow.py keeps one shared
  _client(), and each function (search_kb, create_ticket, ...) gets its own
  tool schema in jarvis_core.TOOL_SCHEMAS. Do not add auth logic anywhere
  else in this file.

Auth:      Basic auth (SN_INSTANCE, SN_USER, SN_PASS from config.env).
           Use a dedicated read-only SN account scoped to kb_knowledge.
Endpoint:  GET /api/now/table/kb_knowledge with sysparm_query
Read-only. No write scopes. Write ops arrive in Phase 2 behind the
human-confirmation gate.

Tool function (signature: (params: dict) -> dict):
  search_kb(params) — params: {"query": str, "limit": int=5}
                      returns {"articles": [{"title","number","snippet","url"}]}

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
"""

from __future__ import annotations

import html
import os
import re
import time
import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from tools.secrets import get_secret  # Phase 2: creds via LastPass CLI -> env

log = structlog.get_logger("jarvis.tools.servicenow_kb")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SN_INSTANCE = os.getenv("SN_INSTANCE", "")  # non-secret — e.g. "cofc" -> cofc.service-now.com
SN_USER     = os.getenv("SN_USER", "")      # non-secret — env only
SN_PASS     = get_secret("SN_PASS")         # secret — LastPass/env
MOCK_MODE   = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES = int(os.getenv("SN_MAX_RETRIES", "3"))

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


# ── SHARED CLIENT (the Phase 2 merge point) ───────────────────────────────────
_client_cache: dict = {"client": None}


def _client() -> httpx.Client:
    """
    Single shared HTTP client for ALL ServiceNow calls — KB now, ticket CRUD
    in Phase 2. Auth, base URL, headers, and timeouts live here and only here.
    """
    if _client_cache["client"] is not None:
        return _client_cache["client"]

    if not (SN_INSTANCE and SN_USER and SN_PASS):
        raise RuntimeError(
            "ServiceNow credentials missing — set SN_INSTANCE, SN_USER, "
            "SN_PASS in /etc/cofc-itip/config.env"
        )

    _client_cache["client"] = httpx.Client(
        base_url=f"https://{SN_INSTANCE}.service-now.com",
        auth=(SN_USER, SN_PASS),
        headers={"Accept": "application/json"},
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


# ── MOCK DATA ─────────────────────────────────────────────────────────────────
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


# ── TOOL FUNCTION ─────────────────────────────────────────────────────────────
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
        "source": "servicenow_kb",
        "mock": MOCK_MODE,
        "query": params.query,
        "article_count": len(articles),
        "articles": articles,
    }
    log.info("tool.success", tool="search_kb", hits=len(articles))
    return result


def health_check(raw_params: dict | None = None) -> dict:
    """Session 5 addition: connector health for briefing + monitoring.
    No network probe — health is creds-present + breaker state, so polling
    this never burns API rate limit. ok | degraded (breaker open) | down."""
    if MOCK_MODE:
        status = "ok"
    elif not (SN_INSTANCE and SN_USER and SN_PASS):
        status = "down"      # unconfigured — creds missing from config.env
    elif sn_breaker.current_state != "closed":
        status = "degraded"  # breaker open/half-open after repeated failures
    else:
        status = "ok"
    result = {"source": "servicenow_kb", "status": status, "mock": MOCK_MODE,
              "breaker": sn_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.servicenow_kb --function search_kb --params '{"query":"bitlocker recovery"}' --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="ServiceNow KB test harness")
    parser.add_argument("--function", default="search_kb",
                        choices=["search_kb"])
    parser.add_argument("--params", default='{"query": "password reset"}')
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    print(_json.dumps(search_kb(_json.loads(args.params)), indent=2,
                      default=str))
