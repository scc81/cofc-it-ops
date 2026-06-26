"""
tools/intune.py — Microsoft Intune / Entra Connector
=====================================================
CofCITIP — Microsoft Graph API connector for the Windows/cross-platform fleet.

Auth:      MSAL client-credentials flow (app registration, no user interaction)
Scope:     https://graph.microsoft.com/.default
Endpoints: /deviceManagement/managedDevices
           /deviceManagement/deviceCompliancePolicies + deviceStatuses
           /deviceAppManagement/mobileApps + deviceStatuses

Graph app permissions required (admin consent):
  DeviceManagementManagedDevices.Read.All
  DeviceManagementConfiguration.Read.All
  DeviceManagementApps.Read.All

All read-only on Thursday — no write scopes requested. Write operations come
later, behind the human-confirmation gate.

Tool functions (signature: (params: dict) -> dict):
  query_compliance(params)       — fleet compliance summary
  query_device_detail(params)    — single device lookup by hostname/UPN
  query_app_deployments(params)  — app install status summary

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
Config:    /etc/cofc-itip/config.env — INTUNE_TENANT_ID, INTUNE_CLIENT_ID,
           INTUNE_CLIENT_SECRET
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from tools.secrets import get_secret  # Phase 2: creds via LastPass CLI -> env

log = structlog.get_logger("jarvis.tools.intune")

# ── CONFIG ────────────────────────────────────────────────────────────────────
TENANT_ID     = os.getenv("INTUNE_TENANT_ID", "")       # non-secret — env only
CLIENT_ID     = os.getenv("INTUNE_CLIENT_ID", "")       # non-secret — env only
CLIENT_SECRET = get_secret("INTUNE_CLIENT_SECRET")      # secret — LastPass/env
GRAPH_BASE    = "https://graph.microsoft.com/v1.0"
MOCK_MODE     = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES   = int(os.getenv("INTUNE_MAX_RETRIES", "3"))

# Graph throttling: 10,000 req / 10 min per tenant for the service.
# DECISION: self-limit to 8 req/sec — far under the ceiling, leaves headroom
# for other Graph consumers in the tenant (Intune console, other automations).
GRAPH_CALLS_PER_SECOND = int(os.getenv("INTUNE_RATE_LIMIT_RPS", "8"))

# One breaker for all Graph calls — if Graph is down, it's down for everything.
graph_breaker = pybreaker.CircuitBreaker(
    fail_max=5, reset_timeout=60, name="intune_graph"
)


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


graph_breaker.add_listener(_BreakerLogger())


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class ComplianceParams(BaseModel):
    filter: Literal["compliant", "non-compliant", "noncompliant", "all"] = "all"
    platform: Optional[str] = None


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


class AppDeploymentParams(BaseModel):
    app_name: Optional[str] = None


# ── AUTH (MSAL client credentials, cached token) ─────────────────────────────
_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_token() -> str:
    """Acquire (or reuse) a Graph token via MSAL client-credentials flow."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    import msal  # lazy — not needed at all in mock mode

    if not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
        raise RuntimeError(
            "Intune credentials missing — set INTUNE_TENANT_ID, INTUNE_CLIENT_ID, "
            "INTUNE_CLIENT_SECRET in /etc/cofc-itip/config.env"
        )

    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        log.error("auth.failed", error=result.get("error_description", "unknown"))
        raise RuntimeError(f"Graph auth failed: {result.get('error')}")

    _token_cache["token"] = result["access_token"]
    _token_cache["expires_at"] = time.time() + result.get("expires_in", 3600)
    log.info("auth.token_acquired", expires_in=result.get("expires_in"))
    return _token_cache["token"]


