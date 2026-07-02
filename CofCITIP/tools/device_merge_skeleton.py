"""
tools/device_merge.py — Cross-Connector Device Merge & Scoring Engine
=======================================================================
SKELETON / REFERENCE FILE — not finished code. This is scaffolding that
matches the exact structural pattern used by every other JARVIS connector
(see tools/intune.py, tools/jamf.py, tools/teams.py, tools/taegis.py for
the real, working examples this mirrors). Fill in the TODO blocks; do not
change the overall shape without a good reason, and say so in the session
TLDR if you do.

Joins device records from multiple connectors into one normalized view
per device, and computes derived state used by the Compliance, Lifecycle,
and Threat Intel web pages (see jarvis_web_architecture_and_dashboards_plan.md
§3-4 for the full design rationale — read that first if you haven't).

Does NOT replace tools/device_authority.py — that resolves "which system
is authoritative for THIS ONE device" for single-device queries. This
module is for FLEET-WIDE aggregation across MANY devices at once.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pybreaker
import structlog
from pydantic import BaseModel, field_validator

log = structlog.get_logger("jarvis.tools.device_merge")

# ── CONFIG ────────────────────────────────────────────────────────────────────
MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"

# Which connectors this module pulls from. Keep this list in one place so
# adding a new source later is a one-line change, not a hunt through the file.
# TODO: confirm this list against whichever connectors actually have solid
# mock data generators right now (intune, jamf, taegis are the safe bets per
# the planning doc — add mcm/pdq once their mocks are confirmed solid).
SOURCE_CONNECTORS = [
    "tools.intune",
    "tools.jamf",
    "tools.taegis",
    # "tools.mcm",
    # "tools.pdq",
]

# DECIDED (do not revisit without good reason): there is NO JARVIS-side
# OS_BASELINES table. CofC's environment has a mix of current and
# legacy/instrument-tied devices where a flat version floor per platform
# would misclassify intentionally-old machines as non-compliant. Intune
# and Jamf compliance policies already encode the real compliance verdict
# (including whatever exceptions exist for legacy devices) — JARVIS reads
# that verdict from each connector's existing device/compliance data rather
# than recomputing it from a version-number comparison.
#
# OPEN QUESTION, explicitly NOT this module's job to solve: how legacy/
# instrument-tied devices are identified and excepted in Intune/Jamf policy
# is still being worked out (Steven has a meeting on this). If policy-
# reported compliance for those devices still comes back "non-compliant"
# after that meeting, the fix is almost certainly in Intune/Jamf policy
# configuration itself, not a JARVIS-side override layer — don't build one
# preemptively. Flag this clearly if it comes up rather than silently
# adding exception logic here.

merge_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60,
                                          name="device_merge")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


merge_breaker.add_listener(_BreakerLogger())


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class MergeParams(BaseModel):
    """Params for merge_devices(). Keep minimal — this is a fleet-wide pull,
    not a single-device lookup (that's device_authority.py's job)."""
    platform_filter: str | None = None  # "windows" | "mac" | None for all

    @field_validator("platform_filter")
    @classmethod
    def platform_ok(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in ("windows", "mac"):
            raise ValueError("platform_filter must be 'windows', 'mac', or omitted")
        return v


# ── PER-SOURCE FETCH HELPERS ──────────────────────────────────────────────────
# Each of these calls one connector's existing query function and normalizes
# its output to a hostname-keyed dict. Wrap each in its own try/except so one
# connector being down degrades the merge rather than failing it entirely —
# same defensive pattern as tools/briefing.py's get_connector_health().

def _fetch_intune() -> dict[str, dict]:
    """Returns {hostname_lower: {...intune fields...}}. Empty dict on failure."""
    # TODO: import tools.intune, call its device-query function (check the
    # actual function name in intune.py — likely query_devices or similar),
    # normalize each record's hostname to lowercase as the join key.
    try:
        raise NotImplementedError("fill in once intune.py's exact query fn is confirmed")
    except Exception as e:  # noqa: BLE001 — boundary catch-all, by design
        log.warning("merge.source_failed", source="intune", error=str(e))
        return {}


def _fetch_jamf() -> dict[str, dict]:
    """Returns {hostname_lower: {...jamf fields...}}. Empty dict on failure."""
    try:
        raise NotImplementedError("fill in once jamf.py's exact query fn is confirmed")
    except Exception as e:  # noqa: BLE001
        log.warning("merge.source_failed", source="jamf", error=str(e))
        return {}


def _fetch_taegis() -> dict[str, dict]:
    """Returns {hostname_lower: {...taegis/threat fields...}}. Empty on failure.
    Used for coverage-gap detection — a device with no entry here that DOES
    have an Intune/Jamf entry is a 'missing agent' gap."""
    try:
        raise NotImplementedError("fill in once taegis.py's exact query fn is confirmed")
    except Exception as e:  # noqa: BLE001
        log.warning("merge.source_failed", source="taegis", error=str(e))
        return {}


# ── SCORING LOGIC ──────────────────────────────────────────────────────────────
# Concept ported from the old standalone lifecycle_tracker.py's
# calc_overall_compliance / calc_lifecycle_status — reimplemented here against
# JARVIS's actual merged-record field names, not copied verbatim.

def _read_policy_compliance(raw: dict) -> str:
    """Returns 'compliant' | 'non-compliant' | 'unknown'.

    DECIDED: this reads the compliance verdict Intune/Jamf ALREADY computed
    (e.g. Intune Graph API's deviceCompliancePolicyStates / complianceState
    field, or Jamf's smart-group/compliance flags) rather than recomputing
    it from a version-number comparison against a JARVIS-side baseline.
    See the OS_BASELINES removal note above for why — CofC has legacy/
    instrument-tied devices where a flat version floor would misclassify
    intentionally-old machines.

    TODO: confirm the EXACT field name(s) each connector exposes for this —
    check tools/intune.py and tools/jamf.py's actual query function output
    shapes (and their mock data generators) before filling this in. Do not
    guess a field name; if it's not obviously present, ask Steven whether
    the connector needs a small addition to surface it, rather than
    inventing a substitute calculation here.
    """
    intune_state = raw.get("intune", {}).get("compliance_state")  # TODO: confirm field name
    jamf_state = raw.get("jamf", {}).get("compliance_state")      # TODO: confirm field name
    for state in (intune_state, jamf_state):
        if state in ("compliant", "non-compliant"):
            return state
    return "unknown"


def _score_compliance_state(os_compliant: str, checkin_days: int | None,
                             lifecycle_status: str) -> str:
    """Returns 'compliant' | 'warning' | 'non-compliant' | 'unknown'.

    `os_compliant` here comes from _read_policy_compliance() — i.e. it's
    already Intune/Jamf's own verdict, not a JARVIS-computed version check.
    This function's job is just to ALSO factor in checkin staleness and
    lifecycle/EOL status on top of that policy verdict, not to second-guess
    the policy verdict itself.

    TODO: confirm the exact thresholds (what counts as 'stale' checkin,
    what counts as 'aging' lifecycle) against config.py or ask Steven —
    do not invent threshold numbers. Lifecycle/EOL status (separate from
    OS compliance) may have its own legacy-device wrinkle too — worth
    asking about in the same meeting covering legacy/instrument-tied
    devices, since "old but policy-compliant" and "old and EOL" may need
    to be distinguishable rather than collapsed together."""
    if lifecycle_status == "end-of-life":
        return "non-compliant"
    if os_compliant == "non-compliant":
        return "non-compliant"
    # TODO: stale-checkin threshold
    if lifecycle_status == "aging":
        return "warning"
    if os_compliant == "unknown":
        return "unknown"
    return "compliant"


def _detect_coverage_gaps(hostname: str, present_sources: list[str]) -> list[str]:
    """Returns a list of gap labels, e.g. ['missing_taegis_agent'].
    A device present in inventory sources (intune/jamf) but absent from
    taegis is the classic gap the old dashboards surfaced — reproduce that
    here, generalized to whichever sources are actually wired."""
    gaps = []
    has_inventory = any(s in present_sources for s in ("intune", "jamf"))
    has_taegis = "taegis" in present_sources
    if has_inventory and not has_taegis:
        gaps.append("missing_taegis_agent")
    return gaps


# ── PUBLIC FUNCTIONS ───────────────────────────────────────────────────────────
# Same (params: dict) -> dict contract as every other JARVIS tool.

def merge_devices(raw_params: dict | None = None) -> dict:
    """See jarvis_web_architecture_and_dashboards_plan.md §3 for the full
    documented return shape. Implementation below is scaffolding — the
    join/dedup/scoring logic needs to be filled in for real."""
    params = MergeParams(**(raw_params or {}))
    log.info("tool.start", tool="merge_devices", platform_filter=params.platform_filter)

    sources_data = {
        "intune": _fetch_intune(),
        "jamf": _fetch_jamf(),
        "taegis": _fetch_taegis(),
    }
    sources_included = [k for k, v in sources_data.items() if v]
    sources_unavailable = [k for k, v in sources_data.items() if not v]

    # Join on hostname (lowercase, already normalized by fetch helpers).
    all_hostnames = set()
    for src_dict in sources_data.values():
        all_hostnames.update(src_dict.keys())

    devices = []
    for hostname in sorted(all_hostnames):
        present_sources = [s for s, d in sources_data.items() if hostname in d]
        raw = {s: sources_data[s][hostname] for s in present_sources}

        # TODO: derive platform, device_age_days, checkin_days,
        # lifecycle_status from `raw` once real connector field shapes are
        # confirmed — this is intentionally left as a stub.
        platform = raw.get("intune", {}).get("platform", "unknown")
        lifecycle_status = "unknown"  # TODO

        os_compliant = _read_policy_compliance(raw)
        compliance_state = _score_compliance_state(
            os_compliant, checkin_days=None, lifecycle_status=lifecycle_status)
        coverage_gaps = _detect_coverage_gaps(hostname, present_sources)

        if params.platform_filter and platform != params.platform_filter:
            continue

        devices.append({
            "hostname": hostname,
            "platform": platform,
            "sources_present": present_sources,
            "compliance_state": compliance_state,
            "lifecycle_status": lifecycle_status,
            "os_compliant": os_compliant,
            "device_age_days": None,       # TODO
            "last_checkin_days": None,     # TODO
            "threat_status": "unknown",    # TODO — derive from raw.taegis
            "coverage_gaps": coverage_gaps,
            "raw": raw,
        })

    result = {
        "source": "device_merge",
        "mock": MOCK_MODE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "device_count": len(devices),
        "sources_included": sources_included,
        "sources_unavailable": sources_unavailable,
        "devices": devices,
    }
    log.info("tool.success", tool="merge_devices", device_count=len(devices),
             sources_included=sources_included, sources_unavailable=sources_unavailable)
    return result


def fleet_summary(raw_params: dict | None = None) -> dict:
    """Aggregate counts over merge_devices() output. Feeds the summary
    cards at the top of Compliance/Lifecycle/Threat Intel pages."""
    merged = merge_devices(raw_params)
    devices = merged["devices"]
    total = len(devices)

    compliance_counts: dict[str, int] = {}
    lifecycle_counts: dict[str, int] = {}
    gap_count = 0
    for d in devices:
        compliance_counts[d["compliance_state"]] = compliance_counts.get(
            d["compliance_state"], 0) + 1
        lifecycle_counts[d["lifecycle_status"]] = lifecycle_counts.get(
            d["lifecycle_status"], 0) + 1
        if d["coverage_gaps"]:
            gap_count += 1

    return {
        "source": "device_merge",
        "mock": MOCK_MODE,
        "generated_at": merged["generated_at"],
        "total_devices": total,
        "compliance_breakdown": compliance_counts,
        "lifecycle_breakdown": lifecycle_counts,
        "devices_with_coverage_gaps": gap_count,
        "sources_included": merged["sources_included"],
        "sources_unavailable": merged["sources_unavailable"],
    }


def health_check(raw_params: dict | None = None) -> dict:
    """Same contract shape as every other connector's health_check, but
    reports merge-engine health — can it currently reach enough sources to
    produce a useful merge — not one external system's health.
    No network probe by design; cheap to poll."""
    if MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif merge_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {merge_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "device_merge", "status": status, "detail": detail,
              "mock": MOCK_MODE, "breaker": merge_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.device_merge --function merge_devices --mock
# python -m tools.device_merge --function fleet_summary --mock
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="Device merge engine test harness")
    parser.add_argument("--function", default="merge_devices",
                        choices=["merge_devices", "fleet_summary", "health_check"])
    parser.add_argument("--params", default="{}")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    fn = {"merge_devices": merge_devices,
          "fleet_summary": fleet_summary,
          "health_check": health_check}[args.function]
    print(_json.dumps(fn(_json.loads(args.params)), indent=2, default=str))
