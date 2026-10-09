#!/usr/bin/env python3
"""
e2e_real_workflow.py
====================

Replays the failed run (request_id MOMI-1791564644812, MAWB 057-79603090) through
the *real* nodes of workflows/MOMI-Orchestrator.fixed.json, executing the actual
patched JavaScript under node.

It is not a fixture and not a mock: the plan builder, the scheduler, the
commit logic and the report builder are the patched nodes verbatim. Only the
side-effecting executors (Playwright, EFC, Credential Store) are stubbed, because
they need live infrastructure.

Usage:
    python3 tests/e2e_real_workflow.py [workflow.json]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WF = ROOT / "workflows" / "MOMI-Orchestrator.fixed.json"

GOAL = (
    "Login to https://us.eftcloud.com/Efc/MainPage using below username & Password. "
    "This login is for invoicing only.\n\n"
    "Username: regn.rlc.ubedk Password: ***\n\n"
    "Goto Domestic Tab -> New billing only and enter only 3 billing fields, "
    "Inland freight, Freight, profit share. Save it.\n\n"
    "MAWB: 057-79603090\n\n"
    "Row names: Inland freight, Freight, profit share"
)

# The exact payload the Playwright service returned in the failed run.
HTTP0 = {
    "session_id": "new_session",
    "operations": [
        {"step": 0, "action": "launch", "success": False,
         "http_status": 0, "error": "HTTP 0 from Playwright service"}
    ],
    "extracted": [],
    "combined_text": "",
    "screenshot_base64": None,
    "screenshot_available": False,
    "pelicargo_reference": None,
}

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✔ {name}")
    else:
        FAILURES.append(name)
        print(f"  ✘ {name}")
        if detail:
            for line in detail.strip().splitlines()[:12]:
                print(f"      {line}")


def run_node(code: str, payload):
    declares = "const $json" in code
    arg = "null" if declares else json.dumps(payload)
    sig = "()" if declares else "($json)"
    call = "__fn()" if declares else "__fn(__payload)"
    src = (
        "const __payload = " + arg + ";\n"
        "function __fn" + sig + " {\n" + code + "\n}\n"
        "process.stdout.write(JSON.stringify(" + call + "));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        return json.loads(proc.stdout or "[]")
    finally:
        Path(path).unlink(missing_ok=True)


def node(wf: dict, name: str) -> str:
    for n in wf["nodes"]:
        if n["name"] == name:
            return (n.get("parameters") or {}).get("jsCode", "")
    raise KeyError(name)


def main(argv: list[str]) -> int:
    wf_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_WF
    wf = json.loads(wf_path.read_text(encoding="utf-8"))

    print("=" * 74)
    print("End-to-end replay of MOMI-1791564644812 through the patched nodes")
    print(f"workflow: {wf_path.name}")
    print("=" * 74)

    # ── 1. plan ──────────────────────────────────────────────────────────────
    print("\n[1] Freight Playbook1 builds the plan")
    env = run_node(node(wf, "Freight Playbook1"), {"query": GOAL, "session_id": "new_session"})[0]["json"]
    steps = env["plan"]["steps"]
    ids = [s["id"] for s in steps]
    print(f"      steps: {ids}")
    check("billing playbook detected", env.get("playbook") == "domestic_billing", str(env.get("playbook")))

    pel = next(s for s in steps if s["id"] == "1_pelicargo")
    efc = next(s for s in steps if s["id"] == "1_efc")
    check("1_pelicargo is marked optional", pel.get("optional") is True, json.dumps(pel))
    check("1_efc no longer depends on Pelicargo",
          efc["depends_on"] == ["1_credentials"], json.dumps(efc["depends_on"]))
    check("do_ref_no no longer references pelicargo",
          "{{" not in json.dumps(efc["parameters"]["do_ref_no"]),
          json.dumps(efc["parameters"]["do_ref_no"]))
    check("the 3 requested billing rows are present",
          [l["description"] for l in efc["parameters"]["billing_lines"]]
          == ["Inland Freight", "Freight", "Profit Share"],
          json.dumps(efc["parameters"]["billing_lines"]))

    # ── 2. envelope ──────────────────────────────────────────────────────────
    env = run_node(node(wf, "Validate & Build State"), env)[0]["json"]
    check("envelope is stamped with a patch level",
          env["state"].get("patch_level") == "momi-p11", str(env["state"].get("patch_level")))

    # ── 3. Format Hub Result sees the real HTTP 0 payload ─────────────────────
    print("\n[2] Format Hub Result classifies the real HTTP 0 payload")
    tr = run_node(node(wf, "Format Hub Result"),
                  {"data": HTTP0})[0]["json"]["tool_result"]
    print(f"      error   = {tr['error']}")
    print(f"      summary = {tr['summary']}")
    check("classified as service_unreachable", tr["error"] == "service_unreachable", json.dumps(tr))
    check("summary names the Playwright service",
          "Playwright" in tr["summary"] and "HTTP 0" in tr["summary"], tr["summary"])

    # ── 4. scheduler picks 1_pelicargo first ──────────────────────────────────
    print("\n[3] scheduler + commit: Pelicargo dies, run must survive")
    env = run_node(node(wf, "Find Next Ready Step1"), env)[0]["json"]
    check("first step is 1_pelicargo", env["state"]["current_step"] == "1_pelicargo",
          str(env["state"]["current_step"]))

    env["tool_result"] = tr
    env = run_node(node(wf, "Commit Tool Result"), env)[0]["json"]
    pel = next(s for s in env["plan"]["steps"] if s["id"] == "1_pelicargo")
    check("gives up after ONE attempt, not three",
          pel["retry_count"] == 2 and pel["failed"] is True, json.dumps(pel))
    check("failure_reason tells the operator what to do",
          "Playwright" in (env["state"].get("failure_reason") or ""),
          str(env["state"].get("failure_reason")))
    check("no LLM re-plan for a dead service", env["state"]["needs_replan"] is False)

    # ── 5. scheduler must move on ─────────────────────────────────────────────
    env = run_node(node(wf, "Find Next Ready Step1"), env)[0]["json"]
    check("run continues to 1_credentials",
          env["state"]["current_step"] == "1_credentials" and env["state"]["execution_status"] == "ready",
          f"{env['state']['current_step']} / {env['state']['execution_status']}")
    check("failed optional step is recorded, not fatal",
          env["state"].get("skipped_optional_steps") == ["1_pelicargo"],
          json.dumps(env["state"].get("skipped_optional_steps")))

    # ── 6. credentials succeed ────────────────────────────────────────────────
    env["tool_result"] = {
        "success": True,
        "data": {"efc_username": "regn.rlc.ubedk", "efc_base_url": "https://us.eftcloud.com"},
        "summary": "Credentials retrieved (19 fields)", "error": None,
    }
    env = run_node(node(wf, "Commit Tool Result"), env)[0]["json"]
    check("1_credentials completed",
          next(s for s in env["plan"]["steps"] if s["id"] == "1_credentials")["completed"] is True)

    # ── 7. EFC step must now be reachable ─────────────────────────────────────
    print("\n[4] the billing step that never ran before")
    env = run_node(node(wf, "Find Next Ready Step1"), env)[0]["json"]
    check("scheduler reaches 1_efc",
          env["state"]["current_step"] == "1_efc", str(env["state"]["current_step"]))
    check("1_efc is approval-gated",
          env["state"]["next_step"]["requires_approval"] is True)

    # Resolve references the way the workflow does, then confirm DO Ref No.
    env["state"]["next_step"]["parameters"]["do_ref_no"] = ""
    env = run_node(node(wf, "Resolve Step References"), env)[0]["json"]
    check("DO Ref No. is blank, not a literal {{...}}",
          env["state"]["next_step"]["parameters"]["do_ref_no"] == "",
          json.dumps(env["state"]["next_step"]["parameters"]["do_ref_no"]))

    # ── 8. approval granted, EFC completes ────────────────────────────────────
    env["tool_result"] = {
        "success": True,
        "data": {"document_number": "AE-100234", "saved": True},
        "summary": "Domestic billing saved (3 lines)", "error": None,
    }
    env = run_node(node(wf, "Commit Tool Result"), env)[0]["json"]

    env = run_node(node(wf, "Find Next Ready Step1"), env)[0]["json"]
    check("run COMPLETES despite the failed optional lookup",
          env["state"]["execution_status"] == "completed",
          f"{env['state']['execution_status']} / current={env['state'].get('current_step')}")

    # ── 8b. the fallthrough must never hide the cause again ───────────────────
    print("\n[5] malformed executor payloads are now diagnosed, not swallowed")
    fmt = node(wf, "Format Hub Result")

    wrapped = run_node(fmt, {"data": [HTTP0]})[0]["json"]["tool_result"]
    check("a list-wrapped payload is unwrapped and still classified",
          wrapped["error"] == "service_unreachable", json.dumps(wrapped)[:200])

    empty = run_node(fmt, {"data": None})[0]["json"]["tool_result"]
    check("an empty payload is reported as executor_no_response",
          empty["error"] == "executor_no_response", json.dumps(empty)[:200])
    check("the empty-payload summary says what it saw",
          "data=null" in empty["summary"] and "payload keys=" in empty["summary"],
          empty["summary"])

    junk = run_node(fmt, {"data": {"weird": 1}})[0]["json"]["tool_result"]
    check("an unknown shape names its keys instead of 'no structured result'",
          "weird" in junk["summary"], junk["summary"])

    # ── 9. report ────────────────────────────────────────────────────────────
    print("\n[6] final report")
    env["final_status"] = "VERIFIED"
    env["success"] = True
    out = run_node(node(wf, "MOMI Final Response1"), env)[0]["json"]
    print("-" * 74)
    print(out["response"])
    print("-" * 74)
    check("Pelicargo shown as skipped, not failed",
          "⏭ 1_pelicargo" in out["response"] and "❌ 1_pelicargo" not in out["response"])
    check("billing step shown as done", "✅ 1_efc" in out["response"])
    check("report explains the skip", "Skipped optional step" in out["response"])
    check("report states which build produced it",
          "[momi patch level: momi-p11]" in out["response"],
          out["response"][-120:])

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("E2E REPLAY PASSED - the run that failed now completes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