# ── HTTP LAYER (rate limit -> retry -> breaker, innermost to outermost) ───────
@graph_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=GRAPH_CALLS_PER_SECOND, period=1)
def _graph_get(path: str, params: dict | None = None) -> dict:
    """Single rate-limited, retried, breaker-protected Graph GET."""
    started = time.monotonic()
    resp = httpx.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {_get_token()}"},
        params=params or {},
        timeout=30,
    )
    # Honor Graph 429s explicitly — sleep Retry-After, then raise to retry.
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "5"))
        log.warning("graph.throttled", retry_after=wait, path=path)
        time.sleep(wait)
    resp.raise_for_status()
    log.debug("graph.get", path=path,
              duration_ms=int((time.monotonic() - started) * 1000))
    return resp.json()


def _graph_get_all(path: str, params: dict | None = None) -> list[dict]:
    """Follow @odata.nextLink pagination to exhaustion."""
    items: list[dict] = []
    data = _graph_get(path, params)
    items += data.get("value", [])
    next_link = data.get("@odata.nextLink")
    while next_link:
        # nextLink is a full URL — strip base, keep query string intact.
        resp = httpx.get(next_link,
                         headers={"Authorization": f"Bearer {_get_token()}"},
                         timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items += data.get("value", [])
        next_link = data.get("@odata.nextLink")
    return items


# ── MOCK DATA ─────────────────────────────────────────────────────────────────
# Realistic CofC-shaped fleet: dept-prefixed hostnames, cougars.cofc.edu UPNs,
# mostly Win11 with a Win10 long tail, ~90% compliance.
_DEPTS = ["ITSV", "BIOL", "CHEM", "ENGL", "HIST", "MATH", "ADMS", "LIBR", "ATHL", "COMM"]
_NONCOMPLIANCE_REASONS = [
    "BitLocker not enabled", "OS version below minimum (Win10 21H2)",
    "Defender signature out of date", "Password policy not met",
    "Device not checked in > 30 days",
]


def _mock_devices(n: int = 240) -> list[dict]:
    rng = random.Random(81)  # deterministic — demo numbers stay stable
    devices = []
    for i in range(n):
        dept = rng.choice(_DEPTS)
        compliant = rng.random() < 0.90
        win11 = rng.random() < 0.78
        last_sync = datetime.now(timezone.utc) - timedelta(hours=rng.randint(1, 200))
        devices.append({
            "id": f"mock-{i:04d}",
            "deviceName": f"{dept}-{1000 + i}",
            "operatingSystem": "Windows",
            "osVersion": "10.0.22631.3447" if win11 else "10.0.19045.4291",
            "complianceState": "compliant" if compliant else "noncompliant",
            "userPrincipalName": f"user{i:03d}@cougars.cofc.edu",
            "lastSyncDateTime": last_sync.isoformat(),
            "manufacturer": rng.choice(["Dell Inc.", "Dell Inc.", "Dell Inc.", "Microsoft"]),
            "model": rng.choice(["OptiPlex 7010", "Latitude 5540", "Precision 3660",
                                 "Surface Pro 9"]),
            "serialNumber": f"CN{rng.randint(10**7, 10**8 - 1)}",
            "nonComplianceReason": None if compliant else rng.choice(_NONCOMPLIANCE_REASONS),
        })
    return devices


def _mock_apps() -> list[dict]:
    rng = random.Random(82)
    apps = ["Google Chrome", "Zoom Workplace", "Adobe Acrobat Reader",
            "Microsoft 365 Apps", "VLC Media Player", "7-Zip",
            "Respondus LockDown Browser", "SPSS Statistics 29"]
    out = []
    for name in apps:
        total = rng.randint(80, 240)
        failed = rng.randint(0, max(2, total // 20))
        pending = rng.randint(0, 10)
        out.append({
            "displayName": name,
            "installedDeviceCount": total - failed - pending,
            "failedDeviceCount": failed,
            "pendingInstallDeviceCount": pending,
            "totalTargeted": total,
        })
    return out


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────
def query_compliance(raw_params: dict) -> dict:
    """Fleet compliance summary from Intune managed devices."""
    params = ComplianceParams(**(raw_params or {}))
    log.info("tool.start", tool="query_compliance", params=params.model_dump())

    if MOCK_MODE:
        devices = _mock_devices()
    else:
        devices = _graph_get_all(
            "/deviceManagement/managedDevices",
            {"$select": "id,deviceName,operatingSystem,osVersion,complianceState,"
                        "userPrincipalName,lastSyncDateTime",
             "$top": 999},
        )

    if params.platform:
        devices = [d for d in devices
                   if d.get("operatingSystem", "").lower() == params.platform.lower()]

    total = len(devices)
    noncompliant = [d for d in devices
                    if d.get("complianceState") in ("noncompliant", "error")]
    compliant_count = sum(1 for d in devices if d.get("complianceState") == "compliant")

    result: dict = {
        "source": "intune",
        "mock": MOCK_MODE,
        "total_devices": total,
        "compliant": compliant_count,
        "non_compliant": len(noncompliant),
        "compliance_rate_pct": round(compliant_count / total * 100, 1) if total else 0,
    }

    want = params.filter.replace("-", "")
    if want == "noncompliant":
        result["non_compliant_devices"] = [
            {"hostname": d.get("deviceName"),
             "user": d.get("userPrincipalName"),
             "os_version": d.get("osVersion"),
             "reason": d.get("nonComplianceReason", "see Intune console"),
             "last_sync": d.get("lastSyncDateTime")}
            for d in noncompliant[:25]  # spoken response — cap the list
        ]
        if len(noncompliant) > 25:
            result["note"] = f"Showing 25 of {len(noncompliant)} non-compliant devices"

    log.info("tool.success", tool="query_compliance", total=total,
             non_compliant=len(noncompliant))
    return result


def query_device_detail(raw_params: dict) -> dict:
    """Look up one managed device by hostname or user principal name."""
    params = DeviceDetailParams(**(raw_params or {}))
    log.info("tool.start", tool="query_device_detail", identifier=params.identifier)

    ident = params.identifier.lower()
    if MOCK_MODE:
        devices = _mock_devices()
        matches = [d for d in devices
                   if ident in d["deviceName"].lower()
                   or ident in d["userPrincipalName"].lower()]
    else:
        # Graph supports $filter on deviceName eq — but partial match needs
        # client-side filtering. DECISION: try exact eq first (cheap), fall
        # back to contains via userPrincipalName startswith.
        matches = _graph_get_all(
            "/deviceManagement/managedDevices",
            {"$filter": f"deviceName eq '{params.identifier}'"},
        )
        if not matches:
            matches = _graph_get_all(
                "/deviceManagement/managedDevices",
                {"$filter": f"startswith(userPrincipalName,'{params.identifier}')"},
            )

    if not matches:
        log.info("tool.success", tool="query_device_detail", found=0)
        return {"source": "intune", "mock": MOCK_MODE, "found": False,
                "message": f"No Intune device found matching '{params.identifier}'"}

    d = matches[0]
    result = {
        "source": "intune",
        "mock": MOCK_MODE,
        "found": True,
        "match_count": len(matches),
        "device": {
            "hostname": d.get("deviceName"),
            "user": d.get("userPrincipalName"),
            "os": f"{d.get('operatingSystem')} {d.get('osVersion')}",
            "compliance": d.get("complianceState"),
            "non_compliance_reason": d.get("nonComplianceReason"),
            "model": f"{d.get('manufacturer', '')} {d.get('model', '')}".strip(),
            "serial": d.get("serialNumber"),
            "last_sync": d.get("lastSyncDateTime"),
        },
    }
    log.info("tool.success", tool="query_device_detail", found=len(matches))
    return result


def query_app_deployments(raw_params: dict) -> dict:
    """App deployment install status across the fleet."""
    params = AppDeploymentParams(**(raw_params or {}))
    log.info("tool.start", tool="query_app_deployments",
             app_name=params.app_name)

    if MOCK_MODE:
        apps = _mock_apps()
    else:
        # mobileApps list, then installSummary per app. Capped at 50 apps to
        # respect rate limits — refine with $filter once live patterns known.
        raw_apps = _graph_get_all(
            "/deviceAppManagement/mobileApps",
            {"$select": "id,displayName", "$top": 50},
        )
        apps = []
        for a in raw_apps:
            try:
                s = _graph_get(f"/deviceAppManagement/mobileApps/{a['id']}/installSummary")
                apps.append({
                    "displayName": a["displayName"],
                    "installedDeviceCount": s.get("installedDeviceCount", 0),
                    "failedDeviceCount": s.get("failedDeviceCount", 0),
                    "pendingInstallDeviceCount": s.get("pendingInstallDeviceCount", 0),
                    "totalTargeted": (s.get("installedDeviceCount", 0)
                                      + s.get("failedDeviceCount", 0)
                                      + s.get("pendingInstallDeviceCount", 0)
                                      + s.get("notInstalledDeviceCount", 0)),
                })
            except Exception as e:
                log.warning("app.summary_failed", app=a["displayName"], error=str(e))

    if params.app_name:
        needle = params.app_name.lower()
        apps = [a for a in apps if needle in a["displayName"].lower()]

    problem_apps = [a for a in apps if a["failedDeviceCount"] > 0]
    result = {
        "source": "intune",
        "mock": MOCK_MODE,
        "apps_checked": len(apps),
        "apps_with_failures": len(problem_apps),
        "deployments": sorted(apps, key=lambda a: -a["failedDeviceCount"])[:15],
    }
    log.info("tool.success", tool="query_app_deployments",
             apps=len(apps), with_failures=len(problem_apps))
    return result


# ── DASHBOARD API (FastAPI) ───────────────────────────────────────────────────
# Serves /api/compliance for endpoint_dashboard.html. Mock mode flows through:
# JARVIS_MOCK=true means the API returns the same deterministic mock fleet.
# Run: python -m tools.intune --serve [--port 8090]    (or --serve --mock)
# CORS is wide-open on purpose — this binds to localhost and the dashboard is
# opened from file:// or a local http.server during demos. Lock down origins
# before this ever binds to a routable interface.
def build_api():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    api = FastAPI(title="Intune Connector API", version="1.0")
    # Session 5: wildcard CORS tightened — private subnets only, matching jarvis_ui
    api.add_middleware(CORSMiddleware,
                       allow_origin_regex=r"^https?://(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|localhost|127\.0\.0\.1)(:\d+)?$",
                       allow_methods=["GET"], allow_headers=["*"])

    @api.get("/api/compliance")
    def api_compliance(filter: str = "all", platform: str | None = None):
        return query_compliance({"filter": filter, "platform": platform})

    @api.get("/health")
    def api_health():
        return {"status": "ok", "mock_mode": MOCK_MODE}

    return api


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: returns {"status": "ok"|"degraded"|"down",
    "detail": str}. No network probe — creds-present + breaker state — so
    polling never burns API rate limit. Extra keys retained for back-compat."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
        status, detail = "down", "credentials missing (INTUNE_TENANT/CLIENT/SECRET)"
    elif graph_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {graph_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "intune", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": graph_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.intune --function query_compliance --params '{"filter":"non-compliant"}'
# python -m tools.intune --function query_compliance --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    # Harness mode: logs to stderr so stdout is clean, pipeable JSON.
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Intune connector test harness")
    parser.add_argument("--function",
                        choices=["query_compliance", "query_device_detail",
                                 "query_app_deployments"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--serve", action="store_true",
                        help="Run the dashboard API server instead of a one-shot function")
    parser.add_argument("--port", default=8090, type=int)
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    if args.serve:
        import uvicorn
        uvicorn.run(build_api(), host="127.0.0.1", port=args.port)
        _sys.exit(0)

    if not args.function:
        parser.error("--function is required unless --serve is given")

    fn = globals()[args.function]
    print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
