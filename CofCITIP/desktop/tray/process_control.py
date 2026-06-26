"""
process_control.py — global hotkey + systemd service control
============================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session D)

Two pieces:

1. GlobalHotkey — a configurable system-wide hotkey (default Ctrl+Alt+J) that
   toggles the main window's visibility ("bring JARVIS to front"). This is a
   manual wake/raise affordance, NOT push-to-talk audio — jarvis-voice owns audio
   wake separately and is untouched here. The pynput listener runs on its OWN
   thread; it NEVER touches a widget. It only emits a Qt signal, which Qt delivers
   to the GUI thread (queued, cross-thread) where the actual show/hide happens.

2. ServiceControlPanel — start/stop/restart + live status for the two managed
   systemd units, via QProcess (Qt's process model, so exit codes/output flow
   through the event loop). Mutating actions go through `sudo systemctl <action>
   <unit>`; status uses unprivileged `systemctl is-active <unit>`.

PRIVILEGE — READ THIS:
The desktop runs as the human admin (steven), not root, so start/stop/restart
need a NOPASSWD sudoers entry scoped to EXACTLY these commands and units. Add it
with `sudo visudo -f /etc/sudoers.d/jarvis-desktop` — do NOT grant blanket sudo,
and the app never edits sudoers itself:

    steven ALL=(root) NOPASSWD: /usr/bin/systemctl start jarvis-core, \
        /usr/bin/systemctl stop jarvis-core, \
        /usr/bin/systemctl restart jarvis-core, \
        /usr/bin/systemctl start ollama, \
        /usr/bin/systemctl stop ollama, \
        /usr/bin/systemctl restart ollama

(`systemctl is-active` is unprivileged and is run WITHOUT sudo, so it is not in
the sudoers entry and never prompts.)
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

# pynput is the planning-doc's named choice. Wrapped so the desktop still starts
# (without the global hotkey) on a box where pynput isn't installed yet.
try:
    from pynput import keyboard as pynput_keyboard  # type: ignore
    _PYNPUT_OK = True
except Exception:
    pynput_keyboard = None  # type: ignore
    _PYNPUT_OK = False

log = structlog.get_logger("jarvis.process_control")

# Configurable global hotkey (pynput GlobalHotKeys syntax). Easy to change here.
HOTKEY_COMBO = "<ctrl>+<alt>+j"

# EXACT systemd unit names from runbook.md — jarvis-core.service and ollama.service.
# ONLY these two are in scope this session (NOT jarvis-ui / jarvis-voice / glados —
# they weren't requested; don't over-scope service control).
MANAGED_UNITS = ("jarvis-core", "ollama")

_ACTIONS = ("start", "stop", "restart")

# Status colours — deliberately conventional status hues (green/amber/grey), NOT
# CofC brand maroon/gold, so service state never reads as branding.
_STATUS_COLORS = {
    "active": "#2e7d32",     # running — green
    "inactive": "#b26a00",   # stopped — amber
    "failed": "#c62828",     # failed — red
}
_STATUS_UNKNOWN_COLOR = "#777777"


class GlobalHotkey(QObject):
    """System-wide hotkey -> Qt signal. The pynput listener lives on its own
    thread; `_on_activate` runs THERE and does nothing but emit `activated`.
    Because this QObject lives in the GUI thread, Qt delivers the signal to GUI-
    thread slots via a queued connection — so no widget is ever touched from the
    pynput thread (connect with Qt.QueuedConnection on the receiving side to make
    that explicit)."""

    activated = Signal()

    def __init__(self, combo: str = HOTKEY_COMBO, parent=None):
        super().__init__(parent)
        self._combo = combo
        self._listener = None

    def start(self) -> None:
        if not _PYNPUT_OK:
            log.warning("hotkey.unavailable",
                        reason="pynput not importable — install pynput; global "
                               "hotkey disabled, tray toggle still works")
            return
        # GlobalHotKeys spins up its own daemon thread; on_activate fires there.
        self._listener = pynput_keyboard.GlobalHotKeys({self._combo: self._on_activate})
        self._listener.start()
        log.info("hotkey.started", combo=self._combo)

    def _on_activate(self) -> None:
        # Runs on pynput's thread — EMIT ONLY, never call into Qt widgets here.
        self.activated.emit()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
            log.info("hotkey.stopped")


class ServiceControlPanel(QWidget):
    """Start/stop/restart + status for MANAGED_UNITS via QProcess."""

    def __init__(self, status_interval_ms: int = 10_000, parent=None):
        super().__init__(parent)
        self._status_labels: dict[str, QLabel] = {}
        self._build_ui()

        # Status changes rarely — poll every 10s, not every second.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(status_interval_ms)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()
        self.refresh_status()  # immediate first read

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        box = QGroupBox("Service Control")
        grid = QGridLayout(box)
        grid.addWidget(QLabel("<b>Service</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Status</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Actions</b>"), 0, 2)

        for r, unit in enumerate(MANAGED_UNITS, start=1):
            grid.addWidget(QLabel(unit), r, 0)
            status = QLabel("unknown")
            status.setStyleSheet(f"color: {_STATUS_UNKNOWN_COLOR};")
            self._status_labels[unit] = status
            grid.addWidget(status, r, 1)

            actions = QWidget()
            h = QHBoxLayout(actions)
            h.setContentsMargins(0, 0, 0, 0)
            for action in _ACTIONS:
                btn = QPushButton(action.capitalize())
                btn.clicked.connect(
                    lambda _=False, u=unit, a=action: self._run_action(u, a))
                h.addWidget(btn)
            grid.addWidget(actions, r, 2)

        outer.addWidget(box)

    # ── mutating actions (sudo) ──
    def _run_action(self, unit: str, action: str) -> None:
        # Hard guard: never act on a unit outside the explicit allow-list, no
        # matter how this method gets called.
        if unit not in MANAGED_UNITS or action not in _ACTIONS:
            log.error("process_control.refused", unit=unit, action=action)
            return
        proc = QProcess(self)
        proc.setProgram("sudo")
        proc.setArguments(["systemctl", action, unit])  # NOPASSWD-scoped command
        proc.finished.connect(
            lambda code, _st, u=unit, a=action, p=proc: self._on_action_finished(u, a, code, p))
        proc.errorOccurred.connect(
            lambda err, u=unit, a=action: log.error(
                "process_control.action_error", unit=u, action=a, error=str(err)))
        log.info("process_control.action", unit=unit, action=action)
        proc.start()

    def _on_action_finished(self, unit: str, action: str, exit_code: int, proc: QProcess) -> None:
        err = bytes(proc.readAllStandardError()).decode(errors="replace").strip()
        log.info("process_control.action_done", unit=unit, action=action,
                 exit_code=exit_code, stderr=err or None)
        self.refresh_status()  # reflect the new state promptly

    # ── status (unprivileged) ──
    def refresh_status(self) -> None:
        for unit in MANAGED_UNITS:
            proc = QProcess(self)
            proc.setProgram("systemctl")
            proc.setArguments(["is-active", unit])  # read-only, no sudo
            proc.finished.connect(
                lambda _code, _st, u=unit, p=proc: self._on_status_finished(u, p))
            proc.errorOccurred.connect(
                lambda _err, u=unit: self._set_status(u, "unknown"))
            proc.start()

    def _on_status_finished(self, unit: str, proc: QProcess) -> None:
        out = bytes(proc.readAllStandardOutput()).decode(errors="replace").strip()
        # is-active prints active/inactive/failed/activating/unknown to stdout.
        self._set_status(unit, out or "unknown")

    def _set_status(self, unit: str, state: str) -> None:
        label = self._status_labels.get(unit)
        if label is None:
            return
        label.setText(state)
        label.setStyleSheet(f"color: {_STATUS_COLORS.get(state, _STATUS_UNKNOWN_COLOR)};")

    def stop(self) -> None:
        self._status_timer.stop()
