"""
telemetry_bridge.py — live GPU/CPU telemetry as Qt properties
=============================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session A)

A PySide6 QObject that polls the local hardware and exposes four live values as
Qt Properties with NOTIFY signals, ready for QML binding (the presence orb in a
later session binds to these). Built standalone here — no QML/orb code yet.

Data sources:
  - GPU (RTX A2000, single card): pynvml / NVML. Single-GPU only by design — BB
    has exactly one card, so no device enumeration loop.
  - CPU: psutil.

Design notes:
  - Polling runs on a QTimer in the owning thread's event loop — NOT a worker
    thread. NVML and psutil calls here are cheap (sub-millisecond), so a 1s
    timer tick won't block the UI, and this keeps the bridge free of cross-
    thread signal/GIL complications for QML binding.
  - NVML is initialised once and degrades gracefully: if init fails (no driver,
    no GPU, NVML library missing) it's logged ONCE and the GPU-only properties
    report 0.0 / False forever after, while the CPU property keeps working. The
    bridge never raises out of a poll.
  - structlog for all error/warning paths. No print() in library code (the
    __main__ smoke test is the only place values are printed, intentionally).
"""

from __future__ import annotations

import sys

import psutil
import structlog

# pynvml is the import name the BB stack uses (installed via `pip install
# pynvml`). Newer wheels alias it to nvidia-ml-py; either provides the same
# `pynvml` module. Import is wrapped so a box without it still constructs the
# bridge (GPU props just stay at 0/False).
try:
    import pynvml  # type: ignore
    _PYNVML_IMPORTED = True
except Exception:  # ImportError, or a broken partial install
    pynvml = None  # type: ignore
    _PYNVML_IMPORTED = False

from PySide6.QtCore import QObject, QTimer, Property, Signal

log = structlog.get_logger("jarvis.telemetry")

# cpuSpillover heuristic tuning. Spillover = "a model fell out of VRAM and is now
# running partly on CPU", which shows up as VRAM pinned near full AND a
# correlated jump in CPU load versus the recent baseline.
_VRAM_SATURATION_PCT = 95.0   # VRAM considered saturated above this
_CPU_SPIKE_DELTA = 25.0       # CPU jump (pts) over the rolling baseline = spike
_ROLLING_WINDOW = 5           # samples kept for the CPU baseline


