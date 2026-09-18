# momi-efc — deterministic EFC EAWB check (no LLM in the loop)

Answers: *"check in EFC if EAWB was transmitted for 077-59696792"* in ~30s,
scripted end-to-end. The Playwright MCP node on the MOMI agent stays useful
for *ad-hoc* browsing, but this fixed, repeating check runs as a script —
a 7B with a 4k context cannot reliably drive an 8–12 step browser session
(one page snapshot alone can exceed its whole context window).

## Files

| File | Purpose |
|---|---|
| `check_eawb.js` | Playwright script: login → search MAWB → open shipment → read EAWB/FWB status. READ-ONLY (a `FORBIDDEN_CLICK` guard refuses Save/Send/Transmit/Delete buttons). Prints one JSON result; saves screenshot+HTML artifacts on any failure. |
| `selectors.json` | Selector candidates per step, tried in order. Calibrate the site here — no code changes. |
| `efc-service.js` | HTTP wrapper (port **8768**, same pattern as momi-websearch on 8767). `GET /check-eawb?mawb=...`, `GET /health`. Serializes runs. |
| `../nodes/format-efc-check-reply.js` | n8n Code node: renders the JSON verbatim (0 tokens, cannot hallucinate a status). |

## Host setup (next to your momi-websearch container)

```bash
cd momi/efc
npm init -y && npm i playwright && npx playwright install chromium
export EFC_USER='your-efc-username'
export EFC_PASS='your-efc-password'          # never hardcode; env only
node efc-service.js                          # listens on 0.0.0.0:8768
```

One-off test without the service:

```bash
EFC_USER=... EFC_PASS=... EFC_HEADLESS=0 node check_eawb.js 077-59696792
```

`EFC_HEADLESS=0` shows the browser — use it for the **first calibration run**.

## First-run calibration (expected!)

`selectors.json` ships with sensible guesses for a typical .NET portal like
eftcloud, but the first run will likely stop at `login` or `search` and drop
`artifacts/<step>.png` + `.html`. Open the HTML, find the real field
ids/names, and put them at the **front** of the matching selector list.
Two or three iterations and it's locked in — deterministic from then on.

## n8n wiring (Face workflow)

The Freight Rule Router + Prepare EFC Billing Call fixes from the previous
change already classify this query as `route=EFC`, `efc_action=check_eawb`,
`efc_mawb=077-59696792`. Add a scripted sub-branch for the check intent:

1. **IF node** "EAWB Check?" after `EFC Ref Known?`(true):
   condition `{{ $json.efc_action }}` equals `check_eawb`.
2. TRUE → **HTTP Request** "Call EFC Check Service":
   - GET `http://host.docker.internal:8768/check-eawb`
   - Query parameter: `mawb` = `{{ $json.efc_mawb || $json.efc_ref }}`
   - Options → timeout `240000`, On Error → *Continue (using regular output)*
3. → **Code node** "Format EFC Check Reply" (paste
   `nodes/format-efc-check-reply.js`) → connect to **Prepare WhatsApp Reply**.
4. FALSE → existing `Call Orchestrator Direct` (billing path unchanged).

```
EFC Ref Known? ─true─► EAWB Check? ─true─► Call EFC Check Service ─► Format EFC Check Reply ─► Prepare WhatsApp Reply
                              └─false─► Call Orchestrator Direct ─► Format EFC Reply (unchanged)
```

## Why not let MOMI + Playwright MCP do it?

- The EFC branch **bypasses** the MOMI agent by design (its flaky tool
  calling caused the R263550 failure) — the MCP tool never sees EFC queries.
- qwen2.5:7b @ numCtx 4096: one `browser_snapshot` of an enterprise page is
  3k–10k tokens → context overflow before step 2.
- ~3.8 tok/s × 8–12 reasoning rounds ≈ 20–40 min per check, with a real risk
  of a hallucinated "transmitted". The script reads the literal page text and
  answers `null` + screenshot when ambiguous — it cannot invent a status.

Keep the MCP node attached to MOMI for one-off browsing tasks that genuinely
need judgment; promote any browsing task you repeat into a script like this.
