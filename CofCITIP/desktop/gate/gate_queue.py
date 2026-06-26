"""
gate_queue.py — native desktop view of pending Stage-4 approvals
================================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session D)

A SECOND way to see and resolve the human confirmation gate, alongside the
existing Teams-card path — NOT a replacement for it. Both paths write the SAME
decision file (approvals/<run_id>.json) in the SAME shape, so either one resolves
a pending request and clears it from this list on the next poll.

WHAT THIS WIDGET IS, AND ALL IT IS: a UI for writing ONE file in exactly the
shape tools/package_pipeline.py's Stage 4 already reads and trusts. It implements
NO approval-token construction or validation of its own. All trust/validation
stays in package_pipeline._validate_token, which this session does not touch.

The decision file shape Stage 4 expects (see package_pipeline._mock_approver and
the read loop in request_approval) — written verbatim by build_decision_doc():
  { "pipeline_run_id": str, "decision": "approved"|"rejected",
    "approver": str (non-empty), "secret": str (the run's stored approval_secret,
    NOT freshly generated), "timestamp": tz-aware UTC ISO8601 }

A wrong `secret` is the dangerous failure mode: Stage 4's rejected_bad_secret
path silently discards the decision and keeps waiting. So the secret is always
read from runs/<run_id>.json's approval_secret field, never invented here.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

# Reuse package_pipeline's OWN path resolution so the queue and the pipeline can
# never disagree about WHERE the runs/approvals dirs live. The desktop process
# runs from desktop/, so add the repo root (parent of desktop/) to sys.path to
# import the tools package, then defer to package_pipeline._data_dir() /
# _run_path() / _approval_path() rather than re-deriving PIPELINE_DATA_DIR (and
# its prod/home fallback) a second time and risking divergence.
_DESKTOP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_DESKTOP_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from tools import package_pipeline as pipeline  # noqa: E402

log = structlog.get_logger("jarvis.gate_queue")

DEFAULT_POLL_MS = 5000  # configurable poll interval (Part 1: default 5s)


# ── PURE DECISION-FILE WRITE (no Qt — kept module-level so it can be unit/
#    integration-tested against package_pipeline without instantiating the GUI) ──
def build_decision_doc(run_id: str, decision: str, approver: str, secret: str) -> dict:
    """The EXACT decision payload Stage 4 reads. timestamp is tz-aware UTC
    isoformat so package_pipeline's datetime.fromisoformat (and the 4h age math
    that follows it) parse it without error."""
    return {
        "pipeline_run_id": run_id,
        "decision": decision,        # "approved" | "rejected"
        "approver": approver,        # non-empty; _validate_token rejects blank
        "secret": secret,            # the run's stored approval_secret, NOT fresh
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def write_decision(run_id: str, decision: str, approver: str) -> Path:
    """Write approvals/<run_id>.json. Reads the run's stored approval_secret and
    writes it back as `secret` — getting this from anywhere else would trip Stage
    4's rejected_bad_secret path. Returns the path written."""
    run = pipeline._load_run(run_id)
    secret = run.get("approval_secret")
    if not secret:
        raise ValueError(
            f"run {run_id} has no approval_secret — not at Stage 4 yet; refusing "
            f"to write a decision with a guessed secret")
    doc = build_decision_doc(run_id, decision, approver, secret)
    path = pipeline._approval_path(run_id)
    path.write_text(json.dumps(doc), encoding="utf-8")
    log.info("gate_queue.decision_written", pipeline_run_id=run_id,
             decision=decision, approver=approver)
    return path


