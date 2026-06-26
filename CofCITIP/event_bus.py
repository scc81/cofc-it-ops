"""
event_bus.py — Minimal in-process pub/sub for JARVIS Core
=========================================================
CofCITIP — Phase 2, Session 3 (OpenJarvis EventBus pattern, grafted in).

WHAT THIS IS / IS NOT:
- IS: a tiny synchronous fan-out so SIDE EFFECTS (audit logging, learning-
  loop feedback, Teams notifications, trace assembly) can be decoupled from
  the main query path instead of being inline calls scattered through it.
- IS NOT: a place for safety-critical control flow. The FERPA guard, egress
  router decision, and the human confirmation gate are NOT events and never
  pass through here. They are synchronous gates that block/allow execution
  inline in the call path. A fire-and-forget handler can be unsubscribed or
  can swallow an exception — that is exactly why gates must never live on the
  bus. This module is observability + notification only.

Design:
- Synchronous dispatch: publish() fires every subscribed handler in order,
  in the caller's thread, BEFORE returning. No threads, no async, no queue —
  nothing to lose on restart, nothing to race. "Lightweight" per session
  brief; no new external dependencies.
- Handler isolation: a raising handler is caught and structlog-logged; it
  never propagates to the publisher and never blocks sibling handlers. A
  broken audit sink must not crash a query, same contract as the old inline
  audit() try/except.
"""

from __future__ import annotations

from typing import Callable

import structlog

log = structlog.get_logger("jarvis.event_bus")

# Handler signature: (payload: dict) -> None. Return values are ignored —
# this is fire-and-forget by construction so no caller can depend on a
# handler's output for control flow (which would smuggle logic onto the bus).
Handler = Callable[[dict], None]


class EventBus:
    """In-process synchronous pub/sub. One instance lives on JarvisCore."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = {}

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register handler for event_type. Multiple handlers per type allowed;
        they fire in subscription order."""
        self._subscribers.setdefault(event_type, []).append(handler)
        log.debug("event.subscribed", event_type=event_type,
                  handler=getattr(handler, "__name__", repr(handler)))

    def publish(self, event_type: str, payload: dict) -> None:
        """Fire all handlers for event_type synchronously. Each handler's
        exceptions are caught + logged so one bad subscriber can't crash the
        publisher or starve the others. Returns nothing — fire-and-forget."""
        handlers = self._subscribers.get(event_type, ())
        for handler in handlers:
            try:
                handler(payload)
            except Exception as e:  # isolation: never let a side effect crash flow
                log.error("event.handler_failed", event_type=event_type,
                          handler=getattr(handler, "__name__", repr(handler)),
                          error=str(e))
