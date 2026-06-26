"""
tray_icon.py — system tray icon + context menu for the JARVIS desktop shell
===========================================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session A)

Thin QSystemTrayIcon wrapper. Context menu: Show/Hide JARVIS, separator, Quit.
Uses a generated placeholder icon (solid CofC maroon square with a white "J") —
real icon art is a later session, intentionally out of scope here.

The window itself owns show/hide/close-to-tray behavior; this class just exposes
the signals (toggle requested, quit requested) and lets main.py wire them.
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

log = structlog.get_logger("jarvis.tray")

COFC_MAROON = "#660000"   # CofC brand maroon — placeholder icon fill


def _make_placeholder_icon(size: int = 64) -> QIcon:
    """Generate a solid maroon square with a centered white 'J'. Avoids shipping
    binary icon art this session while still giving the tray a recognizable
    glyph."""
    pix = QPixmap(size, size)
    pix.fill(QColor(COFC_MAROON))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(int(size * 0.55))
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "J")
    painter.end()
    return QIcon(pix)


class JarvisTrayIcon(QSystemTrayIcon):
    """System tray presence for JARVIS.

    Signals:
      toggleRequested — user wants to show/hide the window (menu item or a
                        left-click activation on the tray icon)
      quitRequested   — user chose Quit (the ONLY path that exits the app)
    """

    toggleRequested = Signal()
    quitRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(_make_placeholder_icon(), parent)
        self.setToolTip("JARVIS — CofC IT")

        menu = QMenu()
        self._toggle_action = QAction("Show/Hide JARVIS", menu)
        self._toggle_action.triggered.connect(self.toggleRequested.emit)
        menu.addAction(self._toggle_action)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quitRequested.emit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

        # Left-click (Trigger) toggles the window; the context menu handles the
        # right-click case on its own.
        self.activated.connect(self._on_activated)
        log.info("tray.ready")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggleRequested.emit()
