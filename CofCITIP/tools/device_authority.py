"""
tools/device_authority.py — Source-of-Authority Routing
========================================================
CofCITIP — decides WHICH endpoint-management system is authoritative for a given
device BEFORE any connector is queried.

THE PROBLEM THIS SOLVES:
CofC runs a hybrid Windows estate — Intune (cloud) and MCM/SCCM (on-prem) split
Windows coverage — plus Jamf for Mac and PDQ filling gaps. A question like
"patch status of laptop X" must NOT fan out to all four systems and guess which
answer is right. JARVIS resolves the authoritative source first, from the device
identifier's shape and any caller-supplied hints, then dispatches to exactly that
one system (with an ordered fallback list if the first has no record).

THE RULE SET (the part Steven will defend/revise with Philip + the team):

1. PLATFORM FIRST.
   - Mac -> Jamf, always, high confidence. No Windows-side system manages Macs at
     CofC, so Mac is unambiguous. (PDQ Inventory may hold a secondary record, so
     it's listed as a fallback, never the authority.)
   - Windows -> the Intune-vs-MCM split below.
   - Unknown platform -> the PDQ gap-filler tier (rule 4).

2. WINDOWS SPLIT — Intune vs MCM — decided by ENROLLMENT/MANAGEMENT STATE:
     cloud-native / Autopilot / Entra(AzureAD)-joined / Intune-managed
        -> Intune authoritative (high).
     domain-joined / hybrid-AD / SCCM(MCM) client-managed
        -> MCM authoritative (high).
     co-managed (BOTH Intune + MCM present)
        -> GENUINELY AMBIGUOUS. The true authority depends on which co-management
           WORKLOAD (patching, compliance, apps, ...) is assigned to which
           authority — something this function cannot know from an identifier.
           It returns Intune as a default but at LOW confidence, with MCM as the
           first fallback and a reason that says to verify the workload split.
   WHY enrollment-state and not hostname: hostname conventions at CofC don't
   reliably encode cloud-vs-domain join, but enrollment state IS the actual thing
   that determines which MDM owns the device. When no enrollment hint is available
   and the device isn't in the local source-of-authority map, a Windows host
   resolves to Intune at LOW confidence (CofC's cloud-first direction) — honest
   about the fact that an MCM-only domain-joined host would be misrouted, so the
   caller is told to check MCM first. A low-confidence honest answer beats a
   confidently wrong one.

3. LOCAL SOURCE-OF-AUTHORITY MAP (mocked here).
   A small cached dict of known hostname -> {source, platform}. A hit is treated
   as high confidence (it's a synced/known mapping). This is intentionally a
   MINIMAL MOCK — real sync tooling (pulling the actual device->source mapping
   from each system on a schedule) is out of scope for this session. The map is
   the mechanism by which a confident answer is possible WITHOUT a live probe.

4. PDQ IS A FALLBACK TIER, NOT A CO-EQUAL SOURCE.
   PDQ "fills gaps". It is never the FIRST authoritative answer for a device that
   Intune/MCM/Jamf already claim — it only appears as the LAST entry in
   fallback_sources for those. It becomes the authoritative answer ONLY for a
   device none of the three primaries claim (unknown platform, no hint, not in the
   map): the gap-filler of last resort, returned at low confidence.

No method here calls any of the four systems' APIs — routing is decided purely
from identifier shape, the optional hint dict, and the local map.

resolve_authority(device_identifier, hint) -> {
    "platform": "windows"|"mac"|"unknown",
    "authoritative_source": "intune"|"mcm"|"jamf"|"pdq"|"unknown",
    "fallback_sources": [ordered by trust],
    "confidence": "high"|"low",
    "reason": str,
}
hint (all optional): {"platform": "windows"|"mac",
                      "enrollment"/"management": free-text enrollment/mgmt state}
"""

from __future__ import annotations

import re

import structlog

log = structlog.get_logger("jarvis.tools.device_authority")

