"""
tools/websearch.py — SearXNG opt-in research connector
=======================================================
CofCITIP — Phase 2, Session 5.

WHAT THIS IS:
  An OPT-IN external research path. It hits a self-hosted SearXNG instance
  (metasearch, on-box at SEARXNG_HOST) and returns title/url/snippet results
  for non-sensitive research questions.

ZERO-EGRESS / FERPA POSTURE (non-negotiable):
  - This tool is gated by the EXISTING egress flag EGRESS_RESEARCH. If
    EGRESS_RESEARCH == "local" (the default), search() refuses and returns
    {"results": [], "blocked": True, "reason": ...} — it does NOT raise, so a
    caller needs no special-case exception handling for the disabled state.
    Default config keeps this OFF; it only activates when an operator has
    deliberately set EGRESS_RESEARCH to a non-local value.
  - This is NOT an ops-data tool. It never touches Intune/Jamf/Taegis/Defender
    /SCCM/ServiceNow data, so it is deliberately NOT in jarvis_core's
    TOOL_DATA_LOCAL FERPA set. Its only governance is EGRESS_RESEARCH.
  - SearXNG itself is bound to localhost on BB (see jarvis-install.sh). The
    only egress is SearXNG's own upstream metasearch — the same opt-in network
    research the EGRESS_RESEARCH flag exists to authorize.

SEARXNG API NOTES (verified against current SearXNG docs, 2026):
  - Endpoint: GET {SEARXNG_HOST}/search?q=...&format=json
  - JSON output is DISABLED by default in SearXNG; the install step writes a
    settings.yml enabling `formats: [html, json]`. Without it the instance
    returns 403 on format=json. Documented here so a 403 is diagnosable.
  - Result fields are title / url / content (we map content -> snippet).

Resilience (same pattern as every other connector):
  pydantic param validation -> ratelimit -> tenacity retry (backoff+jitter)
  -> pybreaker circuit breaker. Mock mode returns canned results and never
  calls out, even to localhost.

Tool function (signature: (params: dict) -> dict):
  search(params)  params: {"query": str, "max_results": int = 5}
                  -> {"results": [{"title","url","snippet"}], "blocked": bool}

Mock mode: JARVIS_MOCK=true.
"""

from __future__ import annotations

import os
import time

import httpx
import pybreaker
import structlog
from pydantic import BaseModel, field_validator
from ratelimit import limits, sleep_and_retry
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_random_exponential)

log = structlog.get_logger("jarvis.tools.websearch")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SEARXNG_HOST = os.getenv("SEARXNG_HOST", "http://localhost:8888")
# Read the SAME egress flag jarvis_core's EgressRouter uses. local = blocked.
EGRESS_RESEARCH = os.getenv("EGRESS_RESEARCH", "local")
MOCK_MODE       = os.getenv("JARVIS_MOCK", "false").lower() == "true"
MAX_RETRIES     = int(os.getenv("SEARXNG_MAX_RETRIES", "3"))
# SearXNG is local metasearch; be polite to its upstreams. 30/min is plenty.
SEARXNG_CALLS_PER_MINUTE = int(os.getenv("SEARXNG_RATE_LIMIT_RPM", "30"))
SEARXNG_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "15"))

searxng_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60,
                                           name="searxng")


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit.state_change", breaker=cb.name,
                    old=old_state.name, new=new_state.name)


searxng_breaker.add_listener(_BreakerLogger())


