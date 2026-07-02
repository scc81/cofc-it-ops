# JARVIS — Web Architecture & Dashboard Expansion Plan

> Planning doc for handoff to Opus in Claude Code. This doc is self-contained —
> it assumes no prior chat history. If you (Opus) are reading this, treat every
> decision below as already made and settled; do not re-litigate them. Where
> something is explicitly marked OPEN QUESTION, stop and ask rather than guess.

---

## 0. Context — what JARVIS is, in one paragraph

JARVIS (CofCITIP) is a local, zero-egress, FERPA-compliant AI operations
assistant for a college IT endpoint team, built and owned by one engineer
(not yet disclosed to leadership — demo-first, raise it when it's earned).
It runs on a server called **BB** (Ubuntu Server 24.04, Ollama, ChromaDB,
systemd services `jarvis-core` :8081, `jarvis-litellm` :4000, `jarvis-mcpo`
:8000, `jarvis-ui` :8080). It has a tool-calling core (`jarvis_core.py`) with
a growing set of connectors to real IT systems (Intune, Jamf, MCM/SCCM, PDQ,
ServiceNow, Taegis/SentinelOne, Teams) — currently all in **mock mode**
(`JARVIS_MOCK=true`), since live credentials haven't been requested yet
(deliberate — same demo-first pattern).

**Non-negotiables — do not violate these regardless of what else this doc says:**
- FERPA firewall: any tool touching device/user/security data forces local
  Ollama inference, never external API calls, regardless of any other setting.
- Human confirmation gate (Stage 4 in `package_pipeline.py`, and the same
  pattern in `servicenow.py` writes): structural, cannot be bypassed by a
  flag or env var. Pipeline stages are deliberately excluded from the LLM's
  own tool registry — the LLM cannot decide to skip the gate.
- Mock-mode-first: every connector must work fully in mock mode before any
  live wiring. Mock-ness lives in the connector layer (e.g. `MOCK_MODE` env
  check inside `tools/intune.py`), never in the UI layer.
- No hardcoded credentials. structlog everywhere, no `print()` in runtime code.
- Known existing bug, NOT in scope for this work unless told otherwise:
  `jarvis_ui.py`'s `/query` endpoint has a `MOCK_MODE` short-circuit that
  bypasses `jarvis_core.py` entirely and returns a stub. Leave this alone
  unless a specific session prompt says to fix it.

---

## 1. The architecture decision this doc is built around

There are **two separate UI surfaces**, by deliberate decision, not by
accident. Do not collapse them into one or treat one as legacy:

### Surface A — Desktop app (PySide6 + Qt Quick 3D)
- Lives at `desktop/main.py` and subfolders (`desktop/orb/`, `desktop/tray/`,
  `desktop/gate/`, `desktop/monitor/`).
- **Role: local-only operator console for whoever is physically at BB** (or
  on the machine running it). Tray icon, global hotkey (Ctrl+Alt+J),
  `QProcess`-based native `systemctl` start/stop/restart for `jarvis-core`
  and `ollama`, the 3D presence orb (faceted icosahedron, state-driven
  shader), pyqtgraph stress panel, native confirmation-gate approve/deny
  queue panel.
- This already exists (Phase 3, Sessions A–F complete). **Nothing in this
  doc removes or replaces it.** It stays exactly as built. Voice input here
  is `voice_listener.py` — always-on wake-word loop (OpenWakeWord + Whisper),
  a separate process, mic physically near BB.

### Surface B — Web app (this doc's actual subject)
- Lives at `ui/jarvis_ui.py` (FastAPI backend) + `ui/index.html` (currently
  a single-file PWA: tabs for Brief / Query / Status).
- **Role: the everywhere-else surface.** Reachable by IP/hostname from any
  device on the network — phone, laptop, any browser. No native OS
  integration (no tray, no hotkey, no local process control — a browser
  tab structurally cannot do these on the *client* device, and that's fine,
  because the actual control target is always BB regardless of which
  device is looking at the page).
- Voice input on the web surface is **click-to-talk, not always-listening**:
  a mic button the user clicks, which records, sends audio to BB for
  transcription, and feeds the resulting text into the exact same `/query`
  path as typed text. Do not build a streaming/always-on listener for the
  web surface — that is explicitly out of scope.
- Confirmation gate (Stage 4) approve/deny is being added to **both**
  surfaces — the desktop native panel stays, AND new web API endpoints +
  UI buttons get added that call the same underlying `package_pipeline.py`
  validation/decision-write path. Same Stage 4 contract, two front doors.
  `write_decision`'s file-based write is a single atomic write per run —
  sanity-check (don't redesign) that two near-simultaneous approvals from
  the two surfaces can't corrupt a decision file, when you build this.

**This doc is entirely about expanding Surface B (the web app).** Surface A
is mentioned only for context — do not touch desktop app files unless a
specific session prompt says to.

---

## 2. Web app — overall page layout

Replace the current tab-bar (Brief / Query / Status) with a **sidebar nav**,
since the page is growing beyond three flat views:

```
┌──────────┬──────────────────────────────────────────────┐
│ JARVIS   │  [Connection: ● BB online]   [orb · small]    │   <- persistent header
│          ├──────────────────────────────────────────────┤
│ ● Brief  │                                                │
│ ○ Query  │         (active page renders here)            │
│ ○ Compli-│                                                │
│   ance   │                                                │
│ ○ Life-  │                                                │
│   cycle  │                                                │
│ ○ Threat │                                                │
│   Intel  │                                                │
│ ○ Gate   │                                                │
│ ○ Status │                                                │
└──────────┴──────────────────────────────────────────────┘
```

Pages, in build-priority order (see §5 for sequencing):

| Page | Status | Notes |
|---|---|---|
| Brief | Exists, keep as-is | Fleet Health / Compliance / Alerts / Patch Status cards, already wired to `tools/briefing.py` |
| Query | Exists, extend | Add mic button (click-to-talk, see §1) inline with the text input — same input box, voice is just an alternate way to fill it, not a separate mode |
| **Compliance** | **New — build first** | See §4 |
| **Lifecycle** | **New — build second** | See §4 |
| **Threat Intel** | **New — build third** | See §4 |
| Gate | New | Web approve/deny, calling `package_pipeline.py`'s existing validation path. Collapse to nothing in the nav badge/page when queue is empty — don't permanently reserve screen space for an empty state. |
| Status | Exists, keep as-is | Connector health grid, already wired to each connector's `health_check()` |

**Header bar, persistent across every page (not a tab):**
- Connection status (BB reachable / `jarvis-core` alive) — small badge,
  always visible. Never make the user navigate to find out if BB is down.
- The orb — small, presence/status indicator, NOT the dominant visual
  element. This is a work tool; the data cards carry the operational
  payload. (The orb's own visual redesign — faceted icosahedron to match
  desktop vs. a distinct web-native treatment — is a SEPARATE, already-
  tracked open decision; see `jarvis_ui_rerender_brief.md` if it exists in
  the repo. Do not redesign the orb as part of this work unless explicitly
  told to. If no orb redesign work is in scope for this session, the
  existing pulsing-dot orb from current `index.html` is fine to reuse as-is.)

**Constraint carried over from the existing `index.html`:** it is currently
a single HTML file on purpose, because service workers (PWA support) need
same-origin URLs and there's no build step/bundler in the picture. If this
work introduces a build step (bundler, framework), that is a deliberate
breaking change to that constraint — flag it explicitly, don't do it silently.

---

## 3. The real work: a merge/scoring engine, not just HTML

**This is the part that matters most. The sidebar/page shells are the easy
20% of this work. The other 80% is backend: none of Compliance, Lifecycle,
or Threat Intel have any real content to show until there's a layer that
calls multiple connectors, joins their results on a device identifier, and
computes derived state (compliance score, lifecycle status, coverage gaps).**

### Why this is needed (context on prior art, do not copy the old code directly)

There are older, fully separate, standalone portfolio-piece dashboards in
this codebase (`endpoint_collector.py` / `endpoint_dashboard.html`,
`lifecycle_tracker.py` / `compliance_report.py` / `os_readiness.py`, etc.).
**These are NOT part of JARVIS** — zero dependencies, built and run
independently, with their own mock data generators, predating or running
parallel to JARVIS. They proved out real, useful patterns:
- Multi-source merge on hostname into one normalized device record
- Compliance scoring from multiple inputs
- OS EOL/lifecycle flagging with department grouping
- "Coverage gap" detection (e.g., device missing its S1/Taegis agent)
- Sidebar-nav, filterable/searchable inventory tables

**The goal is JARVIS-native equivalents of these capabilities** — same or
better functional coverage, built fresh against JARVIS's own connector
pattern (Pydantic validation, pybreaker circuit breakers, structlog,
mock-mode-first, tenacity retries — see `tools/intune.py`, `tools/jamf.py`,
`tools/teams.py`, `tools/servicenow.py` for the exact pattern to follow).
**Do not import or wrap the old standalone scripts.** Build new code that
matches JARVIS's architecture, even though the *ideas* are proven.

JARVIS's advantage over the old dashboards: more connected sources
(Intune, Jamf, MCM, PDQ, ServiceNow, Taegis, Teams vs. the old ones' ~5
sources), a working source-of-authority router already built
(`tools/device_authority.py`, see below), and the Stage 4 gate for any
write actions — none of which the old dashboards had.

### Existing building blocks already in the repo — use these, don't rebuild them

- **`tools/device_authority.py`** — `resolve_authority(identifier, hint)`
  already exists and decides which system is authoritative for a given
  device (Mac → Jamf always; Windows split by enrollment state between
  Intune/MCM; PDQ as a low-confidence fallback). The merge engine should
  use this for routing single-device lookups, but the merge/dashboard
  layer's job is different: it's aggregating *across* devices for fleet-
  wide views, not resolving one device's authority.
- **Connector pattern to replicate exactly** (see `tools/intune.py`,
  `tools/jamf.py`, `tools/mcm.py`, `tools/pdq.py`, `tools/teams.py` as
  reference implementations):
  - Pydantic model for input params, with field validators
  - One `pybreaker.CircuitBreaker` per connector, named, with a
    `_BreakerLogger` listener that logs `circuit.state_change`
  - `tenacity` retry decorator with exponential backoff on the live-call path
  - `structlog` logger (`structlog.get_logger("jarvis.tools.<name>")`)
  - `MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"` gate
  - A `health_check()` function returning
    `{"source": ..., "status": "ok"|"degraded"|"down", "detail": str,
    "mock": bool, "breaker": <state>}` — **no network probe**, just
    creds-present + breaker state, so polling never burns API rate limits
  - A `__main__` CLI test harness block (argparse, `--function`, `--params`,
    `--mock`) matching the style in `teams.py` / `taegis.py`
- **`tools/briefing.py`** already has a pattern for calling multiple
  connectors' `health_check()` defensively (each import/call individually
  try/excepted, a failure becomes `"down"` rather than crashing the whole
  briefing) — follow this same defensive pattern in the merge engine.

### New module to build: `tools/device_merge.py`

This is the core new piece. Sketch of the interface (Opus: treat this as a
strong starting point, not gospel — adjust as real connector shapes demand,
but keep the contract simple and consistent with the rest of the codebase):

```python
"""
tools/device_merge.py — Cross-Connector Device Merge & Scoring Engine
=======================================================================
Joins device records from multiple connectors into one normalized view
per device, and computes derived state used by the Compliance, Lifecycle,
and Threat Intel web pages.

Does NOT replace device_authority.py — that resolves "which system is
authoritative for THIS ONE device" for single-device queries. This module
is for FLEET-WIDE aggregation across MANY devices at once, for dashboard
rendering.

Mock-mode-first, same as every other connector. Pulls from whatever mix of
connectors are available; a connector being down/unreachable degrades the
merge (fewer fields populated, flagged in coverage gaps) rather than
failing the whole call — same defensive pattern as briefing.py.
"""

# Public functions (params: dict) -> dict, same contract as every connector:

def merge_devices(params: dict | None = None) -> dict:
    """
    Pulls device-level data from intune, jamf, mcm, pdq, taegis (whichever
    are reachable), joins on hostname (case-insensitive, normalize before
    join), and returns one record per device with per-source fields plus
    derived scores. This is the data source for ALL THREE new pages —
    Compliance, Lifecycle, and Threat Intel are different VIEWS over the
    same merged record set, not three separate pulls.

    Returns:
    {
      "source": "device_merge",
      "mock": bool,
      "generated_at": iso timestamp,
      "device_count": int,
      "sources_included": [...],   # which connectors actually responded
      "sources_unavailable": [...], # which were down/unreachable
      "devices": [
        {
          "hostname": str,
          "platform": "windows"|"mac"|"other",
          "sources_present": [...],      # e.g. ["intune","taegis"] — used for coverage-gap detection
          "compliance_state": "compliant"|"warning"|"non-compliant"|"unknown",
          "lifecycle_status": "current"|"aging"|"end-of-life"|"unknown",
          "os_compliant": bool | "unknown",
          "device_age_days": int | None,
          "last_checkin_days": int | None,
          "threat_status": "clean"|"flagged"|"unknown",   # from taegis, if present
          "coverage_gaps": [...],   # e.g. ["missing_taegis_agent"]
          "raw": { "intune": {...}, "jamf": {...}, ... }  # per-source raw, for detail drill-down
        },
        ...
      ]
    }

def fleet_summary(params: dict | None = None) -> dict:
    """Aggregate counts over merge_devices() output — totals, % compliant,
    EOL counts, coverage-gap counts, grouped by platform/department where
    that data exists. This is what feeds the summary cards at the top of
    each dashboard page; the device-level table below it reads the
    `devices` list from merge_devices() directly."""

def health_check() -> dict:
    """Same contract as every other connector's health_check — but here it
    reports merge-engine health (can it currently reach enough sources to
    produce a useful merge), not a single external system's health."""
```

**Scoring logic to port (concept, not code) from the old standalone
scripts**, reimplemented fresh against JARVIS's connector outputs:
- Compliance: non-compliant if lifecycle is end-of-life OR check-in is
  stale OR OS is below baseline; warning if device is aging OR
  warranty is expiring; else compliant. (See old `lifecycle_tracker.py`
  `calc_overall_compliance` for the exact logic shape to adapt — same
  *idea*, rebuilt against JARVIS's actual field names.)
- Compliance: read directly from whatever compliance verdict Intune/Jamf
  policy already computes for each device (e.g. Intune's `complianceState`,
  Jamf's smart-group/compliance flags) — **do not compute OS compliance
  from a JARVIS-side version-baseline table.** CofC's environment has a
  mix of current and legacy/instrument-tied devices; a flat version floor
  per platform would misclassify intentionally-old machines as
  non-compliant. Policy already encodes the real verdict, including
  whatever legacy exceptions exist there. See
  `compliance_scoring_approach.py` and `device_merge.py`'s
  `_read_policy_compliance()` for the decided implementation.
  **OPEN QUESTION, not this work's job to resolve:** Steven has a meeting
  scheduled to clarify how legacy/instrument-tied devices are identified
  in Intune/Jamf today. If a legacy device's policy-reported state still
  comes back non-compliant after that meeting, the fix likely belongs in
  Intune/Jamf policy configuration, not a JARVIS-side override — don't
  build one preemptively. The separate "stale check-in" / "aging device"
  thresholds (unrelated to OS compliance) are still a normal open
  question — confirm real numbers with Steven, don't invent them.
- Coverage gaps: a device present in Intune/Jamf/MCM/PDQ inventory but
  absent from Taegis (or vice versa) is a gap — flag it, this was one of
  the most valuable findings in the old dashboards ("missing S1 agent").

---

## 4. The three new pages — what each one actually shows

All three read from `device_merge.merge_devices()` / `fleet_summary()` —
build the merge engine once, then these are presentation differences over
the same underlying data, not three separate backend pulls.

### Compliance page (build first)
- Summary cards at top: total devices, % compliant, non-compliant count,
  warning count (mirrors the existing Brief page's card style for
  consistency)
- Filterable/searchable table below: one row per device, columns for
  hostname, platform, compliance_state, the specific reason (lifecycle/
  check-in/OS — whichever tripped the non-compliant flag), last check-in
- This is the highest demo value of the three — build and prove this one
  end-to-end (merge engine + this page) before replicating the pattern for
  Lifecycle and Threat Intel. Do not build all three merge/scoring layers
  in one pass.

### Lifecycle page (build second)
- Summary cards: device age distribution, EOL count, warranty
  expiring/expired count
- Table: hostname, platform, device_age_days, lifecycle_status,
  os_compliant, warranty status, grouped/filterable by department if that
  field exists in any connector's data (check `config.py` / mock data
  generators for whether department is even tracked yet — if not, that's
  a gap to flag, not silently skip)

### Threat Intel page (build third)
- Surfaces `tools/taegis.py` data, which currently has a working connector
  (`get_alerts`, `get_alert_detail`, `get_investigations`) but **no UI home
  anywhere** — this page is purely additive, giving existing backend
  capability its first visible surface.
- Top alerts list (severity, host, title — same shape as the Brief page's
  existing "Overnight Alerts" top-3, just a full page instead of a
  3-item summary)
- Coverage gap callout: devices with no Taegis presence at all (from the
  merge engine's `coverage_gaps` field) — this was explicitly one of the
  old dashboards' most-valued findings ("the missing S1 agent story"),
  reproduce it here.

---

## 5. Build sequencing — do not deviate without flagging why

1. `tools/device_merge.py` — `merge_devices()` first, mock-mode-only,
   pulling from whichever connectors already have working mock data
   (intune, jamf, taegis at minimum — mcm/pdq if their mock generators
   are solid). Prove the join-on-hostname + scoring logic with mock data
   before touching any UI.
2. `fleet_summary()` in the same module, once `merge_devices()` is solid.
3. Compliance page — new FastAPI route in `jarvis_ui.py` serving merge
   data as JSON, new page/section in `index.html` (or whatever the sidebar
   nav refactor produces) consuming it. Get ONE page fully working,
   reviewed, demo-able.
4. Sidebar nav refactor in `index.html` — only once there's a second real
   page to navigate to (Compliance), not before. Building nav for pages
   that don't exist yet is wasted motion.
5. Lifecycle page — replicate the Compliance page's pattern.
6. Threat Intel page — replicate again.
7. Gate (web approve/deny) and Query mic button — these are independent of
   the merge engine and can be built in parallel/either order relative to
   steps 1–6, whenever convenient.

**Verification standard for every step** (matches existing project
discipline — see `CHANGELOG_C-F.md` for the pattern): `py_compile` on every
file touched; for any function with mock-mode logic, actually CALL it and
inspect real output, don't just read the code and assume it runs; for any
new FastAPI route, confirm it's reachable and returns the documented shape;
state plainly in the session TLDR what was NOT verified (e.g. "no real
browser render confirmed" if PySide6/frontend rendering can't be checked
in the sandbox) rather than implying more confidence than the testing
actually supports.

---

## 6. Explicit non-goals for this round of work

- Not touching the desktop app (Surface A) at all.
- Not redesigning the orb's visuals.
- Not building browser-based always-listening voice (click-to-talk only).
- Not wiring any live (non-mock) credentials — everything here stays in
  `JARVIS_MOCK=true` mode, same as the rest of the project.
- Not importing/wrapping the old standalone portfolio dashboards' code —
  rebuild the ideas fresh against JARVIS's own connector pattern.
- Not fixing the known `jarvis_ui.py` `/query` MOCK_MODE bypass bug unless
  a specific session explicitly says to (it's tracked separately).