# ── LOCAL SOURCE-OF-AUTHORITY MAP (minimal mock — see rule 3) ──────────────────
# hostname (lowercased) -> {"source": ..., "platform": ...}. A real build syncs
# this from each system; here it's a handful of representative entries so a known
# device can resolve at high confidence without a live probe.
_DEVICE_SOURCE_MAP: dict[str, dict] = {
    "itsv-1001":      {"source": "intune", "platform": "windows"},  # cloud-native laptop
    "arts-mac-205":   {"source": "jamf",   "platform": "mac"},      # Jamf-managed Mac
    "libr-lab-12":    {"source": "mcm",    "platform": "windows"},  # domain-joined lab PC
    "kiosk-atrium-1": {"source": "pdq",    "platform": "windows"},  # gap-fill kiosk
}

# ── IDENTIFIER SHAPE HEURISTICS ───────────────────────────────────────────────
# Mac-ish: an explicit "MAC" token in the hostname, or an Apple-style serial
# (C02.../C07...). Windows-ish: dept-#### laptops, or lab/desktop/workstation tags.
_MAC_HOSTNAME_RE = re.compile(r"(?:^|[-_])mac(?:[-_]|\d|$)", re.IGNORECASE)
_APPLE_SERIAL_RE = re.compile(r"^c0[27][0-9a-z]{6,}$", re.IGNORECASE)
_WINDOWS_HOSTNAME_RE = re.compile(
    r"(?:^[a-z]{2,5}-?\d{3,4}$)|(?:[-_](?:lab|desk|pc|wks|win|kiosk|sign)[-_]?)",
    re.IGNORECASE,
)

# Enrollment/management synonyms -> the split bucket they imply.
_INTUNE_ENROLL = {
    "intune", "cloud-native", "cloud native", "cloudnative", "autopilot",
    "entra-joined", "entra joined", "aad-joined", "aad joined",
    "azuread-joined", "azure ad joined", "mdm",
}
_MCM_ENROLL = {
    "mcm", "sccm", "configmgr", "configuration manager", "domain-joined",
    "domain joined", "ad-joined", "ad joined", "hybrid", "hybrid-joined",
    "hybrid joined", "on-prem", "on prem",
}
_COMANAGED_ENROLL = {
    "co-managed", "co managed", "comanaged", "co-management", "co management",
    "comanagement", "dual",
}


