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
  GET  /health      status + uptime + model (no key required)
  GET  /approve     package-pipeline Stage 4 decision capture (token-auth)

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
PIPELINE_DATA_DIR = os.getenv("PIPELINE_DATA_DIR", "/var/lib/cofc-itip/pipeline")

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
        if MOCK_MODE:
            return {"response": f"(mock) You asked: '{q.text}'. In live "
                                f"mode this routes through local Ollama "
                                f"inference on BB.", "timestamp": ts}
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
            ap_dir = Path(PIPELINE_DATA_DIR) / "approvals"
            ap_dir.mkdir(parents=True, exist_ok=True)
            (ap_dir / f"{run_id}.json").write_text(json.dumps({
                "pipeline_run_id": run_id,
                "decision": decision,
                "approver": client,
                "secret": token,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
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
