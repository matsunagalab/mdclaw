"""Tests for optional study/campaign helpers."""

import json

from mdclaw.study_server import (
    add_study_job,
    init_study,
    list_study_jobs,
    record_study_decision,
    record_study_plan,
    record_study_question,
    record_token_usage,
    summarize_study,
)


def test_init_study_creates_minimal_layout(tmp_path):
    study_dir = tmp_path / "study"

    result = init_study(
        str(study_dir),
        title="WT vs mutant",
        objective="Compare stability",
    )

    assert result["success"] is True
    assert (study_dir / "study.json").is_file()
    assert (study_dir / "jobs").is_dir()
    assert (study_dir / "annotations").is_dir()
    assert (study_dir / "evidence").is_dir()
    data = json.loads((study_dir / "study.json").read_text())
    assert data["schema_version"] == 1
    assert data["title"] == "WT vs mutant"
    assert data["objective"] == "Compare stability"
    assert data["jobs"] == []


def test_add_and_list_study_job_with_progress(tmp_path):
    from mdclaw._node import create_node

    study_dir = tmp_path / "study"
    init_study(str(study_dir))
    job_dir = study_dir / "jobs" / "wt"
    create_node(str(job_dir), "source")

    add_result = add_study_job(
        str(study_dir),
        job_id="wt",
        job_dir="jobs/wt",
        role="baseline",
    )
    assert add_result["success"] is True

    listed = list_study_jobs(str(study_dir))
    assert listed["success"] is True
    assert listed["jobs"][0]["job_id"] == "wt"
    assert listed["jobs"][0]["role"] == "baseline"
    assert listed["jobs"][0]["progress"]["node_count"] == 1
    assert listed["jobs"][0]["progress"]["nodes"]["source_001"]["type"] == "source"


def test_duplicate_job_id_is_rejected(tmp_path):
    study_dir = tmp_path / "study"
    init_study(str(study_dir))
    assert add_study_job(str(study_dir), "wt", "jobs/wt")["success"] is True

    result = add_study_job(str(study_dir), "wt", "jobs/wt2")

    assert result["success"] is False
    assert "already exists" in result["errors"][0]


def test_append_decision_question_and_token_logs(tmp_path):
    study_dir = tmp_path / "study"
    init_study(str(study_dir))

    decision = record_study_decision(
        str(study_dir),
        phase="plan",
        decision="run_short_screen",
        reason="Time budget favors triage",
        inputs=["study.json"],
        outputs=["plan.json"],
    )
    question = record_study_question(
        str(study_dir),
        question="Does V148A destabilize the active conformation?",
        rationale="Initial user objective",
    )
    token = record_token_usage(
        str(study_dir),
        phase="critic",
        purpose="Review branch metrics",
        tokens=1234,
        result="extend top candidates",
    )

    assert decision["success"] is True
    assert question["success"] is True
    assert token["success"] is True
    decision_rows = (study_dir / "decisions.jsonl").read_text().splitlines()
    question_rows = (study_dir / "question_history.jsonl").read_text().splitlines()
    token_rows = (study_dir / "token_ledger.jsonl").read_text().splitlines()
    assert json.loads(decision_rows[0])["decision"] == "run_short_screen"
    assert json.loads(question_rows[0])["record_type"] == "question"
    assert json.loads(token_rows[0])["tokens"] == 1234


def test_record_study_plan_rejects_missing_required_fields(tmp_path):
    study_dir = tmp_path / "study"
    init_study(str(study_dir))

    result = record_study_plan(str(study_dir), {"question": "Too small"})

    assert result["success"] is False
    assert "plan missing required field: md_goal" in result["errors"]
    assert not (study_dir / "study_plan.json").exists()


def test_summarize_study_counts_nodes(tmp_path):
    from mdclaw._node import complete_node, create_node

    study_dir = tmp_path / "study"
    init_study(str(study_dir))
    job_dir = study_dir / "jobs" / "wt"
    create_node(str(job_dir), "source")
    (job_dir / "nodes" / "source_001" / "artifacts" / "src.pdb").write_text("HEADER\n")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": "artifacts/src.pdb"},
    )
    add_study_job(str(study_dir), "wt", "jobs/wt")

    summary = summarize_study(str(study_dir))

    assert summary["success"] is True
    assert summary["summary"]["num_jobs"] == 1
    assert summary["summary"]["node_status_counts"]["completed"] == 1
    assert summary["summary"]["node_type_counts"]["source"] == 1
