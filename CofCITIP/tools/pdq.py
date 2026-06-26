"""
tools/pdq.py — PDQ Deploy / PDQ Inventory Connector
===================================================
CofCITIP — the gap-filler. PDQ covers Windows devices the three primary systems
(Intune, MCM, Jamf) don't reach: unmanaged lab spares, kiosks, digital signage,
departmental fixed PCs that never got an MDM enrollment. See device_authority.py
— PDQ is a FALLBACK tier, authoritative only for devices no primary claims.

SCOPE — MOCK MODE ONLY THIS SESSION.
Like MCM, PDQ is reachable from BB only over VPN even on the campus LAN (the same
unresolved wall-port/VLAN networking question). So the live HTTP branch is a hard
NotImplementedError naming BOTH blockers (credentials AND VPN reachability); only
the mock branch is built. Mirrors package_pipeline.py's live-write stub pattern.

Auth (when live, eventually): PDQ Connect / on-prem PDQ API token over HTTPS at
  https://<pdq-server>/api. Endpoints (live, when built):
  /api/devices                  (PDQ Inventory devices)
  /api/deployments              (PDQ Deploy package deployment results)
All read-only — triggering deployments would come later behind the
human-confirmation gate, not in this connector.

Tool functions (signature: (params: dict) -> dict):
  query_devices(params)            — list/filter PDQ-inventoried devices
  query_deployment_status(params)  — PDQ Deploy package deployment status
  query_device_detail(params)      — single device lookup by hostname
  health_check()                   — same shape as intune.py / jamf.py

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
Config (live, deferred): /etc/cofc-itip/config.env — PDQ_SERVER_URL, PDQ_API_KEY
"""

from __future__ import annotations

import os
import random
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

log = structlog.get_logger("jarvis.tools.pdq")

# ── CONFIG ────────────────────────────────────────────────────────────────────
PDQ_SERVER_URL = os.getenv("PDQ_SERVER_URL", "").rstrip("/")  # non-secret — env
PDQ_API_KEY    = get_secret("PDQ_API_KEY")                    # secret — LastPass/env
MOCK_MODE      = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES    = int(os.getenv("PDQ_MAX_RETRIES", "3"))

# PDQ server is a modest on-prem box; keep load light. DECISION: 5 req/sec.
PDQ_CALLS_PER_SECOND = int(os.getenv("PDQ_RATE_LIMIT_RPS", "5"))

# Days since last PDQ Inventory scan before a device counts as "stale".
STALE_DAYS = int(os.getenv("PDQ_STALE_DAYS", "30"))

pdq_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name="pdq")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


pdq_breaker.add_listener(_BreakerLogger())

# TWO blockers, both unresolved — named explicitly (creds AND VPN), so a later
# reader knows there are two separate things to resolve, not one.
_LIVE_BLOCKED_MSG = (
    "PDQ live HTTP path not implemented. TWO separate blockers must BOTH be "
    "resolved before this can go live: "
    "(1) CREDENTIALS — the PDQ API token (PDQ_SERVER_URL / PDQ_API_KEY) is a "
    "deferred ask to Philip and is not configured. "
    "(2) NETWORK REACHABILITY — PDQ is reachable from BB only over VPN even when "
    "BB is on the campus LAN; this is an unresolved wall-port/VLAN networking "
    "question, not a code issue. "
    "Until BOTH are addressed, pdq.py runs in mock mode only (JARVIS_MOCK=true)."
)


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class DevicesParams(BaseModel):
    filter: Optional[str] = "all"   # 'all' | 'stale' | a device-type prefix


class DeploymentParams(BaseModel):
    package: Optional[str] = None   # package name filter; None = all packages


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


# ── HTTP LAYER (rate limit -> retry -> breaker) — LIVE STUB ───────────────────
@pdq_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=PDQ_CALLS_PER_SECOND, period=1)
def _pdq_get(path: str, params: dict | None = None) -> dict:
    """Single rate-limited/retried/breaker-protected PDQ API GET.

    DELIBERATELY UNIMPLEMENTED this session — see _LIVE_BLOCKED_MSG. The decorator
    stack is kept so the live wiring is a fill-in (httpx call + token header)
    rather than a rebuild, like the intune/jamf HTTP layer — but it raises today
    rather than assuming PDQ is reachable from BB (it isn't, without VPN)."""
    raise NotImplementedError(_LIVE_BLOCKED_MSG)


