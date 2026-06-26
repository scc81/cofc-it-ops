"""
stress_panel.py — live GPU/CPU stress instrumentation panel
===========================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session G; restores the missing Session B
artifact that Sessions C/D already import).

A QWidget that VISUALISES the shared TelemetryBridge — it is a pure consumer:

  - It binds to the bridge's existing NOTIFY signals (gpuLoadChanged,
    vramPctChanged, cpuLoadChanged, cpuSpilloverChanged) and to the two
    Session-G-added throughput signals (tokensPerSecChanged, activeModelChanged).
  - It adds NO QTimer, NO pynvml.init(), NO psutil poll loop of its own. The
    bridge is the single polling source (see main.py / telemetry_bridge.py); this
    panel only appends each new value to its plot buffers when notified.
  - It is read-only w.r.t. the bridge: it calls no setter on it.

FERPA: this panel displays ONLY local hardware telemetry (GPU/CPU/VRAM %, and a
not-yet-fed tokens/sec readout). It touches no device/user/security data and makes
no outbound call, so it needs NO TOOL_DATA_LOCAL entry and has no FERPA-firewall
concern. Stated here so a future audit doesn't have to rediscover it.

Layout (jarvis_phase3_native_ui.md §4): three scrolling 0-100% strip charts
stacked vertically (VRAM%, GPU util%, CPU util%), then a row with the per-model
tokens/sec readout and a stress-score badge.
"""

from __future__ import annotations

import time
from collections import deque

import pyqtgraph as pg
import structlog
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

log = structlog.get_logger("jarvis.stress_panel")

# Rolling window shown in each strip chart. The bridge polls at ~1s, so 120s of
# history is ~120 points per curve — bounded, never unbounded: points older than
# the window are pruned on every append (by wall-clock age, not a fixed count, so
# a sparse change-only signal stream still yields a correct time window).
_WINDOW_SECONDS = 120.0

# Most-recent spillover samples used for the rolling spillover %. This is a display
# smoothing of bridge.cpuSpillover values this panel ALREADY received — NOT a
# re-implementation of the spillover heuristic (that lives in telemetry_bridge.py).
_SPILLOVER_WINDOW = 30

# CofC brand palette for the two brand-coloured curves; a neutral steel tone for
# the third (a data line needing a distinct hue, not a status colour). The badge's
# spillover state uses a deliberately OFF-palette amber so an alert never reads as
# a branding choice (build UI rule).
_PEN_VRAM = "#660000"   # maroon (brand)
_PEN_GPU = "#BFA87C"    # gold (brand)
_PEN_CPU = "#4C8FB5"    # neutral steel-blue (data line, not a status colour)
_AMBER = "#E8A33D"      # off-palette alert
_GREY = "#888888"       # unknown / no-data


