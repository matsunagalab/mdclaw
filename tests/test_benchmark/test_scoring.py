"""Scoring arithmetic tests for v1.0.

These cover the three places where v0.1 was wrong:
- weighted_total formula and ceiling
- per-axis aggregation divisor
- manifest.status semantics
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from mdclaw.benchmark import normalization, scoring, validation
from mdclaw.benchmark.models import (
    DeterministicCheck,
    GroundTruthCheck,
    IntegrityCheck,
    Task,
    TaskScoring,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"


def _make_task(primary, secondaries=None, det_checks=None, gt_checks=None):
    return Task(
        schema_version="1.0",
        task_id="t",
        category="engine_sanity",
        primary_score=primary,
        secondary_scores=secondaries or [],
        execution_mode="lite",
        time_limit_minutes=30,
        scoring=TaskScoring(
            deterministic_checks=det_checks or [],
            ground_truth_checks=gt_checks or [],
        ),
        task_intent="x",
    )


def _write_submission(tmp: Path, manifest: dict, metrics: dict | None = None,
                      provenance: dict | None = None,
                      evidence: dict | None = None):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "manifest.json").write_text(json.dumps(manifest))
    if metrics is not None:
        (tmp / "metrics.json").write_text(json.dumps(metrics))
    if provenance is not None:
        (tmp / "provenance.json").write_text(json.dumps(provenance))
    if evidence is not None:
        (tmp / "evidence_report.json").write_text(json.dumps(evidence))


def _raw_output_hashes(tmp: Path, rel_paths: list[str]) -> list[dict[str, str]]:
    out = []
    for rel_path in rel_paths:
        data = (tmp / rel_path).read_bytes()
        out.append({
            "path": rel_path,
            "md5": hashlib.md5(data).hexdigest(),
        })
    return out


def _prep_command_log() -> list[dict]:
    return [
        {
            "stage": "source",
            "command": "fixture source retrieval",
            "exit_code": 0,
            "walltime_seconds": 1.0,
        },
        {
            "stage": "prep",
            "command": "fixture prepare_complex",
            "exit_code": 0,
            "walltime_seconds": 1.0,
        },
        {
            "stage": "topo",
            "command": "fixture build_openmm_system",
            "exit_code": 0,
            "walltime_seconds": 1.0,
        },
        {
            "stage": "min",
            "command": "fixture minimization",
            "exit_code": 0,
            "walltime_seconds": 1.0,
        },
    ]


def _write_openmm_bundle(
    tmp: Path,
    *,
    broken: str | None = None,
    huge_energy: bool = False,
    include_water: bool = False,
    extra_residues: list[tuple[str, int]] | None = None,
) -> None:
    topo_dir = tmp / "topology"
    topo_dir.mkdir(parents=True, exist_ok=True)
    system_xml = topo_dir / "system.xml"
    topology_pdb = topo_dir / "topology.pdb"
    state_xml = topo_dir / "state.xml"
    if broken == "system":
        system_xml.write_text("<not-a-system/>\n")
        topology_pdb.write_text("END\n")
        state_xml.write_text("<State/>\n")
        return
    if broken == "state":
        system_xml.write_text("<System/>\n")
        topology_pdb.write_text("END\n")
        state_xml.write_text("<not-a-state/>\n")
        return

    from openmm import (
        Context,
        Platform,
        CustomExternalForce,
        NonbondedForce,
        System,
        Vec3,
        VerletIntegrator,
        XmlSerializer,
        unit,
    )
    from openmm.app import Element, PDBFile, Topology

    topology = Topology()
    chain = topology.addChain("A")
    residue = topology.addResidue("ALA", chain, "1")
    topology.addAtom("CA", Element.getBySymbol("C"), residue)
    position_value = float("nan") if broken == "nan_positions" else 0.0
    positions = [Vec3(position_value, 0.0, 0.0)]
    pdb_positions = [Vec3(0.0, 0.0, 0.0)]
    system = System()
    system.addParticle(12.0)
    nonbonded = NonbondedForce()
    nonbonded.addParticle(0.0, 0.1, 0.0)
    if include_water:
        water = topology.addResidue("HOH", chain, "2")
        topology.addAtom("O", Element.getBySymbol("O"), water)
        system.addParticle(16.0)
        nonbonded.addParticle(0.0, 0.1, 0.0)
        positions.append(Vec3(0.4, 0.0, 0.0))
        pdb_positions.append(Vec3(0.4, 0.0, 0.0))
    for res_index, (resname, atom_count) in enumerate(extra_residues or [], start=3):
        extra = topology.addResidue(resname, chain, str(res_index))
        for atom_i in range(atom_count):
            topology.addAtom(f"C{atom_i + 1}", Element.getBySymbol("C"), extra)
            system.addParticle(12.0)
            nonbonded.addParticle(0.0, 0.1, 0.0)
            x = 0.8 + 0.1 * atom_i
            positions.append(Vec3(x, 0.0, 0.0))
            pdb_positions.append(Vec3(x, 0.0, 0.0))
    positions_q = positions * unit.nanometer
    pdb_positions_q = pdb_positions * unit.nanometer
    system.addForce(nonbonded)
    if huge_energy:
        force = CustomExternalForce("2000000")
        force.addParticle(0, [])
        system.addForce(force)
    integrator = VerletIntegrator(1.0 * unit.femtoseconds)
    context = Context(system, integrator, Platform.getPlatformByName("Reference"))
    context.setPositions(positions_q)
    state = context.getState(getPositions=True, getEnergy=True)

    system_xml.write_text(XmlSerializer.serialize(system))
    state_xml.write_text(XmlSerializer.serialize(state))
    with topology_pdb.open("w") as handle:
        PDBFile.writeFile(topology, pdb_positions_q, handle, keepIds=True)


def _write_minimization_submission(tmp: Path, *, completed: bool = True,
                                   finite_report_energy: bool = True,
                                   broken: str | None = None,
                                   minimized_structure: str | None = None):
    _write_openmm_bundle(tmp, broken=broken)
    report = {
        "minimization": {
            "attempted": True,
            "completed": completed,
            "energy_initial_kj_mol": 0.0 if finite_report_energy else float("nan"),
            "energy_final_kj_mol": 0.0 if finite_report_energy else float("nan"),
            "energy_is_finite": completed,
            "positions_are_finite": completed,
            "atom_count_preserved": completed,
            "backend": "openmm",
        }
    }
    (tmp / "minimization_report.json").write_text(json.dumps(report))
    (tmp / "minimized_structure.pdb").write_text(
        minimized_structure
        or (
            "HETATM    1  C1  AP5 A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
    )
    _write_submission(
        tmp,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "topology": [
                    "topology/system.xml",
                    "topology/topology.pdb",
                    "topology/state.xml",
                ],
                "minimization_report": "minimization_report.json",
                "minimized_structure": "minimized_structure.pdb",
            },
        },
        metrics={
            "topology": {"backend": "openmm", "build_success": completed},
            "minimization": report["minimization"],
        },
    )


def _source_selection_record(
    *,
    candidate_id: str,
    model_rank: int,
    include_reason: bool = True,
) -> dict:
    selection = {"structure_id": candidate_id}
    if include_reason:
        selection["reason"] = f"Selected model rank {model_rank} from the prompt."
    return {
        "schema_version": 1,
        "source_bundle": "source/source_bundle.json",
        "selection": selection,
        "selected_structure": {
            "structure_id": candidate_id,
            "candidate_id": candidate_id,
            "rank": model_rank,
            "origin": {
                "kind": "pdb",
                "model_index": model_rank - 1,
                "model_rank": model_rank,
                "model_id": str(model_rank),
            },
        },
    }


def _write_source_selection(
    tmp: Path,
    *,
    candidate_id: str,
    model_rank: int,
    include_reason: bool = True,
) -> None:
    (tmp / "source_selection.json").write_text(
        json.dumps(
            _source_selection_record(
                candidate_id=candidate_id,
                model_rank=model_rank,
                include_reason=include_reason,
            )
        )
    )


def test_weighted_total_no_secondary_caps_at_one(tmp_path: Path):
    """A perfect task with no secondaries should hit weighted_total = 1.0,
    not v0.1's 0.8 ceiling."""
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "completed",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.scores["execution"] == 1.0
    assert score.weighted_total == 1.0