# ── MOCK DATA ─────────────────────────────────────────────────────────────────
# CofC-shaped gap devices: kiosks, signage players, lab spares, shared fixed PCs —
# the things that slip past Intune/MCM/Jamf. Hostnames intentionally NOT the
# dept-#### / dept-LAB- shapes the primaries own, so device_authority routes them
# here as the gap-filler.
_DEVICE_TYPES = [
    ("KIOSK", "Self-service kiosk"),
    ("SIGN", "Digital signage player"),
    ("LABSPARE", "Unenrolled lab spare"),
    ("SHARED", "Shared departmental PC"),
    ("CONFRM", "Conference room PC"),
]
_LOCATIONS = ["ATRIUM", "LIBR", "ADDLE", "RSS", "MAYBANK", "STERN", "TATE"]
_PACKAGES = [
    "Google Chrome (Enterprise)", "Mozilla Firefox ESR", "7-Zip",
    "Adobe Acrobat Reader DC", "VLC media player", "Notepad++",
    "Microsoft Edge (channel: stable)",
]
_DEPLOY_STATES = ["Success", "Success", "Success", "Failed", "Running", "Queued"]


def _mock_devices(n: int = 60) -> list[dict]:
    """Deterministic synthetic PDQ Inventory device records."""
    rng = random.Random(63)  # deterministic — demo numbers stay stable
    devices = []
    for i in range(n):
        dtype, dtype_label = rng.choice(_DEVICE_TYPES)
        loc = rng.choice(_LOCATIONS)
        win11 = rng.random() < 0.40   # gap devices skew oldest of all
        scan_days = rng.randint(45, 120) if rng.random() < 0.18 else rng.randint(0, 25)
        last_scan = datetime.now(timezone.utc) - timedelta(days=scan_days)
        devices.append({
            "DeviceId": f"pdq-{i:04d}",
            "Name": f"{dtype}-{loc}-{1 + i}",
            "DeviceType": dtype_label,
            "OperatingSystem": "Windows 11 Pro" if win11 else "Windows 10 Pro",
            "OSVersion": "23H2" if win11 else "22H2",
            "LastScannedAt": last_scan.isoformat(),
            "Online": rng.random() < 0.7,
            "Location": loc,
            "SerialNumber": f"PDQ{rng.randint(10**6, 10**7 - 1)}",
            "Model": rng.choice(["OptiPlex 3000", "OptiPlex 3080", "NUC 11",
                                 "ThinkCentre M70q"]),
        })
    return devices


