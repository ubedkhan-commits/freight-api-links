// ============================================================
// MOMI Face — "Freight Rule Router" Code node (n8n, typeVersion 2)
// FIXED: EFC read/status intents (e.g. "check in EFC if EAWB was
// transmitted for 077-59696792") now route EFC deterministically
// instead of falling through to the flaky 7B agent.
// ============================================================

function stash(env) {
  try {
    const store = $getWorkflowStaticData('global');
    store.face = store.face || {};
    store.face[$execution.id] = env;
  } catch (error) {}
}

const env = $json;
const q = String(env.query || env.chatInput || '');
const s = q.toLowerCase();
function hit(re) { return re.test(s); }

// ============================================================
// FAST PATH 0 — pure arithmetic, zero LLM.
// ============================================================
const MATH_RE = /^[\d\s+\-*/().%^]+$/;
const _expr = q.trim().replace(/\s*[?=]\s*$/, '');
if (_expr.length >= 1 && _expr.length <= 30 && MATH_RE.test(_expr) && /\d/.test(_expr) && /[+\-*/%^()]/.test(_expr)) {
  try {
    const val = Function('"use strict"; return (' + _expr.replace(/\^/g, '**') + ')')();
    if (typeof val === 'number' && isFinite(val)) {
      const out = { ...env, route: 'TRIVIAL', rule_hit: true, reason: 'math fast path (no LLM)', requires_planner: false, requires_tool: false, output: `${_expr} = ${val}` };
      stash(out);
      return [{ json: out }];
    }
    if (typeof val === 'number') {
      const out = { ...env, route: 'TRIVIAL', rule_hit: true, reason: 'math fast path (no LLM)', requires_planner: false, requires_tool: false, output: `${_expr} = undefined (cannot divide by zero)` };
      stash(out);
      return [{ json: out }];
    }
  } catch (e) {}
}

// ============================================================
// FAST PATH 1 — greetings & trivial small talk never touch an LLM.
// ============================================================
const TRIVIAL_RE = /^(thanks|thank you|thx|ty|bye|goodbye|see you|ok|okay|k|cool|nice|great)[!.?\s]*$/i;
if (TRIVIAL_RE.test(q.trim())) {
  const out = { ...env, route: 'TRIVIAL', rule_hit: true, reason: 'trivial fast path (no LLM)', requires_planner: false, requires_tool: false };
  stash(out);
  return [{ json: out }];
}