class StressPanel(QWidget):
    """Live telemetry panel. Constructed as `StressPanel(bridge)` by main.py.

    Parameters
    ----------
    bridge : TelemetryBridge
        The SAME bridge instance main.py constructs and shares with the orb and
        gate queue. This panel observes it; it never mutates it.
    """

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        # Rolling spillover sample buffer (bools), filled once per CPU tick (the
        # most frequent telemetry tick) — bounded by maxlen, no new data source.
        self._spill_samples: deque = deque(maxlen=_SPILLOVER_WINDOW)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("<b>System Stress Monitor</b> — live GPU/CPU telemetry"))

        # Three strip charts. Each is (PlotWidget, curve, (t, value)-deque).
        self._vram_plot, self._vram_curve, self._vram_pts = self._make_chart("VRAM", _PEN_VRAM)
        self._gpu_plot, self._gpu_curve, self._gpu_pts = self._make_chart("GPU util", _PEN_GPU)
        self._cpu_plot, self._cpu_curve, self._cpu_pts = self._make_chart("CPU util", _PEN_CPU)
        root.addWidget(self._vram_plot, 1)
        root.addWidget(self._gpu_plot, 1)
        root.addWidget(self._cpu_plot, 1)

        # Readout row: tokens/sec (left, stretches) + stress-score badge (right).
        row = QHBoxLayout()
        self._tps_label = QLabel()
        self._badge = QLabel()
        row.addWidget(self._tps_label, 1)
        row.addWidget(self._badge, 0)
        root.addLayout(row)
        self._refresh_tps()
        self._refresh_badge()

        # Bind to the bridge's existing NOTIFY signals — the ONLY update trigger;
        # no QTimer here. Each slot reads the current property and appends it.
        bridge.vramPctChanged.connect(self._on_vram)
        bridge.gpuLoadChanged.connect(self._on_gpu)
        bridge.cpuLoadChanged.connect(self._on_cpu)
        bridge.cpuSpilloverChanged.connect(self._refresh_badge)
        bridge.tokensPerSecChanged.connect(self._refresh_tps)
        bridge.activeModelChanged.connect(self._refresh_tps)

        # Seed from whatever the bridge already holds — the panel may be built
        # after the bridge has taken its first sample.
        self._on_vram()
        self._on_gpu()
        self._on_cpu()

        log.info("stress_panel.ready", window_seconds=_WINDOW_SECONDS,
                 spillover_window=_SPILLOVER_WINDOW)

    # ── chart construction ──────────────────────────────────────────────────────
    def _make_chart(self, title: str, pen: str):
        """One static-axis 0-100% strip chart over a fixed time window."""
        plot = pg.PlotWidget()
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=False, y=False)
        plot.setYRange(0, 100)
        plot.setXRange(-_WINDOW_SECONDS, 0, padding=0)
        plot.setLabel("left", title, units="%")
        plot.setLabel("bottom", "seconds ago")
        plot.showGrid(x=False, y=True, alpha=0.2)
        curve = plot.plot(pen=pg.mkPen(pen, width=2))
        return plot, curve, deque()

    # ── value plumbing ──────────────────────────────────────────────────────────
    @staticmethod
    def _safe(v) -> float:
        """Coerce a bridge property to float, tolerating the bridge's documented
        NVML-failure fallback (0.0/None) without crashing the panel."""
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _push(self, curve, pts: deque, value: float) -> None:
        now = time.monotonic()
        pts.append((now, float(value)))
        cutoff = now - _WINDOW_SECONDS
        while pts and pts[0][0] < cutoff:
            pts.popleft()
        # x = seconds-ago (0 = newest, negative = older) → shared window across all
        # three charts without fighting pyqtgraph over linked timestamp axes.
        xs = [t - now for (t, _v) in pts]
        ys = [v for (_t, v) in pts]
        curve.setData(xs, ys)

    # ── bridge slots ────────────────────────────────────────────────────────────
    def _on_vram(self) -> None:
        self._push(self._vram_curve, self._vram_pts, self._safe(self._bridge.vramPct))

    def _on_gpu(self) -> None:
        self._push(self._gpu_curve, self._gpu_pts, self._safe(self._bridge.gpuLoad))

    def _on_cpu(self) -> None:
        self._push(self._cpu_curve, self._cpu_pts, self._safe(self._bridge.cpuLoad))
        # Sample the bridge's CURRENT spillover flag once per CPU tick for the
        # rolling %. Uses a value the panel already has — no new heuristic.
        self._spill_samples.append(bool(self._bridge.cpuSpillover))
        self._refresh_badge()

    # ── readouts ────────────────────────────────────────────────────────────────
    def _refresh_tps(self) -> None:
        """tokens/sec is wired to display but NOT yet fed real data — no jarvis-core
        completion hook calls the bridge setter this session, so it reads 0.0 and
        an empty model. Rendered honestly (never a fabricated/placeholder value)."""
        tps = self._safe(self._bridge.tokensPerSec)
        model = self._bridge.activeModel or "—"
        self._tps_label.setText(f"tok/s: {tps:.1f}   (model: {model})")

    def _refresh_badge(self) -> None:
        """Stress-score badge: the bridge's current spillover state plus a rolling
        % of recent samples flagged. Pure display of bridge.cpuSpillover values —
        the panel does not re-derive spillover."""
        if not self._spill_samples:
            self._badge.setText("Unknown  (no samples yet)")
            self._badge.setStyleSheet(f"color: {_GREY}; font-weight: bold;")
            return
        n = len(self._spill_samples)
        pct = 100.0 * sum(1 for s in self._spill_samples if s) / n
        if bool(self._bridge.cpuSpillover):
            text, color = "CPU-spillover", _AMBER
        else:
            text, color = "GPU-primary", _PEN_GPU
        self._badge.setText(f"{text}  ({pct:.0f}% spill / last {n})")
        self._badge.setStyleSheet(f"color: {color}; font-weight: bold;")