def test_weighted_total_with_secondary_uses_blended_formula(tmp_path: Path):
    """secondaries pull weighted_total = 0.8 * primary + 0.2 * mean(secondary).
    With LLM judge supplying secondary=1.0, weighted_total should still be 1.0
    (not 0.8) — a perfect performance reaches 1.0 regardless of secondary
    presence."""
    task = _make_task(
        primary="execution",
        secondaries=["evidence_communication"],
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "completed",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(
        task, tmp_path,
        llm_judge_payload={"scores": {"evidence_communication": 1.0}},
    )
    assert score.weighted_total == pytest.approx(1.0)


def test_weighted_total_falls_back_when_secondary_unevaluable(tmp_path: Path):
    """No LLM judge file: secondary axis is None and falls out of the
    weighted_total formula. weighted_total reduces to primary alone."""
    task = _make_task(
        primary="execution",
        secondaries=["evidence_communication"],
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "completed",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.scores["evidence_communication"] is None
    assert score.weighted_total == 1.0


def test_status_partial_multiplies_weighted_total_by_0_6(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "partial",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.weighted_total == pytest.approx(0.6)


def test_status_blocked_zeros_weighted_total(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "blocked"},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.weighted_total == 0.0
    assert score.status == "failed"
    assert any("does not allow blocked" in warning
               for warning in score.integrity_warnings)


def test_validate_submission_rejects_disallowed_blocked_status(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    submission_dir = tmp_path / "submission"
    _write_submission(
        submission_dir,
        manifest={"task_id": "t", "status": "blocked"},
        metrics={"execution": {"completed": False}},
    )

    result = validation.validate_submission(task_file, submission_dir)
    assert result["success"] is False
    assert any("does not allow blocked" in err for err in result["errors"])


def test_validate_submission_rejects_manifest_output_path_escape(tmp_path: Path):
    task = _make_task(primary="execution")
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    submission_dir = tmp_path / "submission"
    _write_submission(
        submission_dir,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"metrics": "../task.json"},
        },
        metrics={"execution": {"completed": True}},
    )

    result = validation.validate_submission(task_file, submission_dir)

    assert result["success"] is False
    assert any("escapes submission directory" in err for err in result["errors"])


def test_completed_prep_validation_requires_topology_manifest_output(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="topo",
                check_type="topology_artifact_bundle",
                weight=1.0,
            )
        ],
    )
    task.required_outputs = [
        "manifest.json",
        "metrics.json",
        "provenance.json",
        "evidence_report.json",
        "prepared_structure.pdb",
        "minimized_structure.pdb",
        "minimization_report.json",
    ]
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    submission_dir = tmp_path / "submission"
    _write_submission(
        submission_dir,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "metrics": "metrics.json",
                "provenance": "provenance.json",
                "evidence_report": "evidence_report.json",
                "prepared_structure": "prepared_structure.pdb",
                "minimized_structure": "minimized_structure.pdb",
                "minimization_report": "minimization_report.json",
            },
        },
        metrics={},
        provenance={},
        evidence={},
    )
    for name in task.required_outputs:
        if name != "manifest.json":
            (submission_dir / name).write_text("{}")

    result = validation.validate_submission(task_file, submission_dir)

    assert result["success"] is False
    assert any("outputs.topology" in err for err in result["errors"])


def test_prep_validation_hints_to_wait_for_completed_artifacts(tmp_path: Path):
    task = _make_task(primary="preparation")
    task.required_outputs = ["topology/system.xml"]
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    submission_dir = tmp_path / "submission"
    _write_submission(
        submission_dir,
        manifest={"task_id": "t", "status": "completed", "outputs": {}},
    )

    result = validation.validate_submission(task_file, submission_dir)

    assert result["success"] is False
    assert result["missing_outputs"] == ["topology/system.xml"]
    assert any("still running" in hint for hint in result["hints"])


def test_prep_normalization_reports_incomplete_background_work(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="topo",
                check_type="topology_artifact_bundle",
                weight=1.0,
            )
        ],
    )
    raw_dir = tmp_path / "submission"
    raw_dir.mkdir()

    result = normalization.normalize_preparation_submission(
        task=task,
        raw_submission_dir=raw_dir,
        normalized_submission_dir=tmp_path / "normalized_submission",
    )

    assert result["success"] is False
    assert any("topology/system.xml" in err for err in result["errors"])
    assert any("still running in the background" in err for err in result["errors"])