const SMALLTALK = /^(hi+|hey+|hello+|yo|salam|assalam(u)?(alaikum)?|aoa|hru|how are you|how's it going|good (morning|afternoon|evening|night)|test|testing|ping)[!.?\s]*$/i;
if (SMALLTALK.test(q.trim())) {
  const out = { ...env, route: 'SMALLTALK', rule_hit: true, reason: 'smalltalk fast path (no LLM)', requires_planner: false, requires_tool: false, output: "Hey — MOMI here. What do you need?" };
  stash(out);
  return [{ json: out }];
}

// FAST PATH 2 — anything absurdly short and non-actionable is small talk too.
const _short = q.trim();
if ((_short.length <= 3 && !/\d/.test(_short)) || (/^[\d\s%]{1,4}$/.test(_short) && /\d/.test(_short))) {
  const out = { ...env, route: 'SMALLTALK', rule_hit: true, reason: 'too short to be a task', requires_planner: false, requires_tool: false, output: "Hey — MOMI here. What do you need?" };
  stash(out);
  return [{ json: out }];
}

// ============================================================
// FAST PATH 3 — deterministic ClickUp field audit.
// ============================================================
const AUDIT_SUBJECT = /\b(clickup|tasks?|shipments?|board|list)\b/;
const AUDIT_VERB = /\b(audit|check|scan|review|run through|go through|sweep)\b/;
const AUDIT_OBJECT = /\b(field|fields|missing|incomplete|complete|completed|blank|empty|gaps?|filled)\b/;
const AUDIT_NEGATE = /\b(create|update|write|fill|backfill|send|email|assign|close|delete)\b/;

if (AUDIT_VERB.test(s) && AUDIT_OBJECT.test(s) && AUDIT_SUBJECT.test(s) && !AUDIT_NEGATE.test(s)) {
  const out = { ...env, route: 'AUDIT', rule_hit: true, reason: 'clickup field audit (deterministic, no LLM)', requires_planner: false, requires_tool: true };
  stash(out);
  return [{ json: out }];
}

// ============================================================
// FAST PATH 3b — EFC lookups AND actions (deterministic, direct
// Orchestrator branch — the fix for the EAWB-transmission check).
//
// Old bug: the EFC rule lived at the bottom and required a WRITE verb
// (create|new|add|update|open|post|save|send). A read-only request like
//   "Can you check in EFC if EAWB was transmitted for 077-59696792"
// has none of those verbs, so it fell through to the 7B agent and died.
//
// Now: any message that names the EFC system (efc / eftcloud URL) or an
// EAWB/FWB/FHL artifact, combined with EITHER a read verb OR a write
// verb, routes EFC. A MAWB like 077-59696792 alone is a strong signal
// when paired with a status word.
// ============================================================
const EFC_SYSTEM = /\b(efc|eftcloud|e-?awb|eawb|fwb|fhl)\b/;
const EFC_READ   = /\b(check|verify|confirm|status|transmitted|transmission|transmit|sent|received|look ?up|see if|was|has|did)\b/;
const EFC_WRITE  = /\b(create|new|add|update|open|post|save|send|retransmit|resend)\b/;
// Guard: pure procedure/SOP questions still belong to DOCUMENT.
const EFC_DOCQ   = /\b(procedure|sop|policy|how do we|how should)\b/;

if (EFC_SYSTEM.test(s) && (EFC_READ.test(s) || EFC_WRITE.test(s)) && !EFC_DOCQ.test(s)) {
  const out = {
    ...env,
    route: 'EFC',
    rule_hit: true,
    reason: 'EFC lookup/action (deterministic, direct Orchestrator)',
    requires_planner: true,
    requires_tool: true
  };
  stash(out);
  return [{ json: out }];
}

// ============================================================
// FAST PATH 4 — live/current information from the internet.
// ============================================================
const WEB_STRONG = /\b(flight|flights|weather|forecast|news|exchange rate|currency|stock|diesel price|fuel price|petrol price|schedule|timetable|departure|arrival)\b/;
const WEB_HINT = /\b(latest|current|today|tonight|tomorrow|right now|this week|next available|search|google|look ?up online|check online|on the internet)\b/;
const WEB_NEGATE = /\b(clickup|task|awb|eawb|mawb|efc|eftcloud|s\/r|pro #|our (sop|policy|procedure)|memory|remember|shipment r\d{6}|invoice)\b/;

if (!WEB_NEGATE.test(s) && (WEB_STRONG.test(s) || WEB_HINT.test(s))) {
  const TIME_VARYING = /\b(weather|forecast|news|price|prices|rate|rates|stock|status|delay|delays|strike|congestion)\b/;
  const REFERENCE_LOOKUP = /\b(schedule|timetable|flight|flights|departure|arrival|route|routes)\b/;

  let recency = null;
  if (TIME_VARYING.test(s) && !REFERENCE_LOOKUP.test(s)) {
    if (/\b(today|tonight|right now|current|latest|now)\b/.test(s)) recency = 'd';
    else if (/\b(this week|recent)\b/.test(s)) recency = 'w';
  }

  const out = { ...env, route: 'WEB', rule_hit: true, reason: 'live information request (deterministic web search)', requires_planner: false, requires_tool: true, search_query: q.trim(), search_recency: recency };
  stash(out);
  return [{ json: out }];
}

// ============================================================
// Freight keyword rules (unchanged logic, EFC write-rule kept as a
// second chance for billing phrasings that dodged FAST PATH 3b)
// ============================================================
let route = null;
let reason = '';

if (hit(/\b(load|pro|trailer|bol|pod|detention|appt|appointment|dispatch)\b/) &&
    hit(/\b(create|update|send|email|mail|clickup|task|report|notify|follow|open)\b/)) {
  route = 'ACTION';
  reason = 'freight action keywords';
} else if (hit(/\b(late|delay|miss|rollover|wont make|will not make)\b/) &&
           hit(/\b(appt|appointment|customer|sop|procedure|notify|follow)\b/) &&
           !hit(/^\s*(what|how|when|where|who|why|is|are|does|do we|can we)\b/) &&
           !hit(/\b(procedure|sop|policy)\b.*\?|\?\s*$/)) {
  route = 'ACTION';
  reason = 'delay / missed appointment';
} else if (hit(/\b(procedure|sop|policy|how do we|shipment delay)\b/) &&
           !hit(/\b(create|send|update|email|clickup)\b/)) {
  route = 'DOCUMENT';
  reason = 'SOP / procedure question';
} else if (hit(/\b(efc|billing|invoice|invoicing)\b/) &&
           hit(/\b(create|new|add|update|open|post|save|send)\b/)) {
  route = 'EFC';
  reason = 'EFC / billing action';
} else if (hit(/\b(remember|i prefer|do not email|never email)\b/)) {
  route = 'MEMORY';
  reason = 'explicit memory';
} else if (hit(/\b(weather|news|diesel price)\b/)) {
  route = 'WEB';
  reason = 'current info';
}

const out = {
  ...env,
  rule_hit: Boolean(route),
  route: route || env.route || '',
  reason: route ? ('rule: ' + reason) : (env.reason || ''),
  requires_planner: route === 'ACTION' || route === 'WEB' || route === 'EFC',
  requires_tool: route ? route !== 'SIMPLE' : env.requires_tool
};
stash(out);
return [{ json: out }];
