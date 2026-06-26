"""
tools/auth/sso_gate.py — SSO access-gating SCAFFOLD (read-only v1)
=================================================================
CofCITIP — interfaces/shape only. See docs/sso_access_gating_v1.md.

THIS MODULE DOES NOT IMPLEMENT REAL ENTRA AUTH.
- It does not call Microsoft Graph, MSAL, or any IdP.
- There is no Entra app registration behind it yet — that's the prerequisite
  the design doc names before any of this becomes real.
- It MUST NOT be wired into jarvis_core.py's (or jarvis_ui.py's) query path
  until the design doc's v1 flow has an actual app registration behind it.
  It is intentionally unwired and unreferenced from the running pipeline this
  session — it exists so the SHAPE is ready, not so it is load-bearing today.

What IS real here: reject_if_elevated(). v1's login screen only ever expects the
REGULAR identity; the elevated/PIM ("su-") identity is out of scope for v1
entirely (not merely unused). Since v1 has no design for what an elevated session
should be allowed to do, the only safe behavior is to refuse it outright — not
allow-and-log. reject_if_elevated() is that refusal.

Auth (who may ask) stays fully separate from authorization-to-act (the Stage 4
human-confirmation gate in package_pipeline.py). Nothing here touches that gate.
Pattern matches the existing connectors (structlog, Pydantic, mock-mode) — no
print() in runtime paths.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal

import structlog
from pydantic import BaseModel, Field, field_validator

log = structlog.get_logger("jarvis.auth.sso_gate")

MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"

# Convention for the elevated/PIM identity's UPN. At CofC the elevated tier is a
# SEPARATE Entra identity (distinct UPN), not a flag — commonly an "su-" prefix
# on the local part (e.g. "su-jdoe@cofc.edu"). Tunable here; a real build would
# confirm the actual elevated-account naming with IAM rather than guessing.
_ELEVATED_LOCALPART_PREFIXES = ("su-", "su.", "su_")

# Pointer used in every NotImplementedError so a future implementer lands on the
# design doc rather than guessing intent.
_DESIGN_DOC = "docs/sso_access_gating_v1.md"
_NOT_IMPLEMENTED = (
    "SSO gating is scaffolding only — no Entra app registration exists yet. "
    f"See {_DESIGN_DOC} (read-only v1) before implementing real auth."
)


class ElevatedIdentityRejected(Exception):
    """Raised when an elevated/PIM ("su-") identity attempts to log into JARVIS.

    v1's login screen expects the REGULAR identity only and has no design for an
    elevated session — so an elevated login is refused outright, not handled."""


class IdentityRecord(BaseModel):
    """Minimal identity record — validation scaffolding only (not persisted, not
    yet produced by any real auth flow)."""

    upn: str
    identity_type: Literal["regular", "elevated"]
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("upn")
    @classmethod
    def upn_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("upn cannot be empty")
        return v


def _is_elevated_upn(upn: str) -> bool:
    """Pure pattern check: does this UPN's local part carry the elevated prefix?
    Mock stand-in for what a real build would determine authoritatively against
    Entra, not by string convention."""
    local = (upn or "").strip().lower().split("@", 1)[0]
    return local.startswith(_ELEVATED_LOCALPART_PREFIXES)


def validate_session(token: str) -> bool:
    """Is this an authenticated, valid CofC Entra session?

    v1: identity-presence IS the entire authorization decision. SCAFFOLD ONLY —
    in mock mode returns True (so the shape can be exercised without an IdP);
    otherwise raises NotImplementedError, because there is no real Entra
    validation behind this yet. A real implementation validates the token
    against the (not-yet-created) Entra app registration."""
    if MOCK_MODE:
        log.info("sso.validate_session.mock", result=True)
        return True
    raise NotImplementedError(_NOT_IMPLEMENTED)


def get_identity_type(upn: str) -> str:
    """Return "regular" or "elevated" for a UPN.

    Exists so a future build can refuse/flag elevated-identity logins without
    having already decided what to *do* with them. v1 calls this for awareness
    (via reject_if_elevated) only. SCAFFOLD ONLY — mock mode uses the "su-"
    prefix convention; otherwise raises NotImplementedError, since authoritative
    identity-type determination requires real Entra integration."""
    if MOCK_MODE:
        return "elevated" if _is_elevated_upn(upn) else "regular"
    raise NotImplementedError(_NOT_IMPLEMENTED)


def reject_if_elevated(upn: str) -> None:
    """Enforce v1's regular-identity-only rule. If the UPN is an elevated/PIM
    identity, raise ElevatedIdentityRejected AND write a warning-level audit
    line — refuse outright, never allow-and-log. Returns None for a regular
    identity. This is the one piece of v1 that is real (exercised in mock)."""
    if get_identity_type(upn) == "elevated":
        log.warning("sso.elevated_identity_rejected", upn=upn,
                    reason="v1 expects the regular identity only; the elevated/"
                           "PIM identity is out of scope for v1 and is refused "
                           "at login (no elevated-session design exists yet).")
        raise ElevatedIdentityRejected(
            f"Elevated/PIM identity '{upn}' is not permitted to log into JARVIS "
            f"in v1. Use your regular CofC identity. See {_DESIGN_DOC}.")
    log.info("sso.identity_accepted", upn=upn, identity_type="regular")


# ── MANUAL SMOKE TEST ─────────────────────────────────────────────────────────
# python -m tools.auth.sso_gate   (exercises the mock-mode paths only)
if __name__ == "__main__":
    import sys as _sys

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr))
    os.environ["JARVIS_MOCK"] = "true"
    MOCK_MODE = True

    print("validate_session(mock):", validate_session("fake-token"))
    print("get_identity_type(regular):", get_identity_type("jdoe@cofc.edu"))
    print("get_identity_type(elevated):", get_identity_type("su-jdoe@cofc.edu"))
    reject_if_elevated("jdoe@cofc.edu")  # returns None, logs acceptance
    try:
        reject_if_elevated("su-jdoe@cofc.edu")
    except ElevatedIdentityRejected as e:
        print("reject_if_elevated raised as expected:", e)
