# MOMI Face — Fix: EFC EAWB-transmission check never reached the Orchestrator

## The failing request

> Can you check in EFC (https://us.eftcloud.com/Efc/MainPage) if EAWB was transmitted for 077-59696792

## Root cause (two compounding gaps)

1. **The EFC rule in `Freight Rule Router` only matched WRITE verbs.**
   It required `(efc|billing|invoice|invoicing)` **AND**
   `(create|new|add|update|open|post|save|send)`. A read-only status check
   ("check … if EAWB **was transmitted**") contains none of those verbs, so
   `rule_hit` stayed false and the message fell through to the local 7B agent
   (`MOMI` node) — the exact flaky path the EFC branch was built to bypass.
   The 7B then either answered without calling a tool or picked the wrong
   one, so the check never happened.

2. **`Prepare EFC Billing Call` was billing-only.**
   Even if the message had somehow routed EFC, the prepare node forwarded the
   raw query with no instruction about *what to do* in EFC, and its notes/ask
   text assumed a billing job.

## The fix

### `nodes/freight-rule-router.js` (paste into the **Freight Rule Router** Code node)

Adds **FAST PATH 3b — EFC lookups AND actions**, placed before the web fast
path and the freight keyword rules:

- Matches the EFC system by name/URL/artifact: `efc | eftcloud | e-awb | eawb | fwb | fhl`
- Fires on **read verbs** (`check, verify, confirm, status, transmitted, sent, received, was, has, did …`) **or** write verbs — so both lookups and billing route EFC deterministically.
- Guard: pure SOP/procedure questions ("what is our procedure for EAWB transmission?") still route `DOCUMENT`.
- Also added `eawb|efc|eftcloud` to `WEB_NEGATE` so an EFC check can never be misrouted to DuckDuckGo.

### `nodes/prepare-efc-call.js` (paste into the **Prepare EFC Billing Call** Code node)

- Extracts the MAWB in canonical `3-8` form (`077-59696792`, tolerating spaces/`- `) in addition to `R######` refs.
- Detects intent: `check_eawb` (read-only) vs `billing` (unchanged old behaviour).
- For `check_eawb`, builds an explicit, deterministic Orchestrator instruction:
  open `https://us.eftcloud.com/Efc/MainPage`, search the MAWB, open the
  shipment, read the EAWB/FWB transmission status + timestamp/acks, and
  **READ-ONLY — do not create, edit, or retransmit anything**.
- When no ref is present, the zero-token clarifying question now matches the intent ("Which shipment should I check in EFC? …").

No wiring changes needed — the existing switch output `EFC → Prepare EFC
Billing Call → EFC Ref Known? → Call Orchestrator Direct → Format EFC Reply`
carries the new intent as-is. `Format EFC Reply` already surfaces
approval/failure/placeholder states, so a placeholder EFC adapter will be
reported honestly instead of claiming success.

> One thing this fix cannot do by itself: the **Orchestrator** (workflow
> `bM1IKXOZ7dUqtCup`) must have a real EFC adapter (Playwright/browser or API)
> that can log in to eftcloud and read the shipment's communication status.
> If that adapter is still a placeholder, MOMI will now correctly say so
> instead of silently failing.

## Verification

`node momi/tests/run-tests.js` runs both node scripts outside n8n
(mocking `$json`, `$getWorkflowStaticData`, `$execution`) — 22 checks:

- the exact failing query routes `EFC`, extracts `077-59696792`, sets `efc_action=check_eawb`, and emits a READ-ONLY orchestrator query
- variants (`eftcloud`, `FWB status`) route `EFC`
- billing path is byte-for-byte compatible (ref, rows longest-first, `no figures`)
- regression: `hi`→SMALLTALK, `thanks`/`2+2`→TRIVIAL, ClickUp audit→AUDIT, weather→WEB, SOP questions→DOCUMENT
- missing ref asks a clarifying question with zero tokens

```
ALL TESTS PASSED
```
