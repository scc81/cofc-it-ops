"""
orb_widget.py — embed the Qt Quick 3D presence orb (orb.qml) as a QWidget
=========================================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session C)

Wraps orb.qml in a QQuickWidget so main.py's QMainWindow can drop the orb into an
ordinary QVBoxLayout. QQuickWidget (vs a QQuickView in createWindowContainer) is
chosen because it behaves as a normal QWidget — no native-window-container z-order
or focus caveats to manage against the Session A/B layout. Trade-off: Qt Quick 3D
under QQuickWidget composites through an offscreen FBO (a little more overhead than
a top-level QQuickView); acceptable here, the orb is the only 3D surface and its
shaders are deliberately cheap.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtQuickWidgets import QQuickWidget

# Importing the geometry module registers IcosahedronGeometry into the QML type
# system (via @QmlElement) under `import JarvisOrb`. This MUST happen before
# orb.qml is loaded, hence a module-level side-effect import.
from orb import orb_geometry  # noqa: F401  (imported for QML type registration)

_ORB_DIR = os.path.dirname(os.path.abspath(__file__))


class OrbView(QQuickWidget):
    """QQuickWidget hosting orb.qml.

    The injected TelemetryBridge is exposed to QML as the `telemetry` context
    property — the single source of truth the orb binds to (gpuLoad / cpuLoad /
    cpuSpillover / orbState). State is driven Python-side by setting
    bridge.orbState; the orb reacts via its NOTIFY bindings.
    """

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.rootContext().setContextProperty("telemetry", bridge)
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        # Transparent clear so the orb's transparent SceneEnvironment lets the
        # host window background show through around the mesh.
        self.setClearColor(QColor(0, 0, 0, 0))
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
        self.setSource(QUrl.fromLocalFile(os.path.join(_ORB_DIR, "orb.qml")))
