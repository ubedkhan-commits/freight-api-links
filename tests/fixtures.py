"""
fixtures.py
===========

Verbatim excerpts of the MOMI orchestrator nodes, taken from the failing export
(request_id MOMI-1791564644812).

These are NOT the complete nodes - each file holds the region of the node that
the patcher touches, prefixed with just enough declarations to stay valid,
checkable JavaScript. That keeps the suite honest (every string the patcher
searches for is a byte-for-byte copy of the real thing) without shipping a
90 KB blob.

If you re-export the workflow after editing these nodes by hand, refresh the
corresponding excerpt here before trusting the test suite.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Format Hub Result  (P1 replaces this node wholesale)
# --------------------------------------------------------------------------- #
FORMAT_HUB_RESULT = r"""const j = $json, step = j.state?.next_step || {};
let r = j.tool_result;
if (!r) {
  const c = j.data?.tool_result || j.data || null;
  const co = (c && typeof c === 'object') ? c : null;
  const creds = co ? co.credentials : null;
  const hasCreds = creds && typeof creds === 'object' && Object.keys(creds).length > 0;
  // Credential-store SUCCESS shape: {credentials:{...}, not_found:false, fields_returned:N}.
  if (hasCreds && (co.not_found === false || typeof co.fields_returned === 'number')) {
    r = { success: true, data: creds, summary: `Credentials retrieved (${Object.keys(creds).length} fields)`, output: '', tool: 'credential_lookup', action: step.action || 'Retrieve stored credentials', error: null };
  } else if (typeof j.success === 'boolean' && co && (co.steps_executed !== undefined || co.extracted_texts !== undefined || (co.verification && typeof co.verification === 'object') || co.xlsx !== undefined || co.json_path !== undefined)) {
    // Executor TOP shape (EFC/scrape/API: {success, data, summary, ...} with evidence keys).
    r = { success: j.success === true, data: co, summary: j.summary ?? '', output: j.output ?? '', tool: j.tool || step.tool, action: j.action || step.action, error: j.success === true ? null : (j.error ?? 'executor failed') };
  } else if (co && ('success' in co || 'status' in co || 'error' in co)) {
    r = { success: co.success === true || co.status === 'success' || co.status === 'completed', data: co.data ?? co, summary: co.summary ?? co.message ?? '', output: co.output ?? '', tool: step.tool, action: step.action, error: co.error ?? null };
  } else {
    r = { success: false, data: co, summary: 'Executor returned no structured success result.', output: '', tool: step.tool, action: step.action, error: 'unstructured_executor_result' };
  }
}
return [{ json: { ...j, tool_result: { success: r.success === true, data: r.data ?? null, summary: r.summary ?? '', output: r.output ?? '', tool: r.tool || step.tool || 'unknown', action: r.action || step.action || '', error: r.error ?? null } } }];
"""

# --------------------------------------------------------------------------- #
# Commit Tool Result
# --------------------------------------------------------------------------- #
COMMIT_TOOL_RESULT = r"""const env=$json,steps=env.plan?.steps||[],sid=String(env.state?.current_step??env.state?.next_step?.id??''),step=steps.find(s=>String(s.id)===sid),tr=env.tool_result||{};
if(!step)return[{json:{...env,final_status:'EXECUTION_FAILED',success:false,message:`Current step ${sid||'(none)'} was not found.`,state:{...(env.state||{}),execution_status:'failed',status:'failed',needs_replan:false}}}];
const _isCred=step.tool==='credential_lookup'||step.tool_family==='credential';
const _missing=Array.isArray(tr.data?.missing)?tr.data.missing:[];
const _partial=tr.data?.partial===true||_missing.length>0;
const _credOk=(tr.success===true||tr.status==='success'||tr.status==='completed')&&!_partial;
if(_isCred&&!_credOk){
  const _svc=tr.data?.service||step.parameters?.service||'efc';
  step.failed=true;step.completed=false;step.retry_count=2;
  step.result=tr.data??null;step.summary=tr.summary??'';
  step.error=_missing.length?`Incomplete credentials for '${_svc}': missing ${_missing.join(', ')}. Add them to the Credential Store and re-run.`:'Credential lookup failed';
  return[{json:{...env,plan:{...env.plan,steps},message:step.error,tool_result:tr,state:{...(env.state||{}),results:{...((env.state||{}).results||{}),[String(step.id)]:tr},needs_replan:false,execution_status:'failed',status:'failed',failure_reason:step.error}}}];
}
const ok=tr.success===true||tr.status==='success'||tr.status==='completed';if(ok){step.completed=true;step.failed=false;step.result=tr.data??null;step.summary=tr.summary??'';step.error=null}else{step.retry_count=Number(step.retry_count||0)+1;step.result=tr.data??null;step.summary=tr.summary??'';step.error=tr.error??tr.summary??'Tool failed';if(step.retry_count>=2)step.failed=true}
const failed=step.failed===true;
const _noReplan=(step.tool_family==='web'||step.tool==='pelicargo_resolve_shipment')&&env.use_llm_planner===false;const _capped=(Number(env.state?.replan_count||0)>=2);const _noData=/incomplete/i.test(step.error||'')&&/credential/i.test(step.error||'');
return[{json:{...env,plan:{...env.plan,steps},state:{...(env.state||{}),results:{...((env.state||{}).results||{}),[String(step.id)]:tr},needs_replan:failed&&!_noReplan&&!_capped&&!_noData,execution_status:failed?'failed':'ready',status:failed?'failed':'ready',failure_reason:(failed&&(_noReplan||_capped||_noData))?(step.error||'Tool failed'):null},tool_result:tr}}];
"""

# --------------------------------------------------------------------------- #
# Find Next Ready Step1
# --------------------------------------------------------------------------- #
FIND_NEXT_READY_STEP1 = r"""function stashEnvelope(env) { return env; }

