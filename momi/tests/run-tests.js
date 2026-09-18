// Harness that runs the two n8n Code nodes outside n8n.
// Usage: node momi/tests/run-tests.js
const fs = require('fs');
const path = require('path');

const routerSrc = fs.readFileSync(path.join(__dirname, '../nodes/freight-rule-router.js'), 'utf8');
const prepareSrc = fs.readFileSync(path.join(__dirname, '../nodes/prepare-efc-call.js'), 'utf8');

function runNode(src, json) {
  const fn = new Function('$json', '$getWorkflowStaticData', '$execution', src);
  const staticData = { global: {} };
  return fn(json, () => staticData.global, { id: 'test-exec' })[0].json;
}

let failures = 0;
function expect(name, actual, field, want) {
  const got = actual[field];
  const ok = got === want;
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  [${field}] got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

// ---------------- Router tests ----------------
const cases = [
  // THE bug: read-only EAWB transmission check must route EFC
  { q: 'Can you check in EFC (https://us.eftcloud.com/Efc/MainPage) if EAWB was transmitted for 077-59696792', route: 'EFC' },
  { q: 'check if eawb was sent for 077-59696792 in eftcloud', route: 'EFC' },
  { q: 'verify FWB status for 125-20229016 in EFC', route: 'EFC' },
  // Existing EFC write intents still work
  { q: 'create a billing job for R263550 in EFC with freight and profit share', route: 'EFC' },
  // Regressions: other routes unaffected
  { q: 'hi', route: 'SMALLTALK' },
  { q: 'thanks', route: 'TRIVIAL' },
  { q: '2+2', route: 'TRIVIAL' },
  { q: 'check if all tasks assigned to me have their fields completed in clickup', route: 'AUDIT' },
  { q: "what's the weather in Karachi today", route: 'WEB' },
  { q: 'what is our procedure for shipment delay?', route: 'DOCUMENT' },
  // SOP question about EAWB should stay DOCUMENT, not EFC
  { q: 'what is our procedure for EAWB transmission?', route: 'DOCUMENT' },
];

for (const c of cases) {
  const out = runNode(routerSrc, { query: c.q, chatInput: c.q });
  expect(`router: "${c.q.slice(0, 60)}"`, out, 'route', c.route);
}

// ---------------- Prepare EFC Call tests ----------------
const userQ = 'Can you check in EFC (https://us.eftcloud.com/Efc/MainPage) if EAWB was transmitted for 077-59696792';
const prep = runNode(prepareSrc, { query: userQ, chatInput: userQ, session_id: 's1' });
expect('prepare: MAWB extracted', prep, 'efc_ref', '077-59696792');
expect('prepare: action is check_eawb', prep, 'efc_action', 'check_eawb');
expect('prepare: route is ACTION', prep, 'orchestrator_route', 'ACTION');
if (!/READ-ONLY/.test(prep.orchestrator_query || '')) { failures++; console.log('FAIL  prepare: orchestrator_query lacks READ-ONLY guard'); }
else console.log('PASS  prepare: orchestrator_query carries READ-ONLY guard');
if (!/077-59696792/.test(prep.orchestrator_query || '')) { failures++; console.log('FAIL  prepare: MAWB missing from orchestrator_query'); }
else console.log('PASS  prepare: MAWB present in orchestrator_query');

// billing path unchanged
const billQ = 'create billing for R263550 in EFC with freight and inland freight, no figures';
const bill = runNode(prepareSrc, { query: billQ, chatInput: billQ });
expect('prepare billing: ref', bill, 'efc_ref', 'R263550');
expect('prepare billing: action', bill, 'efc_action', 'billing');
expect('prepare billing: no figures', bill, 'efc_no_figures', true);
if (JSON.stringify(bill.efc_rows) !== JSON.stringify(['inland freight', 'freight'])) { failures++; console.log('FAIL  prepare billing: rows', bill.efc_rows); }
else console.log('PASS  prepare billing: rows extracted');

// missing ref asks instead of failing
const noRef = runNode(prepareSrc, { query: 'check in EFC if eawb was transmitted' });
expect('prepare no-ref: needs_ref', noRef, 'needs_ref', true);

console.log(failures ? `\n${failures} FAILURE(S)` : '\nALL TESTS PASSED');
process.exit(failures ? 1 : 0);
