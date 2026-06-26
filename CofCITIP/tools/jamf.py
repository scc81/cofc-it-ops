"""
tools/jamf.py — Jamf Pro Connector
====================================
CofCITIP — Jamf Pro REST API connector for the Mac fleet.

Auth:      Bearer token via POST /api/v1/auth/token (Basic auth exchange),
           cached and auto-refreshed before expiry. Use a dedicated read-only
           Jamf API role/client — never a personal admin account.
Endpoints: /api/v1/computers-inventory
           /api/v1/mobile-devices
           /api/v2/patch-software-title-configurations (+ /patch-summary)

All read-only on Thursday — write operations come later, behind the
human-confirmation gate.

Tool functions (signature: (params: dict) -> dict):
  query_fleet(params)          — Mac fleet health/compliance summary
  query_device_detail(params)  — single Mac lookup by hostname/serial/user
  query_patch_status(params)   — patch management summary

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
Config:    /etc/cofc-itip/config.env — JAMF_URL, JAMF_USERNAME, JAMF_PASSWORD
           (or JAMF_CLIENT_ID/JAMF_CLIENT_SECRET once API clients are set up)
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from tools.secrets import get_secret  # Phase 2: creds via LastPass CLI -> env

log = structlog.get_logger("jarvis.tools.jamf")

# ── CONFIG ────────────────────────────────────────────────────────────────────
JAMF_URL      = os.getenv("JAMF_URL", "").rstrip("/")   # non-secret — env only
JAMF_USERNAME = os.getenv("JAMF_USERNAME", "")          # non-secret — env only
JAMF_PASSWORD = get_secret("JAMF_PASSWORD")             # secret — LastPass/env
MOCK_MODE     = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES   = int(os.getenv("JAMF_MAX_RETRIES", "3"))

# Jamf Pro guidance: ~100 req/min. DECISION: self-limit to 80/min (1.33/sec
# rounded to 1/sec sustained) so JARVIS never competes with console users.
JAMF_CALLS_PER_MINUTE = int(os.getenv("JAMF_RATE_LIMIT_RPM", "80"))

# How many hours since last check-in before a Mac counts as "stale".
STALE_HOURS = int(os.getenv("JAMF_STALE_HOURS", "168"))  # 7 days

jamf_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name="jamf")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


jamf_breaker.add_listener(_BreakerLogger())


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class FleetParams(BaseModel):
    filter: Optional[str] = "all"   # 'all' | 'stale' | a macOS version prefix


class DeviceDetailParams(BaseModel):
    identifier: str

    @field_validator("identifier")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("identifier cannot be empty")
        if len(v) > 256:
            raise ValueError("identifier too long")
        return v


class PatchParams(BaseModel):
    title: Optional[str] = None


# ── AUTH (Bearer token with auto-refresh) ─────────────────────────────────────
_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_token() -> str:
    """Exchange Basic auth for a bearer token; cache until near expiry."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 120:
        return _token_cache["token"]

    if not (JAMF_URL and JAMF_USERNAME and JAMF_PASSWORD):
        raise RuntimeError(
            "Jamf credentials missing — set JAMF_URL, JAMF_USERNAME, "
            "JAMF_PASSWORD in /etc/cofc-itip/config.env"
        )

    resp = httpx.post(
        f"{JAMF_URL}/api/v1/auth/token",
        auth=(JAMF_USERNAME, JAMF_PASSWORD),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["token"]
    # Jamf returns ISO 'expires'; parse defensively, default to 25 min.
    try:
        exp = datetime.fromisoformat(data["expires"].replace("Z", "+00:00"))
        _token_cache["expires_at"] = exp.timestamp()
    except Exception:
        _token_cache["expires_at"] = time.time() + 1500
    log.info("auth.token_acquired")
    return _token_cache["token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}", "Accept": "application/json"}


# ── HTTP LAYER (rate limit -> retry -> breaker) ───────────────────────────────
@jamf_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=JAMF_CALLS_PER_MINUTE, period=60)
def _jamf_get(path: str, params: dict | None = None) -> dict:
    started = time.monotonic()
    resp = httpx.get(f"{JAMF_URL}{path}", headers=_headers(),
                     params=params or {}, timeout=30)
    if resp.status_code == 401:
        # Token may have been revoked server-side — force refresh and retry once.
        _token_cache["token"] = None
        resp = httpx.get(f"{JAMF_URL}{path}", headers=_headers(),
                         params=params or {}, timeout=30)
    resp.raise_for_status()
    log.debug("jamf.get", path=path,
              duration_ms=int((time.monotonic() - started) * 1000))
    return resp.json()