def test_study_methods_validation_does_not_require_metrics_output(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    task_file = (
        repo_root
        / "benchmarks"
        / "mdstudybench"
        / "tasks"
        / "S03_ppi_evidence_bundle_barnase"
        / "task.json"
    )
    submission_dir = tmp_path / "submission"
    submission_dir.mkdir()
    manifest = {
        "schema_version": "1.0",
        "task_id": "S03_ppi_evidence_bundle_barnase",
        "status": "completed",
        "outputs": {
            "evidence_report": "evidence_report.json",
            "methods": "methods.md",
            "provenance": "provenance.json",
            "decision_log": "decision_log.jsonl",
        },
    }
    (submission_dir / "manifest.json").write_text(json.dumps(manifest))
    (submission_dir / "evidence_report.json").write_text("{}")
    (submission_dir / "methods.md").write_text("## Methods\n\n## Limitations\n")
    (submission_dir / "provenance.json").write_text("{}")
    (submission_dir / "decision_log.jsonl").write_text("{}\n")

    result = validation.validate_submission(task_file, submission_dir)

    assert result["success"] is True
    assert not any("outputs.metrics" in err for err in result["errors"])


def test_study_answer_validation_requires_trajectory_manifest_outputs(
    tmp_path: Path,
):
    repo_root = Path(__file__).resolve().parents[2]
    task_file = (
        repo_root
        / "benchmarks"
        / "mdstudybench"
        / "tasks"
        / "S01_stability_t4l_l99a"
        / "task.json"
    )
    submission_dir = tmp_path / "submission"
    _write_submission(
        submission_dir,
        manifest={
            "schema_version": "1.0",
            "task_id": "S01_stability_t4l_l99a",
            "status": "completed",
            "outputs": {
                "metrics": "metrics.json",
                "provenance": "provenance.json",
                "evidence_report": "evidence_report.json",
            },
        },
        metrics={"md_analysis": {"production_time_ns": 1.0}},
        provenance={},
        evidence={},
    )

    result = validation.validate_submission(task_file, submission_dir)

    assert result["success"] is False
    assert any("outputs.trajectories" in err for err in result["errors"])


def test_score_submission_rejects_manifest_output_path_escape(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[
            DeterministicCheck(
                check_id="ok",
                check_type="json_equals",
                json_path="execution.completed",
                equals=True,
                weight=1.0,
            )
        ],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"metrics": "../stolen_metrics.json"},
        },
        metrics={"execution": {"completed": True}},
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert any("escapes submission directory" in w for w in score.integrity_warnings)


def test_score_submission_rejects_missing_execution_evidence_under_reject_policy(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="ok",
                check_type="json_equals",
                json_path="preparation.topology_ready",
                equals=True,
                weight=1.0,
            )
        ],
    )
    task.scoring.integrity_policy = "reject"
    task.scoring.integrity_checks = [
        IntegrityCheck(
            check_id="workflow_execution_recorded",
            check_type="provenance_execution_evidence",
            required_stages=["source", "prep", "topo", "min"],
            min_command_count=4,
        )
    ]
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        metrics={"preparation": {"topology_ready": True}},
        provenance={"scripts": [{"path": "make_fake.py"}]},
        evidence={},
    )

    score = scoring.score_submission(
        task,
        tmp_path,
        harness_record_file=tmp_path / "missing_harness_execution.json",
    )

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert any("scripts alone" in w for w in score.integrity_warnings)


def test_score_submission_rejects_solver_only_provenance_when_harness_required(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="ok",
                check_type="json_equals",
                json_path="preparation.topology_ready",
                equals=True,
                weight=1.0,
            )
        ],
    )
    task.scoring.integrity_policy = "reject"
    task.scoring.integrity_checks = [
        IntegrityCheck(
            check_id="workflow_execution_recorded",
            check_type="provenance_execution_evidence",
            required_stages=["source", "prep", "topo", "min"],
            min_command_count=4,
            require_harness_record=True,
        )
    ]
    command_log = [
        {"stage": "source", "command": "mdclaw fetch", "exit_code": 0},
        {"stage": "prep", "command": "mdclaw prepare_complex", "exit_code": 0},
        {"stage": "topo", "command": "mdclaw build_openmm_system", "exit_code": 0},
        {"stage": "min", "command": "mdclaw run_minimization", "exit_code": 0},
    ]
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        metrics={"preparation": {"topology_ready": True}},
        provenance={"command_log": command_log},
        evidence={},
    )

    score = scoring.score_submission(
        task,
        tmp_path,
        harness_record_file=tmp_path / "missing_harness_execution.json",
    )

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert any("harness execution record required" in w for w in score.integrity_warnings)


def test_score_submission_accepts_harness_execution_record_when_required(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="ok",
                check_type="json_equals",
                json_path="preparation.topology_ready",
                equals=True,
                weight=1.0,
            )
        ],
    )
    task.scoring.integrity_policy = "reject"
    task.scoring.integrity_checks = [
        IntegrityCheck(
            check_id="workflow_execution_recorded",
            check_type="provenance_execution_evidence",
            required_stages=["source", "prep", "topo", "min"],
            min_command_count=4,
            require_harness_record=True,
        )
    ]
    command_log = [
        {
            "stage": stage,
            "command": f"mdclaw {stage}",
            "exit_code": 0,
            "walltime_seconds": 1.0,
        }
        for stage in ["source", "prep", "topo", "min"]
    ]
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        metrics={"preparation": {"topology_ready": True}},
        provenance={"command_log": command_log},
        evidence={},
    )
    (tmp_path.parent / "harness_execution.json").write_text(
        json.dumps({"records": command_log})
    )

    score = scoring.score_submission(
        task,
        tmp_path,
        harness_record_file=tmp_path.parent / "harness_execution.json",
    )

    assert score.status == "passed"
    assert score.weighted_total == 1.0


def test_score_submission_accepts_jsonl_harness_execution_record_when_required(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="ok",
                check_type="json_equals",
                json_path="preparation.topology_ready",
                equals=True,
                weight=1.0,
            )
        ],
    )
    task.scoring.integrity_policy = "reject"
    task.scoring.integrity_checks = [
        IntegrityCheck(
            check_id="workflow_execution_recorded",
            check_type="provenance_execution_evidence",
            required_stages=["source", "prep", "topo", "min"],
            min_command_count=4,
            require_harness_record=True,
        )
    ]
    command_log = [
        {
            "stage": stage,
            "command": f"mdclaw {stage}",
            "exit_code": 0,
            "walltime_seconds": 1.0,
        }
        for stage in ["source", "prep", "topo", "min"]
    ]
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        metrics={"preparation": {"topology_ready": True}},
        provenance={"command_log": command_log},
        evidence={},
    )
    harness_path = tmp_path.parent / "harness_execution.json"
    harness_path.write_text("\n".join(json.dumps(entry) for entry in command_log))

    score = scoring.score_submission(
        task,
        tmp_path,
        harness_record_file=harness_path,
    )

    assert score.status == "passed"
    assert score.weighted_total == 1.0


