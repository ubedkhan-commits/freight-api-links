#!/usr/bin/env python3
"""
patch_momi_workflow.py
======================

Repairs the MOMI n8n orchestrator workflow that fails with:

    ❌ 1_pelicargo · Resolve Pelicargo shipment 057-79603090
       Executor returned no structured success result. (error: unstructured_executor_result)

Usage
-----
    python3 scripts/patch_momi_workflow.py path/to/MOMI-Orchestrator.json

Writes `MOMI-Orchestrator.fixed.json` next to the input (override with -o).
Use --check to verify that every patch target exists WITHOUT writing anything
(useful right after you re-export).

What it fixes
-------------
P1  Format Hub Result          - recognise the Browser Automation response shape
                                 ({session_id, operations:[...]}) so a dead
                                 Playwright service reports `service_unreachable`
                                 instead of the useless `unstructured_executor_result`.
P2  Commit Tool Result         - fail fast (no 3x retry) on service_unreachable /
                                 auth_failed, and attach a human remediation hint
                                 that surfaces in the final report.
P3  Find Next Ready Step1      - a failed OPTIONAL step must not block its
                                 dependents, and must not mark the run failed.
P4  Resolve Step References    - unresolved {{step.field}} refs collapse to an
                                 empty string instead of being written literally
                                 into form fields.
P5  Freight Playbook1          - EFC Domestic Billing no longer depends on the
                                 Pelicargo lookup (which only fed "DO Ref No."),
                                 and that lookup is marked optional.
P6  MOMI Verification          - skipped optional steps and blank amounts are not
                                 treated as missing evidence.
P7  MOMI Final Response1       - skipped optional steps render as "skipped".
P8  Connections                - MOMI Tool Router1 output 6 (computer family) was
                                 wired to BOTH `Capture Tool Result` and
                                 `Hub Router`, double-executing every
                                 python_execute / api_request / windows_* step and
                                 immediately failing it. Keep only Hub Router.
P9  Route By Execution Status  - add an explicit rule for
                                 `failed && needs_replan === false` so the terminal
                                 path is intentional instead of falling through
                                 the switch fallback.
P10 Apply Recovery Plan         - the node is named `MOMI Tool Registry`, but the
                                 code called `$('MOMI Tool Registry1')`. That threw
                                 every time, silently emptying the tool registry, so
                                 replans were never validated against it.

The script is idempotent: re-running it on an already-patched export applies
nothing and exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# P1 - full replacement for the "Format Hub Result" Code node
# --------------------------------------------------------------------------- #
FORMAT_HUB_RESULT_SENTINEL = "// MOMI-PATCH-P1: this node now understands the Browser Automation operations[] shape."

FORMAT_HUB_RESULT = r"""// ── Normalise whatever the executor sub-workflow returned into a MOMI tool_result ──
// PATCH P1: the Browser Automation sub-workflow returns
//   {session_id, operations:[{success,http_status,error,...}], extracted, combined_text, ...}
// with NO top-level success/status/error key, so the old code fell through to
// `unstructured_executor_result` and hid the real cause (HTTP 0 = service down).
const j = $json;
const step = j.state?.next_step || {};

function firstOpError(ops) {
  for (const op of ops) {
    if (op && op.success === false) {
      return op.error || op.message || `browser operation '${op.action || op.step}' failed`;
    }
  }
  return null;
}

function classify(text) {
  const t = String(text || '');
  if (/HTTP\s*0\b/i.test(t) || /ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNRESET|socket hang up/i.test(t)) {
    return 'service_unreachable';
  }
  if (/unauthor|forbidden|invalid (user|login|password)|credential/i.test(t)) return 'auth_failed';
  if (/timed?\s*out|timeout/i.test(t)) return 'timeout';
  return null;
}

let r = j.tool_result;

