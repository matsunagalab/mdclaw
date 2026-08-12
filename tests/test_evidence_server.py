"""Tests for minimal MD evidence report generation."""

import json

from mdclaw.evidence import (
    generate_md_evidence_report,
    generate_study_evidence_report,
)


def _write_artifact(job_dir, node_id, rel_path, content="x\n"):
    path = job_dir / "nodes" / node_id / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)



def _seed_completed_eq(job_dir):
    """Study jobs no longer accept parentless nodes; satisfy the ancestor rule.

    This is a minimal fixture ancestry, not a canonical MD chain.
    """
    from mdclaw._node import complete_node, create_node

    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", "artifacts/candidates/candidate_001.pdb")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"candidates": "artifacts/candidates/candidate_001.pdb"},
    )
    create_node(str(job_dir), "eq", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "eq_001", "artifacts/state.xml")
    complete_node(
        str(job_dir), "eq_001", artifacts={"state_file": "artifacts/state.xml"}
    )



def test_generate_md_evidence_report_from_job(tmp_path):
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job"
    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", "artifacts/src.pdb", "HEADER\n")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": "artifacts/src.pdb"},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "prep_001", "artifacts/merged.pdb")
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merged.pdb"},
    )

    result = generate_md_evidence_report(str(job_dir), question="What is present?")

    assert result["success"] is True
    report_file = job_dir / "evidence" / "md_evidence_report.json"
    assert report_file.is_file()
    report = json.loads(report_file.read_text())
    assert report["schema_version"] == 1
    assert report["question"] == "What is present?"
    assert report["metrics"]["num_nodes"] == 2
    assert report["metrics"]["node_type_counts"]["source"] == 1
    assert report["metrics"]["node_type_counts"]["prep"] == 1
    assert "No completed production nodes" in report["limitations"][0]





def test_generate_study_evidence_report(tmp_path):
    from mdclaw._node import complete_node, create_node
    from mdclaw.study import add_study_job, init_study, record_study_plan

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    record_study_plan(
        str(study_dir),
        {
            "question": "Does the baseline branch remain stable?",
            "md_goal": "Summarize completed production nodes.",
            "jobs": [{"job_id": "wt", "purpose": "baseline"}],
            "analysis": ["production completion"],
            "decision": {"support": "prod completed", "against": "prod failed"},
        },
    )
    job_dir = study_dir / "jobs" / "wt"
    _seed_completed_eq(job_dir)
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
    )
    create_node(
        str(job_dir),
        "analyze",
        parent_node_ids=["prod_001"],
        conditions={"analysis_data_scope": "segment"},
    )
    _write_artifact(job_dir, "analyze_001", "artifacts/rmsd.csv")
    complete_node(
        str(job_dir),
        "analyze_001",
        artifacts={"rmsd_csv": "artifacts/rmsd.csv"},
        metadata={"analysis_type": "rmsd", "mean_rmsd_nm": 0.12},
    )
    add_study_job(str(study_dir), "wt", "jobs/wt", role="baseline")

    result = generate_study_evidence_report(str(study_dir))

    assert result["success"] is True
    report_file = study_dir / "evidence" / "study_evidence_report.json"
    assert report_file.is_file()
    report = json.loads(report_file.read_text())
    assert report["status"] == "complete"
    assert report["question"] == "Does the baseline branch remain stable?"
    assert report["metrics"]["num_jobs"] == 1
    assert report["metrics"]["jobs"][0]["job_id"] == "wt"
    assert report["metrics"]["study_plan"]["md_goal"] == "Summarize completed production nodes."
    assert report["provenance"]["study_plan_file"].endswith("study_plan.json")
    assert report["metrics"]["aggregate_node_type_counts"]["prod"] == 1
    assert report["metrics"]["analyze"][0]["metrics"]["mean_rmsd_nm"] == 0.12
    assert report["metrics"]["analyze"][0]["job_id"] == "wt"