def _mock_deployments() -> list[dict]:
    """Deterministic synthetic PDQ Deploy package deployment summaries."""
    rng = random.Random(64)
    out = []
    for pkg in _PACKAGES:
        targeted = rng.randint(15, 60)
        failed = rng.randint(0, max(1, targeted // 12))
        running = rng.randint(0, 3)
        queued = rng.randint(0, 4)
        succeeded = targeted - failed - running - queued
        last_run = datetime.now(timezone.utc) - timedelta(hours=rng.randint(1, 240))
        out.append({
            "package": pkg,
            "targeted": targeted,
            "succeeded": max(0, succeeded),
            "failed": failed,
            "running": running,
            "queued": queued,
            "last_run": last_run.isoformat(),
            "last_state": rng.choice(_DEPLOY_STATES),
        })
    return out


def _is_stale(last_scan_iso: str) -> bool:
    try:
        last = datetime.fromisoformat(last_scan_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last) > timedelta(days=STALE_DAYS)
    except Exception:
        return True  # unparseable scan time = treat as stale


def _summarize_device(d: dict) -> dict:
    return {
        "hostname": d.get("Name"),
        "device_type": d.get("DeviceType"),
        "os": f"{d.get('OperatingSystem')} {d.get('OSVersion')}".strip(),
        "online": d.get("Online"),
        "location": d.get("Location"),
        "last_scanned": d.get("LastScannedAt"),
        "stale": _is_stale(d.get("LastScannedAt", "")),
        "model": d.get("Model"),
        "serial": d.get("SerialNumber"),
    }


def _fetch_devices() -> list[dict]:
    if MOCK_MODE:
        return _mock_devices()
    # LIVE: blocked on creds + VPN — routes through the decorated HTTP layer,
    # which raises NotImplementedError(_LIVE_BLOCKED_MSG).
    return _pdq_get("/api/devices").get("devices", [])


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────
def query_devices(raw_params: dict) -> dict:
    """List/filter PDQ-inventoried gap devices."""
    params = DevicesParams(**(raw_params or {}))
    log.info("tool.start", tool="query_devices", params=params.model_dump())

    devices = [_summarize_device(d) for d in _fetch_devices()]

    flt = (params.filter or "all").lower()
    if flt == "stale":
        view = [d for d in devices if d["stale"]]
    elif flt not in ("all", ""):
        view = [d for d in devices
                if flt in (d["device_type"] or "").lower()
                or flt in (d["hostname"] or "").lower()]
    else:
        view = devices

    stale = [d for d in devices if d["stale"]]
    result = {
        "source": "pdq",
        "mock": MOCK_MODE,
        "total_devices": len(devices),
        "online": sum(1 for d in devices if d["online"]),
        "stale_count": len(stale),
        "stale_threshold_days": STALE_DAYS,
        "filtered_view_count": len(view),
        "devices": view[:25],  # spoken response — cap the list
    }
    if len(view) > 25:
        result["note"] = f"Showing 25 of {len(view)} matching PDQ devices"

    log.info("tool.success", tool="query_devices",
             total=len(devices), stale=len(stale))
    return result


def query_deployment_status(raw_params: dict) -> dict:
    """PDQ Deploy package deployment status across the gap fleet."""
    params = DeploymentParams(**(raw_params or {}))
    log.info("tool.start", tool="query_deployment_status", package=params.package)

    if MOCK_MODE:
        deployments = _mock_deployments()
    else:
        # LIVE: blocked on creds + VPN — raises NotImplementedError.
        deployments = _pdq_get("/api/deployments").get("deployments", [])

    if params.package:
        needle = params.package.lower()
        deployments = [d for d in deployments if needle in d["package"].lower()]

    with_failures = [d for d in deployments if d["failed"] > 0]
    result = {
        "source": "pdq",
        "mock": MOCK_MODE,
        "packages_checked": len(deployments),
        "packages_with_failures": len(with_failures),
        "deployments": sorted(deployments, key=lambda d: -d["failed"])[:15],
    }
    log.info("tool.success", tool="query_deployment_status",
             packages=len(deployments), with_failures=len(with_failures))
    return result


def query_device_detail(raw_params: dict) -> dict:
    """Look up one PDQ-inventoried device by hostname."""
    params = DeviceDetailParams(**(raw_params or {}))
    log.info("tool.start", tool="query_device_detail", identifier=params.identifier)

    ident = params.identifier.lower()
    devices = [_summarize_device(d) for d in _fetch_devices()]
    matches = [d for d in devices
               if ident in (d["hostname"] or "").lower()
               or ident in (d["serial"] or "").lower()]

    if not matches:
        log.info("tool.success", tool="query_device_detail", found=0)
        return {"source": "pdq", "mock": MOCK_MODE, "found": False,
                "message": f"No PDQ device found matching '{params.identifier}'"}

    result = {
        "source": "pdq",
        "mock": MOCK_MODE,
        "found": True,
        "match_count": len(matches),
        "device": matches[0],
    }
    log.info("tool.success", tool="query_device_detail", found=len(matches))
    return result


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: returns {"status": "ok"|"degraded"|"down",
    "detail": str}. No network probe. Same return shape as intune/jamf
    health_check. Even fully credentialed, PDQ stays mock-only until the VPN
    reachability gap is resolved — non-mock reports 'down' with both blockers."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not (PDQ_SERVER_URL and PDQ_API_KEY):
        status = "down"
        detail = ("credentials missing (PDQ_SERVER_URL/PDQ_API_KEY) AND PDQ not "
                  "reachable from BB without VPN — mock mode only")
    else:
        status = "down"
        detail = ("live path not implemented — PDQ requires VPN reachability from "
                  "BB (unresolved network question); mock mode only")
    result = {"source": "pdq", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": pdq_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.pdq --function query_devices --mock
# python -m tools.pdq --function query_deployment_status --params '{"package":"Chrome"}' --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr))

    parser = argparse.ArgumentParser(description="PDQ connector test harness (mock only)")
    parser.add_argument("--function", required=True,
                        choices=["query_devices", "query_deployment_status",
                                 "query_device_detail", "health_check"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    fn = globals()[args.function]
    out = fn() if args.function == "health_check" else fn(_json.loads(args.params))
    print(_json.dumps(out, indent=2, default=str))