if (!r) {
  const c = j.data?.tool_result ?? j.data ?? null;
  const co = (c && typeof c === 'object') ? c : null;

  const ops = Array.isArray(co?.operations) ? co.operations : null;
  const opError = ops ? firstOpError(ops) : null;
  const isBrowserShape =
    ops !== null ||
    (co && (co.combined_text !== undefined || (co.session_id !== undefined && co.operations !== undefined)));

  const creds = co ? co.credentials : null;
  const hasCreds = creds && typeof creds === 'object' && Object.keys(creds).length > 0;

  if (hasCreds && (co.not_found === false || typeof co.fields_returned === 'number')) {
    // Credential-store SUCCESS shape: {credentials:{...}, not_found:false, fields_returned:N}.
    r = {
      success: true,
      data: creds,
      summary: `Credentials retrieved (${Object.keys(creds).length} fields)`,
      output: '',
      tool: 'credential_lookup',
      action: step.action || 'Retrieve stored credentials',
      error: null
    };
  } else if (isBrowserShape) {
    // Browser Automation shape - derive success from the operations log.
    const ok = ops ? ops.length > 0 && ops.every(op => op.success !== false) : false;
    const errText = opError || (co?.error ?? co?.message ?? '');
    const httpStatus = Array.isArray(ops)
      ? (ops.find(o => o.http_status !== undefined)?.http_status ?? null)
      : null;
    const kind = classify(errText) || (ok ? null : 'executor_failed');
    r = {
      success: ok,
      data: co,
      summary: ok
        ? `Browser automation OK (${ops ? ops.length : 0} operations)`
        : `Browser automation failed: ${errText || 'no success signal from executor'}` +
          (httpStatus === 0 ? ' (HTTP 0 - Playwright service unreachable)' : ''),
      output: '',
      tool: j.tool || step.tool,
      action: j.action || step.action,
      error: kind
    };
  } else if (typeof j.success === 'boolean' && co && (co.steps_executed !== undefined || co.extracted_texts !== undefined || (co.verification && typeof co.verification === 'object') || co.xlsx !== undefined || co.json_path !== undefined)) {
    // Executor TOP shape (EFC/scrape/API: {success, data, summary, ...} with evidence keys).
    r = {
      success: j.success === true,
      data: co,
      summary: j.summary ?? '',
      output: j.output ?? '',
      tool: j.tool || step.tool,
      action: j.action || step.action,
      error: j.success === true ? null : (j.error ?? 'executor_failed')
    };
  } else if (co && ('success' in co || 'status' in co || 'error' in co)) {
    r = {
      success: co.success === true || co.status === 'success' || co.status === 'completed',
      data: co.data ?? co,
      summary: co.summary ?? co.message ?? '',
      output: co.output ?? '',
      tool: step.tool,
      action: step.action,
      error: co.error ?? null
    };
  } else {
    r = {
      success: false,
      data: co,
      summary: 'Executor returned no structured success result.',
      output: '',
      tool: step.tool,
      action: step.action,
      error: 'unstructured_executor_result'
    };
  }
}

// Last chance: turn a generic failure code into an actionable one.
if (r.error && (r.error === 'executor_failed' || r.error === 'unstructured_executor_result')) {
  const kind = classify(r.summary) || classify(r.output) || classify(JSON.stringify(r.data ?? {}));
  if (kind) r.error = kind;
}

return [{
  json: {
    ...j,
    tool_result: {
      success: r.success === true,
      data: r.data ?? null,
      summary: r.summary ?? '',
      output: r.output ?? '',
      tool: r.tool || step.tool || 'unknown',
      action: r.action || step.action || '',
      error: r.error ?? null
    }
  }
}];