const env = $json;
const steps = env.plan?.steps;

if (!Array.isArray(steps)) {
  throw new Error('MOMI: steps array is missing');
}

const completedIds = new Set(
  steps.filter(step => step.completed === true).map(step => String(step.id))
);

const nextStep = steps.find(step => {
  if (step.completed === true) return false;
  if (step.failed === true) return false;
  const dependencies = Array.isArray(step.depends_on) ? step.depends_on : [];
  return dependencies.every(dep => completedIds.has(String(dep)));
});

const allCompleted = steps.length > 0 && steps.every(step => step.completed === true);

let execution_status = 'ready';
let forceTerminal = false;
let termReason = null;
let current_step = null;
let next_step = null;
let all_steps_completed = false;

if (allCompleted) {
  execution_status = 'completed';
  all_steps_completed = true;
} else if (!nextStep) {
  const failedStep = steps.find(step => step.failed === true);
  if (failedStep) {
    execution_status = 'failed';
    current_step = failedStep.id;
  } else {
    const blockedNow = steps.filter(s => s.completed !== true && (Array.isArray(s.depends_on) ? s.depends_on : []).some(d => !completedIds.has(String(d))));
    forceTerminal = true;
    if (blockedNow.length) {
      const hangIds = blockedNow.map(s => s.id).join(', ');
      const missingDeps = [...new Set(blockedNow.flatMap(s => (Array.isArray(s.depends_on) ? s.depends_on : []).map(String).filter(d => !completedIds.has(d))))].join(', ');
      termReason = `Plan has steps with unmet dependencies (${hangIds} need: ${missingDeps}). The (re)plan is malformed - fix the request or plan and re-run.`;
      execution_status = 'failed';
      current_step = blockedNow[0].id;
    } else {
      forceTerminal = true;
      termReason = 'MOMI: No ready step found. Check step dependencies for a cycle.';
      execution_status = 'failed';
    }
  }
} else {
  execution_status = 'ready';
  current_step = nextStep.id;
  next_step = nextStep;
}

const out = {
    ...env,
    plan: { ...env.plan, steps },
    state: {
      ...(env.state || {}),
      execution_status,
      current_step,
      next_step,
      all_steps_completed,
      needs_replan: execution_status === 'failed' ? (forceTerminal ? false : (env.state?.needs_replan !== false)) : false,
      status: execution_status,
      failure_reason: forceTerminal ? termReason : (env.state?.failure_reason ?? null)
    }
  };
  stashEnvelope(out);
  return [{ json: out }];