# ── INPUT VALIDATION ──────────────────────────────────────────────────────────
class SearchParams(BaseModel):
    query: str
    max_results: int = 5

    @field_validator("query")
    @classmethod
    def query_ok(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("query cannot be empty")
        return v[:400]  # SearXNG handles long queries poorly; trim, don't error

    @field_validator("max_results")
    @classmethod
    def max_ok(cls, v: int) -> int:
        return max(1, min(v, 10))  # clamp 1–10


# ── HTTP LAYER (rate limit -> retry -> breaker) ───────────────────────────────
@searxng_breaker
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    # backoff + jitter — wait_random_exponential spreads retries so concurrent
    # callers don't thunder the local instance in lockstep.
    wait=wait_random_exponential(multiplier=1, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
@sleep_and_retry
@limits(calls=SEARXNG_CALLS_PER_MINUTE, period=60)
def _searxng_get(query: str, max_results: int) -> list[dict]:
    """Single SearXNG JSON query. Returns normalized title/url/snippet dicts."""
    started = time.monotonic()
    resp = httpx.get(
        f"{SEARXNG_HOST.rstrip('/')}/search",
        params={"q": query, "format": "json"},
        timeout=SEARXNG_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in (data.get("results") or [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            # SearXNG calls the snippet "content"; normalize to "snippet".
            "snippet": (r.get("content") or "")[:500],
        })
    log.info("searxng.query", query=query, hits=len(results),
             duration_ms=int((time.monotonic() - started) * 1000))
    return results


# ── MOCK ──────────────────────────────────────────────────────────────────────
def _mock_results(query: str, max_results: int) -> list[dict]:
    """Canned, plausible results. Never calls out — not even to localhost."""
    base = [
        {"title": f"{query} — overview",
         "url": "https://example.edu/overview",
         "snippet": f"A high-level overview of {query}, including background "
                    f"and common considerations for IT teams."},
        {"title": f"Best practices: {query}",
         "url": "https://example.org/best-practices",
         "snippet": f"Recommended practices and pitfalls when dealing with "
                    f"{query} in an enterprise endpoint environment."},
        {"title": f"{query} — vendor documentation",
         "url": "https://docs.example.com/reference",
         "snippet": f"Reference documentation covering {query} configuration "
                    f"and troubleshooting steps."},
    ]
    return base[:max_results]


# ── TOOL FUNCTION ─────────────────────────────────────────────────────────────
def search(raw_params: dict) -> dict:
    """Opt-in web research via SearXNG. Gated by EGRESS_RESEARCH.

    Returns {"results": [...], "blocked": bool, "reason"?: str}. When research
    egress is disabled, returns blocked=True with empty results rather than
    raising, so callers need no special exception handling."""
    p = SearchParams(**(raw_params or {}))

    # ── EGRESS GATE (opt-in) ──────────────────────────────────────────────────
    # Mirrors EgressRouter.route_research: "local" means no external research.
    # This is the structural guarantee that web search is off by default.
    if EGRESS_RESEARCH == "local":
        reason = ("web research is disabled — EGRESS_RESEARCH=local. Set "
                  "EGRESS_RESEARCH to an external value to opt in.")
        log.info("websearch.blocked", query=p.query, reason=reason)
        return {"results": [], "blocked": True, "reason": reason}

    log.info("tool.start", tool="websearch.search", query=p.query,
             max_results=p.max_results, mock=MOCK_MODE)

    if MOCK_MODE:
        results = _mock_results(p.query, p.max_results)
        log.info("tool.success", tool="websearch.search", mock=True,
                 hits=len(results))
        return {"results": results, "blocked": False, "mock": True}

    try:
        results = _searxng_get(p.query, p.max_results)
    except pybreaker.CircuitBreakerError:
        # Breaker open — degrade to empty rather than raising into the loop.
        log.warning("websearch.breaker_open", query=p.query)
        return {"results": [], "blocked": False,
                "reason": "search temporarily unavailable (circuit open)"}
    except httpx.HTTPError as e:
        log.error("websearch.failed", query=p.query, error=str(e))
        return {"results": [], "blocked": False,
                "reason": f"search unavailable: {e}"}

    log.info("tool.success", tool="websearch.search", hits=len(results))
    return {"results": results, "blocked": False}


def health_check(raw_params: dict | None = None) -> dict:
    """Part 3 contract: {"status": "ok"|"degraded"|"down", "detail": str}.
    No network probe (don't burn the upstream); reports config + breaker +
    whether research egress is even enabled."""
    if EGRESS_RESEARCH == "local":
        status, detail = "ok", "disabled (EGRESS_RESEARCH=local) — opt-in path"
    elif MOCK_MODE:
        status, detail = "ok", "mock mode"
    elif not SEARXNG_HOST:
        status, detail = "down", "SEARXNG_HOST missing"
    elif searxng_breaker.current_state != "closed":
        status = "degraded"
        detail = f"circuit breaker {searxng_breaker.current_state}"
    else:
        status, detail = "ok", "configured; breaker closed"
    result = {"source": "websearch", "status": status, "detail": detail,
              "mock": MOCK_MODE, "egress_research": EGRESS_RESEARCH,
              "breaker": searxng_breaker.current_state}
    log.info("tool.health", **result)
    return result


# ── TEST HARNESS ──────────────────────────────────────────────────────────────
# EGRESS_RESEARCH=searxng JARVIS_MOCK=true python -m tools.websearch --query "intune autopilot"
if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_sys.stderr)
    )

    parser = argparse.ArgumentParser(description="SearXNG connector test harness")
    parser.add_argument("--query", default="intune autopilot best practices")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        MOCK_MODE = True

    print(_json.dumps(
        search({"query": args.query, "max_results": args.max_results}),
        indent=2, default=str))
