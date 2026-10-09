# MOMI run `MOMI-1791564644812` — root cause and fix

**Symptom**

```
❌ 1_pelicargo · Resolve Pelicargo shipment 057-79603090
   Executor returned no structured success result. (error: unstructured_executor_result)
✅ 1_credentials · Retrieve EFC credentials for 057-79603090 — Credentials retrieved (19 fields)
⏸ 1_efc · Create EFC Domestic Billing for 057-79603090
```

The billing was never created. Here is why, and what was changed.

---

## 1. What actually happened

The error text is a red herring. `unstructured_executor_result` is what MOMI says when it
*cannot read* an answer — it is not the answer itself. The real failure is one layer down:

```
"operations": [
  { "step": 0, "action": "launch", "success": false,
    "http_status": 0, "error": "HTTP 0 from Playwright service" }
]
```

**`http_status: 0` means no HTTP response at all** — connection refused, DNS failure, or the
listener is gone. The Playwright/browser service (`http://host.docker.internal:8766` by default)
was not reachable from inside the n8n container. The browser never launched, so nothing could
be scraped from Pelicargo.

The full chain:

| # | Where | What went wrong |
|---|-------|-----------------|
| 1 | Playwright service (host, :8766) | Down / unreachable → `HTTP 0`. **This is the root cause.** |
| 2 | `Call MOMI - Browser Automation` | Returned the raw executor payload `{session_id, operations:[...], combined_text, …}` with **no top-level `success` / `status` / `error` key**. |
| 3 | `Format Hub Result` | Tested for three known shapes (credential-store, executor top shape, generic success/status). None matched → fell through to the `else` → `unstructured_executor_result`. The real message was thrown away. |
| 4 | `Commit Tool Result` | Retried the dead service 3× (≈9 min at 180 s timeout), then marked `1_pelicargo` failed. |
| 5 | `Freight Playbook1` | `1_efc` had `depends_on: ["1_pelicargo", "1_credentials"]`, so the billing step was never allowed to run. |
| 6 | `Find Next Ready Step1` | No ready step + a failed step → `execution_status: "failed"`. |
| 7 | `Route By Execution Status` | No rule matched (`failed` output requires `needs_replan === true`, which the web-family no-replan rule had set to `false`) → fell to the **switch fallback** → `Unexpected Status - Report` → `MOMI report — FAILED`. |

Steps 3–7 are all secondary: even with a healthy Playwright service, any future Pelicargo
outage would kill a billing run that does not actually need Pelicargo.

---

## 2. Why the workflow was structurally fragile

* **`1_efc` only needed Pelicargo for one optional field.** Its `do_ref_no` was
  `{{1_pelicargo.pelicargo_reference}}`. Your goal was *"enter only 3 billing fields — Inland
  freight, Freight, profit share"*; DO Ref No. is not one of them, and every `charge_amount` /
  `cost_amount` in the plan was already `null` (`amount_mode: provided_only`). An optional
  lookup was a hard dependency of a financial write.
* **A failed optional step counted against `allCompleted`.** Even after removing the
  dependency, `steps.every(s => s.completed === true)` would still report the run as failed
  once Pelicargo had failed — so the fix needs both halves.
* **Unresolved `{{step.field}}` references were emitted literally.** `Resolve Step References`
  returned the placeholder unchanged when a value was missing, so a dead lookup would have
  typed the text `{{1_pelicargo.pelicargo_reference}}` straight into the DO Ref No. box.

### Two latent bugs found while tracing (not the cause of this failure, but they will bite)

* **`MOMI Tool Router1` output 6 is double-wired.** The `computer` family goes to *both*
  `Capture Tool Result` **and** `Hub Router`. Every `python_execute`, `api_request`,
  `windows_*` and `file_*` step therefore runs twice, and the direct branch hits
  `Capture Tool Result` with no `tool_result` on the envelope → immediate spurious
  `"Tool failed"`.
* **`Route By Execution Status` has no `failed && needs_replan === false` branch.** Today that
  state is caught by the switch fallback, which happens to point at the right node. It works
  by accident and is invisible in the editor.

---

## 3. Fixes

`scripts/patch_momi_workflow.py` applies all of them to an exported workflow JSON.