"""

# --------------------------------------------------------------------------- #
# Resolve Step References
# --------------------------------------------------------------------------- #
RESOLVE_STEP_REFERENCES = r"""const env=$json,step=env.state?.next_step??{},results=env.state?.results??{};function get(path){const a=String(path).split('.');let v=results[a.shift()];for(const p of a){if(v==null)return undefined;if(Object.prototype.hasOwnProperty.call(v,p))v=v[p];else if(v.data&&Object.prototype.hasOwnProperty.call(v.data,p))v=v.data[p];else return undefined}return v}function r(v){if(typeof v==='string'){const e=v.match(/^\{\{\s*([^}]+?)\s*\}\}$/);if(e){const x=get(e[1]);return x===undefined?v:x}return v.replace(/\{\{\s*([^}]+?)\s*\}\}/g,(m,p)=>{const x=get(p);return x===undefined?m:typeof x==='object'?JSON.stringify(x):String(x)})}if(Array.isArray(v))return v.map(r);if(v&&typeof v==='object')return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,r(x)]));return v}const parameters=r(step.parameters||{});const unresolved=JSON.stringify(parameters).match(/\{\{\s*[^}]+\s*\}\}/g)||[];return[{json:{...env,resolved_parameters:parameters,state:{...env.state,next_step:{...step,parameters},resolved_parameter_warnings:unresolved}}}];
"""

# --------------------------------------------------------------------------- #
# Freight Playbook1  (MAWB billing branch only)
# --------------------------------------------------------------------------- #
FREIGHT_PLAYBOOK1 = r"""const $json = { session_id: 'new_session' };
const q = 'bill mawb 057-79603090';
const s = q.toLowerCase();
function st(id,tool,family,action,parameters,depends=[],approval=false){
  return {id,tool,tool_family:family,action,description:action,parameters,depends_on:depends,
    requires_approval:approval,completed:false,failed:false,retry_count:0,result:null,summary:null,error:null};
}
const mawbs = ['057-79603090'];
const rrefs = [];
const billTo = 'Pelicargo Inc-BOS,MA';
const billingDate = '', docNumber = '', doRefNo = '', glPeriod = '', billingRemark = '';
const vatTax = false;
const billingLines = [];
const noAmounts = false;
const EFC_CRED_FIELDS = [];
const billing = true;

if (billing) {
  const steps = [];
  for (let i = 0; i < mawbs.length; i++) {
    const m=mawbs[i],p=`${i+1}`,a=`${p}_pelicargo`,b=`${p}_credentials`,c=`${p}_efc`,
      e=`${p}_cu_search`,f=`${p}_cu_read`,g=`${p}_cu_update`;

    steps.push(st(a,'pelicargo_resolve_shipment','web',`Resolve Pelicargo shipment ${m}`,{
      mawb:m,url:'https://app.pelicargo.com/login',session_id:`pelicargo-${$json.session_id||'default'}`,timeout:180000,
      operations:[
        {action:'goto',timeout:45000},
        {action:'screenshot',full_page:true,label:'pelicargo-page',optional:true},
        {action:'extract',selector:'body',timeout:15000,optional:true}
      ]
    }));

    steps.push(st(b,'credential_lookup','credential',`Retrieve EFC credentials for ${m}`,{
      service:'efc',lookup_fields:EFC_CRED_FIELDS
    }));

    steps.push(st(c,'efc_domestic_billing','efc',`Create EFC Domestic Billing for ${m}`,{
      operation:'DOMESTIC_BILLING',
      mawb:m,
      bill_to:billTo,
      billing_date:billingDate,
      doc_number:docNumber || m,
      do_ref_no:doRefNo || `{{${a}.pelicargo_reference}}`,
      gl_period:glPeriod,
      vat_tax:vatTax,
      billing_remark:billingRemark,
      charge_lines:billingLines.map(x=>x.description || x.code),
      billing_lines:billingLines,
      amount_mode:noAmounts?'leave_blank':'provided_only',
      pelicargo_from_step:a,
      credentials_from_step:b,
      session_id:`efc-${$json.session_id||'default'}`,
      timeout:240000,
      screenshot:true,
      save:true
    },[a,b],true));

    /* ClickUp sub-branch omitted from the fixture. */
  }

  return [{ json: { steps } }];
}
"""

# --------------------------------------------------------------------------- #
# MOMI Verification  (jsonBody - an n8n expression, not JavaScript)
# --------------------------------------------------------------------------- #
MOMI_VERIFICATION_BODY = r"""={{ {model:'qwen2.5:7b-instruct',stream:false,format:'json',keep_alive:'30m',options:{temperature:0,num_ctx:4096,num_predict:900},messages:[{role:'system',content:'You are MOMI Business Goal Verifier. Verify the user goal only from concrete execution evidence. Tool invocation is not proof. Every required step must be completed. For EFC Domestic Billing verify every requested MAWB or R-number/shipment_ref has concrete evidence of a saved billing document, correct Bill To, Billing Date when supplied, Doc Number, DO Ref No, G/L Period when supplied, VAT state when supplied, billing remark when supplied, every requested charge line, and every supplied amount/vendor/ref-pro value. A tool call alone is never proof. For ClickUp verify exact task and final EFT AE #. For batch jobs verify EVERY item. Missing evidence means verified=false. Return ONLY JSON: {\"verified\":true|false,\"issues\":[],\"summary\":\"...\",\"evidence\":[]}'},{role:'user',content:JSON.stringify({query:$json.query,goal:$json.plan?.goal,plan:$json.plan,state:$json.state,tool_result:$json.tool_result})}]} }}"""

# --------------------------------------------------------------------------- #
# MOMI Final Response1
# --------------------------------------------------------------------------- #
MOMI_FINAL_RESPONSE1 = r"""const env = $json;
const steps = Array.isArray(env.plan?.steps) ? env.plan.steps : [];
const mark = (s) => s.failed === true ? '❌' : (s.completed === true ? '✅' : '⏸');

