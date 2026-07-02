"""
tools/os_readiness.py — OS Lifecycle / Upgrade-Readiness Assessment
====================================================================
CofCITIP — JARVIS Phase 4 (Session 4). Pure in-memory port of the standalone
dashboard's `dashboards/Lifecycle/os_readiness.py` assessment engine, for
`tools/device_merge.py` to score merged device records.

Ported AS-IS from the standalone script: the TARGETS version tables (real
numbers, not placeholders — distinct from the still-UNCONFIRMED OS_BASELINES
placeholders in os_baselines_open_question.py), WIN11_INCOMPATIBLE_AGE_DAYS,
version_tuple(), assess_readiness(), _assess_standard(), _assess_linux(),
_get_eol_name().

Deliberately NOT ported (standalone-dashboard-era plumbing, per the
architecture plan's no-wrapping-old-dashboard-code rule): the
`from config import LIFECYCLE_CSV, OUTPUT_DIR` dependency, load_inventory()
and all CSV reading/writing, build_readiness_rows()'s FIELDNAMES row shape,
build_json(), write_summary(), main(). This module does zero file I/O.

Adapted (only) for JARVIS shapes: get_platform_key() accepts device_merge's
lowercase platform values ("windows", "mac") as well as the original
"Windows"/"macOS" spellings.

Lifecycle vs. compliance: these statuses feed `lifecycle_status` and friends
ONLY. They must NOT flow into _score_compliance_state()'s compliance verdict
— the earlier design decision stands: a flat version floor must not mark
legacy/instrument-tied devices "non-compliant". "EOL build" on the Lifecycle
page is information, not a compliance flag.

Not a (params: dict) -> dict tool — internal logic device_merge imports,
like tools/secrets.py. Intentionally not registered in jarvis_core.
"""

from __future__ import annotations

# ── TARGET OS VERSIONS (ported verbatim — real values, do not invent) ─────────
TARGETS = {
    "Windows": {
        "target_build":  "10.0.26100",
        "target_name":   "Windows 11 24H2",
        "minimum_build": "10.0.19045",
        "minimum_name":  "Windows 10 22H2",
        "eol_builds": {
            "10.0.18363": "Windows 10 1909",
            "10.0.19041": "Windows 10 2004",
            "10.0.19042": "Windows 10 20H2",
            "10.0.19043": "Windows 10 21H1",
        },
    },
    "macOS": {
        "target_build":  "15.0",
        "target_name":   "macOS Sequoia 15",
        "minimum_build": "14.0",
        "minimum_name":  "macOS Sonoma 14",
        "eol_builds": {
            "12": "macOS Monterey",
            "11": "macOS Big Sur",
            "10": "macOS Catalina or older",
        },
    },
    "iOS": {
        "target_build":  "18.0",
        "target_name":   "iOS 18",
        "minimum_build": "17.0",
        "minimum_name":  "iOS 17",
        "eol_builds": {
            "15": "iOS 15",
            "14": "iOS 14",
        },
    },
    "Android": {
        "target_build":  "14.0",
        "target_name":   "Android 14",
        "minimum_build": "13.0",
        "minimum_name":  "Android 13",
        "eol_builds": {
            "11": "Android 11",
            "10": "Android 10",
        },
    },
    # Linux targets are per-distro
    "Linux": {
        "Ubuntu": {
            "target_build":  "24.04",
            "target_name":   "Ubuntu 24.04 LTS Noble",
            "minimum_build": "22.04",
            "minimum_name":  "Ubuntu 22.04 LTS Jammy",
            "eol_builds":    {"18.04": "Ubuntu 18.04 LTS (EOL)", "16.04": "Ubuntu 16.04 LTS (EOL)"},
        },
        "RHEL": {
            "target_build":  "9.0",
            "target_name":   "RHEL 9",
            "minimum_build": "8.0",
            "minimum_name":  "RHEL 8",
            "eol_builds":    {"7": "RHEL 7 (EOL)", "6": "RHEL 6 (EOL)"},
        },
        "Debian": {
            "target_build":  "12.0",
            "target_name":   "Debian 12 Bookworm",
            "minimum_build": "11.0",
            "minimum_name":  "Debian 11 Bullseye",
            "eol_builds":    {"9": "Debian 9 Stretch (EOL)", "10": "Debian 10 Buster (EOL)"},
        },
        "CentOS": {
            # CentOS is fully EOL - migrate to RHEL or Rocky Linux
            "target_build":  "999.0",
            "target_name":   "Migrate to RHEL 9 or Rocky Linux 9",
            "minimum_build": "999.0",
            "minimum_name":  "No acceptable CentOS version (EOL)",
            "eol_builds":    {"7": "CentOS 7 (EOL)", "8": "CentOS 8 (EOL)"},
        },
    },
}