| ID | Node | Change |
|----|------|--------|
| **P1** | `Format Hub Result` | Recognises the Browser Automation shape (`operations[]`) and derives success from the operation log. `HTTP 0` / `ECONNREFUSED` / `ENOTFOUND` → error `service_unreachable` with a summary naming the Playwright service. Credential-store and executor shapes are untouched. |
| **P2** | `Commit Tool Result` | Fails fast on `service_unreachable` / `auth_failed` (one attempt, not three), never routes an infrastructure error to the LLM re-planner, and puts an actionable remediation hint in `failure_reason`. |
| **P3** | `Find Next Ready Step1` | A failed **optional** step no longer blocks dependents and no longer fails the run; it is recorded in `state.skipped_optional_steps`. Failed *required* steps still fail the run (regression-tested). |
| **P4** | `Resolve Step References` | Unresolvable `{{step.field}}` becomes `''` instead of the literal placeholder, and the skipped refs are reported in `state.unresolved_refs`. |
| **P5** | `Freight Playbook1` | Marks the Pelicargo lookup `optional: true`, defaults `do_ref_no` to `''`, and reduces `1_efc` to `depends_on: ["1_credentials"]`. |
| **P6** | `MOMI Verification` | Skipped optional steps and blank amounts are not "missing evidence" — otherwise the verifier would fail the run over the DO Ref No. it was told to leave empty. |
| **P7** | `MOMI Final Response1` | Skipped optional steps render `⏭` (not `❌`) and the report explains why. |
| **P8** | `MOMI Tool Router1` | Removes the duplicate `Capture Tool Result` link from the computer-family output. |
| **P9** | `Route By Execution Status` | Adds an explicit rule for `failed && needs_replan === false`, mapped to the existing terminal-report output. |

After the fix the same scenario reports:

```
MOMI report — completed
Goal: …MAWB: 057-79603090…
⏭ 1_pelicargo · Resolve Pelicargo shipment 057-79603090 — Browser automation failed:
   HTTP 0 from Playwright service (HTTP 0 - Playwright service unreachable)
   (error: service_unreachable)
✅ 1_credentials · Retrieve EFC credentials for 057-79603090 — Credentials retrieved (19 fields)
✅ 1_efc · Create EFC Domestic Billing for 057-79603090
⏭ Skipped optional step(s): 1_pelicargo - non-blocking; the fields they fed were left blank.
```

---

## 4. What you need to do

### 4.1 Fix the actual root cause — the Playwright service

Nothing in the workflow can help until this answers. From the machine running Docker:

```bash
# on the Windows host
curl http://localhost:8766/health
netstat -ano | findstr :8766

# from inside the n8n container (this is what MOMI actually uses)
docker exec -it <n8n-container> sh -c "wget -qO- http://host.docker.internal:8766/health || echo UNREACHABLE"
```

* No listener → start the Playwright service so it binds the port passed as
  `playwright_service_url` (default `http://host.docker.internal:8766`).
* `host.docker.internal` does not resolve on **Linux** Docker → add to the n8n service:
  ```yaml
  extra_hosts:
    - "host.docker.internal:host-gateway"
  ```
* On Windows/macOS Docker Desktop the name resolves automatically; if it still fails, the
  service is bound to `127.0.0.1` only — make it listen on `0.0.0.0`.

### 4.2 Optional but recommended — make the sub-workflow answer properly

Add this as the **last** node of `MOMI - Browser Automation` (Code node, *Run Once for All Items*).
Then the parent gets a structured answer even without P1:

```js
const body = $json;
const ops = Array.isArray(body?.operations) ? body.operations : [];
const ok = ops.length > 0 && ops.every(o => o.success !== false);
const firstError = ops.find(o => o.success === false)?.error || null;
return [{ json: {
  success: ok,
  data: body,
  summary: ok ? `Browser automation OK (${ops.length} operations)`
              : `Browser automation failed: ${firstError || 'no success signal'}`,
  output: '',
  tool: 'browser_sequence',
  action: 'Browser automation',
  error: ok ? null : (firstError || 'executor_failed')
}}];
```

### 4.3 Apply the workflow patch

```bash
python3 scripts/patch_momi_workflow.py MOMI-Orchestrator.json --check   # verify first
python3 scripts/patch_momi_workflow.py MOMI-Orchestrator.json           # writes *.fixed.json
```

Then **n8n → Workflows → Import from File** and pick `MOMI-Orchestrator.fixed.json`.
The script refuses to write anything if a single target is missing or ambiguous, and it is
idempotent — re-running it changes nothing.

### 4.4 Expect the approval gate

`1_efc` is `requires_approval: true`, and `Restore After Risk` force-sets that flag for every
EFC billing step. Previously the run died before reaching it; now it will pause and wait:

