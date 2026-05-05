"""Tests for minimal MD evidence report generation."""

import json

from mdclaw.evidence_server import (
    generate_md_evidence_report,
    generate_study_evidence_report,
)


def _write_artifact(job_dir, node_id, rel_path, content="x\n"):
    path = job_dir / "nodes" / node_id / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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


def test_generate_md_evidence_report_includes_analyze_metrics(tmp_path):
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job"
    create_node(str(job_dir), "prod")
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
        metadata={"final_step": 100},
    )
    create_node(str(job_dir), "analyze", parent_node_ids=["prod_001"])
    _write_artifact(job_dir, "analyze_001", "artifacts/rmsd.csv")
    complete_node(
        str(job_dir),
        "analyze_001",
        artifacts={"rmsd_csv": "artifacts/rmsd.csv"},
        metadata={"mean_rmsd_nm": 0.25, "n_frames": 10},
    )

    result = generate_md_evidence_report(str(job_dir))

    assert result["success"] is True
    analyze_metrics = result["report"]["metrics"]["analyze"][0]
    assert analyze_metrics["node_id"] == "analyze_001"
    assert analyze_metrics["metrics"]["mean_rmsd_nm"] == 0.25
    assert result["report"]["status"] == "complete"


def test_generate_study_evidence_report(tmp_path):
    from mdclaw._node import complete_node, create_node
    from mdclaw.study_server import add_study_job, init_study

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    job_dir = study_dir / "jobs" / "wt"
    create_node(str(job_dir), "prod")
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
    )
    add_study_job(str(study_dir), "wt", "jobs/wt", role="baseline")

    result = generate_study_evidence_report(str(study_dir))

    assert result["success"] is True
    report_file = study_dir / "evidence" / "study_evidence_report.json"
    assert report_file.is_file()
    report = json.loads(report_file.read_text())
    assert report["question"] == "compare branches"
    assert report["metrics"]["num_jobs"] == 1
    assert report["metrics"]["jobs"][0]["job_id"] == "wt"
    assert report["metrics"]["aggregate_node_type_counts"]["prod"] == 1