const lines = steps.map(s => {
  let line = `${mark(s)} ${s.id} · ${s.action || s.description || s.tool || ''}`;
  if (s.summary) line += ` — ${String(s.summary).slice(0, 200)}`;
  if (s.error) line += ` (error: ${String(s.error).slice(0, 150)})`;
  return line;
});

const parts = [];
parts.push(`MOMI report — ${env.final_status || env.state?.status || 'completed'}`);
parts.push(`Goal: ${env.plan?.goal || env.query || ''}`);
if (lines.length) {
  parts.push('');
  parts.push(...lines);
}
if (env.verification?.summary) {
  parts.push('');
  parts.push(`Verification: ${String(env.verification.summary).slice(0, 400)}`);
}
if (env.diagnosis?.correction) {
  parts.push(`Next: ${env.diagnosis.correction}`);
}
if (env.state?.waiting_for_approval) {
  parts.push('⏳ Some steps still need your approval.');
}

const response = parts.join('\n');

return [{ json: { ...env, response } }];
"""


def build_mini_workflow() -> dict:
    """Assemble a minimal workflow that carries every patch target."""
    def code_node(name: str, js: str) -> dict:
        return {
            "parameters": {"jsCode": js},
            "id": name.lower().replace(" ", "-"),
            "name": name,
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [0, 0],
        }

    nodes = [
        code_node("Format Hub Result", FORMAT_HUB_RESULT),
        code_node("Commit Tool Result", COMMIT_TOOL_RESULT),
        code_node("Find Next Ready Step1", FIND_NEXT_READY_STEP1),
        code_node("Resolve Step References", RESOLVE_STEP_REFERENCES),
        code_node("Freight Playbook1", FREIGHT_PLAYBOOK1),
        code_node("MOMI Final Response1", MOMI_FINAL_RESPONSE1),
        {
            "parameters": {
                "method": "POST",
                "url": "http://host.docker.internal:11434/api/chat",
                "jsonBody": MOMI_VERIFICATION_BODY,
            },
            "id": "momi-verification",
            "name": "MOMI Verification",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [0, 0],
        },
        {
            "parameters": {
                "rules": {
                    "values": [
                        {"conditions": {"conditions": [{"id": "st-completed"}]}},
                        {"conditions": {"conditions": [{"id": "st-failed"}]}},
                        {"conditions": {"conditions": [{"id": "st-ready"}]}},
                    ]
                },
                "options": {"fallbackOutput": "extra"},
            },
            "id": "route-by-execution-status",
            "name": "Route By Execution Status",
            "type": "n8n-nodes-base.switch",
            "typeVersion": 3.2,
            "position": [0, 0],
        },
        {
            "parameters": {"rules": {"values": []}, "options": {"fallbackOutput": "extra"}},
            "id": "momi-tool-router1",
            "name": "MOMI Tool Router1",
            "type": "n8n-nodes-base.switch",
            "typeVersion": 3.2,
            "position": [0, 0],
        },
    ]

    # MOMI Tool Router1: 10 outputs; index 6 (computer family) is double-wired.
    router_outputs = [[{"node": f"target-{i}", "type": "main", "index": 0}] for i in range(10)]
    router_outputs[6] = [
        {"node": "Capture Tool Result", "type": "main", "index": 0},
        {"node": "Hub Router", "type": "main", "index": 0},
    ]

    # Route By Execution Status: 4 outputs, index 3 = terminal report.
    status_outputs = [
        [{"node": "Hold Envelope Before Verify", "type": "main", "index": 0}],
        [{"node": "Hold Envelope Before Replan", "type": "main", "index": 0}],
        [{"node": "Hold Envelope Before Risk", "type": "main", "index": 0}],
        [{"node": "Unexpected Status - Report", "type": "main", "index": 0}],
    ]

    return {
        "name": "MOMI - Orchestrator (test fixture)",
        "nodes": nodes,
        "connections": {
            "MOMI Tool Router1": {"main": router_outputs},
            "Route By Execution Status": {"main": status_outputs},
        },
        "pinData": {},
    }
