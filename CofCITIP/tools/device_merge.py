"""
tools/device_merge.py — Cross-Connector Device Merge Engine
============================================================
CofCITIP — JARVIS Phase 4 (Session 2). Joins device records from Intune and
Jamf on hostname and computes derived fleet-wide state for the Compliance,
Lifecycle, and Coverage Gaps web pages (Sessions 3-5). Threat Intel reads
alerts straight from tools/taegis.py, not through this merge.

Promoted from device_merge_skeleton.py, with deliberate deviations where the
skeleton's assumed connector shapes did not match the real connectors:

  - Intune has no bulk per-device export function — this module mirrors
    query_compliance()'s MOCK_MODE switch: tools.intune._mock_devices() in
    mock, tools.intune._graph_get_all() in live. Underscore-private reuse is
    intentional (per Session 2 prompt) — preferred over widening intune.py's
    public API.
  - Jamf has NO policy-compliance flag at all. Jamf's "compliance" concept is
    check-in staleness + patch status, which are different things — Jamf-sourced
    records carry compliance_state "unknown" and staleness rides along as its
    own field (last_checkin_stale). Staleness is deliberately NOT proxied into
    the compliance verdict; if it ever should be, that's an explicit design
    decision, not a default.
  - Taegis has no device/agent-inventory endpoint (get_alerts/get_alert_detail/
    get_investigations only) — a device with zero alerts is indistinguishable
    from a device Taegis has never seen. _fetch_taegis() is an honest empty
    stub and the missing_taegis_agent coverage gap is INACTIVE this round
    (every device would flag, which is noise, not signal).

Session 4: lifecycle_status/readiness/upgrade_path/etc. are derived per
device via tools/os_readiness (ported from the standalone dashboard's
engine, pure in-memory). Lifecycle is information for the Lifecycle page —
it deliberately does NOT feed the compliance verdict. device_age_days comes
from Jamf's enrollmentDate as an age proxy (Macs only); Intune exposes no
date field, so Windows age is honestly None and win11_eligible "unknown".

FERPA: device + user data throughout — if this module is ever registered as an
LLM tool in jarvis_core.py, its tool name MUST go into TOOL_DATA_LOCAL. It is
NOT registered this session (web pages call it through jarvis_ui.py, all
local); jarvis_core's default-deny egress rule covers any accidental future
registration, but be explicit anyway.

Tool functions (signature: (params: dict) -> dict):
  merge_devices(params)   — full merged per-device records
  fleet_summary(params)   — aggregate counts for dashboard stat cards
  health_check(params)    — worst-of underlying connector health

Mock mode: JARVIS_MOCK=true or --mock on the CLI harness (propagated into
tools.intune / tools.jamf so their fetch paths go synthetic too).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel

from tools import os_readiness  # pure in-memory lifecycle engine (Session 4)

log = structlog.get_logger("jarvis.tools.device_merge")

MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"

# Field precedence when a hostname appears in BOTH sources: Intune wins for
# the flattened convenience fields (it is authoritative for the Windows fleet
# and carries the only real compliance verdict); the full per-source records
# are kept side by side under "intune"/"jamf" so nothing is lost. In practice
# the fleets barely overlap (platform split), but the join must not corrupt
# data when they do.
_SOURCE_PRECEDENCE = ("intune", "jamf")


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
# No parameters yet — the models exist so the (params: dict) -> dict contract
# validates its input like every other connector, and so future filters
# (platform, source) have a place to land without changing call sites.
class MergeParams(BaseModel):
    pass


class SummaryParams(BaseModel):
    pass


# ── FETCH HELPERS (one per source, all return {hostname_lower: record}) ───────
def _propagate_mock(module) -> None:
    """Force a connector module into mock BEFORE any fetch when this module is
    mocked (the harness sets device_merge.MOCK_MODE after import, so the env
    var alone isn't enough). One-way: never forces a connector OUT of mock."""
    if MOCK_MODE:
        module.MOCK_MODE = True


def _platform_from_os(os_name: str) -> str:
    s = (os_name or "").lower()
    if "windows" in s:
        return "windows"
    if "mac" in s or "osx" in s:
        return "mac"
    return s or "unknown"


def _map_intune_compliance(state: str) -> str:
    """Intune complianceState -> this module's vocabulary. Graph uses
    "noncompliant" (no hyphen) and "error"; both mean a failed verdict."""
    if state == "compliant":
        return "compliant"
    if state in ("noncompliant", "error"):
        return "non-compliant"
    return "unknown"


def _fetch_intune() -> dict[str, dict]:
    """Bulk per-device fetch from Intune, keyed by lowercased deviceName.

    Mirrors tools.intune.query_compliance()'s MOCK_MODE switch rather than
    duplicating Graph logic: _mock_devices() in mock, _graph_get_all() live.
    Returns {} on any failure — a down source degrades the merge, never
    breaks it."""
    try:
        from tools import intune
        _propagate_mock(intune)
        if intune.MOCK_MODE:
            raw = intune._mock_devices()
        else:
            raw = intune._graph_get_all(
                "/deviceManagement/managedDevices",
                {"$select": "id,deviceName,operatingSystem,osVersion,"
                            "complianceState,userPrincipalName,"
                            "lastSyncDateTime,model,serialNumber",
                 "$top": 999},
            )
        out: dict[str, dict] = {}
        for d in raw:
            name = d.get("deviceName") or ""
            if not name:
                continue
            out[name.lower()] = {
                "hostname": name,
                "platform": _platform_from_os(d.get("operatingSystem", "")),
                "compliance_state": _map_intune_compliance(
                    d.get("complianceState", "")),
                "user": d.get("userPrincipalName"),
                "last_sync": d.get("lastSyncDateTime"),
                "os_version": d.get("osVersion"),
                "model": d.get("model"),
                "serial": d.get("serialNumber"),
                # Present in mock data; live Graph v1.0 has no such property
                # (deliberately NOT in the live $select — would 400), so this
                # is honestly None in live mode until a real derivation exists.
                "non_compliance_reason": d.get("nonComplianceReason"),
            }
        log.info("fetch.intune", devices=len(out), mock=MOCK_MODE)
        return out
    except Exception as e:
        log.warning("fetch.intune_failed", error=str(e))
        return {}


def _fetch_jamf() -> dict[str, dict]:
    """Bulk per-device fetch from Jamf, keyed by lowercased hostname.

    Reuses tools.jamf._fetch_macs() + _summarize_mac(), which already
    normalize to nearly this module's shape. Jamf has no policy-compliance
    flag — compliance_state is "unknown" for every Jamf record and check-in
    staleness is carried separately as last_checkin_stale (NOT folded into
    the compliance verdict; see module docstring). Returns {} on failure."""
    try:
        from tools import jamf
        _propagate_mock(jamf)
        out: dict[str, dict] = {}
        for raw in jamf._fetch_macs():
            m = jamf._summarize_mac(raw)
            name = m.get("hostname") or ""
            if not name:
                continue
            # Age PROXY from Jamf's enrollmentDate (the only date field any
            # source exposes — Intune's records have none, so Windows age
            # stays None). Enrollment != purchase, but it bounds age from
            # below honestly; None on any parse failure, never invented.
            age_days = None
            try:
                enrolled = (raw.get("general") or {}).get("enrollmentDate", "")
                if enrolled:
                    dt = datetime.fromisoformat(enrolled.replace("Z", "+00:00"))
                    age_days = max(0, (datetime.now(timezone.utc) - dt).days)
            except (ValueError, TypeError):
                age_days = None
            out[name.lower()] = {
                "hostname": name,
                "platform": "mac",
                "compliance_state": "unknown",  # Jamf has no such flag
                "user": m.get("user"),
                "department": m.get("department"),
                "last_checkin": m.get("last_checkin"),
                "last_checkin_stale": bool(m.get("stale")),
                "os_version": m.get("os_version"),
                "model": m.get("model"),
                "serial": m.get("serial"),
                "device_age_days": age_days,
            }
        log.info("fetch.jamf", devices=len(out), mock=MOCK_MODE)
        return out
    except Exception as e:
        log.warning("fetch.jamf_failed", error=str(e))
        return {}


def _fetch_taegis() -> dict[str, dict]:
    """Honest empty stub — tools/taegis.py exposes alerts and investigations
    only; there is NO device/agent-inventory endpoint, so per-device merge
    rows cannot be sourced from it (a device with zero alerts is
    indistinguishable from one Taegis has never seen). Kept present rather
    than removed so the module keeps its three-source shape for when a Taegis
    inventory endpoint exists. See also _detect_coverage_gaps: the
    missing_taegis_agent gap is inactive for the same reason."""
    return {}


# ── SCORING ───────────────────────────────────────────────────────────────────
def _read_policy_compliance(raw: dict) -> str:
    """Reads Intune's policy compliance verdict if present. Jamf has no
    equivalent compliance flag (see Session 2 notes) — Jamf-only devices
    are 'unknown' for this field, not silently treated as compliant."""
    intune_state = (raw.get("intune") or {}).get("compliance_state")
    if intune_state in ("compliant", "non-compliant"):
        return intune_state
    return "unknown"


def _score_compliance_state(raw: dict) -> str:
    """Overall per-device compliance verdict. Intune's policy verdict is the
    ONLY input: Intune devices score compliant/non-compliant, Jamf-only
    devices score unknown. lifecycle_status/readiness (derived since Session
    4 via tools/os_readiness) are DELIBERATELY excluded — a flat OS-version
    floor must not mark legacy/instrument-tied devices non-compliant (the
    OS_BASELINES removal decision). Do NOT add a staleness proxy either."""
    return _read_policy_compliance(raw)


def _detect_coverage_gaps(record: dict) -> list[str]:
    """INACTIVE THIS ROUND — retained for when Taegis grows an inventory
    endpoint. With _fetch_taegis() always {}, every inventory device would
    flag missing_taegis_agent: a guaranteed 100%-gap result is noise, not
    signal, so merge_devices() does not call this yet and every record ships
    coverage_gaps: []. Session 5 decides what the Coverage Gaps page actually
    surfaces (likely sources_present-based gaps, which ARE real data)."""
    gaps: list[str] = []
    sources = record.get("sources_present", [])
    if sources and "taegis" not in sources:
        gaps.append("missing_taegis_agent")
    return gaps


# ── MERGE ─────────────────────────────────────────────────────────────────────
def merge_devices(raw_params: dict | None = None) -> dict:
    """Join Intune + Jamf device records on lowercased hostname and score
    each merged record. Full device list is returned uncapped — consumers
    are the web pages (Sessions 3-5), not spoken responses."""
    MergeParams(**(raw_params or {}))
    log.info("tool.start", tool="merge_devices", mock=MOCK_MODE)

    fetched = {
        "intune": _fetch_intune(),
        "jamf": _fetch_jamf(),
        "taegis": _fetch_taegis(),  # always {} this round — honest stub
    }

    merged: dict[str, dict] = {}
    for source in _SOURCE_PRECEDENCE + ("taegis",):
        for key, rec in fetched[source].items():
            slot = merged.setdefault(key, {"sources_present": []})
            slot["sources_present"].append(source)
            slot[source] = rec

    devices = []
    for key in sorted(merged):
        raw = merged[key]
        # Flattened convenience fields, first-present-source-wins per
        # _SOURCE_PRECEDENCE; full per-source records stay alongside.
        flat: dict = {
            "hostname": None, "platform": "unknown", "user": None,
            "os_version": None, "model": None, "serial": None,
        }
        for source in _SOURCE_PRECEDENCE:
            rec = raw.get(source)
            if not rec:
                continue
            for f in flat:
                if flat[f] in (None, "unknown") and rec.get(f) not in (None, ""):
                    flat[f] = rec[f]
        # Lifecycle assessment (Session 4): ported os_readiness engine.
        # Feeds lifecycle_* / readiness fields ONLY — never the compliance
        # verdict (see _score_compliance_state).
        age_days = (raw.get("jamf") or {}).get("device_age_days")
        current_status, upgrade_path, readiness, readiness_reason, \
            win11_eligible, priority = os_readiness.assess_readiness({
                "platform": flat["platform"],
                "os_version": flat["os_version"] or "",
                "linux_distro": "",  # no Linux inventory source yet
                "device_age_days": age_days,
            })
        devices.append({
            **flat,
            "sources_present": raw["sources_present"],
            "compliance_state": _score_compliance_state(raw),
            "lifecycle_status": current_status,
            "target_os": os_readiness.target_os_name(flat["platform"]),
            "upgrade_path": upgrade_path,
            "readiness": readiness,
            "readiness_reason": readiness_reason,
            "win11_eligible": win11_eligible,
            "priority": priority,
            "device_age_days": age_days,
            # Jamf-only concept; None (not False) when no Jamf record exists.
            "last_checkin_stale": (raw.get("jamf") or {}).get("last_checkin_stale"),
            "coverage_gaps": [],  # _detect_coverage_gaps inactive — see its docstring
            "intune": raw.get("intune"),
            "jamf": raw.get("jamf"),
        })

    result = {
        "source": "device_merge",
        "mock": MOCK_MODE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources_included": [s for s in ("intune", "jamf", "taegis")
                             if fetched[s]],
        "sources_unavailable": [s for s in ("intune", "jamf", "taegis")
                                if not fetched[s]],
        "notes": {"taegis": "not merged — connector has no device/agent "
                            "inventory endpoint yet (alerts only)"},
        "total_devices": len(devices),
        "devices": devices,
    }
    log.info("tool.success", tool="merge_devices", total=len(devices),
             sources=result["sources_included"])
    return result


