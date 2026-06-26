"""
main.py — JARVIS desktop shell entrypoint (Phase 3, Sessions A–B)
=================================================================
CofCITIP — JARVIS native desktop UI

Desktop shell:
  - one QMainWindow titled "JARVIS — CofC IT" hosting the live StressPanel
    (Session B): scrolling GPU VRAM%/util% and CPU% plots fed by the shared
    telemetry bridge, plus 24h summary tiles from the SQLite inference log
  - one system tray icon (maroon "J" placeholder)
  - window starts HIDDEN; tray click toggles show/hide
  - closing the window (X) HIDES to tray; the app only quits via the tray menu's
    Quit action — standard real-desktop-app tray behavior

The window owns ONE TelemetryBridge and hands it to the StressPanel; there is a
single NVML polling source (the bridge's QTimer). The panel consumes the bridge
via signals — it never polls NVML itself.

Run:  python3 desktop/main.py
"""

from __future__ import annotations

import os
import sys

import structlog
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QVBoxLayout, QWidget,
)

# Allow running as a plain script (python3 desktop/main.py) by ensuring the
# desktop/ dir is importable, then importing the sibling modules. Works whether
# launched from the repo root or from inside desktop/.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from orb.telemetry_bridge import TelemetryBridge  # noqa: E402
from orb.orb_widget import OrbView                 # noqa: E402
from tray.tray_icon import JarvisTrayIcon          # noqa: E402
from tray.process_control import (                 # noqa: E402
    GlobalHotkey, ServiceControlPanel,
)
from gate.gate_queue import ApprovalGateQueue      # noqa: E402
from monitor.stress_panel import StressPanel       # noqa: E402

log = structlog.get_logger("jarvis.desktop")


class MainWindow(QMainWindow):
    """The (currently minimal) main window. Closing it hides to tray rather than
    quitting — see closeEvent."""

    def __init__(self, bridge: TelemetryBridge, tray: JarvisTrayIcon | None = None):
        super().__init__()
        self.setWindowTitle("JARVIS — CofC IT")
        # 1280x800 PLACEHOLDER default (not a finalised design size — see Phase 3
        # follow-ups). Two columns now: left = presence + telemetry, right = the
        # actionable ops panels (approval gate + service control).
        self.resize(1280, 800)

        central = QWidget()
        columns = QHBoxLayout(central)

        # ── Left column: presence orb (Session C) over the StressPanel (Session B).
        left = QVBoxLayout()

        # Session C: the presence orb sits prominent up top (Qt Quick 3D embedded
        # via QQuickWidget — see orb.OrbView). It binds to the SAME injected bridge
        # as the panel below. stretch=3 vs the panel's stretch=2 keeps the orb
        # dominant without crowding the monitor out of the default window.
        self._orb = OrbView(bridge)
        self._orb.setMinimumHeight(360)
        left.addWidget(self._orb, 3)

        # Session B: the StressPanel replaces Session A's four raw telemetry
        # QLabels. It consumes the SAME bridge (live plots) and the SQLite stress
        # log (24h summary tiles) — one presentation of the bridge values, not
        # two competing ones. The bridge is injected; the panel never polls NVML.
        self._stress_panel = StressPanel(bridge)
        left.addWidget(self._stress_panel, 2)

        # ── Right column (Session D): the approval gate queue over the service-
        # control panel. The gate queue gets the optional tray as its notifier so a
        # new pending approval fires a native balloon (reusing Session A's tray).
        right = QVBoxLayout()
        self._gate_queue = ApprovalGateQueue(notifier=tray)
        right.addWidget(self._gate_queue, 3)
        self._service_panel = ServiceControlPanel()
        right.addWidget(self._service_panel, 2)

        columns.addLayout(left, 3)   # keep the orb column dominant
        columns.addLayout(right, 2)

        self.setCentralWidget(central)
        self._bridge = bridge

        # ── DEMO ONLY — TEMPORARY, REMOVE WHEN REAL TRIGGERS EXIST ──────────────
        # No voice pipeline or live-inference hooks are wired yet, so cycle the orb
        # idle -> listening -> processing -> spillover -> idle on a 5s timer purely
        # so the four visual states are verifiable by eye on launch. In a later
        # integration pass jarvis-voice (wake word -> listening) and jarvis-core
        # (inference start/end -> processing/idle, VRAM spill -> spillover) will
        # drive bridge.orbState for real — DELETE THIS BLOCK and _demo_advance then.
        self._demo_states = [0, 1, 2, 3]  # idle, listening, processing, spillover
        self._demo_idx = 0
        self._demo_timer = QTimer(self)
        self._demo_timer.setInterval(5000)
        self._demo_timer.timeout.connect(self._demo_advance_orb_state)
        self._demo_timer.start()

    def _demo_advance_orb_state(self) -> None:
        """DEMO ONLY: step the orb to the next state every 5s (see __init__)."""
        self._demo_idx = (self._demo_idx + 1) % len(self._demo_states)
        self._bridge.orbState = self._demo_states[self._demo_idx]

    def closeEvent(self, event) -> None:
        """X button hides to tray instead of quitting. Quit is tray-menu only."""
        event.ignore()
        self.hide()
        log.info("window.hidden_to_tray")


class JarvisDesktopApp:
    """Owns the QApplication, window, tray, and telemetry bridge, and wires the
    tray signals to window show/hide and app quit."""

    def __init__(self, argv: list[str]):
        self.app = QApplication(argv)
        # Don't quit when the last (only) window is hidden — we live in the tray.
        self.app.setQuitOnLastWindowClosed(False)

        self.bridge = TelemetryBridge(poll_interval_ms=1000)

        # Tray is created BEFORE the window so it can be handed to the window as
        # the approval-gate notifier (Session A tray reused, no new dep).
        self.tray = JarvisTrayIcon()
        self.window = MainWindow(self.bridge, self.tray)

        self.tray.toggleRequested.connect(self._toggle_window)
        self.tray.quitRequested.connect(self._quit)
        self.tray.show()

        # Session D: global hotkey (default Ctrl+Alt+J) toggles the window. The
        # pynput listener runs on its own thread and only EMITS GlobalHotkey
        # .activated; we connect it with an explicit QueuedConnection so the slot
        # runs on the GUI thread — never a cross-thread widget call.
        self.hotkey = GlobalHotkey()
        self.hotkey.activated.connect(self._toggle_window, Qt.ConnectionType.QueuedConnection)
        self.hotkey.start()

        # Window starts hidden — tray-resident on launch.
        self.bridge.start()
        log.info("desktop.ready", hidden=True)

    def _toggle_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()

    def _quit(self) -> None:
        log.info("desktop.quit")
        self.hotkey.stop()
        self.bridge.stop()
        self.tray.hide()
        self.app.quit()

    def run(self) -> int:
        return self.app.exec()


def main() -> int:
    if not QApplication.instance():
        return JarvisDesktopApp(sys.argv).run()
    return JarvisDesktopApp(sys.argv).run()


if __name__ == "__main__":
    sys.exit(main())