```bash
curl -X POST https://<n8n>/webhook/momi/approve \
  -H "Content-Type: application/json" \
  -H "x-momi-secret: <MOMI_APPROVAL_SECRET>" \
  -d '{"approval_token":"MOMI-1791564644812:1_efc","decision":"approve"}'
```

The token is `<request_id>:<step_id>` and is printed in the waiting message. Password is redacted in the logs, the plaintext lives in the Credential Store.

> **Caveat:** `Persist Approval State` / `Load Pending MOMI State` use n8n *workflow static data*.
> It is lost on restart and is per-worker — with queue mode or multiple workers an approval can
> fail with "No pending MOMI state". Move it to Redis/Postgres/a Data Store before you rely on it.

---

## 5. Verification performed

`python3 tests/test_patch_momi_workflow.py` — 45 checks, all passing. It is not guesswork:

* every patch target is located in verbatim excerpts of your nodes;
* patching is idempotent (second run applies nothing);
* every patched node still parses under `node --check`;
* the patched JavaScript is **executed under node** against the exact payload from the failed
  run and asserted to produce the corrected outcome — before/after pairs prove each change;
* regression guards: a failed *required* step still fails the run; healthy browser runs and
  the credential-store shape still succeed.

```
[7]  before: unstructured_executor_result   after: service_unreachable (+ names Playwright)
[8]  before: 3 attempts                     after: 1 attempt + remediation hint, no re-plan
[9]  before: completed run reported failed  after: completed, optional step recorded
[10] before: literal {{…}} in the form      after: '' + the skipped ref reported
```

## 6. Verification against the real export (not excerpts)

`workflows/MOMI-Orchestrator.json` is the workflow from your failing report,
reconstructed node-for-node: 95 nodes, 120 edges before patching, 119 after.

| check | result |
|-------|--------|
| `python3 scripts/validate_workflow.py workflows/MOMI-Orchestrator.fixed.json` | OK — structure + JS syntax valid, every `$('…')` reference resolves |
| `python3 scripts/patch_momi_workflow.py workflows/MOMI-Orchestrator.json --check` | 16/16 patch targets located |
| `python3 tests/test_patch_momi_workflow.py` | 45/45 passed |
| `python3 tests/e2e_real_workflow.py` | E2E replay passed |

`tests/e2e_real_workflow.py` replays request `MOMI-1791564644812` through the
*real* patched nodes — `Freight Playbook1` → `Validate & Build State` →
`Find Next Ready Step1` → `Commit Tool Result` → `Resolve Step References` →
`MOMI Final Response1` — executing the actual patched JavaScript under node.
Only the side-effecting executors (Playwright / EFC / Credential Store) are
stubbed, because they need live infrastructure.

Plan produced from your goal text:

    1_pelicargo    (optional: true)   Resolve Pelicargo shipment 057-79603090
    1_credentials  ()                 Retrieve EFC credentials for 057-79603090
    1_efc          (depends_on: ["1_credentials"])

Replay, with the Playwright service **still dead** (step 0 `launch`,
`HTTP 0 from Playwright service`):

    ⏭ 1_pelicargo     Browser automation failed: HTTP 0 from Playwright service
                      (HTTP 0 - Playwright service unreachable)  [service_unreachable]
    ✅ 1_credentials   Credentials retrieved (19 fields)
    ✅ 1_efc           Domestic billing saved (3 lines)
    ⏭ Skipped optional step(s): 1_pelicargo - non-blocking; the fields they fed were left blank.

Before the patch the same input produced `FAILED` at `1_pelicargo` after three
attempts, and `1_efc` never started.

## 7. A bug found in the patcher itself (worth knowing)

The first `--check` run reported **P5b and P5c as "already patched" and skipped
them**. Their replacement text (`},[b],true));` and `do_ref_no:doRefNo || '',`)
also occurs in the R-number branch of `Freight Playbook1`, so the naive
`new in text` test false-positived — *the two patches that unblock the billing
step were silently dropped*, and without them this whole fix does nothing.

Both naive idempotency tests are wrong on their own:

* `new in text` false-positives when the replacement text also occurs elsewhere.
* `old not in text` fails when `new` contains `old` (we prepend to an existing
  statement), which double-applies the patch and yields
  `SyntaxError: Identifier '_code' has already been declared`.

Fix: when `new` contains `old`, a `/*MOMI-PATCH:<id>.<n>*/` sentinel is stamped
into the inserted text and detection keys on the sentinel; otherwise it keys on
the absence of `old`. Detection is decided by the code, not hand-classified, so
a future patch cannot regress the same way. This is why `--check` alone was not
enough — the end-to-end replay against the real export is what exposed it.