def test_status_failed_keeps_score_when_allowed_truth_passes(tmp_path: Path):
    """A task may define a scorer-side truth check for an intentional failed
    outcome; if that hidden check passes, failed status can still receive
    credit. Public prep tasks should avoid using this for MDClaw guardrails."""
    truth_dir = tmp_path / "task" / "truth"
    truth_dir.mkdir(parents=True)
    (truth_dir / "expected_failure.json").write_text(
        json.dumps({"expected_failure_code": "curator_allowed_failure"})
    )
    task = _make_task(
        primary="preparation",
        gt_checks=[GroundTruthCheck(
            check_id="g", truth_file="truth/expected_failure.json",
            truth_path="expected_failure_code",
            submission_file="metrics.json",
            submission_path="preparation.failure_code",
        )],
    )
    sub_dir = tmp_path / "submission"
    _write_submission(
        sub_dir, manifest={"task_id": "t", "status": "failed"},
        metrics={"preparation": {"failure_code": "curator_allowed_failure"}},
    )
    score = scoring.score_submission(task, sub_dir, task_dir=tmp_path / "task")
    assert score.weighted_total == 1.0


def test_status_failed_zeros_when_no_ground_truth_passes(tmp_path: Path):
    """Random failed status with no compensating ground_truth → weighted = 0."""
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "failed"},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.weighted_total == 0.0


def test_axis_aggregation_divides_by_in_scope_tasks_only():
    """The v0.1 bug: dividing by total task count caps perfect runs at
    1/n_tasks per axis. v1.0 must divide by tasks where the axis is in scope.
    """
    scores = [
        {"weighted_total": 1.0,
         "scores": {"execution": 1.0, "preparation": None,
                    "scientific_answer": None, "evidence_communication": None},
         "runtime": {}},
        {"weighted_total": 1.0,
         "scores": {"execution": 1.0, "preparation": None,
                    "scientific_answer": None, "evidence_communication": None},
         "runtime": {}},
        {"weighted_total": 1.0,
         "scores": {"execution": None, "preparation": 1.0,
                    "scientific_answer": None, "evidence_communication": None},
         "runtime": {}},
    ]
    tasks = [
        {"task_id": "T01", "primary_score": "execution", "secondary_scores": []},
        {"task_id": "T02", "primary_score": "execution", "secondary_scores": []},
        {"task_id": "T03", "primary_score": "preparation", "secondary_scores": []},
    ]
    aggregate = scoring.aggregate_run_scores(scores, tasks)
    assert aggregate["scores"]["execution"] == pytest.approx(1.0)
    assert aggregate["scores"]["preparation"] == pytest.approx(1.0)
    assert aggregate["scores"]["scientific_answer"] is None
    assert aggregate["scores"]["evidence_communication"] is None
    assert aggregate["overall_score"] == pytest.approx(1.0)


def test_required_files_check_fails_on_missing(tmp_path: Path):
    task = _make_task(
        primary="evidence_communication",
        det_checks=[DeterministicCheck(check_id="rf", check_type="required_files",
                                        required_outputs=["methods.md", "evidence_report.json"],
                                        weight=1.0)],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "completed"},
                      evidence={"summary": "x"})
    score = scoring.score_submission(task, tmp_path)
    failed = next(c for c in score.deterministic_checks if c.check_id == "rf")
    assert failed.passed is False
    assert "methods.md" in failed.message


def test_json_min_length_check(tmp_path: Path):
    task = _make_task(
        primary="evidence_communication",
        det_checks=[DeterministicCheck(check_id="figs", check_type="json_min_length",
                                        json_file="manifest.json",
                                        json_path="outputs.figures",
                                        min_length=2, weight=1.0)],
    )
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed",
                  "outputs": {"figures": ["a.png", "b.png", "c.png"]}},
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True


def test_forbidden_files_check(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_bad_file",
            check_type="forbidden_files",
            forbidden_outputs=["prepared_structure.pdb"],
            weight=1.0,
        )],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "completed"})
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    (tmp_path / "prepared_structure.pdb").write_text("END\n")
    score_with_file = scoring.score_submission(task, tmp_path)
    failed = score_with_file.deterministic_checks[0]
    assert failed.passed is False
    assert "forbidden files present" in failed.message


def test_candidate_selection_check_requires_structured_source_selection(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="candidate_metric",
                check_type="json_equals",
                json_path="preparation.selected_candidate_id",
                equals="candidate_005",
                weight=0.1,
            ),
            DeterministicCheck(
                check_id="model_rank_metric",
                check_type="json_equals",
                json_path="preparation.selected_model_rank",
                equals=5,
                weight=0.1,
            ),
            DeterministicCheck(
                check_id="source_selection_model_5",
                check_type="candidate_selection_check",
                required_candidate_id="candidate_005",
                required_model_rank=5,
                require_selection_reason=True,
                source_selection_manifest_path="outputs.source_selection",
                source_selection_path="source_selection.json",
                weight=1.0,
            ),
        ],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"metrics": "metrics.json"},
        },
        metrics={
            "preparation": {
                "selected_candidate_id": "candidate_005",
                "selected_model_rank": 5,
                "candidate_selection_reason_recorded": True,
            }
        },
    )

    score = scoring.score_submission(task, tmp_path)

    # Graded scoring: a fidelity/identity miss like a wrong candidate selection
    # is not a physical-validity gate failure, so it reduces the score
    # proportionally instead of zeroing the whole task.
    assert score.status != "passed"
    assert 0.0 < score.weighted_total < 1.0
    failed = {
        result.check_id: result
        for result in score.deterministic_checks
        if not result.passed
    }
    assert "source_selection_model_5" in failed


def test_candidate_selection_check_accepts_source_selection_artifact(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="source_selection_model_5",
                check_type="candidate_selection_check",
                required_candidate_id="candidate_005",
                required_model_rank=5,
                require_selection_reason=True,
                source_selection_manifest_path="outputs.source_selection",
                source_selection_path="source_selection.json",
                weight=1.0,
            ),
        ],
    )
    _write_source_selection(tmp_path, candidate_id="candidate_005", model_rank=5)
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"source_selection": "source_selection.json"},
        },
        metrics={},
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "passed"
    assert score.weighted_total == 1.0
    assert score.deterministic_checks[0].passed is True


def test_candidate_selection_check_accepts_structured_provenance(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="source_selection_model_5",
                check_type="candidate_selection_check",
                required_candidate_id="candidate_005",
                required_model_rank=5,
                require_selection_reason=True,
                source_selection_manifest_path="outputs.source_selection",
                source_selection_path="source_selection.json",
                weight=1.0,
            ),
        ],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {},
        },
        metrics={},
        provenance={
            "source_selection": _source_selection_record(
                candidate_id="candidate_005",
                model_rank=5,
            )
        },
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "passed"
    assert score.weighted_total == 1.0
    assert score.deterministic_checks[0].passed is True
    assert "provenance.json satisfies candidate selection" in (
        score.deterministic_checks[0].message
    )