// MOMI-PATCH-P1: this node now understands the Browser Automation operations[] shape.
"""


# --------------------------------------------------------------------------- #
# P2..P7 - surgical string replacements, matched verbatim against node code
# --------------------------------------------------------------------------- #

HINT_MAP_LINE = (
    "const _hintMap={service_unreachable:'The Playwright/browser service did not answer "
    "(HTTP 0 = no response at all). Start it on the host so it listens on the URL passed as "
    "playwright_service_url (default http://host.docker.internal:8766), confirm it is reachable "
    "from inside the n8n container, then re-run.',"
    "auth_failed:'Login was rejected. Fix the stored credentials in the Credential Store and re-run.',"
    "timeout:'The browser service timed out. Raise the step timeout or warm up the service and re-run.'};"
)

CODE_PATCHES: list[dict[str, Any]] = [
    # ---------------- P2 - Commit Tool Result ---------------------------- #
    {
        "node": "Commit Tool Result",
        "patch_id": "P2a",
        "title": "classify fatal executor errors and attach a remediation hint",
        "replacements": [
            (
                "const ok=tr.success===true||tr.status==='success'||tr.status==='completed';",
                "const _code=String(tr.error||'');"
                "const _fatal=/service_unreachable|auth_failed/.test(_code);"
                + HINT_MAP_LINE
                + "const _hint=_hintMap[_code]||null;"
                "const ok=tr.success===true||tr.status==='success'||tr.status==='completed';",
            )
        ],
    },
    {
        "node": "Commit Tool Result",
        "patch_id": "P2b",
        "title": "do not retry a dead service or bad credentials 3x",
        "replacements": [
            (
                "else{step.retry_count=Number(step.retry_count||0)+1;",
                "else{step.retry_count=_fatal?2:Number(step.retry_count||0)+1;",
            )
        ],
    },
    {
        "node": "Commit Tool Result",
        "patch_id": "P2c",
        "title": "never send a fatal infrastructure error to the LLM re-planner",
        "replacements": [
            (
                "const _noReplan=(step.tool_family==='web'||step.tool==='pelicargo_resolve_shipment')&&env.use_llm_planner===false;",
                "const _noReplan=((step.tool_family==='web'||step.tool==='pelicargo_resolve_shipment')&&env.use_llm_planner===false)||_fatal;",
            )
        ],
    },
    {
        "node": "Commit Tool Result",
        "patch_id": "P2d",
        "title": "surface the remediation hint in the final report",
        "replacements": [
            (
                "failure_reason:(failed&&(_noReplan||_capped||_noData))?(step.error||'Tool failed'):null}",
                "failure_reason:(failed&&(_noReplan||_capped||_noData))?(_hint||step.error||'Tool failed'):null}",
            )
        ],
    },

    # An executor that answers with NOTHING is an infrastructure fault, not a
    # flaky step: retrying it three times only delays the report.
    {
        "node": "Commit Tool Result",
        "patch_id": "P2e",
        "title": "treat an empty executor response as fatal (fail fast, do not retry)",
        "replacements": [
            (
                "const _fatal=/service_unreachable|auth_failed/.test(_code);",
                "const _fatal=/service_unreachable|auth_failed|executor_no_response/.test(_code);",
            )
        ]
    },
    {
        "node": "Commit Tool Result",
        "patch_id": "P2f",
        "title": "remediation hint for an empty executor response",
        "replacements": [
            (
                "timeout:'The browser service timed out. Raise the step timeout or warm up the service and re-run.'};",
                "timeout:'The browser service timed out. Raise the step timeout or warm up the service and re-run.',"
                "executor_no_response:'The executor sub-workflow returned no data at all. Check the sub-workflow is "
                "active and that THIS workflow calls the right workflow id (re-importing a workflow creates a new id "
                "and the caller keeps calling the old one), then re-run.'};",
            )
        ]
    },

    # ---------------- P3 - Find Next Ready Step1 ------------------------- #
    {
        "node": "Find Next Ready Step1",
        "patch_id": "P3a",
        "title": "failed OPTIONAL steps stop blocking their dependents",
        "replacements": [
            (
                """const completedIds = new Set(
  steps.filter(step => step.completed === true).map(step => String(step.id))
);
""",
                """const completedIds = new Set(
  steps.filter(step => step.completed === true).map(step => String(step.id))
);

