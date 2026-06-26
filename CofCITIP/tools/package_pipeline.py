"""
tools/package_pipeline.py — JARVIS-Assisted Package Deployment Pipeline
========================================================================
CofCITIP — Implements cofc_package_pipeline_arch.md (5 stages, hard human
gate before production).

THE GATE, PLAINLY: Stage 5 will not run without an approval token produced
by Stage 4, and Stage 4 only produces one when a human records a decision.
Token validation is structural (run ID match, 4-hour freshness, non-empty
approver, secret match) and failures are audit-logged hard errors. There is
no flag, env var, or mock shortcut that lets Stage 5 deploy without a token.

DESIGN DECISIONS (inline per session rules):
- Run state persists as JSON at $PIPELINE_DATA_DIR/runs/<run_id>.json so
  stages can be invoked independently (voice, CLI, UI) hours apart and
  survive a jarvis-core restart mid-pipeline.
- Approval transport: Teams incoming webhooks are ONE-WAY. The approval
  card's buttons are links to the JARVIS UI /approve endpoint carrying a
  one-time secret; the UI writes $PIPELINE_DATA_DIR/approvals/<run_id>.json
  and Stage 4 polls that file. Decision never transits Microsoft's cloud.
- Stage 4 BLOCKS (polls) until a decision lands or APPROVAL_TIMEOUT_SECONDS
  expires. In MOCK_MODE a background thread simulates the approver after
  MOCK_APPROVAL_DELAY seconds so demos flow without a phone in hand —
  the token-validation path exercised is IDENTICAL to live.
- LIVE WRITE SAFETY: Stages 2 and 5 are MDM writes. Until live API creds
  exist AND PIPELINE_LIVE_ENABLED=true is set deliberately, the live branch
  raises. Mock mode is fully functional end to end. The Graph/Jamf calls
  are documented at the call sites so going live is a fill-in, not a build.
- Audit: every stage appends JSONL to $PIPELINE_DATA_DIR/audit.jsonl
  (who, stage, run id, params, result, timestamp) in addition to structlog.

Stage functions (all (params: dict) -> dict):
  build_package(params)              Stage 1 — assisted draft, human authors
  test_deploy(params)                Stage 2 — auto-deploy to TEST group only
  generate_validation_report(params) Stage 3 — structured PASS/REVIEW/FAIL
  request_approval(params)           Stage 4 — Teams card + BLOCKING wait
  production_deploy(params)          Stage 5 — token-gated phased rollout
  run_pipeline(params)               convenience driver, mock demos

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
from pydantic import BaseModel, field_validator

log = structlog.get_logger("jarvis.tools.package_pipeline")

# ── CONFIG ────────────────────────────────────────────────────────────────────
MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"
PIPELINE_LIVE_ENABLED = os.getenv("PIPELINE_LIVE_ENABLED",
                                  "false").lower() == "true"

# Data dir: production path first; fall back to a home-dir path so the
# pipeline works on dev laptops without sudo.
_DEFAULT_DATA = "/var/lib/cofc-itip/pipeline"
_FALLBACK_DATA = str(Path.home() / ".cofc-itip" / "pipeline")
PIPELINE_DATA_DIR = os.getenv("PIPELINE_DATA_DIR", _DEFAULT_DATA)

APPROVAL_TIMEOUT_SECONDS = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "3600"))
APPROVAL_POLL_SECONDS    = float(os.getenv("APPROVAL_POLL_SECONDS", "5"))
APPROVAL_TOKEN_TTL_SECONDS = 4 * 3600          # arch doc: 4-hour freshness
MOCK_APPROVAL_DELAY      = float(os.getenv("MOCK_APPROVAL_DELAY", "2"))

# Base URL the approval links point at (the JARVIS UI on BB).
JARVIS_UI_BASE = os.getenv("JARVIS_UI_BASE", "http://BB-IP:8080")

# Test group names per cofc_package_pipeline_arch.md — NEVER production.
TEST_GROUPS = {"windows": "TEST-WIN-PackagePipeline",
               "mac": "TEST-MAC-PackagePipeline"}
PILOT_PCT = 12          # phased rollout: 10–15% pilot per arch doc
FAILURE_PAUSE_PCT = 10  # auto-pause threshold


def _data_dir() -> Path:
    """Resolve a writable data dir (prod path, else home fallback)."""
    for candidate in (PIPELINE_DATA_DIR, _FALLBACK_DATA):
        p = Path(candidate)
        try:
            (p / "runs").mkdir(parents=True, exist_ok=True)
            (p / "approvals").mkdir(parents=True, exist_ok=True)
            return p
        except PermissionError:
            continue
    raise RuntimeError("no writable pipeline data dir — set PIPELINE_DATA_DIR")


# ── AUDIT ─────────────────────────────────────────────────────────────────────
def _audit(stage: str, run_id: str, params: dict, result: dict) -> None:
    """Append-only JSONL audit: who, stage, what, outcome, when."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": os.getenv("JARVIS_ACTOR", getpass.getuser()),
        "stage": stage,
        "pipeline_run_id": run_id,
        "params": params,
        "result_summary": {k: result.get(k) for k in
                           ("status", "decision", "confidence", "rollout_pct")
                           if k in result},
        "mock": MOCK_MODE,
    }
    path = _data_dir() / "audit.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    log.info("audit.logged", stage=stage, pipeline_run_id=run_id)


