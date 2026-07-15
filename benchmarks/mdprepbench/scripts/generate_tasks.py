#!/usr/bin/env python3
"""Generate canonical MDPrepBench task contracts and public prompts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mdclaw.benchmark.task_specs import build_task_payload


SUITE_DIR = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


_EVALUATOR_GENERATED_OUTPUTS = {
    "manifest.json",
    "metrics.json",
    "provenance.json",
    "minimized_structure.pdb",
    "minimization_report.json",
}

_FORBIDDEN_PREP_OUTPUTS = {
    "evidence_report.json",
    "command_log.json",
    "harness_execution.json",
}


def _task_payloads() -> list[tuple[str, dict]]:
    dataset = _read_json(SUITE_DIR / "dataset.json")
    defaults = _read_json(SUITE_DIR / "task_specs" / "defaults.json")

    payloads: list[tuple[str, dict]] = []
    for task_id in dataset["task_ids"]:
        spec_path = SUITE_DIR / "task_specs" / "tasks" / f"{task_id}.json"
        spec = _read_json(spec_path)
        payload = build_task_payload(defaults, spec)
        forbidden = _FORBIDDEN_PREP_OUTPUTS.intersection(payload["required_outputs"])
        if forbidden:
            raise ValueError(
                f"{task_id} requires evaluator-owned prep output(s): {sorted(forbidden)}"
            )
        payloads.append((task_id, payload))
    return payloads


def _raw_outputs(payload: dict) -> list[str]:
    outputs = [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
        "prepared_structure.pdb",
    ]
    for rel in payload.get("required_outputs", []):
        if rel in _EVALUATOR_GENERATED_OUTPUTS or rel == "prepared_structure.pdb":
            continue
        outputs.append(str(rel))
    return list(dict.fromkeys(outputs))


def _render_prompt(task_id: str, payload: dict) -> str:
    public_source = str(payload.get("public_source") or "public task source")
    output_lines = "\n".join(f"- `{rel}`" for rel in _raw_outputs(payload))
    return (
        f"# {task_id}: MD system preparation\n\n"
        f"You are evaluating an MD agent on `{task_id}`.\n\n"
        "Use this prompt as the task statement. Retrieve public sources as needed, "
        "and do not read `truth/` or `scorer/` if those directories exist.\n\n"
        f"Task: {payload['task_intent']}\n\n"
        f"Public source anchors: {public_source}.\n\n"
        "Prepare the requested system and energy-minimize it. Write only these raw "
        "artifacts to the exact submission directory:\n\n"
        f"{output_lines}\n\n"
        "`topology/state.xml` must contain the post-minimization OpenMM state and "
        "must be self-consistent with `topology/system.xml` and "
        "`topology/topology.pdb`. Full equilibration and production MD are not "
        "required.\n\n"
        "Do not write `manifest.json`, `metrics.json`, `provenance.json`, "
        "`minimized_structure.pdb`, `minimization_report.json`, "
        "`evidence_report.json`, a command log, walltime estimates, or artifact "
        "hashes. The evaluator derives the normalized metadata, minimized view, "
        "minimization report, and hashes from the raw artifacts. Evidence reports "
        "and solver command logs are not part of MDPrepBench v0.3. The harness "
        "owns the final record and measures walltime; non-MDClaw stage labels "
        "are solver-declared.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that generated task.json and prompt.md files are current",
    )
    args = parser.parse_args()

    mismatches: list[str] = []
    for task_id, payload in _task_payloads():
        task_path = SUITE_DIR / "tasks" / task_id / "task.json"
        prompt_path = SUITE_DIR / "tasks" / task_id / "prompt.md"
        prompt = _render_prompt(task_id, payload)
        if args.check:
            current = _read_json(task_path)
            if current != payload or prompt_path.read_text() != prompt:
                mismatches.append(task_id)
            continue
        task_path.write_text(json.dumps(payload, indent=2) + "\n")
        prompt_path.write_text(prompt)

    if mismatches:
        for task_id in mismatches:
            print(f"generated task contract or prompt differs: {task_id}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