def test_generate_study_evidence_report_is_incomplete_without_required_nodes(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.study import add_study_job, init_study, record_study_plan

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    record_study_plan(
        str(study_dir),
        {
            "question": "Does the baseline branch remain stable?",
            "md_goal": "Measure structural stability.",
            "jobs": [{"job_id": "wt", "purpose": "baseline"}],
            "analysis": ["RMSD"],
            "decision": {
                "support": "RMSD remains bounded.",
                "against": "RMSD diverges.",
                "inconclusive": "Sampling is insufficient.",
            },
        },
    )
    job_dir = study_dir / "jobs" / "wt"
    _seed_completed_eq(job_dir)
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    add_study_job(str(study_dir), "wt", "jobs/wt", role="baseline")

    result = generate_study_evidence_report(str(study_dir))

    assert result["success"] is True
    assert result["report"]["status"] == "incomplete"
    assert any("no completed production" in item for item in result["report"]["limitations"])
    assert any("no completed analyze" in item for item in result["report"]["limitations"])


def test_generate_study_evidence_report_tracks_missing_planned_jobs(tmp_path):
    from mdclaw._node import complete_node, create_node
    from mdclaw.study import add_study_job, init_study, record_study_plan

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    record_study_plan(
        str(study_dir),
        {
            "question": "Do WT and mutant differ?",
            "md_goal": "Compare structural dynamics.",
            "jobs": [{"job_id": "wt"}, {"job_id": "mutant"}],
            "analysis": [],
            "decision": {},
        },
    )
    job_dir = study_dir / "jobs" / "wt"
    _seed_completed_eq(job_dir)
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
    )
    add_study_job(str(study_dir), "wt", "jobs/wt", role="baseline")

    result = generate_study_evidence_report(str(study_dir))

    assert result["success"] is True
    assert result["report"]["status"] == "incomplete"
    assert result["report"]["metrics"]["planned_job_ids"] == ["wt", "mutant"]
    assert result["report"]["metrics"]["missing_planned_job_ids"] == ["mutant"]


def test_generate_study_evidence_report_selects_named_plan(tmp_path):
    from mdclaw._node import complete_node, create_node
    from mdclaw.study import add_study_job, init_study, record_study_plan

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    record_study_plan(
        str(study_dir),
        {
            "question": "Does the selected branch remain stable?",
            "md_goal": "Analyze the selected branch.",
            "jobs": [{"job_id": "selected"}],
            "analysis": ["RMSD"],
            "decision": {},
        },
        plan_id="revision-1",
    )
    for job_id, rmsd in (("selected", 0.2), ("unrelated", 9.0)):
        job_dir = study_dir / "jobs" / job_id
        _seed_completed_eq(job_dir)
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
        _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
        complete_node(
            str(job_dir),
            "prod_001",
            artifacts={"trajectory": "artifacts/trajectory.dcd"},
        )
        create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
            conditions={"analysis_data_scope": "segment"},
        )
        _write_artifact(job_dir, "analyze_001", "artifacts/rmsd.csv")
        complete_node(
            str(job_dir),
            "analyze_001",
            artifacts={"rmsd_csv": "artifacts/rmsd.csv"},
            metadata={"analysis_type": "rmsd", "mean_rmsd_nm": rmsd},
        )
        add_study_job(str(study_dir), job_id, f"jobs/{job_id}")

    result = generate_study_evidence_report(
        str(study_dir),
        plan_id="revision-1",
        output_name="revision-1.json",
    )

    assert result["success"] is True
    report = result["report"]
    assert report["status"] == "complete"
    assert report["metrics"]["registered_job_ids"] == ["selected", "unrelated"]
    assert report["metrics"]["planned_job_ids"] == ["selected"]
    assert [job["job_id"] for job in report["metrics"]["jobs"]] == ["selected"]
    assert report["metrics"]["analyze"][0]["metrics"]["mean_rmsd_nm"] == 0.2
    assert report["metadata"]["study_plan_id"] == "revision-1"
    assert report["provenance"]["study_plan_file"].endswith(
        "plans/revision-1.json"
    )


def test_generate_study_evidence_report_rejects_missing_named_plan(tmp_path):
    from mdclaw.study import init_study

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")

    result = generate_study_evidence_report(str(study_dir), plan_id="missing")

    assert result["success"] is False
    assert "study plan 'missing' not found" in result["errors"][0]


def test_generate_study_evidence_report_rejects_unreadable_active_plan(tmp_path):
    from mdclaw.study import init_study

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    (study_dir / "study_plan.json").write_text("{")

    result = generate_study_evidence_report(str(study_dir))

    assert result["success"] is False
    assert "study plan is unreadable" in result["errors"][0]



def test_generate_study_evidence_report_registered_as_tool():
    from mdclaw._cli import _discover_tools
    from mdclaw.evidence import TOOLS

    assert "generate_study_evidence_report" in TOOLS
    assert callable(TOOLS["generate_study_evidence_report"])
    assert "generate_study_evidence_report" in _discover_tools()
