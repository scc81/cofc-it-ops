# JARVIS — Output Provenance Taxonomy (FACT / OPINION / UNKNOWN)

**Reference doc only.** This formalizes the provenance taxonomy flagged in the
Phase 3 checklist (Sidix reference). It is the spec for a **future**
`jarvis_core.py` prompt/output change — **no code change to `jarvis_core.py`
this session.** When that output-formatting change is actually made, this is the
contract it should implement.

---

## The three tags

### FACT
A claim **sourced from a live connector call or a logged/cited document**.

In practice, what determines the tag: the claim traces to a `tool_calls` result
**this turn** (e.g. an `intune.query_compliance` / `jamf.query_fleet` /
`servicenow_get_ticket` response), or to a specific cited artifact (a runbook
entry, an audit-log record, a KB article returned this turn). If you can point
at the tool result or document the claim came from, it's a FACT.

### OPINION
**Model reasoning or synthesis not directly backed by a tool result this turn.**

Inference, generalization, prioritization ("the bigger risk is X"), or advice
the model produced by reasoning over context — including reasoning *about* FACTs
— but which is not itself a value read from a connector or document this turn.
Plausible and useful, but not grounded data. A summary that *combines* facts is
itself OPINION about those facts unless it only restates them.

### UNKNOWN
**The model has no grounding for the claim and should say so rather than guess.**

When neither a tool result nor cited context supports an answer, the correct
output is an explicit UNKNOWN ("I don't have data on that") — never a confident
fabrication. UNKNOWN is a first-class, *good* answer here, not a failure.

---

## Why this matters (even in read-only v1, before any delegated auth)

Any single JARVIS answer can already **blend three sources**: a live (read-only)
connector call, retrieved ChromaDB context (past memories/corrections), and the
model's own reasoning. To the person reading or hearing the answer these are
indistinguishable unless tagged — yet they carry very different trust. A tech
acting on "DEVICE-123 is non-compliant" needs to know whether that came from a
live Intune read this turn (FACT), from a months-old memory (closer to OPINION
until re-verified), or from the model inferring it (OPINION/UNKNOWN). This is
true **now**, in v1, where every user already gets blended read-only +
memory + reasoning output — it does not wait on delegated auth or write actions
to start mattering. Mislabeling reasoning as fact is how a confident wrong
answer gets acted on.

---

## Status / follow-up

- This is a **reference doc**, not an implementation.
- Implementing it is a future `jarvis_core.py` change to the system
  prompt and/or the output-formatting/summarization step (e.g. instructing the
  local model to tag spans, or post-tagging based on whether a `tool_calls`
  result was present this turn). Revisit this doc when that change is made.
- Ordering note: provenance labeling is independent of SSO access-gating
  (`sso_access_gating_v1.md`); neither blocks the other.
