"""
tools/mcm.py — Microsoft Configuration Manager (MCM / SCCM) Connector
=====================================================================
CofCITIP — on-prem Windows authority for the half of the Windows estate that is
domain-joined / SCCM client-managed (the cloud-native half lives in Intune; see
device_authority.py for the split rule).

SCOPE — MOCK MODE ONLY THIS SESSION.
Unlike intune.py / jamf.py (Graph and Jamf are confirmed reachable from BB
directly), MCM is reachable from BB ONLY over VPN even on the campus LAN — an
unresolved wall-port/VLAN networking question, NOT a code problem. So the live
HTTP branch is intentionally a hard NotImplementedError naming BOTH blockers
(credentials AND VPN reachability); only the mock branch is built out. This
mirrors the live-write stub pattern in package_pipeline.py.

Auth (when live, eventually): SCCM AdminService (REST over HTTPS at
  https://<smsprovider>/AdminService) with a read-only service account —
  Windows-auth/Negotiate. Endpoints (live, when built):
  /AdminService/wmi/SMS_R_System              (devices)
  /AdminService/wmi/SMS_G_System_PATCHSTATE   (update compliance) — exact class
                                              TBD against the live site
All read-only — write operations (deployments) would come later behind the
human-confirmation gate, not in this connector.

Tool functions (signature: (params: dict) -> dict):
  query_devices(params)        — list/filter MCM-managed devices
  query_patch_status(params)   — software-update compliance for a device or fleet
  query_device_detail(params)  — single device lookup by hostname/user
  health_check()               — same shape as intune.py / jamf.py health_check

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
Config (live, deferred): /etc/cofc-itip/config.env — MCM_ADMINSERVICE_URL,
  MCM_USERNAME, MCM_PASSWORD
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

log = structlog.get_logger("jarvis.tools.mcm")

# ── CONFIG ────────────────────────────────────────────────────────────────────
MCM_ADMINSERVICE_URL = os.getenv("MCM_ADMINSERVICE_URL", "").rstrip("/")  # non-secret — env
MCM_USERNAME         = os.getenv("MCM_USERNAME", "")                      # non-secret — env
MCM_PASSWORD         = get_secret("MCM_PASSWORD")                         # secret — LastPass/env
MOCK_MODE            = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES          = int(os.getenv("MCM_MAX_RETRIES", "3"))

# AdminService is an on-prem IIS endpoint; be conservative so JARVIS never
# competes with console/automation load. DECISION: 5 req/sec self-limit.
MCM_CALLS_PER_SECOND = int(os.getenv("MCM_RATE_LIMIT_RPS", "5"))

# How many days since last update scan before a device's patch state is "stale".
PATCH_STALE_DAYS = int(os.getenv("MCM_PATCH_STALE_DAYS", "14"))

mcm_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name="mcm_adminservice")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


mcm_breaker.add_listener(_BreakerLogger())

# TWO blockers, both unresolved — named explicitly so whoever revisits this knows
# there are two separate things to fix, not one.
_LIVE_BLOCKED_MSG = (
    "MCM live HTTP path not implemented. TWO separate blockers must BOTH be "
    "resolved before this can go live: "
    "(1) CREDENTIALS — the MCM AdminService read-only service account "
    "(MCM_ADMINSERVICE_URL / MCM_USERNAME / MCM_PASSWORD) is a deferred ask to "
    "Philip and is not configured. "
    "(2) NETWORK REACHABILITY — MCM is reachable from BB only over VPN even when "
    "BB is on the campus LAN; this is an unresolved wall-port/VLAN networking "
    "question, not a code issue. "
    "Until BOTH are addressed, mcm.py runs in mock mode only (JARVIS_MOCK=true)."
)


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class DevicesParams(BaseModel):
    filter: Optional[str] = "all"   # 'all' | 'inactive' | 'unhealthy' | OS prefix


class PatchParams(BaseModel):
    identifier: Optional[str] = None   # device hostname; None = fleet summary


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
@mcm_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=MCM_CALLS_PER_SECOND, period=1)
def _mcm_get(path: str, params: dict | None = None) -> dict:
    """Single rate-limited/retried/breaker-protected AdminService GET.

    DELIBERATELY UNIMPLEMENTED this session — see _LIVE_BLOCKED_MSG. The full
    decorator stack is kept in place so the live wiring is a fill-in (drop in the
    httpx call + Negotiate auth) rather than a rebuild, exactly like the
    intune/jamf HTTP layer — but it raises today rather than assuming MCM is
    reachable from BB (it isn't, without VPN)."""
    raise NotImplementedError(_LIVE_BLOCKED_MSG)


# ── MOCK DATA ─────────────────────────────────────────────────────────────────
# CofC-shaped on-prem Windows: domain-joined lab/desktop machines, classroom and
# departmental fixed PCs, a Win10 long tail heavier than the Intune cloud-native
# side. Hostnames are dept-LAB-/DESK-style to match the device_authority heuristic
# and the local source-of-authority map.
_DEPTS = ["LIBR", "ADMS", "CHEM", "PHYS", "MATH", "BUSN", "EDUC", "ITSV"]
_ROLES = ["LAB", "DESK", "CLSRM", "KIOSK"]
_PATCH_BEHIND_REASONS = [
    "Pending reboot for cumulative update",
    "Update scan older than policy window",
    "Failed to install KB (error 0x80070002)",
    "Maintenance window not yet reached",
]
_MCM_UPDATES = [
    "2025-06 Cumulative Update for Windows (KB5039212)",
    "2025-06 .NET Framework Security Update (KB5039895)",
    "Defender Antimalware Platform Update",
    "2025-05 Servicing Stack Update (KB5037018)",
]


def _mock_devices(n: int = 180) -> list[dict]:
    """Deterministic synthetic MCM device records (AdminService SMS_R_System
    shape, trimmed to what the tools surface)."""
    rng = random.Random(71)  # deterministic — demo numbers stay stable
    devices = []
    for i in range(n):
        dept = rng.choice(_DEPTS)
        role = rng.choice(_ROLES)
        win11 = rng.random() < 0.55          # on-prem skews older than Intune side
        healthy = rng.random() < 0.92        # MCM client health
        last_scan_days = rng.randint(0, 40)
        last_scan = datetime.now(timezone.utc) - timedelta(days=last_scan_days,
                                                           hours=rng.randint(0, 23))
        compliant = healthy and last_scan_days <= PATCH_STALE_DAYS and rng.random() < 0.88
        devices.append({
            "ResourceID": 16777000 + i,
            "Name": f"{dept}-{role}-{10 + i}",
            "OperatingSystem": "Microsoft Windows 11" if win11 else "Microsoft Windows 10",
            "OSBuild": "10.0.22631" if win11 else "10.0.19045",
            "Domain": "COFC",
            "UserName": f"COFC\\user{i:03d}",
            "ClientHealth": "Healthy" if healthy else "Inactive/Unhealthy",
            "Collection": f"{dept} Managed Workstations",
            "LastHardwareScan": last_scan.isoformat(),
            "PatchCompliant": compliant,
            "MissingUpdateCount": 0 if compliant else rng.randint(1, 6),
            "PatchBehindReason": None if compliant else rng.choice(_PATCH_BEHIND_REASONS),
            "SerialNumber": f"MCM{rng.randint(10**6, 10**7 - 1)}",
            "Model": rng.choice(["OptiPlex 7020", "OptiPlex 5000", "Precision 3460",
                                 "Latitude 3540"]),
        })
    return devices


def _summarize_device(d: dict) -> dict:
    return {
        "hostname": d.get("Name"),
        "user": d.get("UserName"),
        "os": f"{d.get('OperatingSystem')} ({d.get('OSBuild')})",
        "domain": d.get("Domain"),
        "client_health": d.get("ClientHealth"),
        "collection": d.get("Collection"),
        "last_hardware_scan": d.get("LastHardwareScan"),
        "patch_compliant": d.get("PatchCompliant"),
        "missing_updates": d.get("MissingUpdateCount"),
        "model": d.get("Model"),
        "serial": d.get("SerialNumber"),
    }


def _fetch_devices() -> list[dict]:
    if MOCK_MODE:
        return _mock_devices()
    # LIVE: blocked on creds + VPN — routes through the decorated HTTP layer,
    # which raises NotImplementedError(_LIVE_BLOCKED_MSG).
    return _mcm_get("/wmi/SMS_R_System").get("value", [])


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────
def query_devices(raw_params: dict) -> dict:
    """List/filter MCM-managed devices (client health, OS, collection)."""
    params = DevicesParams(**(raw_params or {}))
    log.info("tool.start", tool="query_devices", params=params.model_dump())

    devices = [_summarize_device(d) for d in _fetch_devices()]

    flt = (params.filter or "all").lower()
    if flt in ("inactive", "unhealthy"):
        view = [d for d in devices if d["client_health"] != "Healthy"]
    elif flt not in ("all", ""):
        view = [d for d in devices if (d["os"] or "").lower().find(flt) >= 0]
    else:
        view = devices

    unhealthy = [d for d in devices if d["client_health"] != "Healthy"]
    result = {
        "source": "mcm",
        "mock": MOCK_MODE,
        "total_devices": len(devices),
        "healthy": len(devices) - len(unhealthy),
        "unhealthy": len(unhealthy),
        "filtered_view_count": len(view),
        "devices": view[:25],  # spoken response — cap the list
    }
    if len(view) > 25:
        result["note"] = f"Showing 25 of {len(view)} matching MCM devices"

    log.info("tool.success", tool="query_devices",
             total=len(devices), unhealthy=len(unhealthy))
    return result


def query_patch_status(raw_params: dict) -> dict:
    """Software-update compliance for a single device or the MCM fleet."""
    params = PatchParams(**(raw_params or {}))
    log.info("tool.start", tool="query_patch_status", identifier=params.identifier)

    devices = [_summarize_device(d) for d in _fetch_devices()]

    if params.identifier:
        ident = params.identifier.lower()
        matches = [d for d in devices if ident in (d["hostname"] or "").lower()]
        if not matches:
            log.info("tool.success", tool="query_patch_status", found=0)
            return {"source": "mcm", "mock": MOCK_MODE, "found": False,
                    "message": f"No MCM device found matching '{params.identifier}'"}
        d = matches[0]
        result = {
            "source": "mcm",
            "mock": MOCK_MODE,
            "found": True,
            "device": {
                "hostname": d["hostname"],
                "patch_compliant": d["patch_compliant"],
                "missing_updates": d["missing_updates"],
                "last_hardware_scan": d["last_hardware_scan"],
            },
            "sample_updates": _MCM_UPDATES,
        }
        log.info("tool.success", tool="query_patch_status", found=len(matches),
                 compliant=d["patch_compliant"])
        return result

    # Fleet summary
    total = len(devices)
    noncompliant = [d for d in devices if not d["patch_compliant"]]
    result = {
        "source": "mcm",
        "mock": MOCK_MODE,
        "total_devices": total,
        "patch_compliant": total - len(noncompliant),
        "patch_non_compliant": len(noncompliant),
        "patch_compliance_rate_pct": round((total - len(noncompliant)) / total * 100, 1) if total else 0,
        "non_compliant_devices": [
            {"hostname": d["hostname"], "missing_updates": d["missing_updates"],
             "last_scan": d["last_hardware_scan"]}
            for d in noncompliant[:25]
        ],
    }
    if len(noncompliant) > 25:
        result["note"] = f"Showing 25 of {len(noncompliant)} non-compliant devices"

    log.info("tool.success", tool="query_patch_status",
             total=total, non_compliant=len(noncompliant))
    return result


def query_device_detail(raw_params: dict) -> dict:
    """Look up one MCM-managed device by hostname or username."""
    params = DeviceDetailParams(**(raw_params or {}))
    log.info("tool.start", tool="query_device_detail", identifier=params.identifier)

    ident = params.identifier.lower()
    devices = [_summarize_device(d) for d in _fetch_devices()]
    matches = [d for d in devices
               if ident in (d["hostname"] or "").lower()
               or ident in (d["user"] or "").lower()]

    if not matches:
        log.info("tool.success", tool="query_device_detail", found=0)
        return {"source": "mcm", "mock": MOCK_MODE, "found": False,
                "message": f"No MCM device found matching '{params.identifier}'"}

    result = {
        "source": "mcm",
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
    health_check. NOTE: even fully credentialed, MCM stays mock-only until the
    VPN-reachability gap is resolved — so non-mock reports 'down' with both
    blockers, never a false 'ok'."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not (MCM_ADMINSERVICE_URL and MCM_USERNAME and MCM_PASSWORD):
        status = "down"
        detail = ("credentials missing (MCM_ADMINSERVICE_URL/USERNAME/PASSWORD) "
                  "AND MCM not reachable from BB without VPN — mock mode only")
    else:
        # Creds present but the live path is still blocked on VPN reachability.
        status = "down"
        detail = ("live path not implemented — MCM requires VPN reachability from "
                  "BB (unresolved network question); mock mode only")
    result = {"source": "mcm", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": mcm_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.mcm --function query_devices --mock
# python -m tools.mcm --function query_patch_status --params '{"identifier":"LIBR-LAB-12"}' --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr))

    parser = argparse.ArgumentParser(description="MCM connector test harness (mock only)")
    parser.add_argument("--function", required=True,
                        choices=["query_devices", "query_patch_status",
                                 "query_device_detail", "health_check"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    fn = globals()[args.function]
    out = fn() if args.function == "health_check" else fn(_json.loads(args.params))
    print(_json.dumps(out, indent=2, default=str))