def test_candidate_selection_check_requires_selection_reason(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="source_selection_model_5",
                check_type="candidate_selection_check",
                required_candidate_id="candidate_005",
                required_model_rank=5,
                require_selection_reason=True,
                source_selection_manifest_path="outputs.source_selection",
                source_selection_path="source_selection.json",
                weight=1.0,
            ),
        ],
    )
    _write_source_selection(
        tmp_path,
        candidate_id="candidate_005",
        model_rank=5,
        include_reason=False,
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"source_selection": "source_selection.json"},
        },
        metrics={},
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    failed = score.deterministic_checks[0]
    assert failed.passed is False
    assert "selection reason missing" in failed.message


def test_trajectory_rescan_uses_manifest_outputs(tmp_path: Path, monkeypatch):
    observed = {}

    def fake_rescan(traj_path: Path, top_path: Path):
        observed["traj_path"] = traj_path
        observed["top_path"] = top_path
        return 8, False, "fake loaded 8 frames"

    monkeypatch.setattr(scoring.integrity, "rescan_trajectory_for_nan", fake_rescan)
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(
            check_id="traj",
            check_type="trajectory_rescan",
            trajectory_path="../work/default_traj.dcd",
            topology_path="../work/default_topology.pdb",
            trajectory_manifest_path="outputs.trajectories.0",
            topology_manifest_path="outputs.topology.0",
            require_min_frames=4,
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "trajectories": ["mdcrow/traj.dcd"],
                "topology": ["mdcrow/topology.pdb"],
            },
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True
    assert observed["traj_path"] == (tmp_path / "mdcrow/traj.dcd").resolve()
    assert observed["top_path"] == (tmp_path / "mdcrow/topology.pdb").resolve()


def test_topology_solvent_rescan_requires_explicit_water(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(
            check_id="explicit_water",
            check_type="topology_solvent_rescan",
            topology_manifest_path="outputs.topology.0",
            water_residue_names=["HOH", "WAT"],
            min_water_residues=2,
            weight=1.0,
        )],
    )
    (tmp_path / "system.topology.pdb").write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "HETATM    2  O   HOH B   2       1.000   0.000   0.000  1.00  0.00           O\n"
        "HETATM    3  O   HOH B   3       2.000   0.000   0.000  1.00  0.00           O\n"
        "END\n"
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"topology": ["system.topology.pdb"]},
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True
    assert "found 2 water residues" in score.deterministic_checks[0].message


def test_topology_solvent_rescan_fails_for_implicit_topology(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(
            check_id="explicit_water",
            check_type="topology_solvent_rescan",
            topology_manifest_path="outputs.topology.0",
            min_water_residues=1,
            weight=1.0,
        )],
    )
    (tmp_path / "system.topology.pdb").write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "END\n"
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"topology": ["system.topology.pdb"]},
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is False
    assert "require >= 1" in score.deterministic_checks[0].message


def test_openmm_topology_and_minimization_checks_pass(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="bundle",
                check_type="topology_artifact_bundle",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="load",
                check_type="openmm_system_load",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="energy",
                check_type="openmm_energy_rescan",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="min_report",
                check_type="minimization_report_check",
                minimization_report_manifest_path="outputs.minimization_report",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path)

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "passed"
    assert all(result.passed for result in score.deterministic_checks)


def test_completed_prep_submission_requires_minimized_structure_output(tmp_path: Path):
    task = _make_task(primary="preparation")
    task.required_outputs = ["manifest.json", "minimized_structure.pdb"]
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"minimized_structure": "missing_minimized_structure.pdb"},
        },
    )

    validation_result = validation.validate_submission(task_file, tmp_path)
    score = scoring.score_submission(task, tmp_path)

    assert validation_result["success"] is False
    assert any(
        "outputs.minimized_structure points to missing file" in error
        for error in validation_result["errors"]
    )
    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert any(
        result.check_type == "minimized_structure_required"
        and result.passed is False
        for result in score.deterministic_checks
    )


def test_non_openmm_backend_cannot_skip_openmm_verification(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="load",
                check_type="openmm_system_load",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="energy",
                check_type="openmm_energy_rescan",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path, broken="system")
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    metrics["topology"]["backend"] = "gromacs"
    (tmp_path / "metrics.json").write_text(json.dumps(metrics))

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert all(not result.passed for result in score.deterministic_checks)
    assert all(
        "loadable OpenMM topology bundle" in result.message
        for result in score.deterministic_checks
    )


def test_unspecified_backend_without_openmm_bundle_fails_openmm_verification(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="load",
                check_type="openmm_system_load",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
        ],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"topology": ["topology/placeholder.top"]},
        },
        metrics={"topology": {"build_success": True}},
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert "loadable OpenMM topology bundle" in score.deterministic_checks[0].message


def test_p01_corrected_openmm_submission_scores_passed(tmp_path: Path):
    task_file = DATASET_DIR / "tasks" / "P01_prep_simple_monomer_t4l" / "task.json"
    task = validation.load_task(task_file)
    _write_openmm_bundle(tmp_path, include_water=True)

    prepared = (
        (tmp_path / "topology" / "topology.pdb").read_text()
        + "REMARK scorer fixture padding for realistic artifact size\n"
    )
    (tmp_path / "prepared_structure.pdb").write_text(prepared)
    (tmp_path / "minimized_structure.pdb").write_text(prepared)

    report = {
        "minimization": {
            "attempted": True,
            "completed": True,
            "energy_initial_kj_mol": 0.0,
            "energy_final_kj_mol": 0.0,
            "energy_is_finite": True,
            "positions_are_finite": True,
            "atom_count_preserved": True,
            "backend": "openmm",
        }
    }
    (tmp_path / "minimization_report.json").write_text(json.dumps(report))
    manifest = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
            "prepared_structure": "prepared_structure.pdb",
            "minimized_structure": "minimized_structure.pdb",
            "minimization_report": "minimization_report.json",
            "topology": [
                "topology/system.xml",
                "topology/topology.pdb",
                "topology/state.xml",
            ],
        },
    }
    _write_submission(
        tmp_path,
        manifest=manifest,
        metrics={
            "preparation": {
                "source_pdb_id": "2LZM",
                "solvent_model": "explicit",
                "topology_ready": True,
            },
            "topology": {
                "backend": "openmm",
                "build_success": True,
                "forcefield": "synthetic-fixture",
                "water_model": "none",
                "solvent_model": "explicit",
            },
            "minimization": report["minimization"],
        },
        provenance={
            "schema_version": "1.0",
            "task_id": task.task_id,
            "command_log": _prep_command_log(),
        },
        evidence={
            "schema_version": "1.0",
            "task_id": task.task_id,
            "summary": (
                "Corrected P01 fixture with current manifest status, topology "
                "artifact list, preparation metrics, topology artifacts, and "
                "finite minimization evidence. This long summary keeps the "
                "artifact realistic enough for integrity byte-floor checks."
            ),
            "evidence": {"topology": "OpenMM artifact triple loaded."},
        },
    )
    provenance = json.loads((tmp_path / "provenance.json").read_text())
    provenance["raw_outputs"] = _raw_output_hashes(
        tmp_path,
        [
            "manifest.json",
            "metrics.json",
            "evidence_report.json",
            "prepared_structure.pdb",
            "minimized_structure.pdb",
            "minimization_report.json",
            "topology/system.xml",
            "topology/topology.pdb",
            "topology/state.xml",
        ],
    )
    (tmp_path / "provenance.json").write_text(json.dumps(provenance))
    (tmp_path.parent / "harness_execution.json").write_text(
        json.dumps({"records": _prep_command_log()})
    )

    validation_result = validation.validate_submission(task_file, tmp_path)
    assert validation_result["success"], validation_result

    score = scoring.score_submission(task, tmp_path)
    assert score.status == "passed"
    assert score.weighted_total == pytest.approx(1.0)