def _norm_enrollment(value) -> str | None:
    """Map a free-text enrollment/management hint to 'intune' | 'mcm' |
    'comanaged' | None."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in _COMANAGED_ENROLL:
        return "comanaged"
    if v in _INTUNE_ENROLL:
        return "intune"
    if v in _MCM_ENROLL:
        return "mcm"
    # tolerate substrings ("device is co-managed", "hybrid ad domain joined")
    if any(tok in v for tok in ("co-manage", "comanage", "co manage")):
        return "comanaged"
    if any(tok in v for tok in ("cloud", "autopilot", "entra", "aad", "azure", "intune")):
        return "intune"
    if any(tok in v for tok in ("domain", "sccm", "mcm", "configmgr", "hybrid", "on-prem", "on prem")):
        return "mcm"
    return None


def _detect_platform(identifier: str, hint: dict) -> str:
    """windows | mac | unknown, from an explicit hint first, then identifier shape."""
    hinted = (hint.get("platform") or "").strip().lower()
    if hinted in ("mac", "macos", "osx", "darwin"):
        return "mac"
    if hinted in ("windows", "win"):
        return "windows"

    ident = (identifier or "").strip()
    if _MAC_HOSTNAME_RE.search(ident) or _APPLE_SERIAL_RE.match(ident):
        return "mac"
    if _WINDOWS_HOSTNAME_RE.search(ident):
        return "windows"
    return "unknown"


def _fallbacks_for(source: str) -> list[str]:
    """Ordered-by-trust fallback list for a given authoritative source. PDQ is
    always LAST among the primaries (gap-filler), never promoted above them."""
    return {
        "jamf":   ["pdq"],
        "intune": ["mcm", "pdq"],
        "mcm":    ["intune", "pdq"],
        "pdq":    ["intune", "mcm", "jamf"],
    }.get(source, ["pdq"])


def _decision(platform: str, source: str, fallbacks: list[str],
              confidence: str, reason: str) -> dict:
    result = {
        "platform": platform,
        "authoritative_source": source,
        "fallback_sources": fallbacks,
        "confidence": confidence,
        "reason": reason,
    }
    log.info("authority.resolved", platform=platform, source=source,
             confidence=confidence)
    return result


def resolve_authority(device_identifier: str, hint: dict | None = None) -> dict:
    """Resolve the authoritative endpoint-management source for a device WITHOUT
    querying any of them. See module docstring for the full rule set."""
    hint = hint or {}
    ident = (device_identifier or "").strip()
    log.info("tool.start", tool="resolve_authority", identifier=ident,
             hint=hint or None)

    platform = _detect_platform(ident, hint)

    # 1) Mac is unambiguous -> Jamf.
    if platform == "mac":
        return _decision(
            "mac", "jamf", _fallbacks_for("jamf"), "high",
            "macOS device — Jamf is the sole authority for Mac at CofC; no "
            "Windows-side system manages Macs. PDQ Inventory may hold a secondary "
            "record, so it's the fallback only.")

    # 2) Local source-of-authority map wins for known (non-Mac) devices.
    cached = _DEVICE_SOURCE_MAP.get(ident.lower())
    if cached:
        src = cached["source"]
        plat = cached.get("platform", platform)
        return _decision(
            plat, src, _fallbacks_for(src), "high",
            f"Known device in the local source-of-authority map -> {src} "
            f"(cached mapping; re-sync if its enrollment has changed).")

    # 3) Windows split: Intune vs MCM by enrollment/management state.
    if platform == "windows":
        enroll = _norm_enrollment(hint.get("enrollment") or hint.get("management"))
        if enroll == "intune":
            return _decision(
                "windows", "intune", _fallbacks_for("intune"), "high",
                "Windows device reported cloud-native / Entra-joined / "
                "Intune-managed -> Intune is authoritative.")
        if enroll == "mcm":
            return _decision(
                "windows", "mcm", _fallbacks_for("mcm"), "high",
                "Windows device reported domain-joined / SCCM(MCM) client-managed "
                "-> MCM is authoritative.")
        if enroll == "comanaged":
            return _decision(
                "windows", "intune", _fallbacks_for("intune"), "low",
                "Co-managed Windows device — BOTH Intune and MCM hold a record; the "
                "true authority depends on which co-management WORKLOAD (patching, "
                "compliance, apps) is assigned where. Defaulting to Intune but LOW "
                "confidence: confirm the workload split and check MCM (first fallback).")
        # Windows, but no usable enrollment hint and not in the map.
        return _decision(
            "windows", "intune", _fallbacks_for("intune"), "low",
            "Windows device but enrollment state unknown (no hint, not in the local "
            "map). Defaulting to Intune as CofC's cloud-first direction at LOW "
            "confidence — an MCM-only domain-joined host would be misrouted here, so "
            "verify against MCM (first fallback) before trusting it.")

    # 4) Unknown platform, no hint, not in map -> PDQ gap-filler tier.
    return _decision(
        "unknown", "pdq", _fallbacks_for("pdq"), "low",
        "No platform signal, no enrollment hint, and no local-map entry. PDQ "
        "Deploy/Inventory is the gap-filler of last resort for devices the three "
        "primary systems don't claim — LOW confidence; fall through the primaries "
        "if PDQ has no record either.")


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# python -m tools.device_authority
if __name__ == "__main__":
    import json as _json
    import sys as _sys

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr))

    cases = [
        ("BIOL-MAC-210", None),                                        # (a) mac -> jamf high
        ("LAPTOP-NEW-1", {"platform": "windows", "enrollment": "cloud-native"}),  # (b) intune high
        ("WIN-AMBIG-9", {"platform": "windows", "enrollment": "co-managed"}),     # (c) ambiguous low
        ("ZZZ-XYZ-UNKNOWN", None),                                     # (d) unknown -> pdq low
    ]
    for ident, hint in cases:
        print(_json.dumps({"identifier": ident, "hint": hint,
                           "decision": resolve_authority(ident, hint)},
                          indent=2, default=str))