class ApprovalGateQueue(QWidget):
    """List of genuinely-pending Stage-4 approvals with Approve/Deny per row.

    `notifier` is an optional QSystemTrayIcon (Session A's JarvisTrayIcon) used to
    fire a native balloon when a NEW pending approval appears — reusing the
    existing tray mechanism rather than adding a notification dependency.
    """

    COLS = ["App", "Platform", "Version", "Test Result", "Age", "Run", "Decision"]
    _COL_RUN = 5
    _COL_DECISION = 6

    def __init__(self, notifier=None, poll_interval_ms: int = DEFAULT_POLL_MS, parent=None):
        super().__init__(parent)
        self._notifier = notifier
        self._known_pending: set[str] = set()
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()  # immediate first scan

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def stop(self) -> None:
        self._timer.stop()

    # ── UI ──
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Pending Production Approvals</b> — Stage 4 gate"))

        self._empty = QLabel("No pending approvals.")
        self._empty.setStyleSheet("color: #888;")
        layout.addWidget(self._empty)

        self._table = QTableWidget(0, len(self.COLS))
        self._table.setHorizontalHeaderLabels(self.COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

    # ── poll / scan ──
    def refresh(self) -> None:
        try:
            pending = self._scan_pending()
        except Exception as e:  # never let a bad poll kill the timer
            log.warning("gate_queue.scan_failed", error=str(e))
            return
        ids_now = {r["pipeline_run_id"] for r in pending}
        for r in pending:
            if r["pipeline_run_id"] not in self._known_pending:
                self._notify_new(r)
        self._known_pending = ids_now
        self._populate(pending)

    def _scan_pending(self) -> list[dict]:
        """Runs at stage 4 with NO decision file yet = genuinely pending. A run
        resolved via either path (this widget OR the Teams card) drops a file in
        approvals/, so it stops matching here and the row clears next cycle."""
        runs_dir = pipeline._data_dir() / "runs"
        pending: list[dict] = []
        for rp in sorted(runs_dir.glob("*.json")):
            try:
                run = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if run.get("stage") != 4:
                continue
            run_id = run.get("pipeline_run_id")
            if not run_id:
                continue
            if pipeline._approval_path(run_id).exists():
                continue  # already resolved (either path) — not pending
            pending.append(run)
        return pending

    # ── row rendering ──
    @staticmethod
    def _test_summary(run: dict) -> str:
        vr = run.get("validation_report") or {}
        if vr:
            return (f"{vr.get('install_result', '?')} / "
                    f"{vr.get('detection_result', '?')} / "
                    f"{vr.get('confidence', '?')}")
        tr = run.get("test_result") or {}
        if tr:
            return f"{str(tr.get('status', '?')).capitalize()} / exit {tr.get('exit_code', '?')}"
        return "—"

    @staticmethod
    def _age(run: dict) -> str:
        ts = run.get("approval_requested_at") or run.get("created_at")
        if not ts:
            return "—"
        try:
            then = datetime.fromisoformat(ts)
        except ValueError:
            return "—"
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - then).total_seconds()
        if secs < 90:
            return f"{int(secs)}s"
        if secs < 5400:
            return f"{int(secs // 60)}m"
        return f"{secs / 3600:.1f}h"

    def _populate(self, pending: list[dict]) -> None:
        self._empty.setVisible(not pending)
        self._table.setVisible(bool(pending))
        self._table.setRowCount(len(pending))
        for row, run in enumerate(pending):
            cfg = run.get("draft_config") or {}
            run_id = run["pipeline_run_id"]
            cells = [
                cfg.get("app_name", "?"),
                cfg.get("platform", "?"),
                cfg.get("version", "?"),
                self._test_summary(run),
                self._age(run),
                f"{run_id[:8]}…",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == self._COL_RUN:
                    item.setToolTip(run_id)  # full id on hover (Part 1 requirement)
                self._table.setItem(row, col, item)

            cell = QWidget()
            h = QHBoxLayout(cell)
            h.setContentsMargins(2, 2, 2, 2)
            approve = QPushButton("Approve")
            deny = QPushButton("Deny")
            approve.clicked.connect(
                lambda _=False, rid=run_id: self._decide(rid, "approved"))
            deny.clicked.connect(
                lambda _=False, rid=run_id: self._decide(rid, "rejected"))
            h.addWidget(approve)
            h.addWidget(deny)
            self._table.setCellWidget(row, self._COL_DECISION, cell)

    # ── decision ──
    def _decide(self, run_id: str, decision: str) -> None:
        verb = "approve" if decision == "approved" else "deny"
        # Single-person tech team: capture WHO clicked, not a full auth flow.
        approver, ok = QInputDialog.getText(
            self, "Approver name",
            f"Your name to {verb} run {run_id[:8]}…:")
        if not ok:
            return
        approver = (approver or "").strip()
        if not approver:
            # An empty approver fails _validate_token anyway — don't write a dud
            # file that would just confuse the Stage-4 reader.
            log.warning("gate_queue.decision_no_approver", pipeline_run_id=run_id)
            return
        try:
            write_decision(run_id, decision, approver)
        except Exception as e:
            log.error("gate_queue.write_failed", pipeline_run_id=run_id, error=str(e))
            return
        self.refresh()  # the row clears now that approvals/<run_id>.json exists

    # ── notification ──
    def _notify_new(self, run: dict) -> None:
        cfg = run.get("draft_config") or {}
        title = "JARVIS — approval needed"
        msg = (f"{cfg.get('app_name', '?')} {cfg.get('version', '')} "
               f"({cfg.get('platform', '?')}) awaiting production sign-off")
        if self._notifier is not None:
            try:
                # Reuses Session A's QSystemTrayIcon — its built-in showMessage
                # balloon is sufficient; no notify2/D-Bus dependency introduced.
                self._notifier.showMessage(title, msg)
            except Exception as e:
                log.debug("gate_queue.notify_failed", error=str(e))
        log.info("gate_queue.new_pending",
                 pipeline_run_id=run.get("pipeline_run_id"))
