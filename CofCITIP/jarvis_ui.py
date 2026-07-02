"""
ui/jarvis_ui.py — JARVIS Mobile UI Backend
===========================================
CofCITIP — LAN-facing FastAPI app: serves the PWA shell and a mobile-
friendly API for iPhone/iPad/Android on the campus network.

PORT PLAN (decision): this app owns 0.0.0.0:8080 — the URL Philip gets is
http://<BB-IP>:8080. jarvis_core moves to 127.0.0.1:8081 (loopback-only;
it was loopback-only on 8080 before, so nothing external breaks — update
the jarvis-core systemd unit, the Prometheus scrape target, and
voice_listener's JARVIS_CORE_URL; runbook §"Mobile UI" has the exact
steps). This app is the ONLY LAN-exposed surface, and every data route on
it requires the X-JARVIS-KEY header.

SECURITY LAYERS:
  1. Private-network gate: requests from non-RFC1918/loopback client IPs
     are rejected outright (defense in depth on top of campus firewall).
  2. X-JARVIS-KEY header on /query and /briefing (JARVIS_UI_KEY in
     config.env). /health is open for Uptime Kuma. / and static assets are
     open (the shell must load before the user can enter the key — the
     shell contains no data). /approve authenticates via its one-time
     per-run token instead of the header (Teams buttons can't send headers).
  3. CORS locked to private-subnet origins.

Routes:
  GET  /            PWA shell (ui/index.html)
  GET  /manifest.json, /sw.js   PWA support files (served inline so the
                    frontend stays a single HTML file; service workers
                    cannot load from data: URIs — they need same-origin URLs)
  POST /query       {"text": str} → forwards to jarvis_core /query
  GET  /briefing    latest briefing dict (tools/briefing.generate)
  GET  /compliance  merged fleet compliance (tools/device_merge)
  GET  /lifecycle   merged fleet lifecycle/readiness (tools/device_merge)
  GET  /coverage-gaps  merged fleet for inventory-presence gap view
  GET  /threat-intel   Taegis alerts + investigations (tools/taegis direct)
  GET  /gate/pending   pipeline runs awaiting a Stage-4 decision
  POST /gate/decision  record a Stage-4 decision from the web Gate page
  GET  /health      status + uptime + model (no key required)
  GET  /approve     package-pipeline Stage 4 decision capture (token-auth)

Stage 4 gate — two front doors, one contract: /approve (Teams card link,
one-time URL token because Teams buttons can't send headers) and POST
/gate/decision (web Gate page, X-JARVIS-KEY header, approval secret read
server-side and never sent to the browser). Both write the decision file
through the same helper (_record_gate_decision), so package_pipeline's
Stage-4 poller sees an identical artifact from either door.

Mock mode: JARVIS_MOCK=true — /query returns canned text, /briefing
returns the mock briefing (connectors are mock-aware themselves).

Run:  python3 ui/jarvis_ui.py            (binds 0.0.0.0:8080)
      python3 ui/jarvis_ui.py --port N   (override)
systemd unit: jarvis-ui.service (see runbook §Mobile UI).
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response)
from pydantic import BaseModel, field_validator

load_dotenv(os.getenv("JARVIS_CONFIG", "/etc/cofc-itip/config.env"))
log = structlog.get_logger("jarvis.ui")

# ── CONFIG ────────────────────────────────────────────────────────────────────
MOCK_MODE       = os.getenv("JARVIS_MOCK", "false").lower() == "true"
JARVIS_UI_KEY   = os.getenv("JARVIS_UI_KEY", "")
# Core moved to 8081 so this app can own 8080 (see PORT PLAN above).
JARVIS_CORE_URL = os.getenv("JARVIS_CORE_URL", "http://127.0.0.1:8081")
PRIMARY_MODEL   = os.getenv("PRIMARY_MODEL", "llama3:8b")
UI_DIR          = Path(__file__).resolve().parent
# NOTE: the pipeline data dir is no longer resolved here — decision files go
# through package_pipeline._approval_path() (see _record_gate_decision), which
# reads PIPELINE_DATA_DIR itself and adds the home-dir fallback.

_STARTED = time.monotonic()

# Private ranges allowed to talk to this app (campus LAN + loopback).
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                 ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12",
                  "127.0.0.0/8")]


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETS)


class QueryIn(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text cannot be empty")
        if len(v) > 4000:
            raise ValueError("text too long")
        return v


class GateDecisionIn(BaseModel):
    run_id: str
    decision: str

    @field_validator("run_id")
    @classmethod
    def run_id_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("run_id cannot be empty")
        return v

    @field_validator("decision")
    @classmethod
    def decision_ok(cls, v: str) -> str:
        v = v.strip()
        if v not in ("approved", "rejected", "changes_requested"):
            raise ValueError("decision must be approved, rejected, or "
                             "changes_requested")
        return v


def _record_gate_decision(run_id: str, decision: str, approver: str,
                          secret: str) -> None:
    """The ONE writer of Stage-4 decision files — both front doors (/approve
    Teams link and POST /gate/decision) go through here so the file shape
    can never diverge. The path comes from package_pipeline's own
    _approval_path helper (deliberate underscore-private reuse, same policy
    as device_merge's connector internals) so writer and Stage-4 poller
    agree on location by construction — including the module's home-dir
    fallback when /var/lib isn't writable, which the old inline write here
    didn't have. Write is atomic (temp file + os.replace) so the poller's
    json.loads can never see a torn file if the two doors race — last write
    wins whole. Raises on failure; callers turn that into their own error
    responses."""
    sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
    from tools import package_pipeline
    ap = package_pipeline._approval_path(run_id)  # _data_dir() mkdirs for us
    tmp = ap.with_name(f"{ap.name}.tmp-{os.urandom(4).hex()}")
    tmp.write_text(json.dumps({
        "pipeline_run_id": run_id,
        "decision": decision,
        "approver": approver,
        "secret": secret,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    os.replace(tmp, ap)


def _uptime() -> str:
    s = int(time.monotonic() - _STARTED)
    return f"{s // 3600}h {(s % 3600) // 60}m {s % 60}s"


# ── APP ───────────────────────────────────────────────────────────────────────
def build_app() -> FastAPI:
    app = FastAPI(title="JARVIS Mobile UI", version="1.0",
                  docs_url=None, redoc_url=None, openapi_url=None)

    # CORS: same-origin use needs nothing, but lock cross-origin to private
    # subnets anyway (e.g. dev from a laptop hitting BB's IP).
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(192\.168\.\d{1,3}\.\d{1,3}|"
                           r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
                           r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
                           r"localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["GET", "POST"],
        allow_headers=["X-JARVIS-KEY", "Content-Type"],
    )

    # Routes that don't need the API key header:
    #   shell + PWA files (no data), /health (Uptime Kuma), /approve (token).
    _OPEN_PATHS = {"/", "/index.html", "/manifest.json", "/sw.js",
                   "/health", "/approve", "/favicon.ico"}

    @app.middleware("http")
    async def gate(request: Request, call_next):
        started = time.monotonic()
        client_ip = request.client.host if request.client else "unknown"

        # Layer 1 — private network only.
        if not _is_private(client_ip):
            log.warning("request.blocked_nonprivate", ip=client_ip,
                        path=request.url.path)
            return JSONResponse(status_code=403,
                                content={"error": "forbidden"})

        # Layer 2 — API key on data routes.
        if request.url.path not in _OPEN_PATHS:
            if not JARVIS_UI_KEY:
                log.error("config.missing_ui_key")
                return JSONResponse(status_code=503,
                                    content={"error": "JARVIS_UI_KEY not "
                                             "configured on server"})
            if request.headers.get("X-JARVIS-KEY") != JARVIS_UI_KEY:
                log.warning("request.unauthorized", ip=client_ip,
                            path=request.url.path)
                return JSONResponse(status_code=401,
                                    content={"error": "unauthorized"})

        response = await call_next(request)
        log.info("request", method=request.method, path=request.url.path,
                 ip=client_ip, status=response.status_code,
                 duration_ms=int((time.monotonic() - started) * 1000))
        return response

    # ── SHELL + PWA SUPPORT ──────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def shell():
        index = UI_DIR / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>JARVIS UI shell missing</h1>"
                                "<p>ui/index.html not found.</p>",
                                status_code=500)
        return FileResponse(index)

    @app.get("/manifest.json")
    def manifest():
        return JSONResponse({
            "name": "JARVIS — CofC IT Ops",
            "short_name": "JARVIS",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0a0f0d",
            "theme_color": "#0a0f0d",
            "icons": [{
                # 1x1 dark-teal PNG placeholder — replace with a real icon
                # later; iOS uses apple-touch-icon from index.html anyway.
                "src": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkqPlfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
                "sizes": "192x192", "type": "image/png",
            }],
        })

    @app.get("/sw.js")
    def service_worker():
        # Stub SW: makes the app installable; network-first, no caching of
        # data routes (briefing must stay fresh; key lives in sessionStorage
        # and never touches the SW).
        js = ("self.addEventListener('install',e=>self.skipWaiting());"
              "self.addEventListener('activate',e=>self.clients.claim());"
              "self.addEventListener('fetch',e=>{});")
        return Response(content=js, media_type="application/javascript")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    # ── DATA ROUTES ──────────────────────────────────────────────────────────
    @app.post("/query")
    def query(q: QueryIn):
        ts = datetime.now(timezone.utc).isoformat()
        # Mock-ness lives at the connector layer (tools/*.py), never in UI
        # routing — /query always forwards to jarvis-core, like /briefing
        # and /health do.
        try:
            r = httpx.post(f"{JARVIS_CORE_URL}/query",
                           json={"query": q.text}, timeout=120)
            r.raise_for_status()
            return {"response": r.json().get("response", ""), "timestamp": ts}
        except httpx.HTTPError as e:
            log.error("core.unreachable", error=str(e))
            return JSONResponse(status_code=502, content={
                "response": "JARVIS core is unreachable — check the "
                            "jarvis-core service on BB.", "timestamp": ts})

    @app.get("/briefing")
    def briefing():
        try:
            sys.path.insert(0, str(UI_DIR.parent))  # so `tools` resolves
            from tools import briefing as briefing_tool
            return briefing_tool.generate({})
        except Exception as e:
            log.error("briefing.failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "briefing unavailable",
                                         "detail": str(e)[:200]})

    @app.get("/compliance")
    def compliance():
        """Merged fleet compliance for the Compliance page (Session 3).
        Returns tools.device_merge.merge_devices() verbatim — the frontend
        derives its stat cards from the same devices array it renders, so
        the numbers can never disagree with the table. Key-protected (not
        in _OPEN_PATHS)."""
        try:
            sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
            from tools import device_merge
            return device_merge.merge_devices({})
        except Exception as e:
            log.error("compliance.failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "compliance unavailable",
                                         "detail": str(e)[:200]})

    @app.get("/lifecycle")
    def lifecycle():
        """Merged fleet lifecycle/upgrade-readiness for the Lifecycle page
        (Session 4). Same contract as /compliance: merge_devices() verbatim,
        frontend derives its stat cards from the devices it renders.
        Key-protected (not in _OPEN_PATHS)."""
        try:
            sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
            from tools import device_merge
            return device_merge.merge_devices({})
        except Exception as e:
            log.error("lifecycle.failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "lifecycle unavailable",
                                         "detail": str(e)[:200]})

    @app.get("/coverage-gaps")
    def coverage_gaps():
        """Merged fleet for the Coverage Gaps page (Session 5). Same contract
        as /compliance and /lifecycle: merge_devices() verbatim — the page is
        a different VIEW over the same merge data (gap derivation happens
        frontend-side from sources_present / staleness fields, like the other
        pages derive their stat cards). Key-protected (not in _OPEN_PATHS)."""
        try:
            sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
            from tools import device_merge
            return device_merge.merge_devices({})
        except Exception as e:
            log.error("coverage_gaps.failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "coverage gaps unavailable",
                                         "detail": str(e)[:200]})

    @app.get("/threat-intel")
    def threat_intel(severity: str = "all", hours: int = 24):
        """Taegis alerts + investigations for the Threat Intel page (Session
        5). Reads tools/taegis.py DIRECTLY — alerts are not part of the
        device merge (Taegis has no inventory endpoint; see device_merge
        notes). Params pass through taegis' own Pydantic validation
        (AlertsParams clamps hours, unknown severities fall back to "all").
        Key-protected (not in _OPEN_PATHS)."""
        try:
            sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
            from tools import taegis
            alerts = taegis.get_alerts({"severity": severity, "hours": hours})
            investigations = taegis.get_investigations({"status": "open"})
            return {
                "source": "taegis",
                "mock": alerts.get("mock", False),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "alerts": alerts,
                "investigations": investigations,
            }
        except Exception as e:
            log.error("threat_intel.failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "threat intel unavailable",
                                         "detail": str(e)[:200]})

    @app.get("/gate/pending")
    def gate_pending():
        """Pipeline runs waiting at Stage 4 (Session 6 Gate page). Read-only
        listing via package_pipeline.list_pending_approvals() — that module
        owns the runs/ directory layout. The per-run approval secret is NOT
        in the payload; it never leaves the server. Key-protected (not in
        _OPEN_PATHS)."""
        try:
            sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
            from tools import package_pipeline
            return package_pipeline.list_pending_approvals({})
        except Exception as e:
            log.error("gate_pending.failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "gate queue unavailable",
                                         "detail": str(e)[:200]})

    @app.post("/gate/decision")
    def gate_decision(d: GateDecisionIn):
        """Web front door to the Stage 4 gate (Session 6). Auth is the
        X-JARVIS-KEY header (route is NOT in _OPEN_PATHS) instead of
        /approve's one-time URL token — that scheme exists only because
        Teams buttons can't send headers. The run's approval secret is read
        server-side from the run file and the decision is written by the
        SAME helper /approve uses, so Stage 4's poller validates an
        identical artifact from either door. This route cannot weaken the
        gate: it only records a decision for Stage 4 to validate — token
        checks in package_pipeline are untouched."""
        sys.path.insert(0, str(UI_DIR))  # so `tools` resolves
        from tools import package_pipeline
        try:
            run = package_pipeline._load_run(d.run_id)
        except ValueError:
            return JSONResponse(status_code=404,
                                content={"error": "unknown pipeline run"})
        secret = run.get("approval_secret", "")
        if run.get("stage") != 4 or not secret \
                or (run.get("approval") or {}).get("decision"):
            return JSONResponse(status_code=409,
                                content={"error": "run is not awaiting a "
                                         "decision"})
        approver = "approver-via-web-gate"
        try:
            _record_gate_decision(d.run_id, d.decision, approver, secret)
        except Exception as e:
            log.error("gate_decision.write_failed", error=str(e))
            return JSONResponse(status_code=500,
                                content={"error": "could not record "
                                         "decision"})
        log.info("approval.recorded", pipeline_run_id=d.run_id,
                 decision=d.decision, via="web-gate")
        return {"recorded": True, "pipeline_run_id": d.run_id,
                "decision": d.decision, "approver": approver}

    @app.get("/health")
    def health():
        core = "down"
        try:
            r = httpx.get(f"{JARVIS_CORE_URL}/health", timeout=3)
            core = r.json().get("status", "degraded")
        except Exception:
            pass
        return {"status": "ok", "uptime": _uptime(), "model": PRIMARY_MODEL,
                "core": core, "mock_mode": MOCK_MODE}

    # ── APPROVAL CAPTURE (package pipeline Stage 4) ──────────────────────────
    @app.get("/approve", response_class=HTMLResponse)
    def approve(run_id: str = "", decision: str = "", token: str = ""):
        """Records a human approval decision. Auth = the one-time per-run
        token in the URL (Teams card buttons cannot send headers). Writes
        the file Stage 4 is polling; secret validation happens there too."""
        client = "approver-via-teams-card"
        if decision not in ("approved", "rejected", "changes_requested") \
                or not run_id or not token:
            return HTMLResponse("<h2>Invalid approval link.</h2>",
                                status_code=400)
        try:
            # Shared writer (see _record_gate_decision): same file shape as
            # before, secret = the URL token, validated by Stage 4's poller.
            _record_gate_decision(run_id, decision, client, token)
        except Exception as e:
            log.error("approve.write_failed", error=str(e))
            return HTMLResponse("<h2>Could not record decision — "
                                "check pipeline data dir.</h2>",
                                status_code=500)
        log.info("approval.recorded", pipeline_run_id=run_id,
                 decision=decision)
        nice = decision.replace("_", " ")
        return HTMLResponse(
            f"<body style='background:#0a0f0d;color:#9ef0c8;font-family:"
            f"-apple-system,sans-serif;text-align:center;padding-top:20vh'>"
            f"<h1>Decision recorded: {nice}</h1>"
            f"<p>Pipeline run {run_id[:8]}… — JARVIS is proceeding "
            f"accordingly. You can close this tab.</p></body>")

    return app


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
# systemd unit (etc/jarvis-ui.service):
#   ExecStart=/usr/bin/python3 /opt/cofc-itip/ui/jarvis_ui.py
#   Environment=JARVIS_CONFIG=/etc/cofc-itip/config.env
#   User=cofc-itip   Restart=on-failure
if __name__ == "__main__":
    import argparse
    import uvicorn

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr)
    )

    parser = argparse.ArgumentParser(description="JARVIS Mobile UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if not JARVIS_UI_KEY and not MOCK_MODE:
        log.warning("startup.no_ui_key",
                    note="set JARVIS_UI_KEY in config.env — data routes "
                         "will 503 until it exists")

    log.info("server.starting", host=args.host, port=args.port,
             mock=MOCK_MODE, core=JARVIS_CORE_URL)
    uvicorn.run(build_app(), host=args.host, port=args.port,
                log_level="warning")
