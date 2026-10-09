#!/usr/bin/env python3
"""
test_patch_momi_workflow.py
===========================

Two layers of verification for scripts/patch_momi_workflow.py:

1. Structural   - every patch target is found in the fixture, patching is
                  idempotent, and the resulting JSON is a valid n8n export.
2. Behavioural  - the patched JavaScript is executed under node, using the exact
                  payload from the failed run (request_id MOMI-1791564644812),
                  and asserted to produce the corrected outcome.

node must be on PATH for the behavioural tests (they skip otherwise).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import patch_momi_workflow as pmw  # noqa: E402

HAS_NODE = shutil.which("node") is not None
FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(name)
        print(f"  ✔ {name}")
    else:
        FAILURES.append(name)
        print(f"  ✘ {name}")
        if detail:
            for line in detail.strip().splitlines():
                print(f"      {line}")


# --------------------------------------------------------------------------- #
# node helpers
# --------------------------------------------------------------------------- #
def node_syntax_ok(code: str) -> tuple[bool, str]:
    # Some fixture excerpts declare their own `const $json`; don't shadow it.
    sig = "()" if "const $json" in code else "($json)"
    src = f"function __n{sig} {{\n{code}\n}}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        return proc.returncode == 0, (proc.stderr or "").strip()
    finally:
        Path(path).unlink(missing_ok=True)


def run_node(code: str, payload):
    """Execute an n8n Code node body with the given $json; return its output."""
    declares_json = "const $json" in code
    sig = "()" if declares_json else "($json)"
    arg = "null" if declares_json else json.dumps(payload)
    src = (
        "const __payload = " + arg + ";\n"
        "function __fn" + sig + " {\n" + code + "\n}\n"
        "const __out = __fn(" + ("" if declares_json else "__payload") + ");\n"
        "process.stdout.write(JSON.stringify(__out));\n"
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


def node_by_name(workflow: dict, name: str) -> dict:
    for n in workflow["nodes"]:
        if n["name"] == name:
            return n
    raise KeyError(name)


# --------------------------------------------------------------------------- #
# 1. structural
# --------------------------------------------------------------------------- #
def test_all_patches_apply() -> dict:
    print("\n[1] structural - every patch target is found")
    wf = fixtures.build_mini_workflow()
    rep = pmw.patch(wf, check_only=False)

    check("no missing patch targets", rep.ok(), "\n".join(rep.missing))
    expected = (
        len(pmw.FULL_CODE_PATCHES)
        + len(pmw.CODE_PATCHES)
        + 2  # P8 connections + P9 switch rule
    )
    check(f"all {expected} patches applied", len(rep.applied) == expected,
          f"applied={len(rep.applied)} expected={expected}\n" + "\n".join(rep.applied))
    return wf


def test_idempotent(wf: dict) -> None:
    print("\n[2] idempotency - re-running changes nothing")
    rep = pmw.patch(wf, check_only=False)
    check("second pass applies nothing", rep.applied == [], "\n".join(rep.applied))
    total_replacements = sum(len(p["replacements"]) for p in pmw.CODE_PATCHES)
    expected = total_replacements + len(pmw.FULL_CODE_PATCHES) + 2
    check("second pass reports everything already patched",
          len(rep.skipped) == expected,
          f"skipped={len(rep.skipped)} expected={expected}\n" + "\n".join(rep.skipped))
    check("second pass has no missing targets", rep.ok(), "\n".join(rep.missing))


def test_syntax(wf: dict) -> None:
    print("\n[3] syntax - patched code nodes still parse")
    if not HAS_NODE:
        print("  · skipped (node not on PATH)")
        return
    for name in ("Format Hub Result", "Commit Tool Result", "Find Next Ready Step1",
                 "Resolve Step References", "Freight Playbook1", "MOMI Final Response1"):
        code = node_by_name(wf, name)["parameters"]["jsCode"]
        ok, err = node_syntax_ok(code)
        check(f"{name} parses", ok, err)


def test_connections(wf: dict) -> None:
    print("\n[4] connections - computer family is no longer double-wired")
    out6 = wf["connections"]["MOMI Tool Router1"]["main"][6]
    nodes = [c["node"] for c in out6]
    check("Capture Tool Result removed", "Capture Tool Result" not in nodes, str(nodes))
    check("Hub Router kept", "Hub Router" in nodes, str(nodes))


def test_switch_rule(wf: dict) -> None:
    print("\n[5] switch - explicit failed-terminal rule on the right output")
    node = node_by_name(wf, "Route By Execution Status")
    rules = node["parameters"]["rules"]["values"]
    check("rule appended", len(rules) == 4, str(len(rules)))
    check("rule marked P9", rules[-1].get("_patch_id") == "P9")
    outs = wf["connections"]["Route By Execution Status"]["main"]
    check("output index 3 exists (terminal report)",
          len(outs) > 3 and outs[3][0]["node"] == "Unexpected Status - Report")


def test_playbook(wf: dict) -> None:
    print("\n[6] playbook - EFC billing no longer blocked by Pelicargo")
    code = node_by_name(wf, "Freight Playbook1")["parameters"]["jsCode"]
    if HAS_NODE:
        out = run_node(code, None)[0]["json"]["steps"]
        pel = next(s for s in out if s["id"] == "1_pelicargo")
        efc = next(s for s in out if s["id"] == "1_efc")
        check("1_pelicargo marked optional", pel.get("optional") is True, json.dumps(pel))
        check("1_efc depends on credentials only",
              efc["depends_on"] == ["1_credentials"], json.dumps(efc["depends_on"]))
        check("do_ref_no no longer references pelicargo",
              "{{" not in json.dumps(efc["parameters"]["do_ref_no"]),
              json.dumps(efc["parameters"]["do_ref_no"]))
    else:
        print("  · skipped (node not on PATH)")


# --------------------------------------------------------------------------- #
# 2. behavioural
# --------------------------------------------------------------------------- #
HTTP0_PAYLOAD = {
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


def test_format_hub_result(wf: dict) -> None:
    print("\n[7] behaviour - HTTP 0 is reported as service_unreachable")
    if not HAS_NODE:
        print("  · skipped (node not on PATH)")
        return

    def tr(code, payload):
        return run_node(code, payload)[0]["json"]["tool_result"]

    old = tr(fixtures.FORMAT_HUB_RESULT, {"data": HTTP0_PAYLOAD})
    new = tr(node_by_name(wf, "Format Hub Result")["parameters"]["jsCode"],
             {"data": HTTP0_PAYLOAD})

    check("before: useless unstructured_executor_result",
          old["error"] == "unstructured_executor_result", json.dumps(old))
    check("after: classified as service_unreachable",
          new["error"] == "service_unreachable", json.dumps(new))
    check("after: success is false", new["success"] is False)
    check("after: summary names the Playwright service",
          "Playwright" in new["summary"] and "HTTP 0" in new["summary"], new["summary"])

    # A healthy browser run must still succeed.
    healthy = {"data": {"session_id": "s1", "operations": [
        {"step": 0, "action": "launch", "success": True},
        {"step": 1, "action": "goto", "success": True},
    ], "combined_text": "ok"}}
    ok = tr(node_by_name(wf, "Format Hub Result")["parameters"]["jsCode"], healthy)
    check("healthy browser run still succeeds", ok["success"] is True, json.dumps(ok))

    # Credential-store shape must be untouched.
    creds = {"data": {"credentials": {"efc_username": "u"}, "not_found": False,
                      "fields_returned": 1}}
    cr = tr(node_by_name(wf, "Format Hub Result")["parameters"]["jsCode"], creds)
    check("credential-store shape still succeeds",
          cr["success"] is True and cr["data"]["efc_username"] == "u", json.dumps(cr))


def test_commit_tool_result(wf: dict) -> None:
    print("\n[8] behaviour - dead service fails fast with a remediation hint")
    if not HAS_NODE:
        print("  · skipped (node not on PATH)")
        return

    def run(code, error):
        env = {
            "use_llm_planner": False,
            "plan": {"goal": "g", "steps": [
                {"id": "1_pelicargo", "tool": "pelicargo_resolve_shipment",
                 "tool_family": "web", "retry_count": 0}]},
            "state": {"current_step": "1_pelicargo", "results": {}, "execution_status": "ready"},
            "tool_result": {"success": False, "data": {}, "summary": "Browser automation failed",
                            "error": error},
        }
        out = run_node(code, env)[0]["json"]
        return out["plan"]["steps"][0], out["state"]

    old_step, old_state = run(fixtures.COMMIT_TOOL_RESULT, "unstructured_executor_result")
    new_step, new_state = run(node_by_name(wf, "Commit Tool Result")["parameters"]["jsCode"],
                              "service_unreachable")

    check("before: needs 3 attempts before giving up",
          old_step["retry_count"] == 1 and old_step.get("failed") is not True,
          json.dumps(old_step))
    check("after: gives up on the first attempt",
          new_step["retry_count"] == 2 and new_step["failed"] is True,
          json.dumps(new_step))
    check("after: failure_reason tells you what to do",
          "Playwright" in (new_state.get("failure_reason") or ""),
          json.dumps(new_state.get("failure_reason")))
    check("after: no re-plan (never ask an LLM to fix a dead service)",
          new_state["needs_replan"] is False)


def test_find_next_ready_step(wf: dict) -> None:
    print("\n[9] behaviour - a failed optional step no longer fails the run")
    if not HAS_NODE:
        print("  · skipped (node not on PATH)")
        return

    code_new = node_by_name(wf, "Find Next Ready Step1")["parameters"]["jsCode"]

    def run(code, pelicargo_optional):
        env = {
            "plan": {"goal": "g", "steps": [
                {"id": "1_pelicargo", "failed": True, "completed": False,
                 "optional": pelicargo_optional, "depends_on": []},
                {"id": "1_credentials", "failed": False, "completed": True, "depends_on": []},
                {"id": "1_efc", "failed": False, "completed": True,
                 "depends_on": ["1_credentials"]},
            ]},
            "state": {"execution_status": "ready", "needs_replan": False},
        }
        return run_node(code, env)[0]["json"]["state"]

    old = run(fixtures.FIND_NEXT_READY_STEP1, True)
    new = run(code_new, True)

    check("before: completed run is reported as failed",
          old["execution_status"] == "failed", json.dumps(old))
    check("after: completed run is reported as completed",
          new["execution_status"] == "completed", json.dumps(new))
    check("after: skipped optional step is recorded",
          new.get("skipped_optional_steps") == ["1_pelicargo"], json.dumps(new))

    # Regression guard: a failed REQUIRED step must still fail the run.
    hard = run(code_new, False)
    check("regression: failed required step still fails the run",
          hard["execution_status"] == "failed" and hard["current_step"] == "1_pelicargo",
          json.dumps(hard))
    check("regression: required failure is not reported as skipped",
          hard.get("skipped_optional_steps") == [], json.dumps(hard))


def test_resolve_references(wf: dict) -> None:
    print("\n[10] behaviour - unresolved refs become empty, never literal text")
    if not HAS_NODE:
        print("  · skipped (node not on PATH)")
        return

    env = {
        "state": {
            "results": {},
            "next_step": {"id": "1_efc", "parameters": {
                "mawb": "057-79603090",
                "do_ref_no": "{{1_pelicargo.pelicargo_reference}}",
                "doc_number": "057-79603090"}},
        }
    }

    old = run_node(fixtures.RESOLVE_STEP_REFERENCES, env)[0]["json"]
    new = run_node(node_by_name(wf, "Resolve Step References")["parameters"]["jsCode"],
                   env)[0]["json"]

    check("before: literal placeholder would be typed into the form",
          old["state"]["next_step"]["parameters"]["do_ref_no"]
          == "{{1_pelicargo.pelicargo_reference}}",
          json.dumps(old["state"]["next_step"]["parameters"]))
    check("after: unresolvable ref collapses to ''",
          new["state"]["next_step"]["parameters"]["do_ref_no"] == "",
          json.dumps(new["state"]["next_step"]["parameters"]))
    check("after: the skipped ref is reported",
          new["state"]["unresolved_refs"] == ["1_pelicargo.pelicargo_reference"],
          json.dumps(new["state"].get("unresolved_refs")))
    check("after: non-referenced parameters survive",
          new["state"]["next_step"]["parameters"]["mawb"] == "057-79603090")


def test_final_response(wf: dict) -> None:
    print("\n[11] behaviour - the report explains skipped optional steps")
    if not HAS_NODE:
        print("  · skipped (node not on PATH)")
        return

    env = {
        "final_status": "FAILED",
        "plan": {"goal": "bill 057-79603090", "steps": [
            {"id": "1_pelicargo", "action": "Resolve Pelicargo shipment", "failed": True,
             "optional": True, "error": "service_unreachable"},
            {"id": "1_credentials", "action": "Retrieve EFC credentials", "completed": True},
            {"id": "1_efc", "action": "Create EFC Domestic Billing", "completed": True},
        ]},
        "state": {"execution_status": "completed", "skipped_optional_steps": ["1_pelicargo"]},
    }
    out = run_node(node_by_name(wf, "MOMI Final Response1")["parameters"]["jsCode"],
                   env)[0]["json"]
    check("optional failure does not render as a hard failure",
          "❌ 1_pelicargo" not in out["response"], out["response"])
    check("optional failure renders as skipped", "⏭ 1_pelicargo" in out["response"],
          out["response"])
    check("report explains why it was skipped",
          "Skipped optional step" in out["response"], out["response"])


def test_cli_roundtrip(tmpdir: Path) -> None:
    print("\n[12] cli - end-to-end import/export round trip")
    src = tmpdir / "MOMI.json"
    src.write_text(json.dumps(fixtures.build_mini_workflow()), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(Path(pmw.__file__)), str(src)],
        capture_output=True, text=True,
    )
    check("patcher exits 0", proc.returncode == 0, proc.stdout + proc.stderr)

    out = tmpdir / "MOMI.fixed.json"
    check("output file written", out.is_file())
    if out.is_file():
        data = json.loads(out.read_text(encoding="utf-8"))
        check("output is a valid n8n export",
              isinstance(data.get("nodes"), list) and "connections" in data)
        rep = pmw.patch(data, check_only=True)
        check("output passes --check", rep.ok() and rep.applied == [],
              "\n".join(rep.missing))


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 72)
    print("MOMI workflow patcher - test suite")
    print("=" * 72)

    wf = test_all_patches_apply()
    test_idempotent(wf)
    test_syntax(wf)
    test_connections(wf)
    test_switch_rule(wf)
    test_playbook(wf)
    test_format_hub_result(wf)
    test_commit_tool_result(wf)
    test_find_next_ready_step(wf)
    test_resolve_references(wf)
    test_final_response(wf)

    with tempfile.TemporaryDirectory() as td:
        test_cli_roundtrip(Path(td))

    print("\n" + "=" * 72)
    print(f"passed: {len(PASSES)}   failed: {len(FAILURES)}")
    if FAILURES:
        for f in FAILURES:
            print(f"  ✘ {f}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
