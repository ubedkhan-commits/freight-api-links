// ============================================================
// MOMI Face — "Format EFC Check Reply" Code node (n8n, typeVersion 2)
// Renders the momi-efc service JSON verbatim. 0 tokens, cannot hallucinate.
// Sits after the "Call EFC Check Service" HTTP Request node.
// ============================================================
const raw = $json?.body ?? $json ?? {};
const j = Array.isArray(raw) ? (raw[0] || {}) : raw;

const mawb = j.mawb || 'the shipment';
let output;

if (j.ok && j.transmitted === true) {
  output = `EFC check for ${mawb}: EAWB **was transmitted**` +
    (j.timestamp_text ? ` (timestamp on page: ${j.timestamp_text})` : '') + '.';
} else if (j.ok && j.transmitted === false) {
  output = `EFC check for ${mawb}: EAWB **NOT transmitted** — status on page indicates pending/failed/not sent.`;
} else if (j.ok && j.transmitted === null) {
  output = `EFC check for ${mawb}: I found EAWB-related text but it is ambiguous — please read the evidence below and judge.`;
} else {
  output = `I could not complete the EFC check for ${mawb} — ${j.error || 'the EFC service did not respond'}. ` +
    `(reached step: ${j.step_reached || 'unknown'}). Nothing was changed in EFC. ` +
    `Failure screenshots are in momi/efc/artifacts/ for selector calibration.`;
}

if (Array.isArray(j.evidence) && j.evidence.length) {
  output += '\n\nWhat the page actually says:\n' + j.evidence.map(l => '• ' + l).join('\n');
}
output += '\n\n_Read directly from EFC by a deterministic script — no AI interpretation of the status._';

return [{ json: {
  output,
  status: j.ok ? 'completed' : 'failed',
  success: j.ok === true && j.transmitted !== null,
  route: 'EFC',
  transmitted: j.transmitted,
  mawb: j.mawb || '',
  evidence: j.evidence || []
} }];