// An optional step (e.g. the Pelicargo lookup that only feeds "DO Ref No.") must
// never block the run: once it has exhausted its retries, treat it as satisfied
// so downstream steps (EFC Domestic Billing) can still execute.
const skippedOptionalIds = new Set(
  steps
    .filter(step => step.failed === true && step.optional === true)
    .map(step => String(step.id))
);
const satisfiedIds = new Set([...completedIds, ...skippedOptionalIds]);
""",
            ),
            (
                "  return dependencies.every(dep => completedIds.has(String(dep)));",
                "  return dependencies.every(dep => satisfiedIds.has(String(dep)));",
            ),
            (
                "const allCompleted = steps.length > 0 && steps.every(step => step.completed === true);",
                "const allCompleted = steps.length > 0 && steps.every(step => step.completed === true || skippedOptionalIds.has(String(step.id)));",
            ),
            (
                "  const failedStep = steps.find(step => step.failed === true);",
                "  const failedStep = steps.find(step => step.failed === true && step.optional !== true);",
            ),
            (
                "    const blockedNow = steps.filter(s => s.completed !== true && (Array.isArray(s.depends_on) ? s.depends_on : []).some(d => !completedIds.has(String(d))));",
                "    const blockedNow = steps.filter(s => s.completed !== true && (Array.isArray(s.depends_on) ? s.depends_on : []).some(d => !satisfiedIds.has(String(d))));",
            ),
            (
                """      status: execution_status,
      failure_reason: forceTerminal ? termReason : (env.state?.failure_reason ?? null)
    }""",
                """      status: execution_status,
      failure_reason: forceTerminal ? termReason : (env.state?.failure_reason ?? null),
      skipped_optional_steps: [...skippedOptionalIds]
    }""",
            ),
        ],
    },

    # ---------------- P4 - Resolve Step References ----------------------- #
    {
        "node": "Resolve Step References",
        "patch_id": "P4a",
        "title": "collect unresolved references instead of emitting them literally",
        "replacements": [
            (
                "const env=$json,step=env.state?.next_step??{},results=env.state?.results??{};",
                "const env=$json,step=env.state?.next_step??{},results=env.state?.results??{};const skipped=[];",
            )
        ],
    },
    {
        "node": "Resolve Step References",
        "patch_id": "P4b",
        "title": "an unresolvable {{step.field}} becomes '' (never the literal string)",
        "replacements": [
            (
                "function r(v){if(typeof v==='string'){const e=v.match(/^\\{\\{\\s*([^}]+?)\\s*\\}\\}$/);if(e){const x=get(e[1]);return x===undefined?v:x}return v.replace(/\\{\\{\\s*([^}]+?)\\s*\\}\\}/g,(m,p)=>{const x=get(p);return x===undefined?m:typeof x==='object'?JSON.stringify(x):String(x)})}",
                "function r(v){if(typeof v==='string'){const e=v.match(/^\\{\\{\\s*([^}]+?)\\s*\\}\\}$/);if(e){const x=get(e[1]);if(x===undefined||x===null){skipped.push(e[1]);return ''}return x}return v.replace(/\\{\\{\\s*([^}]+?)\\s*\\}\\}/g,(m,p)=>{const x=get(p);if(x===undefined||x===null){skipped.push(p);return ''}return typeof x==='object'?JSON.stringify(x):String(x)})}",
            ),
            (
                "const unresolved=JSON.stringify(parameters).match(/\\{\\{\\s*[^}]+\\s*\\}\\}/g)||[];return[{json:{...env,resolved_parameters:parameters,state:{...env.state,next_step:{...step,parameters},resolved_parameter_warnings:unresolved}}}];",
                "const unresolved=JSON.stringify(parameters).match(/\\{\\{\\s*[^}]+\\s*\\}\\}/g)||[];const unresolved_refs=[...new Set(skipped)];return[{json:{...env,resolved_parameters:parameters,state:{...env.state,next_step:{...step,parameters},resolved_parameter_warnings:unresolved,unresolved_refs}}}];",
            ),
        ],
    },

    # ---------------- P5 - Freight Playbook1 ----------------------------- #
    {
        "node": "Freight Playbook1",
        "patch_id": "P5a",
        "title": "mark the Pelicargo lookup optional (it only feeds DO Ref No.)",
        "replacements": [
            (
                "      ]\n    }));\n\n    steps.push(st(b,'credential_lookup','credential',`Retrieve EFC credentials for ${m}`,{",
                "      ]\n    }));\n"
                "    steps[steps.length-1].optional = true;   // optional: only feeds DO Ref No.\n\n"
                "    steps.push(st(b,'credential_lookup','credential',`Retrieve EFC credentials for ${m}`,{",
            )
        ],
    },
    {
        "node": "Freight Playbook1",
        "patch_id": "P5b",
        "title": "EFC billing: DO Ref No. defaults to blank, not a Pelicargo reference",
        "replacements": [
            (
                "      do_ref_no:doRefNo || `{{${a}.pelicargo_reference}}`,",
                "      do_ref_no:doRefNo || '',",
            )
        ],
    },
    {
        "node": "Freight Playbook1",
        "patch_id": "P5c",
        "title": "EFC billing depends on credentials only - Pelicargo can no longer block it",
        "replacements": [
            (
                "      save:true\n    },[a,b],true));",
                "      save:true\n    },[b],true));",
            )
        ],
    },

    # ---------------- P6 - MOMI Verification ----------------------------- #
    {
        "node": "MOMI Verification",
        "patch_id": "P6a",
        "title": "skipped optional steps and blank amounts are not missing evidence",
        "field": "jsonBody",
        "replacements": [
            (
                "For batch jobs verify EVERY item. Missing evidence means verified=false.",
                "For batch jobs verify EVERY item. Optional steps (step.optional === true, or ids listed in "
                "state.skipped_optional_steps) that failed are NOT missing evidence - note them as unavailable. "
                "A field is only missing evidence when the user explicitly supplied a value for it or the goal "
                "named it as required; amount_mode 'leave_blank' and null charge/cost amounts are valid, not "
                "missing. Missing required evidence means verified=false.",
            )
        ],
    },

    # ---------------- P10 - Apply Recovery Plan --------------------------- #
    {
        "node": "Apply Recovery Plan",
        "patch_id": "P10a",
        "title": "fix dangling $('MOMI Tool Registry1') - the node is named 'MOMI Tool Registry'",
        "replacements": [
            (
                "registry = $('MOMI Tool Registry1').first().json.planner_tool_registry ?? [];",
                "registry = $('MOMI Tool Registry').first().json.planner_tool_registry ?? [];",
            )
        ],
    },

    # ---------------- P7 - MOMI Final Response1 -------------------------- #
    {
        "node": "MOMI Final Response1",
        "patch_id": "P7a",
        "title": "render skipped optional steps distinctly and explain them",
        "replacements": [
            (
                "const mark = (s) => s.failed === true ? '❌' : (s.completed === true ? '✅' : '⏸');",
                "const mark = (s) => (s.failed === true && s.optional === true) ? '⏭' : (s.failed === true ? '❌' : (s.completed === true ? '✅' : '⏸'));",
            ),
            (
                "if (env.state?.waiting_for_approval) {",
                """const skippedOptional = (env.state?.skipped_optional_steps || []).filter(Boolean);