# Windows 11 hardware eligibility heuristic: devices > 4 years old are likely ineligible
WIN11_INCOMPATIBLE_AGE_DAYS = 1460


# ── VERSION COMPARISON ────────────────────────────────────────────────────────
def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:4])
    except (ValueError, AttributeError):
        return (0,)


def get_platform_key(platform):
    """Original logic, extended to accept device_merge's lowercase platform
    vocabulary ("windows"/"mac") alongside the standalone spellings."""
    p = str(platform or "")
    low = p.lower()
    if "windows" in low:
        return "Windows"
    if low in ("mac", "macos", "osx") or "macos" in low:
        return "macOS"
    if p in ("iOS", "Android", "Linux"):
        return p
    if low in ("ios", "android", "linux"):
        return {"ios": "iOS", "android": "Android", "linux": "Linux"}[low]
    return None


# ── READINESS ASSESSMENT (ported verbatim below this line) ────────────────────
def assess_readiness(device):
    """
    Returns tuple:
      (current_status, upgrade_path, readiness, readiness_reason, win11_eligible, priority)
    """
    platform    = device.get("platform", "")
    os_version  = device.get("os_version", "")
    linux_distro= device.get("linux_distro", "")
    age_days    = device.get("device_age_days", "")

    plat_key = get_platform_key(platform)
    if not plat_key:
        return ("unknown", "Unrecognized platform", "unknown", "Platform not recognized", "n/a", "low")
    if not os_version:
        return ("unknown", "No OS version data", "unknown", "Missing OS version", "n/a", "low")

    # Route Linux to distro-aware assessment
    if plat_key == "Linux":
        return _assess_linux(os_version, linux_distro)

    return _assess_standard(plat_key, os_version, age_days)


def _assess_standard(plat_key, os_version, age_days):
    """Assessment for Windows, macOS, iOS, Android."""
    target      = TARGETS[plat_key]
    target_name = target["target_name"]
    min_name    = target["minimum_name"]
    current_ver = version_tuple(os_version)
    target_ver  = version_tuple(target["target_build"])
    minimum_ver = version_tuple(target["minimum_build"])
    win11_elig  = "n/a"

    # Windows 11 hardware eligibility
    if plat_key == "Windows":
        try:
            win11_elig = "no (age)" if int(age_days) >= WIN11_INCOMPATIBLE_AGE_DAYS else "likely-yes"
        except (ValueError, TypeError):
            win11_elig = "unknown"

    # EOL build check
    eol_name = _get_eol_name(target["eol_builds"], os_version)
    if eol_name:
        if plat_key == "Windows" and win11_elig == "no (age)":
            return ("eol-build",
                    f"Replace hardware -> upgrade to {target_name}",
                    "blocked",
                    f"Running EOL build ({eol_name}) on aging hardware - hardware replacement needed",
                    win11_elig, "high")
        return ("eol-build",
                f"Upgrade from {eol_name} -> {target_name}",
                "needs-upgrade",
                f"Running EOL build ({eol_name}) - immediate upgrade required",
                win11_elig, "high")

    # At or above target
    if current_ver >= target_ver:
        return ("at-target", "No upgrade needed", "ready",
                f"Running {target_name} or newer", win11_elig, "low")

    # Above minimum, below target
    if current_ver >= minimum_ver:
        if plat_key == "Windows" and win11_elig == "no (age)":
            return ("minimum",
                    f"Replace hardware -> upgrade to {target_name}",
                    "blocked",
                    f"Hardware too old for Windows 11 - replacement needed before upgrade",
                    win11_elig, "medium")
        return ("upgradeable",
                f"Upgrade current -> {target_name}",
                "needs-upgrade",
                f"Below target {target_name} - upgrade recommended",
                win11_elig, "medium")

    # Below minimum
    if plat_key == "Windows" and win11_elig == "no (age)":
        return ("eol-build",
                f"Replace hardware -> upgrade to {target_name}",
                "blocked",
                f"Below minimum version on aging hardware - replacement needed",
                win11_elig, "high")
    return ("eol-build",
            f"Upgrade to {min_name} minimum, then {target_name}",
            "needs-upgrade",
            f"Below minimum acceptable version ({min_name})",
            win11_elig, "high")