# ── RUN STATE ─────────────────────────────────────────────────────────────────
def _run_path(run_id: str) -> Path:
    return _data_dir() / "runs" / f"{run_id}.json"


def _save_run(run: dict) -> None:
    _run_path(run["pipeline_run_id"]).write_text(
        json.dumps(run, indent=2, default=str), encoding="utf-8")


def _load_run(run_id: str) -> dict:
    p = _run_path(run_id)
    if not p.exists():
        raise ValueError(f"unknown pipeline run: {run_id}")
    return json.loads(p.read_text(encoding="utf-8"))


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class BuildParams(BaseModel):
    app_name: str
    version: str
    platform: str
    install_cmd: str
    detect_rule: str = ""

    @field_validator("app_name", "version", "install_cmd")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("platform")
    @classmethod
    def platform_ok(cls, v: str) -> str:
        v = (v or "").strip().lower()
        v = {"win": "windows", "macos": "mac", "osx": "mac"}.get(v, v)
        if v not in ("windows", "mac"):
            raise ValueError("platform must be 'windows' or 'mac'")
        return v


class RunIdParams(BaseModel):
    pipeline_run_id: str = ""
    package_id: str = ""
    run_id: str = ""   # Stage 3 accepts run_id per spec — normalized below

    def resolved_run_id(self) -> str:
        rid = self.pipeline_run_id or self.run_id or self.package_id
        if not rid.strip():
            raise ValueError("a pipeline run id is required")
        return rid.strip()


# ── STAGE 1 — PACKAGE BUILD (ASSISTED) ────────────────────────────────────────
# Dangerous-pattern screens for install strings. JARVIS flags, human decides.
_INSTALL_WARNINGS = [
    (re.compile(r"rm\s+-rf\s+/(?:\s|$)"), "install command contains rm -rf / "
                                          "— almost certainly wrong"),
    (re.compile(r"\bformat\b", re.I), "install command references 'format'"),
    (re.compile(r"reg\s+delete\s+HKLM", re.I), "deletes HKLM registry keys"),
    (re.compile(r"(curl|wget|iwr|Invoke-WebRequest)", re.I),
     "downloads at install time — package the payload instead "
     "(zero-trust install sources)"),
]
_SILENT_HINTS = {"windows": ("/qn", "/quiet", "/s", "-silent", "/verysilent"),
                 "mac": ("installer -pkg", "-target /")}