if (skippedOptional.length) {
  parts.push(`⏭ Skipped optional step(s): ${skippedOptional.join(', ')} - non-blocking; the fields they fed were left blank.`);
}
if (env.state?.waiting_for_approval) {""",
            ),
        ],
    },

    # ---------------- P11 - make the build self-identifying ---------------- #
    # The point: every report now states which build produced it, so "did my
    # import actually take effect?" is answerable from the report text alone.
    # This matters because re-importing an n8n workflow creates a NEW workflow
    # id, and a parent workflow that calls it by id keeps calling the old one.
    {
        "node": "Validate & Build State",
        "patch_id": "P11a",
        "title": "stamp a patch_level into the envelope",
        "replacements": [
            (
                "waiting_for_approval:false,retry_count:0,approval_history:[],results:{},status:'ready'}",
                "waiting_for_approval:false,patch_level:'momi-p11',retry_count:0,approval_history:[],results:{},status:'ready'}",
            )
        ]
    },
    {
        "node": "MOMI Final Response1",
        "patch_id": "P11b",
        "title": "print the patch level in the report so an unpatched build is obvious",
        "replacements": [
            (
                "const response = parts.join('\\n');\n\nreturn [{ json: { ...env, response } }];",
                "const _patchLevel = env.state?.patch_level || env.patch_level || "
                "'NONE - this is the UNPATCHED build';\n"
                "const response = parts.join('\\n') + `\\n\\n[momi patch level: ${_patchLevel}]`;\n\n"
                "return [{ json: { ...env, response } }];",
            )
        ]
    },

    # P11c/P11d - the fallthrough must never swallow the cause again. Two holes:
    #   * an ARRAY payload (Execute Workflow can hand back multiple items, and
    #     $json.data can be a list) matched none of the shape tests;
    #   * the else branch said only "no structured success result", which told
    #     the operator nothing about what it actually saw.
    {
        "node": "Format Hub Result",
        "patch_id": "P11c",
        "title": "unwrap array payloads before shape detection",
        "replacements": [
            (
                "  const c = j.data?.tool_result ?? j.data ?? null;\n"
                "  const co = (c && typeof c === 'object') ? c : null;",
                "  let c = j.data?.tool_result ?? j.data ?? null;\n"
                "  if (Array.isArray(c)) c = c[0];\n"
                "  const co = (c && typeof c === 'object') ? c : null;",
            )
        ]
    },
    {
        "node": "Format Hub Result",
        "patch_id": "P11d",
        "title": "the fallthrough reports exactly what it saw instead of hiding it",
        "replacements": [
            (
                """  } else {
    r = {
      success: false,
      data: co,
      summary: 'Executor returned no structured success result.',
      output: '',
      tool: step.tool,
      action: step.action,
      error: 'unstructured_executor_result'
    };
  }""",
                """  } else {
    const shape = (c === null || c === undefined)
      ? String(c)
      : (Array.isArray(c) ? `array(${c.length})` : (Object.keys(c).slice(0, 12).join(',') || '{}'));
    const payloadKeys = Object.keys(j || {}).slice(0, 12).join(',');
    const empty = (c === null || c === undefined) || (Array.isArray(c) && c.length === 0);
    r = {
      success: false,
      data: co,
      summary: `Executor returned no recognisable result (data=${shape}; payload keys=[${payloadKeys}]).`,
      output: '',
      tool: step.tool,
      action: step.action,
      error: empty ? 'executor_no_response' : (classify(JSON.stringify(c ?? {})) || 'unstructured_executor_result')
    };
  }""",
            ),
            (
                "if (r.error && (r.error === 'executor_failed' || r.error === 'unstructured_executor_result')) {",
                "if (r.error && (r.error === 'executor_failed' || r.error === 'unstructured_executor_result' || r.error === 'executor_no_response')) {",
            )
        ]
    },
]

FULL_CODE_PATCHES: list[dict[str, str]] = [
    {"node": "Format Hub Result", "patch_id": "P1", "code": FORMAT_HUB_RESULT,
     "sentinel": FORMAT_HUB_RESULT_SENTINEL,
     "title": "understand the Browser Automation response shape"},
]

ROUTER_NODE = "MOMI Tool Router1"
STATUS_SWITCH_NODE = "Route By Execution Status"


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.applied: list[str] = []
        self.skipped: list[str] = []
        self.missing: list[str] = []

    def ok(self) -> bool:
        return not self.missing


def find_node(workflow: dict[str, Any], name: str) -> dict[str, Any] | None:
    for node in workflow.get("nodes", []):
        if node.get("name") == name:
            return node
    return None


def params(node: dict[str, Any]) -> dict[str, Any]:
    """n8n keeps node settings under `parameters` (jsCode, jsonBody, rules, ...)."""
    if "parameters" not in node or not isinstance(node.get("parameters"), dict):
        node["parameters"] = {}
    return node["parameters"]


def apply_full_code(workflow: dict[str, Any], rep: Report, check_only: bool) -> None:
    for item in FULL_CODE_PATCHES:
        node = find_node(workflow, item["node"])
        if node is None:
            rep.missing.append(f"{item['patch_id']}: node '{item['node']}' not found")
            continue
        p = params(node)
        field = "jsCode" if "jsCode" in p else ("code" if "code" in p else None)
        if field is None:
            rep.missing.append(f"{item['patch_id']}: node '{item['node']}' has no jsCode/code field")
            continue
        sentinel = item.get("sentinel")
        # Marker-based, not equality: a later patch (P11c/P11d) edits the code this
        # one installs, so equality would re-apply P1 forever and silently undo them.
        if (sentinel and sentinel in p[field]) or p[field] == item["code"]:
            rep.skipped.append(f"{item['patch_id']} {item['node']}: already patched ({item['title']})")
            continue
        rep.applied.append(f"{item['patch_id']} {item['node']}: {item['title']}")
        p[field] = item["code"]


def apply_code_patches(workflow: dict[str, Any], rep: Report, check_only: bool) -> None:
    for patch in CODE_PATCHES:
        node = find_node(workflow, patch["node"])
        if node is None:
            rep.missing.append(f"{patch['patch_id']}: node '{patch['node']}' not found")
            continue
        field = patch.get("field", "jsCode")
        p = params(node)
        if not isinstance(p.get(field), str):
            rep.missing.append(f"{patch['patch_id']}: node '{patch['node']}' has no string field parameters.{field}")
            continue

        text = p[field]
        changed = False
        for idx, (old, new) in enumerate(patch["replacements"]):
            # Idempotency is subtle and BOTH naive tests are wrong on their own:
            #   * `new in text`  false-positives when the replacement text also
            #     occurs elsewhere (},[b],true)); already exists in the R-number
            #     branch of Freight Playbook1) - the patch is then silently skipped.
            #   * `old not in text` fails when `new` contains `old` (we prepend to an
            #     existing statement) - the patch is then applied twice.
            # So: when `new` contains `old`, stamp a sentinel into the inserted text
            # and detect on that; otherwise detect on the absence of `old`.
            if old in new:
                sentinel = "/*MOMI-PATCH:%s.%d*/" % (patch["patch_id"], idx)
                new_effective = new.replace(old, sentinel + old, 1)
            else:
                sentinel = None
                new_effective = new

            def already(detail: str) -> None:
                rep.skipped.append(
                    f"{patch['patch_id']} {patch['node']}: already patched ({patch['title']}) {detail}"
                )

            if sentinel and sentinel in text:
                already("sentinel")
                continue

            count = text.count(old)
            if count == 0:
                if sentinel is None and new in text:
                    already("target gone")
                else:
                    rep.missing.append(
                        f"{patch['patch_id']} {patch['node']}: TARGET NOT FOUND ({patch['title']})\n"
                        f"      expected: {old[:160]!r}"
                    )
                continue
            if count > 1:
                rep.missing.append(
                    f"{patch['patch_id']} {patch['node']}: target is ambiguous ({count} matches) - refusing to patch"
                )
                continue
            text = text.replace(old, new_effective)
            changed = True

        if changed:
            rep.applied.append(f"{patch['patch_id']} {patch['node']}: {patch['title']}")
            p[field] = text


def apply_connection_patch(workflow: dict[str, Any], rep: Report, check_only: bool) -> None:
    """P8: MOMI Tool Router1 output 6 (computer family) was double-wired."""
    conns = workflow.get("connections", {})
    router = conns.get(ROUTER_NODE)
    if not router:
        rep.missing.append(f"P8: node '{ROUTER_NODE}' has no connections")
        return

    outputs = router.get("main", [])
    if len(outputs) < 7:
        rep.missing.append(
            f"P8: expected at least 7 outputs on '{ROUTER_NODE}', found {len(outputs)}"
        )
        return

    target = outputs[6]
    before = len(target)
    kept = [c for c in target if c.get("node") != "Capture Tool Result"]
    if len(kept) == before:
        rep.skipped.append("P8 MOMI Tool Router1: already patched (computer family no longer double-wired)")
        return

    rep.applied.append(
        "P8 MOMI Tool Router1: removed duplicate 'Capture Tool Result' link from the computer-family "
        "output (it made every python_execute / api_request / windows_* step run twice and fail instantly)"
    )
    outputs[6] = kept


def apply_switch_patch(workflow: dict[str, Any], rep: Report, check_only: bool) -> None:
    """P9: explicit rule for failed && needs_replan === false."""
    node = find_node(workflow, STATUS_SWITCH_NODE)
    if node is None:
        rep.missing.append(f"P9: node '{STATUS_SWITCH_NODE}' not found")
        return

    p = params(node)
    rules = p.setdefault("rules", {}).setdefault("values", [])
    if any(r.get("_patch_id") == "P9" for r in rules):
        rep.skipped.append("P9 Route By Execution Status: already patched")
        return

    # Output index must equal len(rules) so it lands on the existing 4th output,
    # which is the 'Unexpected Status - Report' terminal branch.
    index = len(rules)
    conns = workflow.get("connections", {}).get(STATUS_SWITCH_NODE, {}).get("main", [])
    if len(conns) <= index:
        rep.missing.append(
            f"P9: '{STATUS_SWITCH_NODE}' has {len(conns)} outputs; cannot map rule to output {index}"
        )
        return

    rule = {
        "_patch_id": "P9",
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [
                {
                    "leftValue": "={{ $json.state.execution_status }}",
                    "rightValue": "failed",
                    "operator": {"type": "string", "operation": "equals"},
                    "id": "st-failed-terminal",
                },
                {
                    "leftValue": "={{ $json.state.needs_replan }}",
                    "operator": {"type": "boolean", "operation": "false"},
                    "id": "nr-false",
                },
            ],
            "combinator": "and",
        },
    }
    rep.applied.append(
        f"P9 {STATUS_SWITCH_NODE}: added explicit rule for 'failed && needs_replan === false' "
        f"(output {index} -> terminal report) instead of relying on the switch fallback"
    )
    rules.append(rule)


def patch(workflow: dict[str, Any], check_only: bool = False) -> Report:
    """Apply every patch to `workflow` (an in-memory copy) and report.

    The mutation is unconditional, even with check_only=True: several patches
    target text that an earlier patch introduces (P2e refines the line P2a
    inserts; P11c/P11d refine the node P1 rewrites). Refusing to mutate under
    --check made those unvalidatable. The caller decides whether to write the
    result back to disk.
    """
    rep = Report()
    apply_full_code(workflow, rep, check_only)
    apply_code_patches(workflow, rep, check_only)
    apply_connection_patch(workflow, rep, check_only)
    apply_switch_patch(workflow, rep, check_only)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workflow", help="Path to the exported MOMI orchestrator workflow JSON")
    ap.add_argument("-o", "--output", help="Output path (default: <input>.fixed.json)")
    ap.add_argument("--check", action="store_true", help="Verify patch targets only; write nothing")
    args = ap.parse_args(argv)

    src = Path(args.workflow)
    if not src.is_file():
        print(f"error: {src} not found", file=sys.stderr)
        return 2

    try:
        workflow = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: {src} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    rep = patch(workflow, check_only=args.check)

    print(f"MOMI workflow patcher  ·  {src}")
    print("-" * 72)
    if rep.applied:
        print(f"APPLIED ({len(rep.applied)}):")
        for line in rep.applied:
            print(f"  ✔ {line}")
    if rep.skipped:
        print(f"ALREADY PATCHED ({len(rep.skipped)}):")
        for line in rep.skipped:
            print(f"  · {line}")
    if rep.missing:
        print(f"NOT APPLIED ({len(rep.missing)}):")
        for line in rep.missing:
            print(f"  ✘ {line}")
    print("-" * 72)

    if not rep.ok():
        print(
            "\nSome patch targets were not found. Either the workflow was already modified,\n"
            "or the export differs from the version this patcher was written against.\n"
            "Nothing was written.",
            file=sys.stderr,
        )
        return 1

    if args.check:
        print("Check passed: every patch target was located.")
        return 0

    out = Path(args.output) if args.output else src.with_suffix("")
    out = out.with_name(out.name + ".fixed.json")
    out.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Written: {out}")
    print("\nNext: in n8n, Workflows -> Import from File -> pick the fixed JSON.")
    print("NOTE: the EFC billing step is approval-gated. After import the run will pause at")
    print("      1_efc and wait for  POST /webhook/momi/approve  with header x-momi-secret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
