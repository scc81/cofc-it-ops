"""
tools/taegis.py — Secureworks Taegis SIEM Connector
====================================================
CofCITIP — Read-only alert/investigation queries against Taegis XDR.

STATUS: Built fully against mock. Live credentials pending — coordinate
        with Alejandro Torres (InfoSec, Taegis vendor POC) before flipping
        JARVIS_MOCK off. FERPA note: SIEM data NEVER leaves the box —
        jarvis_core's egress guard already forces local inference for any
        query that touches this tool.

Auth:      Bearer token. TAEGIS_API_KEY + TAEGIS_URL from config.env.
           Taegis supports client-credential OAuth too; for a single-box
           read-only integration a long-lived API key (Tenant settings >
           Integrations > API keys) is the simpler, supported path.
Endpoint:  POST {TAEGIS_URL}/graphql   (single GraphQL endpoint)
Read-only. No mutations. Alert state changes (resolve/assign) are Phase 2
work behind the human-confirmation gate.

GRAPHQL SCHEMA REFERENCE (documented so this is drop-in when creds arrive)
--------------------------------------------------------------------------
Taegis exposes alerts via the `alertsServiceSearch` query (Alerts API v2):

  query alertsServiceSearch($in: SearchRequestInput) {
    alertsServiceSearch(in: $in) {
      alerts {
        list {
          id
          metadata { title severity createdAt { seconds } }
          entities { entities }     # host/user entities as strings
          status
        }
      }
    }
  }

  SearchRequestInput of interest:
    cql_query : str  — Taegis CQL, e.g.
                "FROM alert WHERE severity >= 0.6 AND
                 earliest=-24h"
    offset    : int
    limit     : int

  Severity in Taegis is a float 0.0–1.0:
    >= 0.8 critical | >= 0.6 high | >= 0.4 medium | < 0.4 low/info
  (_sev_to_cql / _float_to_sev below encode this mapping.)

Investigations use the `investigationsSearch` query:

  query investigationsSearch($page: Int, $perPage: Int, $query: String) {
    investigationsSearch(page: $page, perPage: $perPage, query: $query) {
      investigations {
        id shortId description priority status createdAt
      }
    }
  }

Tool functions (signature: (params: dict) -> dict):
  get_alerts(params)         params: {"severity": str="all", "hours": int=24}
  get_alert_detail(params)   params: {"alert_id": str}
  get_investigations(params) params: {"status": str="open"}

jarvis_core compatibility: execute_tool() calls taegis.query_alerts() —
aliased to get_alerts at the bottom of this file.

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from tools.secrets import get_secret  # Phase 2: creds via LastPass CLI -> env

log = structlog.get_logger("jarvis.tools.taegis")

# ── CONFIG ────────────────────────────────────────────────────────────────────
TAEGIS_URL     = os.getenv("TAEGIS_URL", "")      # non-secret — e.g. https://api.ctpx.secureworks.com
TAEGIS_API_KEY = get_secret("TAEGIS_API_KEY")     # secret — LastPass/env
MOCK_MODE      = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES    = int(os.getenv("TAEGIS_MAX_RETRIES", "3"))

# Taegis published rate limits are generous (600/min on most tenants) but
# start conservative until Alejandro confirms our tenant's quota.
TAEGIS_CALLS_PER_MINUTE = int(os.getenv("TAEGIS_RATE_LIMIT_RPM", "30"))

taegis_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60,
                                          name="taegis")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


taegis_breaker.add_listener(_BreakerLogger())

# Severity mapping — Taegis uses float 0.0–1.0, JARVIS speaks in words.
_SEV_FLOOR = {"critical": 0.8, "high": 0.6, "medium": 0.4, "low": 0.0,
              "all": 0.0}


def _float_to_sev(v: float) -> str:
    if v >= 0.8:
        return "critical"
    if v >= 0.6:
        return "high"
    if v >= 0.4:
        return "medium"
    return "low"


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class AlertsParams(BaseModel):
    severity: str = "all"
    hours: int = 24

    @field_validator("severity")
    @classmethod
    def sev_ok(cls, v: str) -> str:
        v = (v or "all").strip().lower()
        # Voice-friendly: unknown severity falls back to "all" rather than erroring.
        return v if v in _SEV_FLOOR else "all"

    @field_validator("hours")
    @classmethod
    def hours_ok(cls, v: int) -> int:
        return max(1, min(v, 24 * 30))  # clamp 1h–30d


class AlertDetailParams(BaseModel):
    alert_id: str

    @field_validator("alert_id")
    @classmethod
    def id_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("alert_id cannot be empty")
        return v


class InvestigationsParams(BaseModel):
    status: str = "open"

    @field_validator("status")
    @classmethod
    def status_ok(cls, v: str) -> str:
        v = (v or "open").strip().lower()
        return v if v in ("open", "active", "closed", "all") else "open"


# ── SHARED CLIENT ─────────────────────────────────────────────────────────────
_client_cache: dict = {"client": None}


def _client() -> httpx.Client:
    """Single shared HTTP client — auth, base URL, timeouts live here only."""
    if _client_cache["client"] is not None:
        return _client_cache["client"]

    if not (TAEGIS_URL and TAEGIS_API_KEY):
        raise RuntimeError(
            "Taegis credentials missing — set TAEGIS_URL and TAEGIS_API_KEY "
            "in /etc/cofc-itip/config.env (coordinate with Alejandro Torres)"
        )

    _client_cache["client"] = httpx.Client(
        base_url=TAEGIS_URL.rstrip("/"),
        headers={
            "Authorization": f"Bearer {TAEGIS_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30,
    )
    log.info("client.created", url=TAEGIS_URL)
    return _client_cache["client"]


# ── HTTP LAYER (rate limit -> retry -> breaker) ───────────────────────────────
@taegis_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=TAEGIS_CALLS_PER_MINUTE, period=60)
def _gql(query: str, variables: dict) -> dict:
    """POST a GraphQL query. Raises on transport AND on GraphQL-level errors."""
    started = time.monotonic()
    resp = _client().post("/graphql",
                          json={"query": query, "variables": variables})
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        # GraphQL returns 200 with an errors array — surface as a real failure
        # so retry/breaker logic sees it.
        log.error("gql.errors", errors=data["errors"][:3])
        raise httpx.HTTPError(f"GraphQL errors: {data['errors'][:1]}")
    log.debug("gql.ok", duration_ms=int((time.monotonic() - started) * 1000))
    return data.get("data", {})


# ── MOCK DATA ─────────────────────────────────────────────────────────────────
# CofC-shaped defensive-ops alerts: the mix a real campus SOC sees overnight.
# Timestamps are generated relative to "now" so demos always look fresh.
def _mock_alerts() -> list[dict]:
    now = datetime.now(timezone.utc)

    def ts(hours_ago: float) -> str:
        return (now - timedelta(hours=hours_ago)).isoformat()

    return [
        {
            "id": "alert://priv:taegis:mock-a1c4f",
            "title": "Multiple failed logons followed by success — possible password spray",
            "severity": "critical",
            "severity_score": 0.9,
            "host": "cofc-win-3382",
            "user": "facstaff\\rmartinez",
            "timestamp": ts(2.1),
            "status": "OPEN",
            "description": "47 failed authentication attempts against 12 accounts "
                           "from a single internal source, followed by one "
                           "successful logon. Source host is a library kiosk VLAN "
                           "address. Recommend credential reset and host isolation "
                           "review.",
            "mitre": ["T1110.003 Password Spraying"],
        },
        {
            "id": "alert://priv:taegis:mock-b77e2",
            "title": "Malware detected and quarantined — Trojan.GenericKD",
            "severity": "high",
            "severity_score": 0.7,
            "host": "cofc-win-1209",
            "user": "students\\jkhall",
            "timestamp": ts(5.4),
            "status": "OPEN",
            "description": "SentinelOne quarantined a trojan dropped by a browser "
                           "download in a residence-hall subnet. Quarantine "
                           "succeeded; no lateral movement observed. Verify agent "
                           "full-scan completion.",
            "mitre": ["T1204.002 Malicious File"],
        },
        {
            "id": "alert://priv:taegis:mock-c310d",
            "title": "Impossible travel — sign-in from two distant locations",
            "severity": "high",
            "severity_score": 0.65,
            "host": "cofc-mac-0457",
            "user": "facstaff\\dlowell",
            "timestamp": ts(7.8),
            "status": "OPEN",
            "description": "Entra sign-in from Charleston SC followed 22 minutes "
                           "later by a sign-in from an overseas IP. MFA was "
                           "satisfied on both. Could be VPN use — confirm with the "
                           "user before forcing credential reset.",
            "mitre": ["T1078 Valid Accounts"],
        },
        {
            "id": "alert://priv:taegis:mock-d92ab",
            "title": "Outbound connection to newly registered domain",
            "severity": "medium",
            "severity_score": 0.45,
            "host": "cofc-win-2741",
            "user": "facstaff\\tnguyen",
            "timestamp": ts(11.2),
            "status": "OPEN",
            "description": "HTTP beaconing pattern to a domain registered 9 days "
                           "ago. Low-confidence detection — often ad-tech noise, "
                           "but pattern interval is regular. Watchlist candidate.",
            "mitre": ["T1071.001 Web Protocols"],
        },
        {
            "id": "alert://priv:taegis:mock-e55f0",
            "title": "Phishing URL clicked — credential harvesting page",
            "severity": "medium",
            "severity_score": 0.5,
            "host": "cofc-mac-0188",
            "user": "students\\bcarter",
            "timestamp": ts(14.6),
            "status": "RESOLVED",
            "description": "User clicked a link in a quarantined-late phish. Page "
                           "was a Microsoft 365 credential-harvesting clone, taken "
                           "down before submission per proxy logs. User reported "
                           "via Report Phish button — no credentials entered.",
            "mitre": ["T1566.002 Spearphishing Link"],
        },
    ]


def _mock_investigations() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {
            "id": "inv-mock-0042",
            "short_id": "INV-0042",
            "description": "Password spray from library kiosk VLAN — credential "
                           "reset sweep in progress",
            "priority": "critical",
            "status": "open",
            "created_at": (now - timedelta(hours=2)).isoformat(),
        },
        {
            "id": "inv-mock-0039",
            "short_id": "INV-0039",
            "description": "Residence-hall trojan detections — pattern review "
                           "across 3 hosts this week",
            "priority": "high",
            "status": "open",
            "created_at": (now - timedelta(days=1, hours=4)).isoformat(),
        },
        {
            "id": "inv-mock-0035",
            "short_id": "INV-0035",
            "description": "Newly-registered-domain beaconing — watchlist "
                           "validation with Secureworks analyst",
            "priority": "medium",
            "status": "open",
            "created_at": (now - timedelta(days=2, hours=9)).isoformat(),
        },
    ]


# ── GRAPHQL QUERIES (live mode) ───────────────────────────────────────────────
_ALERTS_QUERY = """
query alertsServiceSearch($in: SearchRequestInput) {
  alertsServiceSearch(in: $in) {
    alerts {
      list {
        id
        status
        metadata { title severity createdAt { seconds } }
        entities { entities }
      }
    }
  }
}
"""

_INVESTIGATIONS_QUERY = """
query investigationsSearch($page: Int, $perPage: Int, $query: String) {
  investigationsSearch(page: $page, perPage: $perPage, query: $query) {
    investigations {
      id shortId description priority status createdAt
    }
  }
}
"""


def _entity_host(entities: list[str] | None) -> str:
    """Pull the first hostname-looking entity from Taegis entity strings."""
    for e in entities or []:
        # Taegis entities look like "hostname:cofc-win-3382" / "user:jdoe"
        if e.lower().startswith(("hostname:", "host:")):
            return e.split(":", 1)[1]
    return "unknown"


def _parse_live_alert(row: dict) -> dict:
    meta = row.get("metadata", {}) or {}
    score = float(meta.get("severity", 0) or 0)
    created = (meta.get("createdAt") or {}).get("seconds")
    return {
        "id": row.get("id", ""),
        "title": meta.get("title", "Untitled alert"),
        "severity": _float_to_sev(score),
        "severity_score": score,
        "host": _entity_host((row.get("entities") or {}).get("entities")),
        "timestamp": (datetime.fromtimestamp(int(created), tz=timezone.utc)
                      .isoformat() if created else ""),
        "status": row.get("status", "UNKNOWN"),
    }


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────
def get_alerts(raw_params: dict) -> dict:
    """Recent Taegis alerts filtered by severity and time window."""
    params = AlertsParams(**(raw_params or {}))
    log.info("tool.start", tool="get_alerts", severity=params.severity,
             hours=params.hours)

    if MOCK_MODE:
        rows = _mock_alerts()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=params.hours)
        rows = [a for a in rows
                if datetime.fromisoformat(a["timestamp"]) >= cutoff]
    else:
        floor = _SEV_FLOOR[params.severity]
        cql = (f"FROM alert WHERE severity >= {floor} "
               f"AND status = 'OPEN' EARLIEST=-{params.hours}h")
        data = _gql(_ALERTS_QUERY,
                    {"in": {"cql_query": cql, "offset": 0, "limit": 50}})
        raw = (((data.get("alertsServiceSearch") or {})
                .get("alerts") or {}).get("list")) or []
        rows = [_parse_live_alert(r) for r in raw]

    # Severity filter applies in both modes (mock keeps full mix on disk).
    if params.severity != "all":
        floor = _SEV_FLOOR[params.severity]
        rows = [a for a in rows if a.get("severity_score", 0) >= floor]

    rows.sort(key=lambda a: -a.get("severity_score", 0))
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for a in rows:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1

    alerts = [{k: a.get(k, "") for k in
               ("id", "title", "severity", "host", "timestamp", "status")}
              for a in rows]

    result = {
        "source": "taegis",
        "mock": MOCK_MODE,
        "window_hours": params.hours,
        "severity_filter": params.severity,
        "alert_count": len(alerts),
        "by_severity": counts,
        "alerts": alerts,
    }
    log.info("tool.success", tool="get_alerts", hits=len(alerts),
             by_severity=counts)
    return result


def get_alert_detail(raw_params: dict) -> dict:
    """Full detail for a single Taegis alert by ID."""
    params = AlertDetailParams(**(raw_params or {}))
    log.info("tool.start", tool="get_alert_detail", alert_id=params.alert_id)

    if MOCK_MODE:
        match = next((a for a in _mock_alerts()
                      if a["id"] == params.alert_id
                      or params.alert_id in a["id"]), None)
    else:
        # Live: alertsServiceSearch with an ID-scoped CQL query. (Taegis also
        # exposes alertsServiceRetrieve(ids:[...]) — swap in if our tenant
        # schema supports it; search-by-id works everywhere.)
        data = _gql(_ALERTS_QUERY,
                    {"in": {"cql_query":
                            f"FROM alert WHERE id = '{params.alert_id}'",
                            "offset": 0, "limit": 1}})
        raw = (((data.get("alertsServiceSearch") or {})
                .get("alerts") or {}).get("list")) or []
        match = _parse_live_alert(raw[0]) if raw else None

    if match is None:
        log.warning("tool.not_found", tool="get_alert_detail",
                    alert_id=params.alert_id)
        return {"source": "taegis", "mock": MOCK_MODE, "found": False,
                "alert_id": params.alert_id,
                "spoken": "I couldn't find an alert with that ID."}

    result = {"source": "taegis", "mock": MOCK_MODE, "found": True,
              "alert": match}
    log.info("tool.success", tool="get_alert_detail", alert_id=params.alert_id)
    return result


def get_investigations(raw_params: dict) -> dict:
    """Open (or filtered) Taegis investigations."""
    params = InvestigationsParams(**(raw_params or {}))
    log.info("tool.start", tool="get_investigations", status=params.status)

    if MOCK_MODE:
        rows = _mock_investigations()
    else:
        q = "" if params.status == "all" else f"status:{params.status}"
        data = _gql(_INVESTIGATIONS_QUERY,
                    {"page": 1, "perPage": 25, "query": q})
        raw = ((data.get("investigationsSearch") or {})
               .get("investigations")) or []
        rows = [{
            "id": r.get("id", ""),
            "short_id": r.get("shortId", ""),
            "description": r.get("description", ""),
            "priority": r.get("priority", ""),
            "status": (r.get("status") or "").lower(),
            "created_at": r.get("createdAt", ""),
        } for r in raw]

    if params.status != "all":
        rows = [r for r in rows if r["status"] == params.status]

    result = {
        "source": "taegis",
        "mock": MOCK_MODE,
        "status_filter": params.status,
        "investigation_count": len(rows),
        "investigations": rows,
    }
    log.info("tool.success", tool="get_investigations", hits=len(rows))
    return result


# ── JARVIS_CORE COMPATIBILITY ALIAS ───────────────────────────────────────────
# execute_tool() in jarvis_core.py dispatches query_taegis_alerts ->
# taegis.query_alerts(). Keep both names valid; get_alerts is canonical.
query_alerts = get_alerts


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: returns {"status": "ok"|"degraded"|"down",
    "detail": str}. No network probe — creds-present + breaker state — so
    polling never burns API rate limit. Extra keys retained for back-compat."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not (TAEGIS_URL and TAEGIS_API_KEY):
        status, detail = "down", "credentials missing (TAEGIS_URL/API_KEY)"
    elif taegis_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {taegis_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "taegis", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": taegis_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.taegis --function get_alerts --params '{"severity":"high"}' --mock
# python -m tools.taegis --function get_investigations --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Taegis connector test harness")
    parser.add_argument("--function", default="get_alerts",
                        choices=["get_alerts", "get_alert_detail",
                                 "get_investigations"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    fn = {"get_alerts": get_alerts,
          "get_alert_detail": get_alert_detail,
          "get_investigations": get_investigations}[args.function]
    print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