def _jamf_get_paged(path: str, extra_params: dict | None = None,
                    page_size: int = 100) -> list[dict]:
    """Jamf page/page-size pagination to exhaustion."""
    results: list[dict] = []
    page = 0
    while True:
        params = {"page": page, "page-size": page_size, **(extra_params or {})}
        data = _jamf_get(path, params)
        batch = data.get("results", [])
        if not batch:
            break
        results += batch
        page += 1
        # Stop early if totalCount tells us we have everything.
        if data.get("totalCount") is not None and len(results) >= data["totalCount"]:
            break
    return results


# ── MOCK DATA ─────────────────────────────────────────────────────────────────
# Realistic CofC Mac fleet: heavy Sonoma/Sequoia mix, a Ventura long tail,
# dept-tagged hostnames, a few stale lab machines over summer.
_DEPTS = ["ARTS", "MUSC", "COMM", "BIOL", "LIBR", "ITSV", "SOTA", "EDUC"]
_MACOS = [
    ("15.5", "Sequoia", 0.35),
    ("14.7.6", "Sonoma", 0.40),
    ("13.7.6", "Ventura", 0.18),
    ("12.7.6", "Monterey", 0.07),  # the stragglers Greg keeps mentioning
]
_PATCH_TITLES = ["Google Chrome", "Zoom", "Microsoft Office", "Firefox",
                 "Adobe Creative Cloud", "Slack"]


def _mock_macs(n: int = 85) -> list[dict]:
    rng = random.Random(53)  # deterministic demo data
    macs = []
    for i in range(n):
        roll = rng.random()
        cum = 0.0
        version, name = _MACOS[-1][0], _MACOS[-1][1]
        for v, nm, w in _MACOS:
            cum += w
            if roll < cum:
                version, name = v, nm
                break
        dept = rng.choice(_DEPTS)
        # ~12% stale (summer lab machines powered off)
        hours_ago = rng.randint(2000, 4000) if rng.random() < 0.12 \
            else rng.randint(1, 120)
        last = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        macs.append({
            "id": str(i),
            "general": {
                "name": f"{dept}-MAC-{200 + i}",
                "lastContactTime": last.isoformat(),
                "enrollmentDate": "2024-08-15T12:00:00Z",
            },
            "hardware": {
                "serialNumber": f"C02{rng.randint(10**6, 10**7 - 1)}X",
                "model": rng.choice(["MacBook Air (M2)", "MacBook Pro 14 (M3)",
                                     "iMac 24 (M3)", "Mac mini (M2)"]),
            },
            "userAndLocation": {
                "username": f"user{i:03d}",
                "department": dept,
            },
            "operatingSystem": {"version": version, "name": f"macOS {name}"},
        })
    return macs


def _mock_patch() -> list[dict]:
    rng = random.Random(54)
    out = []
    for title in _PATCH_TITLES:
        total = rng.randint(40, 85)
        latest = rng.randint(int(total * 0.6), total)
        out.append({
            "title": title,
            "latest_version_installed": latest,
            "total_installed": total,
            "up_to_date_pct": round(latest / total * 100, 1),
        })
    return out


# ── HELPERS ───────────────────────────────────────────────────────────────────
def _is_stale(last_contact_iso: str) -> bool:
    try:
        last = datetime.fromisoformat(last_contact_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last) > timedelta(hours=STALE_HOURS)
    except Exception:
        return True  # unparseable check-in time = treat as stale


def _summarize_mac(d: dict) -> dict:
    g, hw = d.get("general", {}), d.get("hardware", {})
    ul, osys = d.get("userAndLocation", {}), d.get("operatingSystem", {})
    return {
        "hostname": g.get("name"),
        "user": ul.get("username"),
        "department": ul.get("department"),
        "os": f"{osys.get('name', 'macOS')} {osys.get('version', '')}".strip(),
        "os_version": osys.get("version"),
        "model": hw.get("model"),
        "serial": hw.get("serialNumber"),
        "last_checkin": g.get("lastContactTime"),
        "stale": _is_stale(g.get("lastContactTime", "")),
    }


_INVENTORY_SECTIONS = "GENERAL,HARDWARE,USER_AND_LOCATION,OPERATING_SYSTEM"


def _fetch_macs() -> list[dict]:
    if MOCK_MODE:
        return _mock_macs()
    return _jamf_get_paged("/api/v1/computers-inventory",
                           {"section": _INVENTORY_SECTIONS})


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────
def query_fleet(raw_params: dict) -> dict:
    """Mac fleet health summary: counts by macOS version + stale devices."""
    params = FleetParams(**(raw_params or {}))
    log.info("tool.start", tool="query_fleet", params=params.model_dump())

    macs = [_summarize_mac(d) for d in _fetch_macs()]

    flt = (params.filter or "all").lower()
    if flt == "stale":
        macs_view = [m for m in macs if m["stale"]]
    elif flt not in ("all", ""):
        macs_view = [m for m in macs if (m["os_version"] or "").startswith(flt)]
    else:
        macs_view = macs

    by_version: dict[str, int] = {}
    for m in macs:
        key = m["os"] or "unknown"
        by_version[key] = by_version.get(key, 0) + 1

    stale = [m for m in macs if m["stale"]]
    result = {
        "source": "jamf",
        "mock": MOCK_MODE,
        "total_macs": len(macs),
        "by_os_version": dict(sorted(by_version.items(), key=lambda kv: -kv[1])),
        "stale_count": len(stale),
        "stale_threshold_days": STALE_HOURS // 24,
        "filtered_view_count": len(macs_view),
        "devices": macs_view[:25],  # spoken response — cap the list
    }
    if len(macs_view) > 25:
        result["note"] = f"Showing 25 of {len(macs_view)} matching Macs"

    log.info("tool.success", tool="query_fleet",
             total=len(macs), stale=len(stale))
    return result