def _assess_linux(os_version, distro):
    """Distro-aware Linux readiness assessment."""
    linux_targets = TARGETS["Linux"]
    win11_elig    = "n/a"

    if not distro or distro not in linux_targets:
        return ("unknown", "Unknown Linux distro", "unknown",
                f"Distro '{distro}' not in target list", win11_elig, "low")

    dtarget     = linux_targets[distro]
    target_name = dtarget["target_name"]
    min_name    = dtarget["minimum_name"]
    current_ver = version_tuple(os_version)
    target_ver  = version_tuple(dtarget["target_build"])
    minimum_ver = version_tuple(dtarget["minimum_build"])

    # CentOS - always blocked/non-compliant
    if distro == "CentOS":
        return ("eol-build",
                f"Migrate from CentOS -> {target_name}",
                "blocked",
                "CentOS is end-of-life with no viable upgrade path - migration to RHEL or Rocky Linux required",
                win11_elig, "high")

    # EOL build check
    eol_name = _get_eol_name(dtarget["eol_builds"], os_version)
    if eol_name:
        return ("eol-build",
                f"Upgrade from {eol_name} -> {target_name}",
                "needs-upgrade",
                f"Running EOL version ({eol_name}) - upgrade required",
                win11_elig, "high")

    # At or above target
    if current_ver >= target_ver:
        return ("at-target", "No upgrade needed", "ready",
                f"Running {target_name}", win11_elig, "low")

    # Above minimum, below target
    if current_ver >= minimum_ver:
        return ("upgradeable",
                f"Upgrade current -> {target_name}",
                "needs-upgrade",
                f"Below target {target_name} - upgrade recommended",
                win11_elig, "medium")

    # Below minimum
    return ("eol-build",
            f"Upgrade to {min_name} first, then {target_name}",
            "needs-upgrade",
            f"Below minimum acceptable version ({min_name})",
            win11_elig, "high")


def _get_eol_name(eol_builds, os_version):
    """Check if os_version matches any EOL build entry."""
    if os_version in eol_builds:
        return eol_builds[os_version]
    major = os_version.split(".")[0] if os_version else ""
    if major in eol_builds:
        return eol_builds[major]
    return None


def target_os_name(platform, linux_distro=""):
    """Display label for a device's target OS (ported from the standalone
    build_readiness_rows() target-label logic, minus the CSV row)."""
    plat_key = get_platform_key(platform)
    if plat_key == "Linux" and linux_distro and linux_distro in TARGETS["Linux"]:
        return TARGETS["Linux"][linux_distro]["target_name"]
    if plat_key and plat_key in TARGETS:
        return TARGETS[plat_key]["target_name"]
    return "unknown"


# ── MANUAL SMOKE TEST ─────────────────────────────────────────────────────────
# python -m tools.os_readiness — spot-checks the ported logic against known
# inputs (no network, no files; pure functions only).
if __name__ == "__main__":
    cases = [
        {"platform": "windows", "os_version": "10.0.26100.1", "device_age_days": 300},
        {"platform": "windows", "os_version": "10.0.22631.3447", "device_age_days": None},
        {"platform": "windows", "os_version": "10.0.19042", "device_age_days": 2000},
        {"platform": "mac", "os_version": "15.5"},
        {"platform": "mac", "os_version": "14.7.6"},
        {"platform": "mac", "os_version": "12.7.6"},
        {"platform": "Linux", "os_version": "7.9", "linux_distro": "CentOS"},
        {"platform": "toaster", "os_version": "1.0"},
    ]
    for c in cases:
        print(f"{str(c):<75} -> {assess_readiness(c)}")
