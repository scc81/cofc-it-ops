# JARVIS — SSO Access-Gating Design (Read-Only v1)

**Status:** internal design doc, not yet raised with anyone. Built ready, to be
surfaced *if/when* it earns the conversation — same posture as the deferred
credential-request and IP-ownership items. Nothing here is scheduled, imminent,
or committed. This document **supersedes** the four-way-delegation auth model
sketched in `jarvis_phase3_native_ui.md` §9 (three-tier delegated design); do
not implement that.

---

## 1. Status block (three independent facts)

These three lines are each true *today*, and each is independent — none implies
the others:

- **System status:** JARVIS Core runs on BB (Ubuntu 24.04, A2000 6GB); the
  desktop shell, connectors, and pipeline are functional.
- **Auth status:** JARVIS has **no login gating of any kind**. Anyone with
  shell/network access to BB can query it. Nothing in this document is built.
- **Data status:** all connectors run **mock-mode only** — no live Graph/Jamf/
  ServiceNow/Taegis data is flowing yet.

"JARVIS runs" does not imply "JARVIS is gated." "Org SSO is live" (§2) does not
imply "JARVIS uses it." "Connectors exist" does not imply "they return live
data." Keep these separate when reasoning about readiness.

---

## 2. Org-level SSO state (confirmed against each console, not assumed)

All four downstream systems **already have org-wide SSO live today**. None is
"in progress," pending, or absent. JARVIS neither configures nor depends on any
of this being built — it already is. This is existing CofC infrastructure.

| System | SSO state | Identity used |
|---|---|---|
| Entra ID + Intune | Live, PIM-tiered | **Exception — see below** |
| Jamf | Live (SAML), confirmed | Regular (non-elevated) account |
| ServiceNow | Live (same Entra IdP) | Regular account |
| Taegis | Live (SAML 2.0, Enterprise SSO, long-dated cert) | Regular account |

**The Entra/Intune exception — shape, not status.** Entra's elevated/PIM-admin
tier is a **separate Entra identity** (a distinct UPN, e.g. an `su-`-prefixed
account), **not a flag on the regular account**. There is no single login that
silently carries both tiers: authenticating to Entra/Graph means picking one
identity or the other up front. Jamf, ServiceNow, and Taegis all authenticate
via the **single regular identity**; only Entra/Graph/Intune forces a choice
between the regular identity and a separate elevated identity. (Taegis in
particular is **not** API-key-only as once assumed — it has full org SSO like
the others.)

**Independence:** org SSO being fully live everywhere **does not change
JARVIS's own auth status**. JARVIS still has nothing built and still uses
service-account connectors only. Org readiness and JARVIS's auth posture are
separate facts.

---

## 3. v1 scope — read-only-for-everyone

**The entire authorization decision in v1 is binary identity-presence:**

- A valid CofC Entra identity ⇒ allowed to query JARVIS *at all*.
- Invalid / no identity ⇒ no access.

**Every authenticated user gets identical answers.** There is no elevated/admin
answer tier in v1. Answers are scoped to JARVIS's **existing service-account
credentials**, per connector, exactly as `intune.py` works today. JARVIS's own
connector calls stay on JARVIS's service credentials **regardless of which
identity the person used to log into JARVIS** — v1 never re-authenticates the
person against any downstream system and never makes a delegated call.

**Regular identity only at login.** Because the elevated tier is a *separate
identity* rather than a flag, there is no way to "check elevation without
branching on it." v1 sidesteps this completely: **the login screen only ever
expects the regular identity.** The elevated/`su-` identity is **out of scope
for v1 entirely** — not merely unused. If someone attempts to log into JARVIS
with the elevated identity, v1 **rejects it outright** (see §4 / `sso_gate.py`'s
`reject_if_elevated`); designing what an elevated session *should* be allowed to
do is a v2 problem, deliberately not handled gracefully now.

**Criteria-bound, not calendar-bound.** This read-only-for-everyone period has
**no target date and no deadline.** It ends based on demonstrated usage,
functional maturity, and operational need — not the calendar. Candidate signals
that would *justify revisiting* (none of which is a commitment, and none of
which is dated):

- Sustained query volume showing read-only is a genuine, recurring bottleneck
  for the team — not a one-off wish.
- A concrete, repeated use case that read-only cannot serve at all (e.g. a
  workflow that inherently needs a per-user delegated action).
- Functional/operational maturity reaching the point where the *absence* of
  per-identity scoping is the limiting factor, rather than connector coverage
  or data quality.

**Auth vs. authorization-to-act stay fully separate.** SSO answers only "is this
a valid logged-in user." It never answers "should this action execute" — that
remains the existing Stage 4 human-confirmation gate in `package_pipeline.py`,
which this work does not touch.

---

## 4. Auth flow (v1)

```
1. User opens JARVIS desktop UI.
2. UI initiates Entra login — REGULAR IDENTITY ONLY.
   (No prompt for, and no acceptance of, the elevated/su- identity.)
3. Identity check — THE ENTIRE GATE:
      valid CofC Entra identity?  yes -> continue
                                  no  -> deny access, no query path opened
4. Guard: if the authenticated UPN is an elevated/su- identity ->
      reject outright (reject_if_elevated), warning-audited. v1 has no
      design for an elevated session, so it is refused, not handled.
5. Authenticated (regular identity) -> all queries route through JARVIS's
   EXISTING service-account connectors (Graph/Jamf/ServiceNow/Taegis),
   identical for every user. No delegated call is ever made. Which identity
   logged in changes nothing about how connectors authenticate.
```

The logged-in identity gates *access to JARVIS*. It is never propagated
downstream. Downstream auth is, and in v1 remains, JARVIS's service account.

---

## 5. Explicitly not yet (deferred to v2+, named — not work items, not dated)

- **Per-user delegated tokens / on-behalf-of flows** against Graph / Jamf /
  ServiceNow / Taegis. v1 never makes a delegated call.
- **Any handling of the elevated/`su-` Entra identity inside JARVIS** beyond
  rejecting it at login. v1 only ever expects the regular identity.
- **Mid-session token/identity revocation handling.** Not relevant in v1, which
  grants nothing tied to *which* identity logged in.

These are listed so the boundary is explicit, not because they are scheduled.

---

## 6. Stakeholder note

The eventual ask is small: a single **Entra app registration** for login gating
(native-app auth-code flow), nothing more. **John Schroeder** (senior admin —
Conditional Access / PIM) is the likely **technical point of contact**, not the
decision-maker. **Jim Bennett** (John's manager) is the likely **actual IAM
approval authority**. Both are **currently unaware** this is being considered;
**no conversation is scheduled.** This note is identification only — it is
deliberately *not* a set of talking points or a conversation outline.

---

## 7. Open questions

- **(a) Native-app auth shape.** Does CofC's Entra tenant support a clean
  auth-code flow for a native PySide6 desktop app (system browser / MSAL
  public-client), or will it require an embedded web-view? This determines how
  the login screen in §4 is actually built.
- **(b) v2 delegated-auth identity question.** *If* delegated Entra auth is ever
  pursued, does JARVIS authenticate as the regular identity, or could a person
  need/want to invoke it under their **elevated** identity? The latter implies
  JARVIS would, at some point, hold a session token tied to an admin-PIM
  account — with all the blast-radius and revocation consequences that carries.
  This is exactly why v1 refuses the elevated identity outright rather than
  quietly accommodating it.
