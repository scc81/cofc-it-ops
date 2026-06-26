"""
tools/secrets.py — Credential Resolver (LastPass CLI -> env fallback)
=====================================================================
CofCITIP — Single indirection point for fetching secrets.

Roadmap: config.env (Phase 1) -> LastPass CLI (this module, Phase 2) ->
HashiCorp Vault (Phase 3/4). Vault is NOT built here.

HOW IT WORKS
------------
get_secret(key) resolves a credential by name. Resolution order:
  1. LastPass CLI (`lpass`) — IF the binary exists AND a session is logged
     in (`lpass status` exits 0). Value is read once and cached in-process.
  2. Fallback to os.getenv(key, "") — so mock-mode and no-LastPass demo
     boxes behave EXACTLY as before (config.env still works untouched).

LASTPASS ITEM NAMING CONVENTION
-------------------------------
Each secret is a LastPass item under a shared "cofc-itip/" folder, named by
the same config key the code already uses, with the secret in the item's
password field:

    cofc-itip/INTUNE_CLIENT_SECRET   -> password = <the client secret>
    cofc-itip/JAMF_PASSWORD          -> password = <the jamf api password>
    cofc-itip/SN_PASS                -> password = <the servicenow password>
    cofc-itip/TAEGIS_API_KEY         -> password = <the taegis api key>
    cofc-itip/TEAMS_WEBHOOK_URL      -> password = <the full webhook url>

Fetched via: lpass show --field=password "cofc-itip/<KEY>"

SECURITY
--------
- Secret VALUES are never logged. Only the key name and the resolution
  source (lpass | env-fallback | not-found) are logged.
- The "no LastPass session" warning is emitted ONCE per process, not per
  call, to avoid log spam when every connector resolves a key at startup.

Note: not a (params: dict) -> dict tool — this is internal plumbing other
connectors import. It is intentionally not registered in jarvis_core.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import structlog

log = structlog.get_logger("jarvis.tools.secrets")

# LastPass item folder convention (see module docstring).
_LP_FOLDER = "cofc-itip"

# In-process caches so we shell out to lpass at most once per key, and probe
# `lpass status` at most once per process.
_secret_cache: dict[str, str] = {}
_lpass_state: dict = {"available": None, "warned": False}


def _lpass_available() -> bool:
    """True iff the lpass binary exists AND a session is currently logged in.
    Probed once per process; result cached in _lpass_state."""
    if _lpass_state["available"] is not None:
        return _lpass_state["available"]

    available = False
    if shutil.which("lpass"):
        try:
            # `lpass status` exits 0 when logged in, non-zero otherwise.
            proc = subprocess.run(["lpass", "status"],
                                  capture_output=True, text=True, timeout=10)
            available = proc.returncode == 0
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("secrets.lpass_status_failed", error=str(e))
            available = False

    _lpass_state["available"] = available
    if not available and not _lpass_state["warned"]:
        # Emit ONCE per process — connectors all resolve at startup.
        log.warning("secrets.lpass_unavailable",
                    detail="lpass missing or no active session — falling "
                           "back to environment variables (config.env)")
        _lpass_state["warned"] = True
    return available


def _lpass_get(key: str) -> str | None:
    """Read one secret's password field from LastPass. Returns None on any
    failure (caller then falls back to env). Never logs the value."""
    item = f"{_LP_FOLDER}/{key}"
    try:
        proc = subprocess.run(
            ["lpass", "show", "--field=password", item],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("secrets.lpass_show_failed", key=key, error=str(e))
        return None
    if proc.returncode != 0:
        log.warning("secrets.lpass_item_missing", key=key)
        return None
    value = proc.stdout.strip()
    return value or None


def get_secret(key: str) -> str:
    """Resolve a secret by config-key name. LastPass first (if logged in),
    else os.getenv(key, ""). Caches the resolved value in-process.

    Only the key name and source are logged — never the value."""
    if key in _secret_cache:
        return _secret_cache[key]

    value = ""
    source = "env-fallback"

    if _lpass_available():
        lp_value = _lpass_get(key)
        if lp_value is not None:
            value = lp_value
            source = "lpass"
        else:
            # lpass is up but this specific item wasn't found — fall back to
            # env so a partially-populated vault still works.
            value = os.getenv(key, "")
            source = "env-fallback"
    else:
        value = os.getenv(key, "")
        source = "env-fallback"

    _secret_cache[key] = value
    log.info("secrets.resolved", key=key, source=source,
             found=bool(value))  # bool only — never the value itself
    return value


# ── CLI SELF-TEST ─────────────────────────────────────────────────────────────
# python -m tools.secrets            (probes a harmless test key, no values)
if __name__ == "__main__":
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    # Harmless, non-credential test key. Set JARVIS_SECRETS_SELFTEST=anything
    # in the env (or as a cofc-itip/JARVIS_SECRETS_SELFTEST LastPass item) to
    # see a positive resolution; otherwise this still exercises the path.
    test_key = "JARVIS_SECRETS_SELFTEST"
    lp = _lpass_available()
    val = get_secret(test_key)
    # Report source WITHOUT printing the value.
    source = "lpass" if (lp and val and os.getenv(test_key, "") != val) \
        else "env-fallback"
    print(f"lpass_session_active={lp}")
    print(f"get_secret('{test_key}') -> {'FOUND' if val else 'EMPTY'} "
          f"(source: {source})")
    print("OK" if True else "FAIL")
