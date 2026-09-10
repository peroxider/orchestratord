"""PR merge-gate runner — DESIGN_PR_GATE_TEST.md §7.1.

Usage::

    python tests/gate/gate_runner.py

Runs (1) the gate suite ``tests/gate -m gate`` and (2) the G5 guard
nodeids, then assembles ``gate-report.json`` / ``gate-report.md`` with
per-layer durations. Exit code aggregates all failures.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

GUARD_NODEIDS = [
    "tests/test_architecture.py",
    "tests/test_layer_isolation.py",
    "tests/test_agent_cli_command_lock.py",
    "tests/test_capability_drift.py",
]


def _run_pytest(targets: list[str], junit: Path, extra: list[str]) -> tuple[int, float]:
    t0 = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *targets,
            "--junitxml",
            str(junit),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *extra,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode, time.monotonic() - t0


def _parse_junit(junit: Path) -> list[dict]:
    if not junit.exists():
        return []
    root = ET.parse(junit).getroot()
    cases = []
    for tc in root.iter("testcase"):
        verdict = "PASS"
        message = ""
        for child in tc:
            if child.tag == "failure":
                verdict = "FAIL"
                message = (child.get("message") or "")[:1500]
            elif child.tag == "error":
                verdict = "FAIL"
                message = (child.get("message") or "")[:1500]
            elif child.tag == "skipped":
                verdict = "SKIP"
                message = (child.get("message") or "")[:300]
        cases.append(
            {
                "id": tc.get("classname", "") + "::" + tc.get("name", ""),
                "file": tc.get("file", ""),
                "verdict": verdict,
                "duration_s": round(float(tc.get("time", 0)), 3),
                "message": message,
            }
        )
    return cases


def _layer_of(case: dict) -> str:
    # junitxml 的 file 属性可能缺失；classname/id 形如
    # "tests.gate.test_g0_static.TestG0::test_x"，从 id 解析层号。
    if re.search(r"test_g\d_", case["id"]):
        return "G" + re.search(r"test_g(\d)_", case["id"]).group(1)
    return "G5"


def main() -> int:
    junit_gate = REPO_ROOT / "gate-junit-gate.xml"
    junit_guard = REPO_ROOT / "gate-junit-guard.xml"

    print("== gate suite: tests/gate -m gate ==", flush=True)
    rc_gate, dur_gate = _run_pytest(["tests/gate"], junit_gate, ["-m", "gate"])
    print("== guard tests (G5) ==", flush=True)
    rc_guard, dur_guard = _run_pytest(list(GUARD_NODEIDS), junit_guard, [])

    cases = _parse_junit(junit_gate) + _parse_junit(junit_guard)
    for c in cases:
        c["layer"] = _layer_of(c)

    failed = [c for c in cases if c["verdict"] == "FAIL"]
    skipped = [c for c in cases if c["verdict"] == "SKIP"]
    passed = [c for c in cases if c["verdict"] == "PASS"]

    layers: dict[str, dict] = {}
    for c in sorted(cases, key=lambda c: c["layer"]):
        agg = layers.setdefault(
            c["layer"], {"pass": 0, "fail": 0, "skip": 0, "duration_s": 0.0}
        )
        agg[c["verdict"].lower()] += 1
        agg["duration_s"] = round(agg["duration_s"] + c["duration_s"], 3)

    verdict = "PASS" if not failed else "FAIL"
    report = {
        "verdict": verdict,
        "total_duration_s": round(dur_gate + dur_guard, 1),
        "summary": {"pass": len(passed), "fail": len(failed), "skip": len(skipped)},
        "layers": layers,
        "cases": cases,
    }

    report_json = REPO_ROOT / "gate-report.json"
    report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        f"# Gate report — {verdict}",
        "",
        f"Total: {report['total_duration_s']}s · "
        f"PASS {len(passed)} / FAIL {len(failed)} / SKIP {len(skipped)}",
        "",
        "| Layer | PASS | FAIL | SKIP | Duration |",
        "|---|---|---|---|---|",
    ]
    for layer, agg in sorted(layers.items()):
        lines.append(
            f"| {layer} | {agg['pass']} | {agg['fail']} | {agg['skip']} "
            f"| {agg['duration_s']}s |"
        )
    if failed:
        lines += ["", "## Failures", ""]
        for c in failed:
            lines += [
                f"- **{c['id']}** ({c['duration_s']}s)",
                f"  ```{c['message'][:600]}```",
            ]
    if skipped:
        lines += ["", "## Skips (registered)", ""]
        for c in skipped:
            lines.append(f"- {c['id']}: {c['message'][:200]}")
    (REPO_ROOT / "gate-report.md").write_text("\n".join(lines), encoding="utf-8")

    print()
    for layer, agg in sorted(layers.items()):
        print(
            f"  {layer}: PASS {agg['pass']}  FAIL {agg['fail']}  "
            f"SKIP {agg['skip']}  ({agg['duration_s']}s)"
        )
    print(f"\nGATE VERDICT: {verdict}  (suite {dur_gate:.0f}s + guards {dur_guard:.0f}s)")
    if failed:
        print(f"report: {report_json}")
    junit_gate.unlink(missing_ok=True)
    junit_guard.unlink(missing_ok=True)
    return 0 if not failed and rc_gate == 0 and rc_guard == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
