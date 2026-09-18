// ============================================================
// MOMI Face — "Prepare EFC Billing Call" Code node (n8n, typeVersion 2)
// GENERALIZED: now handles two EFC intents instead of billing-only:
//   1. billing      — "create billing for R263550, rows freight + profit share"
//   2. check_eawb   — "check in EFC if EAWB was transmitted for 077-59696792"
// Both build a deterministic Orchestrator payload; the 7B never decides.
// ============================================================

const env = $json;
const q = String(env.query || env.chatInput || '').trim();
const lower = q.toLowerCase();

// ---- reference extraction (R number or MAWB) ----
const refM = q.match(/\bR\s?(\d{6})\b/i);
const mawbM = q.match(/\b(\d{3})\s*[- ]?\s*(\d{8})\b/);
// Keep the MAWB in canonical 3-8 form with the dash: 077-59696792
const mawb = mawbM ? `${mawbM[1]}-${mawbM[2]}` : '';
const ref = refM ? 'R' + refM[1] : mawb;

// ---- intent detection ----
// EAWB / FWB transmission-status checks are READ-only lookups.
const isEawbCheck =
  /\b(e-?awb|eawb|fwb|fhl)\b/i.test(lower) &&
  /\b(check|verify|confirm|status|transmitted|transmission|transmit|sent|received|was|has|did)\b/i.test(lower);

// Billing intents keep their old shape.
const isBilling =
  /\b(billing|invoice|invoicing)\b/i.test(lower) ||
  /\b(create|new|add|post|save)\b/i.test(lower);

// Known billing row labels, longest-first so "freight" doesn't swallow "inland freight".
const ROW_LABELS = ['inland freight', 'profit share', 'freight'];
const rows = ROW_LABELS.filter(r => lower.includes(r));
const noFigures = /no figures|without (figures|amounts)|don'?t add any figures|leave (amounts|figures) (blank|empty)/i.test(q);

// Strip any pasted passwords before the query travels to the Orchestrator.
const cleanQ = q.replace(/(password\s*[:=]\s*)\S+/gi, '$1***');

// ---- missing reference: ask, zero tokens ----
if (!ref) {
  return [{ json: {
    ...env,
    output: isEawbCheck
      ? 'Which shipment should I check in EFC? Please give me the MAWB (e.g. 077-59696792) or R number (e.g. R263550).'
      : 'Which shipment should I bill? Please give me the MAWB (e.g. 125-20229016) or R number (e.g. R263550) and the row names.',
    status: 'completed',
    success: false,
    route: 'EFC',
    needs_ref: true
  } }];
}

// ---- build the deterministic Orchestrator instruction ----
let orchestratorQuery;
let efcAction;

if (isEawbCheck && !/\b(create|add|post|save|retransmit|resend)\b/i.test(lower)) {
  efcAction = 'check_eawb';
  orchestratorQuery =
    `In EFC (https://us.eftcloud.com/Efc/MainPage), look up shipment ${ref} ` +
    `and report whether the EAWB (FWB/FHL) was transmitted. ` +
    `Steps: open EFC MainPage, search by ${mawb ? 'MAWB ' + mawb : 'reference ' + ref}, ` +
    `open the shipment, check the EAWB/communication status ` +
    `(transmitted / not transmitted, timestamp, and any FWB acknowledgement or error). ` +
    `This is READ-ONLY: do not create, edit, or retransmit anything. ` +
    `Report exactly what the page shows; if the shipment or status cannot be found, say so.`;
} else {
  efcAction = 'billing';
  orchestratorQuery = cleanQ;
}

return [{ json: {
  ...env,
  efc_ref: ref,
  efc_mawb: mawb,
  efc_action: efcAction,
  efc_rows: rows,
  efc_no_figures: noFigures,
  efc_url: 'https://us.eftcloud.com/Efc/MainPage',
  orchestrator_query: orchestratorQuery,
  orchestrator_route: 'ACTION'
} }];