def test_broken_openmm_system_is_critical_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="bundle",
                check_type="topology_artifact_bundle",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="load",
                check_type="openmm_system_load",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path, broken="system")

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert any(
        result.check_id == "load" and not result.passed
        for result in score.deterministic_checks
    )


def test_broken_openmm_state_is_critical_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="load",
                check_type="openmm_system_load",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path, broken="state")

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0


def test_openmm_energy_rescan_rejects_nan_positions(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="energy",
                check_type="openmm_energy_rescan",
                topology_manifest_path="outputs.topology",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path, broken="nan_positions")

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0


def test_openmm_energy_rescan_rejects_huge_finite_energy(tmp_path: Path):
    _write_openmm_bundle(tmp_path, huge_energy=True)
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "topology": [
                    "topology/system.xml",
                    "topology/topology.pdb",
                    "topology/state.xml",
                ],
            },
        },
        metrics={"topology": {"backend": "openmm"}},
    )
    task = _make_task(
        primary="execution",
        det_checks=[
            DeterministicCheck(
                check_id="energy",
                check_type="openmm_energy_rescan",
                weight=1.0,
            )
        ],
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert "physically implausible" in score.deterministic_checks[0].message


def test_incomplete_minimization_report_is_critical_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="min_report",
                check_type="minimization_report_check",
                minimization_report_manifest_path="outputs.minimization_report",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path, completed=False)

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0


def test_nonfinite_minimization_report_energy_is_critical_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="min_report",
                check_type="minimization_report_check",
                minimization_report_manifest_path="outputs.minimization_report",
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path, finite_report_energy=False)

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0


def test_huge_finite_minimization_report_energy_is_critical_failure(tmp_path: Path):
    _write_minimization_submission(tmp_path)
    report_path = tmp_path / "minimization_report.json"
    report = json.loads(report_path.read_text())
    report["minimization"]["energy_final_kj_mol"] = 1.0e20
    report_path.write_text(json.dumps(report))
    metrics_path = tmp_path / "metrics.json"
    metrics = json.loads(metrics_path.read_text())
    metrics["minimization"]["energy_final_kj_mol"] = 1.0e20
    metrics_path.write_text(json.dumps(metrics))
    task = _make_task(
        primary="execution",
        det_checks=[
            DeterministicCheck(
                check_id="min",
                check_type="minimization_report_check",
                minimization_report_manifest_path="outputs.minimization_report",
                weight=1.0,
            )
        ],
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0


def test_minimized_structure_component_loss_is_critical_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="min_ap5",
                check_type="minimized_structure_component_rescan",
                minimized_structure_manifest_path="outputs.minimized_structure",
                min_residue_counts={"AP5": 1},
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(
        tmp_path,
        minimized_structure=(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        ),
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0


def test_prepared_structure_component_loss_is_critical_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="prepared_ap5",
                check_type="structure_component_rescan",
                structure_manifest_path="outputs.prepared_structure",
                min_residue_counts={"AP5": 1},
                weight=1.0,
            ),
        ],
    )
    _write_minimization_submission(tmp_path)
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["outputs"]["prepared_structure"] = "prepared_structure.pdb"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert score.deterministic_checks[0].check_type == "structure_component_rescan"
    assert score.deterministic_checks[0].passed is False


def test_topology_component_rescan_requires_component_in_openmm_topology(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="topology_ap5",
                check_type="topology_component_rescan",
                topology_manifest_path="outputs.topology",
                min_residue_counts={"AP5": 1},
                weight=1.0,
            ),
        ],
    )
    _write_openmm_bundle(tmp_path, extra_residues=[("AP5", 1)])
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "topology": [
                    "topology/system.xml",
                    "topology/topology.pdb",
                    "topology/state.xml",
                ],
            },
        },
        metrics={},
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "passed"
    assert score.weighted_total == 1.0
    assert score.deterministic_checks[0].passed is True


def test_topology_component_loss_is_physical_hard_failure(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="topology_ap5",
                check_type="topology_component_rescan",
                topology_manifest_path="outputs.topology",
                min_residue_counts={"AP5": 1},
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="reported_ok",
                check_type="json_equals",
                json_path="preparation.reported_ok",
                equals=True,
                weight=1.0,
            ),
        ],
    )
    _write_openmm_bundle(tmp_path)
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "topology": [
                    "topology/system.xml",
                    "topology/topology.pdb",
                    "topology/state.xml",
                ],
            },
        },
        metrics={"preparation": {"reported_ok": True}},
    )

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert score.deterministic_checks[0].passed is False
    assert "AP5: observed 0 < min 1" in score.deterministic_checks[0].message


def test_pdb_residue_state_check_requires_variant_and_hydrogen(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="glu11_glh",
            check_type="pdb_residue_state",
            structure_manifest_path="outputs.prepared_structure",
            residue_chain="A",
            residue_number="11",
            required_residue_name="GLH",
            required_atom_names=["HE2"],
            weight=1.0,
        )],
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  N   GLH A  11       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  GLH A  11       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  HE2 GLH A  11       2.000   0.000   0.000  1.00  0.00           H\n"
        "END\n"
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  N   GLU A  11       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  GLU A  11       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    failed_score = scoring.score_submission(task, tmp_path)
    failed = failed_score.deterministic_checks[0]
    assert failed.passed is False
    assert "do not include GLH" in failed.message


