"""
stress_log.py — persistent SQLite inference log (STUB — unwired infrastructure)
==============================================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session G).

jarvis_phase3_native_ui.md §5 calls for a persistent per-query log: one row per
completed inference recording the model, how long it ran, its tokens/sec, the peak
VRAM it reached, and whether it spilled to CPU.

THIS IS A STUB, same posture as Session F's sso_gate.py: schema + insert are fully
callable, but NOTHING wires them in yet. Hooking log_inference() into
jarvis_core.py on each completion is explicitly OUT OF SCOPE this session — that
requires a jarvis_core.py edit this prompt does not authorise. A future session
adds the caller; until then this collects no real data.

FERPA: model name + hardware/throughput numbers only — no device/user/security
data. No outbound call. No TOOL_DATA_LOCAL entry required.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import structlog

log = structlog.get_logger("jarvis.stress_log")

# Exact column set from §5: model, duration, tokens/sec, peak VRAM, spillover flag
# per query. `id`/`ts` are bookkeeping columns, not part of that telemetry set.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,           -- ISO8601 UTC, when the row was logged
    model         TEXT    NOT NULL,           -- e.g. "mistral", "llama3"
    duration_s    REAL    NOT NULL,           -- wall-clock seconds of the inference
    tokens_per_sec REAL   NOT NULL,           -- throughput
    peak_vram_pct REAL    NOT NULL,           -- highest VRAM % seen during the run
    spillover     INTEGER NOT NULL            -- 0/1 — did it spill to CPU
);
"""

# Insertable fields (id/ts are handled automatically). The future jarvis-core hook
# passes these by keyword: log_inference(path, model=..., duration_s=..., ...).
_FIELDS = ("model", "duration_s", "tokens_per_sec", "peak_vram_pct", "spillover")


def create_log_db(path) -> None:
    """Create the inference_log table at `path` (idempotent). Parent dirs are
    created if missing."""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    log.info("stress_log.db_ready", path=str(p))


def log_inference(path, **fields) -> None:
    """Insert one completed-inference row. STUB: no caller is wired yet.

    Required keyword fields (per §5): model, duration_s, tokens_per_sec,
    peak_vram_pct, spillover. `ts` is set automatically to now (UTC ISO8601).
    """
    from datetime import datetime, timezone

    missing = [f for f in _FIELDS if f not in fields]
    if missing:
        raise ValueError(f"log_inference missing required field(s): {missing}")

    spillover = 1 if fields["spillover"] else 0
    conn = sqlite3.connect(str(Path(path)))
    try:
        conn.execute(
            "INSERT INTO inference_log "
            "(ts, model, duration_s, tokens_per_sec, peak_vram_pct, spillover) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                str(fields["model"]),
                float(fields["duration_s"]),
                float(fields["tokens_per_sec"]),
                float(fields["peak_vram_pct"]),
                spillover,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("stress_log.row_inserted", model=fields["model"], spillover=bool(spillover))
