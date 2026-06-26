"""JARVIS desktop stress-monitoring package (Phase 3, Session B/G).

Holds the live instrumentation panel (stress_panel.StressPanel) and the
persistent inference-log helpers (stress_log). The panel consumes the shared
TelemetryBridge; it never polls NVML/psutil itself.
"""
