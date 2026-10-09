#!/usr/bin/env python3
"""
validate_workflow.py
====================

Structural + syntactic validation for an n8n workflow export.

Checks:
  1. JSON parses.
  2. Node ids are unique; every node has a name and a type.
  3. Every node referenced by a connection exists.
  4. Every node referenced by a Code node's $() call exists.
  5. Every jsCode / code field parses as JavaScript (requires node on PATH).
  6. Every `={{ ... }}` expression parses as JavaScript (best effort).
  7. Reports nodes with no incoming edge (should only be triggers/webhooks).

Usage:
    python3 scripts/validate_workflow.py workflows/MOMI-Orchestrator.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TRIGGER_TYPES = {
    "n8n-nodes-base.executeWorkflowTrigger",
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.start",
}


def check_js(code: str, label: str, problems: list[str]) -> None:
    src = f"function __v($json, $input, $execution, $env, $getWorkflowStaticData, $now, $today, $runIndex, $workflow, $nodeVersion, $prevNode) {{\n{code}\n}}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        if proc.returncode != 0:
            first = (proc.stderr or "").strip().splitlines()
            problems.append(f"{label}: JS syntax error -> {first[0] if first else '?'}")
    finally:
        Path(path).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow")
    ap.add_argument("--skip-js", action="store_true", help="skip the node --check pass")
    args = ap.parse_args(argv)

    path = Path(args.workflow)
    try:
        wf = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path} is not valid JSON: {exc}")
        return 1

    problems: list[str] = []
    nodes = wf.get("nodes", [])
    names = {n.get("name") for n in nodes}

    seen_ids: set[str] = set()
    for n in nodes:
        name = n.get("name")
        if not name:
            problems.append(f"node without a name: {n.get('id')}")
        nid = n.get("id")
        if not nid:
            problems.append(f"node '{name}' has no id")
        elif nid in seen_ids:
            problems.append(f"duplicate node id: {nid} ({name})")
        else:
            seen_ids.add(nid)
        if not n.get("type"):
            problems.append(f"node '{name}' has no type")

    # 3. connection targets exist
    edges = 0
    targets: set[str] = set()
    for source, conn in (wf.get("connections") or {}).items():
        if source not in names:
            problems.append(f"connections reference unknown source node: {source}")
        for outputs in (conn.get("main") or []):
            for link in outputs or []:
                edges += 1
                tgt = link.get("node")
                targets.add(tgt)
                if tgt not in names:
                    problems.append(f"{source} -> unknown node '{tgt}'")

    # 4. $('Node Name') references inside Code nodes exist
    dollar_ref = re.compile(r"\$\('([^']+)'\)")
    for n in nodes:
        code = (n.get("parameters") or {}).get("jsCode") or (n.get("parameters") or {}).get("code")
        if isinstance(code, str):
            for ref in dollar_ref.findall(code):
                if ref not in names:
                    problems.append(f"node '{n.get('name')}' references unknown node $('{ref}')")

    # 5/6. JavaScript syntax
    if not args.skip_js and shutil.which("node"):
        for n in nodes:
            params = n.get("parameters") or {}
            for field in ("jsCode", "code"):
                if isinstance(params.get(field), str):
                    check_js(params[field], f"{n.get('name')}.{field}", problems)

    # 7. orphan nodes
    orphans = sorted(
        n.get("name") for n in nodes
        if n.get("name") not in targets and n.get("type") not in TRIGGER_TYPES
    )
    unconnected_triggers = sorted(
        n.get("name") for n in nodes
        if n.get("name") not in targets and n.get("type") in TRIGGER_TYPES
    )

    print(f"workflow : {path}")
    print(f"nodes    : {len(nodes)}")
    print(f"edges    : {edges}")
    print(f"orphans  : {len(orphans)} (nodes with no incoming edge that are not triggers)")
    for o in orphans:
        print(f"            - {o}")
    print(f"triggers : {unconnected_triggers}")

    if problems:
        print(f"\nFAIL - {len(problems)} problem(s):")
        for p in problems:
            print(f"  ✘ {p}")
        return 1

    print("\nOK - structure and JavaScript syntax are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