def build_package(raw_params: dict) -> dict:
    """Stage 1 — JARVIS assists with package draft. HUMAN authors and
    validates; nothing is created in Intune/Jamf at this stage — the draft
    config is staged locally for the engineer to review."""
    p = BuildParams(**(raw_params or {}))
    run_id = str(uuid.uuid4())
    package_id = f"pkg-{p.platform}-{re.sub(r'[^a-z0-9]+', '-', p.app_name.lower()).strip('-')}-{p.version}"
    log.info("tool.start", tool="build_package", stage=1,
             pipeline_run_id=run_id, package=package_id)

    warnings: list[str] = []
    for pattern, msg in _INSTALL_WARNINGS:
        if pattern.search(p.install_cmd):
            warnings.append(msg)
    if not any(h in p.install_cmd.lower() for h in _SILENT_HINTS[p.platform]):
        warnings.append(
            f"no silent-install flag detected for {p.platform} — "
            f"expected one of: {', '.join(_SILENT_HINTS[p.platform])}")

    # Detection rule assist: suggest a sane default if the engineer left it
    # blank. Suggestion only — human validates before promotion.
    detect_rule = p.detect_rule.strip()
    if not detect_rule:
        if p.platform == "windows":
            detect_rule = (f"MSI product version >= {p.version} OR file "
                           f"%ProgramFiles%\\{p.app_name}\\{p.app_name}.exe "
                           f"version >= {p.version}")
        else:
            detect_rule = (f"pkgutil --pkg-info receipt contains "
                           f"'{p.app_name.lower()}' with version >= {p.version}")
        warnings.append("detection rule was blank — JARVIS suggested one; "
                        "validate before promoting to test")

    draft_config = {
        "app_name": p.app_name,
        "version": p.version,
        "platform": p.platform,
        "install_cmd": p.install_cmd,
        "detect_rule": detect_rule,
        # Live wiring (Stage 1 stays read-only even live — drafts are local):
        #   Windows: package via Win32 Content Prep Tool -> .intunewin, then
        #     POST /deviceAppManagement/mobileApps (win32LobApp, draft).
        #   Mac: upload via Jamf POST /api/v1/packages, policy left unscoped.
        "target_test_group": TEST_GROUPS[p.platform],
        "state": "draft",
    }

    run = {
        "pipeline_run_id": run_id,
        "package_id": package_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stage": 1,
        "draft_config": draft_config,
        "warnings": warnings,
    }
    _save_run(run)

    result = {"pipeline_run_id": run_id, "package_id": package_id,
              "draft_config": draft_config, "warnings": warnings,
              "next_step": "human review, then test_deploy"}
    _audit("1_build_package", run_id, p.model_dump(), {"status": "drafted"})
    log.info("tool.success", tool="build_package", stage=1,
             pipeline_run_id=run_id, warnings=len(warnings))
    return result


# ── STAGE 2 — AUTO-TEST (TEST GROUP ONLY) ─────────────────────────────────────
def test_deploy(raw_params: dict) -> dict:
    """Stage 2 — automated deploy to the DESIGNATED TEST GROUP ONLY, then
    monitor for check-in and install result. No human action required; no
    production group is reachable from this code path by construction."""
    p = RunIdParams(**(raw_params or {}))
    run_id = p.resolved_run_id()
    run = _load_run(run_id)
    cfg = run["draft_config"]
    log.info("tool.start", tool="test_deploy", stage=2,
             pipeline_run_id=run_id, group=cfg["target_test_group"])

    if MOCK_MODE:
        # Realistic test-rig result: success with a believable IME/Jamf log.
        deploy = {
            "status": "success",
            "device": ("COFC-TESTWIN-01" if cfg["platform"] == "windows"
                       else "COFC-TESTMAC-01"),
            "os_version": ("Windows 11 23H2" if cfg["platform"] == "windows"
                           else "macOS 15.5"),
            "exit_code": 0,
            "detection_result": "Detected",
            "log_excerpt": (
                f"[Win32App] {cfg['app_name']} {cfg['version']} install "
                f"completed, exit code 0. Detection rule evaluated TRUE."
                if cfg["platform"] == "windows" else
                f"installer: Package name is {cfg['app_name']} "
                f"{cfg['version']}\ninstaller: The install was successful."
            ),
        }
    else:
        if not PIPELINE_LIVE_ENABLED:
            raise RuntimeError(
                "live test deploy blocked: set PIPELINE_LIVE_ENABLED=true "
                "after Graph/Jamf write credentials are approved by Philip. "
                "Mock mode is fully functional for demos."
            )
        # LIVE WIRING (fill in when creds land):
        #   Windows: POST /deviceAppManagement/mobileApps/{id}/assignments
        #     with target = group id of TEST-WIN-PackagePipeline (required
        #     intent). Then poll deviceStatuses for install state + exit code.
        #   Mac: scope the policy to TEST-MAC-PackagePipeline static group
        #     (PUT /api/v1/policies/{id}/scope), flush, poll policy logs.
        raise NotImplementedError("live path stubbed pending credentials")

    run.update({"stage": 2, "test_result": deploy,
                "tested_at": datetime.now(timezone.utc).isoformat()})
    _save_run(run)

    result = {"run_id": run_id, "pipeline_run_id": run_id, **deploy}
    _audit("2_test_deploy", run_id,
           {"group": cfg["target_test_group"]}, {"status": deploy["status"]})
    log.info("tool.success", tool="test_deploy", stage=2,
             pipeline_run_id=run_id, status=deploy["status"])
    return result