def fleet_summary(raw_params: dict | None = None) -> dict:
    """Aggregate counts over merge_devices() for dashboard stat cards."""
    SummaryParams(**(raw_params or {}))
    log.info("tool.start", tool="fleet_summary", mock=MOCK_MODE)

    m = merge_devices({})
    devices = m["devices"]

    by_platform: dict[str, int] = {}
    compliance = {"compliant": 0, "non-compliant": 0, "unknown": 0}
    by_sources: dict[str, int] = {}
    lifecycle: dict[str, int] = {}
    readiness_counts: dict[str, int] = {}
    stale = 0
    for d in devices:
        by_platform[d["platform"]] = by_platform.get(d["platform"], 0) + 1
        compliance[d["compliance_state"]] += 1
        src_key = "+".join(d["sources_present"])
        by_sources[src_key] = by_sources.get(src_key, 0) + 1
        lifecycle[d["lifecycle_status"]] = lifecycle.get(d["lifecycle_status"], 0) + 1
        readiness_counts[d["readiness"]] = readiness_counts.get(d["readiness"], 0) + 1
        if d["last_checkin_stale"]:
            stale += 1

    known = compliance["compliant"] + compliance["non-compliant"]
    result = {
        "source": "device_merge",
        "mock": MOCK_MODE,
        "generated_at": m["generated_at"],
        "sources_included": m["sources_included"],
        "sources_unavailable": m["sources_unavailable"],
        "total_devices": len(devices),
        "by_platform": by_platform,
        "by_sources_present": by_sources,
        "compliance": {
            **compliance,
            # Rate over devices with a REAL verdict (Intune). Jamf devices
            # have no compliance flag — including them would silently deflate
            # the rate with unknowns that aren't failures.
            "rate_pct_of_known": round(
                compliance["compliant"] / known * 100, 1) if known else 0,
            "devices_with_verdict": known,
        },
        "lifecycle": lifecycle,
        "readiness": readiness_counts,
        "stale_checkins": stale,
        # Coverage Gaps placeholder (Session 5): every device is inventory-
        # only w.r.t. Taegis because no inventory endpoint exists to cross-
        # reference against — this reports that plainly instead of implying
        # gap detection ran.
        "inventory_only_devices": len(devices),
        "taegis_cross_reference": (
            f"0 of {len(devices)} devices cross-referenced against Taegis — "
            f"no inventory endpoint yet"),
    }
    log.info("tool.success", tool="fleet_summary", total=len(devices))
    return result


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: {"status": "ok"|"degraded"|"down", "detail": str}.
    This module holds no credentials of its own — health is the WORST of the
    underlying merge sources' health checks (which are themselves probe-free:
    creds-present + breaker state, no API burn)."""
    from tools import intune, jamf
    _propagate_mock(intune)
    _propagate_mock(jamf)
    sub = {"intune": intune.health_check({}), "jamf": jamf.health_check({})}
    rank = {"ok": 0, "degraded": 1, "down": 2}
    status = max((s["status"] for s in sub.values()),
                 key=lambda s: rank.get(s, 2))
    detail = "; ".join(f"{name}: {s['status']} ({s['detail']})"
                       for name, s in sub.items())
    result = {"source": "device_merge", "status": status,
              "detail": detail + "; taegis: not merged (no inventory endpoint)",
              "mock": MOCK_MODE, "sources": sub}
    log.info("tool.health", source="device_merge", status=status,
             mock=MOCK_MODE)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.device_merge --function merge_devices --mock
# python -m tools.device_merge --function fleet_summary --mock
# python -m tools.device_merge --function health_check --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    # Harness mode: logs to stderr so stdout is clean, pipeable JSON.
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Device merge test harness")
    parser.add_argument("--function", required=True,
                        choices=["merge_devices", "fleet_summary",
                                 "health_check"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True  # _propagate_mock pushes this into the connectors

    fn = globals()[args.function]
    print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
