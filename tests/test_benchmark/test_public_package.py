"""Public package export tests for external-agent benchmark use.

These tests keep the agent-visible package distinct from the canonical
evaluator tree. External agents should receive prompts and submission
contracts, not scorer metadata or held-back truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_export_private_package_omits_public_prompt(
    tmp_path: Path,
):
    out_dir = tmp_path / "private_mdstudybench"
    result = cli.export_benchmark_private_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    truth_file = (
        out_dir
        / "tasks"
        / "S01_pressure_hydration_t4l_l99a"
        / "truth"
        / "experimental_truth.json"
    )
    assert truth_file.is_file()
    assert not list(out_dir.glob("tasks/*/prompt.md"))
    assert not list(out_dir.glob("tasks/*/submission_contract.json"))
    assert not list(out_dir.glob("tasks/*/submission_checklist.md"))


def test_export_studybench_public_package_uses_study_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdstudybench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(out_dir),
    )

    assert result["success"], result
    dataset = json.loads((out_dir / "dataset.json").read_text())
    marker = json.loads(
        (out_dir / ".md-benchmark-public-export.json").read_text()
    )
    assert "dataset_dir" not in marker
    assert str(STUDY_DATASET_DIR.resolve()) not in json.dumps(marker)
    assert dataset["benchmark_version"] == "MDStudyBench-v0.4"
    assert dataset["task_ids"] == ["S01_pressure_hydration_t4l_l99a"]
    assert dataset["tiers"]["pilot"]["task_ids"] == dataset["task_ids"]
    assert dataset["tiers"]["pilot"]["primary_leaderboard"] is False
    assert result["task_count"] == 1
    assert (out_dir / "schemas" / "confirmatory_plan.schema.json").is_file()
    assert (out_dir / "schemas" / "claim.schema.json").is_file()
    assert not (
        out_dir / "schemas" / "submission_manifest.schema.json"
    ).exists()
    assert not (out_dir / "tools" / "package_submission.py").exists()
    assert (out_dir / "tools" / "validate_submission.py").is_file()
    for internal_tool in (
        "preregistration_v2.py",
        "study_evidence_v2.py",
        "study_identity_v2.py",
        "study_execution_v2.py",
    ):
        assert not (out_dir / "tools" / internal_tool).exists()
    public_readme = (out_dir / "README.md").read_text()
    assert "confirmatory_plan.json" in public_readme
    assert "claim.json" in public_readme
    assert "runner generates `manifest.json`" in public_readme

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "S01_pressure_hydration_t4l_l99a"
            / "submission_contract.json"
        ).read_text()
    )
    assert contract["primary_score"] == "scientific_answer"
    assert contract["evaluation_protocol"] == "grounded_correct_v2"
    assert contract["required_outputs"] == [
        "confirmatory_plan.json",
        "claim.json",
    ]
    assert "submission_manifest_schema" not in contract
    assert contract["manifest_contract"] == {
        "generated_by": {"tool": "mdclaw_benchmark_runner"},
        "agent_authored": [
            "confirmatory_plan.json",
            "claim.json",
        ],
        "runner_generated": [
            "manifest.json",
            "episode/episode.json",
            "episode/artifacts/",
        ],
        "agent_must_not_write_runner_outputs": True,
    }
    assert contract["confirmatory_plan_schema"] == (
        "../../schemas/confirmatory_plan.schema.json"
    )
    assert contract["claim_schema"] == "../../schemas/claim.schema.json"
    assert contract["runner_episode_contract"]["agent_authored"] == [
        "confirmatory_plan.json",
        "claim.json",
    ]
    assert contract["runner_episode_contract"]["runner_generated"] == [
        "manifest.json",
        "episode/episode.json",
        "episode/artifacts/",
    ]

    blueprint = contract["submission_blueprint"]
    assert set(blueprint) == {
        "confirmatory_plan_minimum",
        "claim_minimum",
        "runner_generated",
    }
    plan = blueprint["confirmatory_plan_minimum"]
    assert {run["condition_role"] for run in plan["runs"]} == {
        "reference",
        "variant",
    }
    assert all(run["simulation_time_ns"] == 10.0 for run in plan["runs"])
    claim = blueprint["claim_minimum"]
    assert claim["status"]["one_of"] == [
        "resolved",
        "unresolved",
    ]
    assert claim["outcome"]["one_of"] == [
        "increased_hydration",
        "decreased_hydration",
        "no_material_change",
        None,
    ]

    lifecycle = contract["submission_lifecycle"]
    assert "preflight_command_template" not in lifecycle
    assert "No agent-side public preflight" in lifecycle["preflight_policy"]
    assert "write claim.json, and exit" in lifecycle["exit_condition"]
    public_keys = {str(key) for key in _walk_keys(contract)}
    assert {
        "analysis_intent",
        "study_index",
        "evidence_report",
        "preregistration_implementation",
        "identity_implementation",
        "execution_implementation",
        "expected_outcome",
        "expected_direction",
        "expected_answer",
        "ground_truth",
        "ground_truth_checks",
        "experimental_anchors",
        "scoring",
        "references",
    }.isdisjoint(public_keys)

    checklist = (
        out_dir
        / "tasks"
        / "S01_pressure_hydration_t4l_l99a"
        / "submission_checklist.md"
    ).read_text()
    assert "`confirmatory_plan.json`" in checklist
    assert "`claim.json`" in checklist
    assert "## Manifest Outputs" not in checklist
    assert "No agent-side public preflight" in checklist
    task_dir = out_dir / "tasks" / "S01_pressure_hydration_t4l_l99a"
    assert not (task_dir / "task.json").exists()
    assert not (task_dir / "truth").exists()