# ── STAGE 3 — VALIDATION REPORT ───────────────────────────────────────────────
def generate_validation_report(raw_params: dict) -> dict:
    """Stage 3 — parse the test result into a structured PASS/REVIEW/FAIL
    report for the human approver."""
    p = RunIdParams(**(raw_params or {}))
    run_id = p.resolved_run_id()
    run = _load_run(run_id)
    if "test_result" not in run:
        raise ValueError(f"run {run_id} has no test result — "
                         f"run test_deploy first")
    cfg, tr = run["draft_config"], run["test_result"]
    log.info("tool.start", tool="generate_validation_report", stage=3,
             pipeline_run_id=run_id)

    # Confidence logic: clean exit + detected = PASS; success-with-oddities
    # (nonzero-but-soft exit codes, undetected) = REVIEW; failure = FAIL.
    if tr["status"] == "success" and tr["exit_code"] == 0 \
            and tr["detection_result"] == "Detected":
        confidence = "PASS"
    elif tr["status"] in ("success", "timeout"):
        confidence = "REVIEW"
    else:
        confidence = "FAIL"

    report = {
        "package_name": cfg["app_name"],
        "version": cfg["version"],
        "platform": cfg["platform"],
        "device": tr["device"],
        "os_version": tr["os_version"],
        "install_result": tr["status"].capitalize(),
        "detection_result": tr["detection_result"],
        "exit_code": tr["exit_code"],
        "confidence": confidence,
        "log_excerpts": tr["log_excerpt"],
        "pipeline_run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    run.update({"stage": 3, "validation_report": report})
    _save_run(run)
    _audit("3_validation_report", run_id, {}, {"confidence": confidence})
    log.info("tool.success", tool="generate_validation_report", stage=3,
             pipeline_run_id=run_id, confidence=confidence)
    return report


# ── STAGE 4 — HUMAN SIGNOFF (HARD GATE) ───────────────────────────────────────
def _approval_path(run_id: str) -> Path:
    return _data_dir() / "approvals" / f"{run_id}.json"


def _mock_approver(run_id: str, secret: str) -> None:
    """MOCK ONLY — simulates the human tapping Approve after a short delay.
    Writes the same file shape the UI /approve endpoint writes, so the
    validation path in Stage 5 is identical to live."""
    time.sleep(MOCK_APPROVAL_DELAY)
    _approval_path(run_id).write_text(json.dumps({
        "pipeline_run_id": run_id,
        "decision": "approved",
        "approver": "mock-approver (Steven)",
        "secret": secret,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    log.info("mock.approval_written", pipeline_run_id=run_id)


def request_approval(raw_params: dict) -> dict:
    """Stage 4 — send the Teams approval card, then BLOCK until a human
    decision lands in the approval store or the timeout expires. JARVIS
    cannot reach Stage 5 without the token this function returns."""
    p = RunIdParams(**(raw_params or {}))
    run_id = p.resolved_run_id()
    run = _load_run(run_id)
    report = (raw_params or {}).get("validation_report") \
        or run.get("validation_report")
    if not report:
        raise ValueError(f"run {run_id} has no validation report — "
                         f"run generate_validation_report first")
    log.info("tool.start", tool="request_approval", stage=4,
             pipeline_run_id=run_id)

    # One-time secret embedded in the approval URLs. The URL *is* the
    # credential — Teams buttons can't send headers. Scoped to one run,
    # expires with the 4h token TTL, single decision file.
    secret = secrets.token_urlsafe(24)
    run.update({"stage": 4, "approval_secret": secret,
                "approval_requested_at": datetime.now(timezone.utc).isoformat()})
    _save_run(run)

    base = f"{JARVIS_UI_BASE}/approve?run_id={run_id}&token={secret}"
    from tools import teams
    card = teams.send_approval_request({
        "package_name": report["package_name"],
        "version": report["version"],
        "platform": report["platform"],
        "test_result": f"{report['install_result']} / "
                       f"{report['detection_result']} / "
                       f"confidence {report['confidence']}",
        "pipeline_run_id": run_id,
        "approve_url": f"{base}&decision=approved",
        "reject_url": f"{base}&decision=rejected",
        "changes_url": f"{base}&decision=changes_requested",
    })

    # Clear any stale decision file before waiting (defensive).
    ap = _approval_path(run_id)
    if ap.exists():
        ap.unlink()

    if MOCK_MODE:
        threading.Thread(target=_mock_approver, args=(run_id, secret),
                         daemon=True).start()

    # BLOCKING WAIT — the gate itself.
    deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
    log.info("approval.waiting", pipeline_run_id=run_id,
             timeout_s=APPROVAL_TIMEOUT_SECONDS)
    while time.monotonic() < deadline:
        if ap.exists():
            decision = json.loads(ap.read_text(encoding="utf-8"))
            if decision.get("secret") != secret:
                # Wrong/stale secret — ignore and keep waiting; audit it.
                _audit("4_request_approval", run_id, {},
                       {"status": "rejected_bad_secret"})
                log.warning("approval.bad_secret", pipeline_run_id=run_id)
                ap.unlink()
                continue
            token = {"approver": decision.get("approver", ""),
                     "timestamp": decision.get("timestamp", ""),
                     "pipeline_run_id": run_id,
                     "secret": secret}
            result = {"approval_token": token,
                      "decision": decision.get("decision", "rejected"),
                      "teams_request_id": card.get("request_id")}
            run.update({"approval": result})
            _save_run(run)
            _audit("4_request_approval", run_id,
                   {"approver": token["approver"]},
                   {"decision": result["decision"]})
            log.info("tool.success", tool="request_approval", stage=4,
                     pipeline_run_id=run_id, decision=result["decision"],
                     approver=token["approver"])
            return result
        time.sleep(APPROVAL_POLL_SECONDS)

    _audit("4_request_approval", run_id, {}, {"decision": "timeout"})
    log.warning("approval.timeout", pipeline_run_id=run_id)
    return {"approval_token": None, "decision": "timeout",
            "teams_request_id": card.get("request_id")}


# ── STAGE 5 — PRODUCTION DEPLOY (TOKEN-GATED) ─────────────────────────────────
class _TokenInvalid(RuntimeError):
    pass


def _validate_token(run_id: str, token: dict) -> None:
    """Hard validation per arch doc. Raises — no soft failures, ever."""
    if not isinstance(token, dict):
        raise _TokenInvalid("approval_token missing or malformed")
    if token.get("pipeline_run_id") != run_id:
        raise _TokenInvalid("approval token is for a different pipeline run")
    if not (token.get("approver") or "").strip():
        raise _TokenInvalid("approval token has no approver")
    run = _load_run(run_id)
    if token.get("secret") != run.get("approval_secret"):
        raise _TokenInvalid("approval token secret mismatch")
    try:
        ts = datetime.fromisoformat(token.get("timestamp", ""))
    except ValueError:
        raise _TokenInvalid("approval token timestamp unreadable")
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > APPROVAL_TOKEN_TTL_SECONDS:
        raise _TokenInvalid(f"approval token expired "
                            f"({int(age // 60)} min old, limit 240)")
    decision = (run.get("approval") or {}).get("decision")
    if decision != "approved":
        raise _TokenInvalid(f"recorded decision is '{decision}', "
                            f"not 'approved'")


def production_deploy(raw_params: dict) -> dict:
    """Stage 5 — phased production rollout. REQUIRES a valid Stage-4
    approval token; any validation failure is a hard, audited error."""
    p = RunIdParams(**(raw_params or {}))
    run_id = p.resolved_run_id()
    token = (raw_params or {}).get("approval_token")
    log.info("tool.start", tool="production_deploy", stage=5,
             pipeline_run_id=run_id)

    try:
        _validate_token(run_id, token or {})
    except _TokenInvalid as e:
        _audit("5_production_deploy", run_id,
               {"token_present": token is not None},
               {"status": f"BLOCKED: {e}"})
        log.error("gate.blocked", pipeline_run_id=run_id, reason=str(e))
        raise RuntimeError(f"PRODUCTION DEPLOY BLOCKED — {e}. "
                           f"This action requires valid human approval.") from e

    run = _load_run(run_id)
    cfg = run["draft_config"]

    if MOCK_MODE:
        deploy = {
            "status": "pilot_in_progress",
            "pilot_group_result": {
                "group": f"PILOT-{cfg['platform'].upper()}-"
                         f"{cfg['app_name'].replace(' ', '')}",
                "targeted": 24, "succeeded": 23, "failed": 1,
                "failure_pct": 4.2,
            },
            "rollout_pct": PILOT_PCT,
            "next_step": (f"pilot below {FAILURE_PAUSE_PCT}% failure "
                          f"threshold — broad deployment will proceed; "
                          f"completion notice at 95%"),
        }
    else:
        if not PIPELINE_LIVE_ENABLED:
            raise RuntimeError(
                "live production deploy blocked: PIPELINE_LIVE_ENABLED is "
                "not set. Enable deliberately after credential approval.")
        # LIVE WIRING (fill in when creds land):
        #   Windows: assignment to pilot group (10–15% of production),
        #     monitor deviceStatuses; if failure_pct > FAILURE_PAUSE_PCT,
        #     delete the assignment (auto-pause) and send_alert via teams.
        #     Else add broad production group assignment.
        #   Mac: scope policy to pilot smart group, monitor logs, widen scope.
        raise NotImplementedError("live path stubbed pending credentials")

    run.update({"stage": 5, "production": deploy,
                "deployed_at": datetime.now(timezone.utc).isoformat()})
    _save_run(run)
    _audit("5_production_deploy", run_id,
           {"approver": token["approver"]},
           {"status": deploy["status"], "rollout_pct": deploy["rollout_pct"]})
    log.info("tool.success", tool="production_deploy", stage=5,
             pipeline_run_id=run_id, status=deploy["status"])
    return deploy


# ── HEALTH CHECK ──────────────────────────────────────────────────────────────
def health_check() -> dict:
    """Part 3 contract: returns {"status": "ok"|"degraded"|"down",
    "detail": str}. No external API of its own — reports on local pipeline
    state: writable data dir, pending approvals, last successful deploy. Extra
    keys retained for callers that want the structured detail."""
    try:
        d = _data_dir()  # also proves the data dir is writable
    except Exception as e:
        result = {"source": "package_pipeline", "status": "down",
                  "detail": f"data dir not writable: {e}", "mock": MOCK_MODE}
        log.info("tool.health", **result)
        return result

    # Pending approvals = approval decision files with no matching deploy yet.
    pending = 0
    last_deploy = None
    try:
        runs_dir = d / "runs"
        for rp in runs_dir.glob("*.json"):
            try:
                run = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if run.get("stage") == 4 and not run.get("production"):
                pending += 1
            dep_at = run.get("deployed_at")
            if dep_at and (last_deploy is None or dep_at > last_deploy):
                last_deploy = dep_at
    except Exception as e:
        result = {"source": "package_pipeline", "status": "degraded",
                  "detail": f"state read partial: {e}", "mock": MOCK_MODE}
        log.info("tool.health", **result)
        return result

    detail = (f"{pending} pending approval(s); "
              f"last deploy {last_deploy or 'none recorded'}")
    result = {"source": "package_pipeline", "status": "ok", "detail": detail,
              "pending_approvals": pending, "last_deploy": last_deploy,
              "mock": MOCK_MODE}
    log.info("tool.health", **result)
    return result


# ── CONVENIENCE DRIVER (demo) ─────────────────────────────────────────────────
def run_pipeline(raw_params: dict) -> dict:
    """Drive Stages 1→5 in sequence. In mock mode this is the demo path;
    live, Stage 4 will block on a real human. Same gate either way."""
    s1 = build_package(raw_params)
    rid = {"pipeline_run_id": s1["pipeline_run_id"]}
    s2 = test_deploy(rid)
    s3 = generate_validation_report(rid)
    s4 = request_approval(rid)
    if s4["decision"] != "approved":
        return {"pipeline_run_id": s1["pipeline_run_id"],
                "halted_at_stage": 4, "decision": s4["decision"],
                "validation_report": s3}
    s5 = production_deploy({**rid, "approval_token": s4["approval_token"]})
    return {"pipeline_run_id": s1["pipeline_run_id"], "stages_completed": 5,
            "confidence": s3["confidence"], "decision": s4["decision"],
            "production": s5}


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.package_pipeline --mock
# python -m tools.package_pipeline --mock --params '{"app_name":"7-Zip","version":"24.08","platform":"windows","install_cmd":"msiexec /i 7z.msi /qn"}'
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Package pipeline harness")
    parser.add_argument("--params", default=_json.dumps({
        "app_name": "Notepad++", "version": "8.7.1", "platform": "windows",
        "install_cmd": "npp.installer.exe /S",
    }))
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True
        os.environ["JARVIS_MOCK"] = "true"

    print(_json.dumps(run_pipeline(_json.loads(args.params)), indent=2,
                      default=str))