def query_device_detail(raw_params: dict) -> dict:
    """Look up one Mac by hostname, serial number, or username."""
    params = DeviceDetailParams(**(raw_params or {}))
    log.info("tool.start", tool="query_device_detail", identifier=params.identifier)

    ident = params.identifier.lower()

    if MOCK_MODE:
        macs = [_summarize_mac(d) for d in _mock_macs()]
    else:
        # Jamf supports RSQL filtering on computers-inventory — try server-side
        # first (cheap), fall back to client-side contains.
        try:
            hits = _jamf_get_paged(
                "/api/v1/computers-inventory",
                {"section": _INVENTORY_SECTIONS,
                 "filter": f'general.name=="{params.identifier}"'},
            )
            if hits:
                macs = [_summarize_mac(d) for d in hits]
            else:
                macs = [_summarize_mac(d) for d in _fetch_macs()]
        except Exception:
            macs = [_summarize_mac(d) for d in _fetch_macs()]

    matches = [m for m in macs if
               ident in (m["hostname"] or "").lower()
               or ident in (m["serial"] or "").lower()
               or ident in (m["user"] or "").lower()]

    if not matches:
        log.info("tool.success", tool="query_device_detail", found=0)
        return {"source": "jamf", "mock": MOCK_MODE, "found": False,
                "message": f"No Mac found matching '{params.identifier}'"}

    result = {
        "source": "jamf",
        "mock": MOCK_MODE,
        "found": True,
        "match_count": len(matches),
        "device": matches[0],
    }
    log.info("tool.success", tool="query_device_detail", found=len(matches))
    return result


def query_patch_status(raw_params: dict) -> dict:
    """Patch management status from Jamf patch software titles."""
    params = PatchParams(**(raw_params or {}))
    log.info("tool.start", tool="query_patch_status", title=params.title)

    if MOCK_MODE:
        titles = _mock_patch()
    else:
        configs = _jamf_get_paged("/api/v2/patch-software-title-configurations",
                                  page_size=100)
        titles = []
        for c in configs:
            try:
                s = _jamf_get(
                    f"/api/v2/patch-software-title-configurations/{c['id']}/patch-summary"
                )
                total = s.get("hostCount", 0) or 0
                latest = s.get("upToDate", 0) or 0
                titles.append({
                    "title": c.get("displayName", s.get("title", "unknown")),
                    "latest_version_installed": latest,
                    "total_installed": total,
                    "up_to_date_pct": round(latest / total * 100, 1) if total else 0,
                })
            except Exception as e:
                log.warning("patch.summary_failed", title=c.get("displayName"),
                            error=str(e))

    if params.title:
        needle = params.title.lower()
        titles = [t for t in titles if needle in t["title"].lower()]

    behind = [t for t in titles if t["up_to_date_pct"] < 90]
    result = {
        "source": "jamf",
        "mock": MOCK_MODE,
        "titles_checked": len(titles),
        "titles_below_90pct": len(behind),
        "patch_status": sorted(titles, key=lambda t: t["up_to_date_pct"]),
    }
    log.info("tool.success", tool="query_patch_status",
             titles=len(titles), behind=len(behind))
    return result


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: returns {"status": "ok"|"degraded"|"down",
    "detail": str}. No network probe — creds-present + breaker state — so
    polling never burns API rate limit. Extra keys retained for back-compat."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not (JAMF_URL and JAMF_USERNAME and JAMF_PASSWORD):
        status, detail = "down", "credentials missing (JAMF_URL/USERNAME/PASSWORD)"
    elif jamf_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {jamf_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "jamf", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": jamf_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.jamf --function query_fleet --mock
# python -m tools.jamf --function query_device_detail --params '{"identifier":"ARTS-MAC-205"}' --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    # Harness mode: logs to stderr so stdout is clean, pipeable JSON.
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Jamf connector test harness")
    parser.add_argument("--function", required=True,
                        choices=["query_fleet", "query_device_detail",
                                 "query_patch_status"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    fn = globals()[args.function]
    print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
