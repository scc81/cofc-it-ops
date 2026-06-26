"""
tools/briefing.py — Morning Briefing Aggregator
================================================
CofCITIP — Pulls fleet, compliance, alert, and patch data from every
connector and produces both a structured dict and a GLaDOS-ready spoken
summary.

Design decisions (inline per session rules):
- Each connector call is individually wrapped: a failing connector degrades
  to a "data unavailable" section rather than killing the whole briefing.
  Morning briefings must NEVER hard-fail — partial intel beats silence.
- Connectors are imported lazily inside _gather() so a missing optional
  dependency in one tool doesn't break import of this module.
- "trend" in compliance_summary is a placeholder until ChromaDB/sqlite
  history exists (Phase 2): today it compares against a static baseline
  from config (BRIEFING_BASELINE_INTUNE_PCT / _JAMF_PCT) and emits
  improving/steady/declining. Wire to real history later — interface won't
  change.
- Spoken strings: plain conversational sentences, no markdown, 1-2 per
  section, numbers rounded — these go straight to TTS.

Tool functions (signature: (params: dict) -> dict):
  generate(params)                — params: {} (none needed)
  generate_spoken_summary(brief)  — helper, takes the briefing dict,
                                    returns a single spoken string

jarvis_core compatibility: execute_tool() calls briefing.generate() — the
returned dict includes a "spoken" key the summarizer can read verbatim.

Mock mode: inherited — each connector honors JARVIS_MOCK itself; this
module never needs its own mock branch.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel

log = structlog.get_logger("jarvis.tools.briefing")

MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"

# Static trend baselines until real history lands (Phase 2 — see header).
BASELINE_INTUNE_PCT = float(os.getenv("BRIEFING_BASELINE_INTUNE_PCT", "90"))
BASELINE_JAMF_PCT   = float(os.getenv("BRIEFING_BASELINE_JAMF_PCT", "90"))
TREND_BAND          = 2.0  # +/- pct points considered "steady"


class BriefingParams(BaseModel):
    """No params today — model exists so the (params: dict) -> dict contract
    and future extension (e.g. {"sections": [...]}) stay uniform."""
    pass


# ── SAFE CONNECTOR CALLS ──────────────────────────────────────────────────────
def _safe(label: str, fn, params: dict) -> dict | None:
    """Run one connector call; on ANY failure log it and return None so the
    briefing degrades section-by-section instead of failing whole."""
    try:
        return fn(params)
    except Exception as e:  # noqa: BLE001 — deliberate catch-all at boundary
        log.error("briefing.section_failed", section=label, error=str(e))
        return None


def _gather() -> dict:
    """Lazy-import connectors and pull raw data. Each value may be None."""
    raw: dict = {}

    try:
        from tools import intune
        raw["intune_compliance"] = _safe(
            "intune_compliance", intune.query_compliance, {"filter": "all"})
    except Exception as e:
        log.error("briefing.import_failed", module="intune", error=str(e))
        raw["intune_compliance"] = None

    try:
        from tools import jamf
        raw["jamf_fleet"] = _safe("jamf_fleet", jamf.query_fleet, {})
        raw["jamf_patch"] = _safe("jamf_patch", jamf.query_patch_status, {})
    except Exception as e:
        log.error("briefing.import_failed", module="jamf", error=str(e))
        raw["jamf_fleet"] = raw["jamf_patch"] = None

    try:
        from tools import taegis
        raw["taegis_alerts"] = _safe(
            "taegis_alerts", taegis.get_alerts,
            {"severity": "all", "hours": 24})
    except Exception as e:
        log.error("briefing.import_failed", module="taegis", error=str(e))
        raw["taegis_alerts"] = None

    return raw


# ── SYSTEM HEALTH (Part 3) ────────────────────────────────────────────────────
def health_summary() -> dict:
    """Invoke health_check() across all six connectors and return a flat
    {connector: status} map for the briefing's system_health key. Each call is
    individually guarded — a connector that fails to import or raises is
    reported as 'down' rather than breaking the briefing."""
    # (module_path, attr_name, label) for each connector.
    targets = [
        ("tools.intune", "intune"),
        ("tools.jamf", "jamf"),
        ("tools.servicenow", "servicenow"),
        ("tools.taegis", "taegis"),
        ("tools.teams", "teams"),
        ("tools.package_pipeline", "package_pipeline"),
    ]
    summary: dict[str, str] = {}
    import importlib
    for module_path, label in targets:
        try:
            mod = importlib.import_module(module_path)
            hc = mod.health_check()
            summary[label] = hc.get("status", "down")
        except Exception as e:  # noqa: BLE001 — boundary catch-all
            log.error("briefing.health_failed", connector=label, error=str(e))
            summary[label] = "down"
    return summary


# ── SECTION BUILDERS ──────────────────────────────────────────────────────────
def _trend(current: float, baseline: float) -> str:
    if current >= baseline + TREND_BAND:
        return "improving"
    if current <= baseline - TREND_BAND:
        return "declining"
    return "steady"


def _fleet_health(intune_c: dict | None, jamf_f: dict | None) -> dict:
    win_total = (intune_c or {}).get("total_devices", 0)
    mac_total = (jamf_f or {}).get("total_macs", 0)
    compliant = (intune_c or {}).get("compliant", 0)
    non_compliant = (intune_c or {}).get("non_compliant", 0)
    return {
        "total": win_total + mac_total,
        "compliant": compliant,
        "non_compliant": non_compliant,
        "platform_breakdown": {
            "windows": win_total,
            "mac": mac_total,
            "mac_stale": (jamf_f or {}).get("stale_count", 0),
        },
        "available": intune_c is not None or jamf_f is not None,
    }


def _compliance_summary(intune_c: dict | None, jamf_f: dict | None) -> dict:
    intune_pct = (intune_c or {}).get("compliance_rate_pct", 0.0)
    # Jamf connector has no compliance pct — derive "healthy" as non-stale.
    if jamf_f and jamf_f.get("total_macs"):
        jamf_pct = round(
            (jamf_f["total_macs"] - jamf_f.get("stale_count", 0))
            / jamf_f["total_macs"] * 100, 1)
    else:
        jamf_pct = 0.0
    # Combined trend: average distance from the two baselines.
    avg_cur = (intune_pct + jamf_pct) / 2
    avg_base = (BASELINE_INTUNE_PCT + BASELINE_JAMF_PCT) / 2
    return {
        "intune_pct": intune_pct,
        "jamf_pct": jamf_pct,
        "trend": _trend(avg_cur, avg_base),
        "available": intune_c is not None or jamf_f is not None,
    }


def _overnight_alerts(taegis_a: dict | None) -> dict:
    if taegis_a is None:
        return {"critical": 0, "high": 0, "medium": 0, "top_3": [],
                "available": False}
    sev = taegis_a.get("by_severity", {})
    top3 = [{"title": a.get("title", ""),
             "severity": a.get("severity", ""),
             "host": a.get("host", "")}
            for a in taegis_a.get("alerts", [])[:3]]
    return {
        "critical": sev.get("critical", 0),
        "high": sev.get("high", 0),
        "medium": sev.get("medium", 0),
        "top_3": top3,
        "available": True,
    }


def _patch_status(jamf_p: dict | None) -> dict:
    if jamf_p is None:
        return {"pending": 0, "failed": 0, "up_to_date": 0, "available": False}
    titles = jamf_p.get("patch_status", [])
    up_to_date = sum(1 for t in titles if t.get("up_to_date_pct", 0) >= 90)
    # Jamf patch summaries don't expose per-device failures — "pending" here
    # means titles below the 90% threshold; "failed" stays 0 until the
    # Phase 2 deployment-log integration distinguishes pending vs failed.
    pending = jamf_p.get("titles_below_90pct", 0)
    return {
        "pending": pending,
        "failed": 0,
        "up_to_date": up_to_date,
        "worst_title": (titles[0].get("title") if titles else None),
        "worst_pct": (titles[0].get("up_to_date_pct") if titles else None),
        "available": True,
    }


# ── SPOKEN SUMMARY ────────────────────────────────────────────────────────────
def generate_spoken_summary(briefing: dict) -> str:
    """Convert the structured briefing dict into one GLaDOS-ready string.
    Rules: plain sentences, no markdown, 1-2 sentences per section."""
    parts: list[str] = ["Good morning. Here's your briefing."]

    fh = briefing.get("fleet_health", {})
    if fh.get("available"):
        pb = fh.get("platform_breakdown", {})
        parts.append(
            f"Fleet health: {fh.get('total', 0)} devices total, "
            f"{pb.get('windows', 0)} Windows and {pb.get('mac', 0)} Macs. "
            f"{fh.get('non_compliant', 0)} Windows devices are out of "
            f"compliance."
        )
        if pb.get("mac_stale"):
            parts.append(f"{pb['mac_stale']} Macs haven't checked in recently.")
    else:
        parts.append("Fleet health data is unavailable this morning.")

    cs = briefing.get("compliance_summary", {})
    if cs.get("available"):
        parts.append(
            f"Compliance is at {round(cs.get('intune_pct', 0))} percent on "
            f"Intune and {round(cs.get('jamf_pct', 0))} percent on Jamf, "
            f"trending {cs.get('trend', 'steady')}."
        )

    oa = briefing.get("overnight_alerts", {})
    if oa.get("available"):
        crit, high, med = (oa.get("critical", 0), oa.get("high", 0),
                           oa.get("medium", 0))
        if crit or high:
            parts.append(
                f"Security: {crit} critical and {high} high severity alerts "
                f"overnight, plus {med} medium."
            )
            if oa.get("top_3"):
                top = oa["top_3"][0]
                parts.append(
                    f"Top alert: {top.get('title', 'unknown')} on "
                    f"{top.get('host', 'an unknown host')}."
                )
        elif med:
            parts.append(f"Security: quiet night, just {med} medium "
                         f"severity alerts to review.")
        else:
            parts.append("Security: no overnight alerts. Quiet night.")
    else:
        parts.append("Security alert data is unavailable — check Taegis "
                     "directly.")

    ps = briefing.get("patch_status", {})
    if ps.get("available"):
        if ps.get("pending"):
            tail = ""
            if ps.get("worst_title"):
                tail = (f" Worst is {ps['worst_title']} at "
                        f"{round(ps.get('worst_pct') or 0)} percent.")
            parts.append(
                f"Patching: {ps['pending']} software titles are below ninety "
                f"percent up to date.{tail}"
            )
        else:
            parts.append("Patching looks good — all titles above ninety "
                         "percent.")

    parts.append("That's the briefing.")
    return " ".join(parts)


# ── TOOL FUNCTION ─────────────────────────────────────────────────────────────
def generate(raw_params: dict) -> dict:
    """Build the full morning briefing: structured dict + spoken string."""
    BriefingParams(**(raw_params or {}))  # validates contract; no params today
    log.info("tool.start", tool="morning_briefing", mock=MOCK_MODE)

    raw = _gather()

    briefing = {
        "source": "briefing",
        "mock": MOCK_MODE,
        "fleet_health": _fleet_health(raw["intune_compliance"],
                                      raw["jamf_fleet"]),
        "compliance_summary": _compliance_summary(raw["intune_compliance"],
                                                  raw["jamf_fleet"]),
        "overnight_alerts": _overnight_alerts(raw["taegis_alerts"]),
        "patch_status": _patch_status(raw["jamf_patch"]),
        "system_health": health_summary(),  # Part 3: connector health map
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    briefing["spoken"] = generate_spoken_summary(briefing)

    sections_ok = sum(1 for k in ("fleet_health", "compliance_summary",
                                  "overnight_alerts", "patch_status")
                      if briefing[k].get("available"))
    log.info("tool.success", tool="morning_briefing",
             sections_available=sections_ok, sections_total=4)
    return briefing


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.briefing --mock           (full briefing, JSON to stdout)
# python -m tools.briefing --mock --spoken  (spoken string only)
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Briefing test harness")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--spoken", action="store_true",
                        help="print only the spoken summary")
    args = parser.parse_args()

    if args.mock:
        os.environ["JARVIS_MOCK"] = "true"
        MOCK_MODE = True
        # Connectors read JARVIS_MOCK at import — set env BEFORE _gather()
        # triggers their lazy imports. (Already-imported modules would need
        # a reload; in harness use this runs first, so we're fine.)

    b = generate({})
    if args.spoken:
        print(b["spoken"])
    else:
        print(_json.dumps(b, indent=2, default=str))
