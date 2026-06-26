"""
tools/teams.py — Microsoft Teams Outbound Connector
====================================================
CofCITIP — Posts alerts, briefings, and approval requests to a Teams
channel via an incoming webhook.

DESIGN DECISIONS (inline per session rules):
- Card format: Adaptive Cards wrapped in the Workflows envelope
  ({"type":"message","attachments":[...]}) — Microsoft retired classic
  O365 connector MessageCards; Power Automate "Workflows" webhooks are the
  current supported path and they expect this envelope. If the channel
  still runs a legacy connector it also accepts this format.
- ONE-WAY CHANNEL: incoming webhooks cannot receive button responses.
  Approve / Reject / Request Changes buttons are Action.OpenUrl links that
  hit the JARVIS UI /approve endpoint with a one-time token in the URL.
  The pipeline (tools/package_pipeline.py) polls the local approval store —
  Teams never talks back to us, the approver's phone does, over campus LAN.
  Zero-egress preserved: nothing inbound from Microsoft's cloud.
- Outbound webhook POST is the ONLY egress this module performs, and only
  when MOCK_MODE is off. In mock mode the full card JSON is logged via
  structlog instead of posted — demo-safe with no Teams tenant touched.

Auth:      TEAMS_WEBHOOK_URL from config.env. No OAuth, no app registration.
Write-ish but not state-changing on our side: posting a card mutates
nothing in CofC systems, so no human gate needed HERE — the gate lives in
package_pipeline.py Stage 4/5.

Tool functions (signature: (params: dict) -> dict):
  send_alert(params)             {"title","body","severity","source"}
  send_briefing(params)          {"briefing": dict from tools/briefing.py}
  send_approval_request(params)  {"package_name","version","platform",
                                  "test_result","pipeline_run_id",
                                  "approve_url","reject_url","changes_url"}
                                 → returns {"request_id", ...}

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from tools.secrets import get_secret  # Phase 2: creds via LastPass CLI -> env

log = structlog.get_logger("jarvis.tools.teams")

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Webhook URL is treated as a SECRET — it's bearer-equivalent (anyone with it
# can post to the channel), so it resolves via LastPass/env like other creds.
TEAMS_WEBHOOK_URL = get_secret("TEAMS_WEBHOOK_URL")
MOCK_MODE         = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES       = int(os.getenv("TEAMS_MAX_RETRIES", "3"))
# Teams throttles webhooks around 4 req/sec; 30/min is far below and plenty.
TEAMS_CALLS_PER_MINUTE = int(os.getenv("TEAMS_RATE_LIMIT_RPM", "30"))

teams_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60,
                                         name="teams")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


teams_breaker.add_listener(_BreakerLogger())

# Severity → Adaptive Card accent color + emoji prefix for the title line.
_SEV_STYLE = {
    "critical": ("attention", "🔴"),
    "high":     ("warning",   "🟠"),
    "medium":   ("accent",    "🟡"),
    "low":      ("good",      "🟢"),
    "info":     ("default",   "🔵"),
}


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class AlertParams(BaseModel):
    title: str
    body: str
    severity: str = "info"
    source: str = "jarvis"

    @field_validator("title", "body")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v[:3000]  # Teams card text ceiling — trim, don't error

    @field_validator("severity")
    @classmethod
    def sev_ok(cls, v: str) -> str:
        v = (v or "info").strip().lower()
        return v if v in _SEV_STYLE else "info"


class BriefingCardParams(BaseModel):
    briefing: dict

    @field_validator("briefing")
    @classmethod
    def briefing_ok(cls, v: dict) -> dict:
        if not isinstance(v, dict) or "generated_at" not in v:
            raise ValueError("briefing must be the dict from tools/briefing.py")
        return v


class ApprovalCardParams(BaseModel):
    package_name: str
    version: str
    platform: str
    test_result: str
    pipeline_run_id: str
    # URLs are built by package_pipeline (they embed the one-time token).
    approve_url: str = ""
    reject_url: str = ""
    changes_url: str = ""

    @field_validator("package_name", "version", "platform",
                     "test_result", "pipeline_run_id")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


# ── HTTP LAYER (rate limit -> retry -> breaker) ───────────────────────────────
@teams_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=TEAMS_CALLS_PER_MINUTE, period=60)
def _post_card(envelope: dict) -> None:
    if not TEAMS_WEBHOOK_URL:
        raise RuntimeError(
            "TEAMS_WEBHOOK_URL missing — create an incoming webhook on the "
            "IT Teams channel and set it in /etc/cofc-itip/config.env"
        )
    started = time.monotonic()
    resp = httpx.post(TEAMS_WEBHOOK_URL, json=envelope, timeout=15)
    resp.raise_for_status()
    log.info("teams.posted",
             duration_ms=int((time.monotonic() - started) * 1000))


def _deliver(envelope: dict, kind: str) -> dict:
    """Mock-aware delivery. Mock logs the full card; live posts it."""
    if MOCK_MODE:
        log.info("teams.mock_card", kind=kind, card=envelope)
        return {"delivered": False, "mock": True}
    _post_card(envelope)
    return {"delivered": True, "mock": False}


# ── CARD BUILDERS ─────────────────────────────────────────────────────────────
def _envelope(card_body: list, actions: list | None = None) -> dict:
    """Wrap an Adaptive Card body in the Teams Workflows message envelope."""
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": card_body,
    }
    if actions:
        card["actions"] = actions
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }],
    }


def _fact(label: str, value) -> dict:
    return {"title": label, "value": str(value)}


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────
def send_alert(raw_params: dict) -> dict:
    """Post a severity-styled alert card to the Teams channel."""
    p = AlertParams(**(raw_params or {}))
    log.info("tool.start", tool="send_alert", severity=p.severity,
             source=p.source)

    color, dot = _SEV_STYLE[p.severity]
    envelope = _envelope([
        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
         "color": color, "text": f"{dot} {p.title}", "wrap": True},
        {"type": "TextBlock", "text": p.body, "wrap": True},
        {"type": "TextBlock", "isSubtle": True, "size": "Small", "wrap": True,
         "text": f"Source: {p.source} · Severity: {p.severity} · "
                 f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
    ])

    status = _deliver(envelope, "alert")
    result = {"source": "teams", "kind": "alert", "severity": p.severity,
              **status}
    log.info("tool.success", tool="send_alert", **status)
    return result


def send_briefing(raw_params: dict) -> dict:
    """Post the morning briefing as a sectioned card."""
    p = BriefingCardParams(**(raw_params or {}))
    b = p.briefing
    log.info("tool.start", tool="send_briefing",
             generated_at=b.get("generated_at"))

    fh, cs = b.get("fleet_health", {}), b.get("compliance_summary", {})
    oa, ps = b.get("overnight_alerts", {}), b.get("patch_status", {})
    pb = fh.get("platform_breakdown", {})

    body = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
         "text": "☀️ JARVIS Morning Briefing", "wrap": True},
        {"type": "TextBlock", "isSubtle": True, "size": "Small",
         "text": b.get("generated_at", ""), "wrap": True},
        {"type": "TextBlock", "weight": "Bolder", "text": "Fleet Health",
         "spacing": "Medium"},
        {"type": "FactSet", "facts": [
            _fact("Total devices", fh.get("total", "n/a")),
            _fact("Windows / Mac",
                  f"{pb.get('windows', 0)} / {pb.get('mac', 0)}"),
            _fact("Non-compliant", fh.get("non_compliant", "n/a")),
            _fact("Stale Macs", pb.get("mac_stale", 0)),
        ]},
        {"type": "TextBlock", "weight": "Bolder", "text": "Compliance",
         "spacing": "Medium"},
        {"type": "FactSet", "facts": [
            _fact("Intune", f"{cs.get('intune_pct', 0)}%"),
            _fact("Jamf", f"{cs.get('jamf_pct', 0)}%"),
            _fact("Trend", cs.get("trend", "n/a")),
        ]},
        {"type": "TextBlock", "weight": "Bolder", "text": "Overnight Alerts",
         "spacing": "Medium"},
        {"type": "FactSet", "facts": [
            _fact("Critical", oa.get("critical", 0)),
            _fact("High", oa.get("high", 0)),
            _fact("Medium", oa.get("medium", 0)),
        ]},
    ]
    if oa.get("top_3"):
        top = oa["top_3"][0]
        body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                     "text": f"Top: {top.get('title','')} "
                             f"({top.get('host','')})"})
    body += [
        {"type": "TextBlock", "weight": "Bolder", "text": "Patch Status",
         "spacing": "Medium"},
        {"type": "FactSet", "facts": [
            _fact("Titles below 90%", ps.get("pending", 0)),
            _fact("Up to date", ps.get("up_to_date", 0)),
            _fact("Worst", f"{ps.get('worst_title', 'n/a')} "
                           f"({ps.get('worst_pct', 'n/a')}%)"),
        ]},
    ]

    status = _deliver(_envelope(body), "briefing")
    result = {"source": "teams", "kind": "briefing", **status}
    log.info("tool.success", tool="send_briefing", **status)
    return result


def send_approval_request(raw_params: dict) -> dict:
    """Post the Stage-4 approval Adaptive Card. Returns a request_id.

    Buttons are OpenUrl links into the JARVIS UI /approve endpoint — the
    incoming webhook is one-way, so the decision comes back over campus LAN,
    never through Microsoft's cloud. package_pipeline polls the approval
    store; this function only delivers the card."""
    p = ApprovalCardParams(**(raw_params or {}))
    request_id = str(uuid.uuid4())
    log.info("tool.start", tool="send_approval_request",
             pipeline_run_id=p.pipeline_run_id, request_id=request_id)

    actions = []
    for label, style, url in (
        ("✅ Approve", "positive", p.approve_url),
        ("❌ Reject", "destructive", p.reject_url),
        ("✏️ Request Changes", "default", p.changes_url),
    ):
        if url:
            actions.append({"type": "Action.OpenUrl", "title": label,
                            "style": style, "url": url})

    body = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
         "text": "📦 Package Deployment Approval Required", "wrap": True},
        {"type": "TextBlock", "wrap": True,
         "text": "A package passed test deployment and is staged for "
                 "production. JARVIS will NOT deploy without your signoff."},
        {"type": "FactSet", "facts": [
            _fact("Package", p.package_name),
            _fact("Version", p.version),
            _fact("Platform", p.platform),
            _fact("Test result", p.test_result),
            _fact("Pipeline run", p.pipeline_run_id),
            _fact("Request ID", request_id),
        ]},
        {"type": "TextBlock", "isSubtle": True, "size": "Small", "wrap": True,
         "text": "Buttons open the JARVIS approval page on the campus "
                 "network. Approval links expire with the pipeline run."},
    ]

    status = _deliver(_envelope(body, actions), "approval_request")
    result = {"source": "teams", "kind": "approval_request",
              "request_id": request_id,
              "pipeline_run_id": p.pipeline_run_id, **status}
    log.info("tool.success", tool="send_approval_request",
             request_id=request_id, **status)
    return result


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: returns {"status": "ok"|"degraded"|"down",
    "detail": str}. No network probe — creds-present + breaker state — so
    polling never burns API rate limit. Extra keys retained for back-compat."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not TEAMS_WEBHOOK_URL:
        status, detail = "down", "webhook URL missing (TEAMS_WEBHOOK_URL)"
    elif teams_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {teams_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "teams", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": teams_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.teams --function send_alert --params '{"title":"Test","body":"Hello","severity":"high"}' --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Teams connector test harness")
    parser.add_argument("--function", default="send_alert",
                        choices=["send_alert", "send_briefing",
                                 "send_approval_request"])
    parser.add_argument("--params", default='{"title":"Test alert","body":"Harness test","severity":"medium"}')
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    fn = {"send_alert": send_alert,
          "send_briefing": send_briefing,
          "send_approval_request": send_approval_request}[args.function]
    print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