def test_structure_component_rescan_counts_required_and_forbidden_residues(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="components",
            check_type="structure_component_rescan",
            structure_manifest_path="outputs.prepared_structure",
            min_residue_counts={"AP5": 1},
            max_residue_counts={"SO4": 0},
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  C1  AP5 B   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  S   SO4 B   2       1.000   0.000   0.000  1.00  0.00           S\n"
        "END\n"
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "AP5" in failed.message
    assert "SO4" in failed.message


def test_unexpected_residue_rescan_allows_requested_nonstandard_only(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_extra_nonstandard",
            check_type="unexpected_residue_rescan",
            structure_manifest_path="outputs.prepared_structure",
            allowed_nonstandard_residue_names=["AP5"],
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  C1  AP5 B   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    3  O   HOH C   3       2.000   0.000   0.000  1.00  0.00           O\n"
        "HETATM    4 NA   NA  D   4       3.000   0.000   0.000  1.00  0.00          NA\n"
        "END\n"
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  C1  AP5 B   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    3  S   SO4 C   3       2.000   0.000   0.000  1.00  0.00           S\n"
        "END\n"
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "SO4" in failed.message
    assert "AP5" not in failed.message


def test_pdb_no_deuterium_atoms_check(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_d",
            check_type="pdb_no_deuterium_atoms",
            structure_manifest_path="outputs.prepared_structure",
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  N   ARG A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "END\n"
    )
    passed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert passed.passed is True

    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  N   ARG A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  D1  ARG A   1       0.100   0.000   0.000  1.00  0.00           D\n"
        "END\n"
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "deuterium" in failed.message
    assert "D1" in failed.message


def test_pdb_no_deuterium_atoms_does_not_flag_deoxy_atom_names(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_d",
            check_type="pdb_no_deuterium_atoms",
            structure_manifest_path="outputs.prepared_structure",
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  D5'  DG A   1       0.000   0.000   0.000  1.00  0.00            \n"
        "ATOM      2  D3'  DG A   1       1.000   0.000   0.000  1.00  0.00            \n"
        "END\n"
    )

    passed = scoring.score_submission(task, tmp_path).deterministic_checks[0]

    assert passed.passed is True


def test_prepared_deuterium_check_failure_is_critical(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_d",
            check_type="pdb_no_deuterium_atoms",
            structure_manifest_path="outputs.prepared_structure",
            weight=1.0,
        )],
    )
    _write_minimization_submission(tmp_path)
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  N   ARG A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  D1  ARG A   1       0.100   0.000   0.000  1.00  0.00           D\n"
        "END\n"
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["outputs"]["prepared_structure"] = "prepared_structure.pdb"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    score = scoring.score_submission(task, tmp_path)

    assert score.status == "failed"
    assert score.weighted_total == 0.0
    assert score.deterministic_checks[0].passed is False