class TelemetryBridge(QObject):
    """Live telemetry exposed as bindable Qt properties.

    Properties (all NOTIFY-backed so QML re-binds on change):
      gpuLoad      float  0-100  GPU utilization %
      vramPct      float  0-100  VRAM used / VRAM total %
      cpuLoad      float  0-100  overall CPU utilization %
      cpuSpillover bool          VRAM saturated AND CPU spiked vs. baseline
      orbState     int   0-3     presence-orb UI state, set FROM Python (not
                                 measured): 0 idle | 1 listening | 2 processing |
                                 3 spillover. Carried on the bridge so the orb has
                                 one binding source for measured AND presentation
                                 state; written by main.py's demo timer now and by
                                 jarvis-core (voice/inference triggers) later.
      tokensPerSec float        inference throughput of the active model, set FROM
                                 Python (not measured here). NOTHING populates this
                                 yet — no jarvis-core completion hook exists. It
                                 stays 0.0 until a future session calls
                                 set_tokens_per_sec() on each inference. Exposed now
                                 so the StressPanel (Session G) has a real binding
                                 target instead of a fabricated placeholder.
      activeModel  str          name of the model tokensPerSec refers to (e.g.
                                 "mistral", "llama3"). Same posture as tokensPerSec:
                                 set FROM Python by a future jarvis-core hook,
                                 empty "" until then.
    """

    gpuLoadChanged = Signal()
    vramPctChanged = Signal()
    cpuLoadChanged = Signal()
    cpuSpilloverChanged = Signal()
    orbStateChanged = Signal()
    tokensPerSecChanged = Signal()
    activeModelChanged = Signal()

    def __init__(self, poll_interval_ms: int = 1000, parent: QObject | None = None):
        super().__init__(parent)

        self._gpu_load: float = 0.0
        self._vram_pct: float = 0.0
        self._cpu_load: float = 0.0
        self._cpu_spillover: bool = False
        # Presentation state, not telemetry — set from Python (see class docstring).
        self._orb_state: int = 0
        # Inference throughput, set from Python — NOT measured by this bridge and
        # NOT yet populated by anything. Stays at the defaults below until a future
        # jarvis-core completion hook calls set_tokens_per_sec()/set_active_model().
        self._tokens_per_sec: float = 0.0
        self._active_model: str = ""

        # Rolling CPU samples for the spillover baseline.
        self._cpu_history: list[float] = []

        # NVML state. Stays False (and GPU props stay 0) if init fails.
        self._nvml_ready: bool = False
        self._gpu_handle = None
        self._init_nvml()

        # psutil's first cpu_percent() call returns 0.0 (it needs a prior
        # reference point); prime it once so the first real tick is meaningful.
        psutil.cpu_percent(interval=None)

        # QTimer drives polling on the owning event loop — no worker thread.
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self._poll)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Begin polling. Safe to call once an event loop exists."""
        self._poll()          # take an immediate first sample
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _init_nvml(self) -> None:
        """Initialise NVML once. Any failure logs ONCE and leaves the GPU props
        at their 0/False defaults — CPU telemetry is unaffected."""
        if not _PYNVML_IMPORTED:
            log.warning("telemetry.nvml_unavailable",
                        reason="pynvml not importable — GPU telemetry disabled, "
                               "CPU telemetry continues")
            return
        try:
            pynvml.nvmlInit()
            # Single GPU by design — index 0 is the A2000.
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_ready = True
            log.info("telemetry.nvml_ready", device_index=0)
        except Exception as e:  # NVMLError or anything else NVML throws
            self._nvml_ready = False
            self._gpu_handle = None
            log.warning("telemetry.nvml_init_failed",
                        error=str(e),
                        effect="GPU props report 0/false; CPU telemetry continues")

    # ── polling ───────────────────────────────────────────────────────────────
    def _poll(self) -> None:
        """One sample of all sources. Never raises; per-source failures degrade
        that source only."""
        self._sample_gpu()
        self._sample_cpu()
        self._recompute_spillover()

    def _sample_gpu(self) -> None:
        if not self._nvml_ready or self._gpu_handle is None:
            # Degraded path — GPU props report 0 but still emit each tick so
            # chart consumers keep scrolling (a flat 0 line, honestly rendered).
            self._set_gpu_load(0.0)
            self._set_vram_pct(0.0)
            return
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu_handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self._gpu_handle)
            gpu_load = float(util.gpu)
            vram_pct = (float(mem.used) / float(mem.total) * 100.0) if mem.total else 0.0
            self._set_gpu_load(gpu_load)
            self._set_vram_pct(vram_pct)
        except Exception as e:
            # A transient NVML read error shouldn't crash the UI; report 0 for
            # this tick and keep going. Logged at debug to avoid log spam if the
            # GPU is briefly busy/unavailable.
            log.debug("telemetry.gpu_read_failed", error=str(e))
            self._set_gpu_load(0.0)
            self._set_vram_pct(0.0)

    def _sample_cpu(self) -> None:
        try:
            cpu = float(psutil.cpu_percent(interval=None))
        except Exception as e:
            log.debug("telemetry.cpu_read_failed", error=str(e))
            return
        self._set_cpu_load(cpu)
        # Maintain the rolling baseline AFTER reading current (baseline excludes
        # the current sample so a spike is measured against recent history).
        self._cpu_history.append(cpu)
        if len(self._cpu_history) > _ROLLING_WINDOW:
            self._cpu_history.pop(0)

    def _recompute_spillover(self) -> None:
        """Spillover heuristic: VRAM saturated AND current CPU load jumped
        meaningfully above the rolling baseline. Simplest form that's still real
        signal — not a constant False, not a raw threshold that fires on any
        busy CPU."""
        if len(self._cpu_history) < 2:
            self._set_cpu_spillover(False)
            return
        # Baseline = mean of the history EXCLUDING the most recent sample.
        baseline = sum(self._cpu_history[:-1]) / len(self._cpu_history[:-1])
        current = self._cpu_history[-1]
        spiked = (current - baseline) >= _CPU_SPIKE_DELTA
        saturated = self._vram_pct > _VRAM_SATURATION_PCT
        self._set_cpu_spillover(bool(saturated and spiked))

    # ── setters ───────────────────────────────────────────────────────────────
    # The three MEASURED metrics (gpuLoad, vramPct, cpuLoad) emit on EVERY poll
    # tick, unconditionally. Consumers plotting time series (stress_panel.py)
    # append one point per signal — change-gated emission froze the VRAM strip
    # chart whenever NVML reported byte-identical usage across ticks (GPU/CPU
    # values jitter every tick, so only VRAM ever hit the gate). A 1 Hz signal
    # to a lightweight slot is negligible; a frozen chart is not.
    # State-like properties (cpuSpillover, orbState, tokensPerSec, activeModel)
    # keep emit-on-change: they are event edges, not samples.
    def _set_gpu_load(self, v: float) -> None:
        self._gpu_load = v
        self.gpuLoadChanged.emit()

    def _set_vram_pct(self, v: float) -> None:
        self._vram_pct = v
        self.vramPctChanged.emit()

    def _set_cpu_load(self, v: float) -> None:
        self._cpu_load = v
        self.cpuLoadChanged.emit()

    def _set_cpu_spillover(self, v: bool) -> None:
        if v != self._cpu_spillover:
            self._cpu_spillover = v
            self.cpuSpilloverChanged.emit()

    def set_orb_state(self, v: int) -> None:
        """Set the orb presentation state (0-3). Convenience for Python callers;
        QML/Python can equally assign the `orbState` property directly."""
        self._set_orb_state(v)

    def _set_orb_state(self, v: int) -> None:
        iv = int(v)
        if iv != self._orb_state:
            self._orb_state = iv
            self.orbStateChanged.emit()

    def set_tokens_per_sec(self, v: float) -> None:
        """Set the active model's inference throughput. For a FUTURE jarvis-core
        completion hook to call — nothing in the current tree calls this, so the
        property reads 0.0 in this session."""
        self._set_tokens_per_sec(v)

    def _set_tokens_per_sec(self, v: float) -> None:
        fv = float(v)
        if fv != self._tokens_per_sec:
            self._tokens_per_sec = fv
            self.tokensPerSecChanged.emit()

    def set_active_model(self, v: str) -> None:
        """Set the model name tokensPerSec refers to. Same future-hook posture as
        set_tokens_per_sec — unwired this session."""
        self._set_active_model(v)

    def _set_active_model(self, v: str) -> None:
        sv = str(v)
        if sv != self._active_model:
            self._active_model = sv
            self.activeModelChanged.emit()

    # ── Qt Properties (QML-bindable) ──────────────────────────────────────────
    @Property(float, notify=gpuLoadChanged)
    def gpuLoad(self) -> float:
        return self._gpu_load

    @Property(float, notify=vramPctChanged)
    def vramPct(self) -> float:
        return self._vram_pct

    @Property(float, notify=cpuLoadChanged)
    def cpuLoad(self) -> float:
        return self._cpu_load

    @Property(bool, notify=cpuSpilloverChanged)
    def cpuSpillover(self) -> bool:
        return self._cpu_spillover

    @Property(int, notify=orbStateChanged)
    def orbState(self) -> int:
        return self._orb_state

    @orbState.setter
    def orbState(self, v: int) -> None:
        self._set_orb_state(v)

    @Property(float, notify=tokensPerSecChanged)
    def tokensPerSec(self) -> float:
        return self._tokens_per_sec

    @tokensPerSec.setter
    def tokensPerSec(self, v: float) -> None:
        self._set_tokens_per_sec(v)

    @Property(str, notify=activeModelChanged)
    def activeModel(self) -> str:
        return self._active_model

    @activeModel.setter
    def activeModel(self, v: str) -> None:
        self._set_active_model(v)


# ── MANUAL SMOKE TEST ─────────────────────────────────────────────────────────
# Run under a minimal QCoreApplication loop, print the four values once per
# second for 10 seconds, then exit. On a box without a GPU/NVML this exercises
# the degraded path (GPU props = 0, cpuSpillover = False) without crashing.
if __name__ == "__main__":
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)
    bridge = TelemetryBridge(poll_interval_ms=1000)

    ticks = {"n": 0}

    def _report() -> None:
        ticks["n"] += 1
        print(
            f"[{ticks['n']:2d}/10] "
            f"gpuLoad={bridge.gpuLoad:5.1f}%  "
            f"vramPct={bridge.vramPct:5.1f}%  "
            f"cpuLoad={bridge.cpuLoad:5.1f}%  "
            f"cpuSpillover={bridge.cpuSpillover}"
        )
        if ticks["n"] >= 10:
            bridge.stop()
            app.quit()

    bridge.start()
    report_timer = QTimer()
    report_timer.setInterval(1000)
    report_timer.timeout.connect(_report)
    report_timer.start()

    sys.exit(app.exec())