def test_structure_component_rescan_counts_residue_aliases(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="components",
            check_type="structure_component_rescan",
            structure_manifest_path="outputs.prepared_structure",
            min_residue_counts={"POPC": 2, "K": 1, "CL": 1},
            residue_aliases={
                "POPC": ["PC"],
                "K": ["K+"],
                "CL": ["CL-", "Cl-"],
            },
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "HETATM    1  C1   PC A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  C1   PC A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    3  K+  K+  B   3       2.000   0.000   0.000  1.00  0.00           K\n"
        "HETATM    4 Cl-  Cl- B   4       3.000   0.000   0.000  1.00  0.00          Cl\n"
        "END\n"
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True


def test_p14_glycam_linked_asn_is_allowed_without_counting_as_nag(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[
            DeterministicCheck(
                check_id="nag_retained",
                check_type="structure_component_rescan",
                structure_manifest_path="outputs.prepared_structure",
                min_residue_counts={"NAG": 1},
                residue_aliases={"NAG": ["0YB", "4YA", "4YB"]},
                weight=1.0,
            ),
            DeterministicCheck(
                check_id="no_extra",
                check_type="unexpected_residue_rescan",
                structure_manifest_path="outputs.prepared_structure",
                allowed_nonstandard_residue_names=["NAG", "NLN"],
                residue_aliases={"NAG": ["0YB", "4YA", "4YB"]},
                weight=1.0,
            ),
        ],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  NLN A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  C1  0YB B   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )

    score = scoring.score_submission(task, tmp_path)

    assert [check.passed for check in score.deterministic_checks] == [True, True]

    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  NLN A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False


def test_json_allowed_values_accepts_lipid_ratio_synonym(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="lipid_ratio",
            check_type="json_allowed_values",
            json_path="preparation.lipid_ratio",
            allowed_values=[
                "POPC:POPE:CHL1=2:1:1",
                "PC:PE:CHL=2:1:1",
            ],
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        metrics={"preparation": {"lipid_ratio": "PC:PE:CHL=2:1:1"}},
    )
    passed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert passed.passed is True

    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        metrics={"preparation": {"lipid_ratio": "PC:PE:CHL=1:1:1"}},
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "PC:PE:CHL=1:1:1" in failed.message
    assert "PC:PE:CHL=2:1:1" in failed.message


def _lipid_pdb_line(serial: int, atom: str, resname: str, chain: str,
                    resseq: int) -> str:
    """A PDB ATOM record with the residue name in columns 18-21 (index 17:21).

    Mirrors the long-residue-name placement MDClaw writes for 4-character
    CHARMM lipid names so the scorer's fixed-column reader recovers them.
    """
    return (
        f"HETATM{serial:>5} {atom:<4}{'':1}{resname[:4]:<4}{chain:1}"
        f"{resseq:>4}{'':1}   "
        f"{0.0:>8.3f}{0.0:>8.3f}{0.0:>8.3f}{1.0:>6.2f}{0.0:>6.2f}\n"
    )


def _lipid_structure(residues: list[tuple[str, int]], atoms_per_res: int,
                     chain: str = "B", start_resseq: int = 1) -> str:
    """Build a PDB string. ``residues`` = list of (resname, count)."""
    lines: list[str] = []
    serial = 1
    resseq = start_resseq
    for resname, count in residues:
        for _ in range(count):
            for a in range(atoms_per_res):
                lines.append(
                    _lipid_pdb_line(serial, f"C{a + 1}", resname, chain, resseq)
                )
                serial += 1
            resseq += 1
    lines.append("END\n")
    return "".join(lines)


def test_p18_lipid_aliases_count_species_but_not_tail_fragments(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="lipids",
            check_type="structure_component_rescan",
            structure_manifest_path="outputs.prepared_structure",
            min_residue_atom_count=20,
            min_residue_counts={"POPC": 2, "POPE": 1, "CHL1": 1},
            residue_aliases={
                "POPC": ["PC", "OPC"],
                "POPE": ["PE", "OPE"],
                "CHL1": ["CHL", "CHOL", "HL1"],
            },
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
    )
    # Whole lipids (with the last-3-character truncations some agents emit) plus
    # OPC water that must NOT be miscounted as POPC because it is too small.
    (tmp_path / "prepared_structure.pdb").write_text(
        _lipid_structure([("OPC", 2), ("OPE", 1), ("HL1", 1)], atoms_per_res=25)
        + _lipid_structure([("OPC", 50)], atoms_per_res=4,
                           chain="W", start_resseq=100)
    )
    passed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert passed.passed is True, passed.message

    # Acyl-tail fragment residues (PA/OL) must not be counted as whole lipids.
    (tmp_path / "prepared_structure.pdb").write_text(
        _lipid_structure([("PA", 2), ("OL", 2)], atoms_per_res=25)
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "POPC" in failed.message
    assert "POPE" in failed.message


def test_p18_unexpected_residue_rescan_ignores_lipid21_tail_fragments(
    tmp_path: Path,
):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_extra_nonstandard",
            check_type="unexpected_residue_rescan",
            structure_manifest_path="outputs.topology",
            structure_path="topology/topology.pdb",
            allowed_nonstandard_residue_names=["POPC", "POPE", "CHL1"],
            ignored_residue_names=["PA", "OL"],
            min_residue_atom_count=2,
            residue_aliases={
                "POPC": ["PC", "OPC"],
                "POPE": ["PE", "OPE"],
                "CHL1": ["CHL", "CHOL", "HL1"],
            },
            weight=1.0,
        )],
    )
    topology_dir = tmp_path / "topology"
    topology_dir.mkdir()
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"topology": ["topology/topology.pdb"]},
        },
    )

    (topology_dir / "topology.pdb").write_text(
        _lipid_structure([("PC", 1), ("PE", 1), ("CHL", 1), ("PA", 1), ("OL", 1)],
                         atoms_per_res=25)
    )
    passed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert passed.passed is True, passed.message

    (topology_dir / "topology.pdb").write_text(
        _lipid_structure([("PC", 1), ("PA", 1), ("OL", 1), ("BEN", 1)],
                         atoms_per_res=25)
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "BEN" in failed.message
    assert "PA" not in failed.message
    assert "OL" not in failed.message


def test_artifact_provenance_text_checks_provenance_and_evidence(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="ion_triage_documented",
            check_type="artifact_provenance_text",
            required_text_groups=[
                ["crystallographic"],
                ["K+", "potassium"],
                ["water", "HOH"],
                ["excluded", "removed"],
            ],
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        provenance={
            "decisions": [
                "Retained crystallographic K+ ions.",
                "Excluded deposited water molecules during source triage.",
            ],
        },
        evidence={"summary": "Prepared explicit solvent system after source triage."},
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed"},
        provenance={"decisions": ["Retained ions but did not describe source triage."]},
        evidence={"summary": "No relevant details."},
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "required provenance/evidence text" in failed.message


def test_assembly_identity_check_matches_structure_and_chain_map(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="assembly",
            check_type="assembly_identity_check",
            structure_manifest_path="outputs.prepared_structure",
            assembly_id_json_path="preparation.assembly_id",
            required_assembly_id="1",
            chain_identity_json_path="preparation.assembly_chain_identity_map",
            exact_chain_count=4,
            min_mapping_entries=4,
            min_distinct_output_chains=4,
            required_mapping_fields=[
                "source_pdb_id",
                "assembly_id",
                "source_auth_asym_id",
                "source_label_asym_id|source_subchain_id",
                "operator_id",
                "output_chain_id",
                "naming_policy",
            ],
            required_operator_ids=["1", "2", "3", "4"],
            require_output_chains_in_structure=True,
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"prepared_structure": "prepared_structure.pdb"},
        },
        metrics={
            "preparation": {
                "assembly_id": "1",
                "assembly_chain_identity_map": [
                    {
                        "source_pdb_id": "1STP",
                        "assembly_id": "1",
                        "source_auth_asym_id": "A",
                        "source_label_asym_id": "A",
                        "operator_id": str(index),
                        "output_chain_id": chain_id,
                        "naming_policy": "short",
                    }
                    for index, chain_id in enumerate(["A", "B", "C", "D"], start=1)
                ],
            },
        },
    )
    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  GLY A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  GLY B   1       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY C   1       2.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      4  CA  GLY D   1       3.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    5  C1  BTN E 300       4.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    6  C1  BTN F 300       5.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    7  C1  BTN G 300       6.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    8  C1  BTN H 300       7.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    (tmp_path / "prepared_structure.pdb").write_text(
        "ATOM      1  CA  GLY A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    failed = scoring.score_submission(task, tmp_path).deterministic_checks[0]
    assert failed.passed is False
    assert "mapped output chains absent from structure: ['B', 'C', 'D']" in failed.message


def test_ground_truth_check_uses_separate_truth_file(tmp_path: Path):
    truth_dir = tmp_path / "task" / "truth"
    truth_dir.mkdir(parents=True)
    (truth_dir / "experimental_truth.json").write_text(
        json.dumps({"expected_direction": "destabilizing"})
    )
    task = _make_task(
        primary="scientific_answer",
        gt_checks=[GroundTruthCheck(
            check_id="dir", truth_path="expected_direction",
            submission_path="effect.direction", weight=1.0,
        )],
    )
    sub_dir = tmp_path / "submission"
    _write_submission(
        sub_dir, manifest={"task_id": "t", "status": "completed"},
        evidence={"effect": {"direction": "destabilizing"}},
    )
    score = scoring.score_submission(task, sub_dir, task_dir=tmp_path / "task")
    gt = score.ground_truth_checks[0]
    assert gt.passed is True

    # Wrong answer → ground_truth fails and weighted_total drops.
    sub_dir2 = tmp_path / "submission_wrong"
    _write_submission(
        sub_dir2, manifest={"task_id": "t", "status": "completed"},
        evidence={"effect": {"direction": "stabilizing"}},
    )
    score_wrong = scoring.score_submission(
        task, sub_dir2, task_dir=tmp_path / "task")
    assert score_wrong.ground_truth_checks[0].passed is False
    assert score_wrong.weighted_total == 0.0
