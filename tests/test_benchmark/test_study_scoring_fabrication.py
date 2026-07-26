"""StudyBench anti-gaming / fabrication coverage.

These tests lock down the Tier-1 hard-fail gates that bind a scientific-answer
submission to real, loadable, correctly-built comparative MD. They are the
StudyBench analogue of ``test_scoring_fabrication.py`` for the prep suite: an
honest fixture passes the conjunctive verdict, while invalid raw MD, unsupported
reasoning, a truth-only guess, and stale/missing judge evidence cannot pass.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mdclaw.benchmark import cli, scoring, validation
from tests.test_benchmark import _fake_study_submissions as fakes


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
COMPARATIVE_TASKS = [
    "S01_stability_t4l_l99a",
    "S02_ppi_hotspot_barnase_d39a",
    "S03_stability_nuclease_h124l",
    "S04_affinity_t4l_l99a_alkylbenzene",
]


def _make(tmp_path: Path, task_id: str, mode: str = "honest") -> Path:
    sub_dir = tmp_path / task_id / "submission"
    fakes.make_study_submission(sub_dir, run_id="fab", mode=mode, task_id=task_id)
    return sub_dir


def _score(
    task_id: str,
    sub_dir: Path,
    *,
    judge_payload: dict | None = None,
    include_judge: bool = True,
) -> dict:
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    judge_file: Path | None = None
    if include_judge:
        payload = judge_payload or fakes.grounded_judge_payload(sub_dir, task_id)
        judge_file = sub_dir.parent / "judge.json"
        judge_file.write_text(json.dumps(payload))
    scored = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(sub_dir),
        run_id="fab",
        output_file=str(sub_dir.parent / "score.json"),
        llm_judge_file=str(judge_file) if judge_file else None,
    )
    assert scored["success"], scored
    return scored["score"]


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_honest_comparative_submission_passes(tmp_path: Path, task_id: str):
    score = _score(task_id, _make(tmp_path, task_id))
    assert score["weighted_total"] == pytest.approx(1.0)
    assert score["status"] == "passed"
    assert score["study_verdict"]["grounded_correct"] is True
    assert score["study_verdict"]["valid_md"] is True
    assert score["study_verdict"]["evidence_verified"] is True
    assert score["study_verdict"]["reasoning_grounded"] is True
    assert score["study_verdict"]["truth_correct"] is True
    assert not score["integrity_warnings"]


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_garbage_trajectory_is_hard_failed(tmp_path: Path, task_id: str):
    """A DCD-magic header over junk bytes is not loadable MD -> rescan clamps."""
    sub_dir = _make(tmp_path, task_id)
    (sub_dir / "trajectories/wt.dcd").write_bytes(
        b"\x54\x00\x00\x00CORD" + b"not real md frames\n" * 64
    )
    score = _score(task_id, sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["status"] == "failed"


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_missing_mutation_is_hard_failed(tmp_path: Path, task_id: str):
    """Copying the WT system over the mutant means no real mutation was built."""
    sub_dir = _make(tmp_path, task_id)
    shutil.copy(sub_dir / "topology/wt.pdb", sub_dir / "topology/mutant.pdb")
    shutil.copy(sub_dir / "trajectories/wt.dcd", sub_dir / "trajectories/mutant.dcd")
    score = _score(task_id, sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["status"] == "failed"
    failed = {c["check_id"] for c in score["deterministic_checks"] if not c["passed"]}
    assert any(cid.startswith("paired") for cid in failed), failed


def test_every_declared_replica_topology_must_have_the_mutation(tmp_path: Path):
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id)
    index_path = sub_dir / "study_index.json"
    study_index = json.loads(index_path.read_text())
    variant = next(
        system for system in study_index["systems"] if system["role"] == "variant"
    )
    variant["replicas"].append(
        {
            "replica_id": "variant_bad_topology",
            "topology": "topology/wt.pdb",
            "trajectory": "trajectories/mutant.dcd",
        }
    )
    index_path.write_text(json.dumps(study_index))
    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    check = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        check,
        sub_dir,
        manifest,
    )

    assert passed is False
    assert score == 0.0
    assert "every declared replica topology" in message


def test_s01_mutation_site_is_pinned_to_resseq_99(tmp_path: Path):
    """A chemically correct LEU->ALA swap at 99 must not satisfy an expected
    site of 98; residue identity alone is insufficient."""
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id)
    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    canonical = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    wrong_site = canonical.model_copy(update={"mutation_resseq": 98})
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        wrong_site,
        sub_dir,
        manifest,
    )

    assert passed is False
    assert score == 0.0
    assert "resSeq 99->99, expected 98" in message


def test_s01_residue_rename_with_leucine_side_chain_is_rejected(tmp_path: Path):
    """Renaming LEU 99 to ALA in PDB text must not count as building L99A."""
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id)
    reference_path = sub_dir / "topology/wt.pdb"
    variant_path = sub_dir / "topology/mutant.pdb"
    renamed_lines: list[str] = []
    retained_atom_names: set[str] = set()
    for line in reference_path.read_text().splitlines(keepends=True):
        if (
            line.startswith(("ATOM  ", "HETATM"))
            and line[22:26].strip() == "99"
            and line[17:20] == "LEU"
        ):
            retained_atom_names.add(line[12:16].strip())
            line = f"{line[:17]}ALA{line[20:]}"
        renamed_lines.append(line)
    assert {"CG", "CD1", "CD2"} <= retained_atom_names
    variant_path.write_text("".join(renamed_lines))

    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    check = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        check,
        sub_dir,
        manifest,
    )

    assert passed is False
    assert score == 0.0
    assert "ALA" in message
    assert "heavy" in message.lower()


@pytest.mark.parametrize(
    ("task_id", "res_seq", "canonical", "alias", "expected_substitution"),
    [
        ("S03_stability_nuclease_h124l", 124, "HIS", "HID", "HIS[124]->LEU[124]"),
        ("S03_stability_nuclease_h124l", 124, "HIS", "HIE", "HIS[124]->LEU[124]"),
        ("S03_stability_nuclease_h124l", 124, "HIS", "HIP", "HIS[124]->LEU[124]"),
        ("S02_ppi_hotspot_barnase_d39a", 39, "ASP", "ASH", "ASP[39]->ALA[39]"),
    ],
)
def test_mutation_gate_accepts_protonation_state_residue_aliases(
    tmp_path: Path,
    task_id: str,
    res_seq: int,
    canonical: str,
    alias: str,
    expected_substitution: str,
):
    sub_dir = _make(tmp_path, task_id)
    reference_path = sub_dir / "topology/wt.pdb"
    renamed_lines: list[str] = []
    changed_atoms = 0
    for line in reference_path.read_text().splitlines(keepends=True):
        if (
            line.startswith(("ATOM  ", "HETATM"))
            and line[22:26].strip() == str(res_seq)
            and line[17:20] == canonical
        ):
            line = f"{line[:17]}{alias}{line[20:]}"
            changed_atoms += 1
        renamed_lines.append(line)
    assert changed_atoms > 0
    reference_path.write_text("".join(renamed_lines))

    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    check = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        check,
        sub_dir,
        manifest,
    )

    assert passed is True, message
    assert score == 1.0
    assert expected_substitution in message


def test_s04_honest_ligand_swap_satisfies_heavy_atom_contract(tmp_path: Path):
    task_id = "S04_affinity_t4l_l99a_alkylbenzene"
    sub_dir = _make(tmp_path, task_id)
    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    check = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        check,
        sub_dir,
        manifest,
    )

    assert check.reference_changed_residue_heavy_atom_count == 6
    assert check.variant_changed_residue_heavy_atom_count == 10
    assert check.reference_changed_residue_element_counts == {"C": 6}
    assert check.variant_changed_residue_element_counts == {"C": 10}
    assert passed is True
    assert score == 1.0
    assert "BNZ[201]->NBB[201]" in message


def test_s04_ligand_heavy_atom_count_mismatch_is_rejected(tmp_path: Path):
    task_id = "S04_affinity_t4l_l99a_alkylbenzene"
    sub_dir = _make(tmp_path, task_id)
    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    canonical = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    wrong_count = canonical.model_copy(
        update={"variant_changed_residue_heavy_atom_count": 9}
    )
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        wrong_count,
        sub_dir,
        manifest,
    )

    assert passed is False
    assert score == 0.0
    assert "variant changed residue has 10 heavy atoms, expected 9" in message


def test_s04_ligand_element_composition_mismatch_is_rejected(tmp_path: Path):
    task_id = "S04_affinity_t4l_l99a_alkylbenzene"
    sub_dir = _make(tmp_path, task_id)
    task = validation.load_task(DATASET_DIR / "tasks" / task_id / "task.json")
    canonical = next(
        item
        for item in task.scoring.deterministic_checks
        if item.check_type == "paired_mutation_topology"
    )
    wrong_composition = canonical.model_copy(
        update={"variant_changed_residue_element_counts": {"N": 10}}
    )
    manifest = json.loads((sub_dir / "manifest.json").read_text())

    passed, score, message = scoring._check_paired_mutation_topology(
        wrong_composition,
        sub_dir,
        manifest,
    )

    assert passed is False
    assert score == 0.0
    assert "heavy-element composition" in message


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_real_md_wrong_direction_scores_partial_answer(tmp_path: Path, task_id: str):
    """Real correct-mutation MD whose observable supports the literature
    direction, but the agent claims the opposite. The truth-blind judge records
    a contradiction and the official conjunctive outcome stays zero."""
    sub_dir = _make(tmp_path, task_id)
    evidence = json.loads((sub_dir / "evidence_report.json").read_text())
    truth = json.loads(
        (DATASET_DIR / "tasks" / task_id / "truth" / "experimental_truth.json").read_text()
    )
    allowed = {
        "S01_stability_t4l_l99a": ["destabilizing", "stabilizing", "neutral"],
        "S02_ppi_hotspot_barnase_d39a": [
            "weakened_binding", "strengthened_binding", "neutral",
        ],
        "S03_stability_nuclease_h124l": [
            "destabilizing", "stabilizing", "neutral",
        ],
        "S04_affinity_t4l_l99a_alkylbenzene": [
            "stronger_binding", "weaker_binding", "similar",
        ],
    }[task_id]
    wrong = next(v for v in allowed if v != truth["expected_direction"])
    evidence["conclusion"]["direction"] = wrong
    evidence["effect"]["direction"] = wrong
    (sub_dir / "evidence_report.json").write_text(json.dumps(evidence))
    judge = fakes.grounded_judge_payload(
        sub_dir,
        task_id,
        support_verdict="contradicted",
        logical_grounding_supported=False,
    )
    score = _score(task_id, sub_dir, judge_payload=judge)
    assert score["weighted_total"] == 0.0
    assert score["study_verdict"]["truth_correct"] is False
    assert score["study_verdict"]["reasoning_grounded"] is False
    assert score["status"] == "partial"


def test_agent_selected_prior_knowledge_is_not_restricted_to_private_pool(
    tmp_path: Path,
):
    sub_dir = _make(tmp_path, "S01_stability_t4l_l99a")
    evidence = json.loads((sub_dir / "evidence_report.json").read_text())
    evidence["prior_knowledge"]["citations"] = [
        {"source": "agent-selected-public-source", "doi": "10.1000/example"}
    ]
    (sub_dir / "evidence_report.json").write_text(json.dumps(evidence))
    score = _score("S01_stability_t4l_l99a", sub_dir)
    assert score["weighted_total"] == 1.0
    assert not score["integrity_warnings"]


def test_undersized_evidence_report_is_rejected(tmp_path: Path):
    sub_dir = _make(tmp_path, "S01_stability_t4l_l99a")
    (sub_dir / "evidence_report.json").write_text('{"effect": {"direction": "x"}}')
    score = _score("S01_stability_t4l_l99a", sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["integrity_warnings"]


def test_llm_judge_rubric_scores_fill_secondary_axis(tmp_path: Path):
    """Regression: the judge reports rubric-keyed scores; the scorer must
    aggregate them into the task's secondary axis (previously they were read by
    axis name and silently dropped, zeroing the qualitative dimension)."""
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id)
    judge_file = sub_dir.parent / "judge.json"
    judge_payload = fakes.grounded_judge_payload(sub_dir, task_id)
    judge_payload["scores"].update(
        confidence_calibration=0.8,
        overclaim_detection=0.6,
    )
    judge_file.write_text(json.dumps(judge_payload))
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    scored = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(sub_dir),
        run_id="fab",
        output_file=str(sub_dir.parent / "score.json"),
        llm_judge_file=str(judge_file),
    )
    assert scored["success"], scored
    score = scored["score"]
    # The axis remains a useful diagnostic, but cannot compensate for a failed
    # conjunctive verdict. Here every required condition is met, so pass is 1.
    assert score["scores"]["evidence_communication"] == pytest.approx(0.85)
    assert score["weighted_total"] == 1.0


def test_truth_only_guess_cannot_pass(tmp_path: Path):
    """A true conclusion cannot pass when the judge finds no logical support."""
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id, mode="honest")
    judge = fakes.grounded_judge_payload(
        sub_dir,
        task_id,
        support_verdict="inconclusive",
        logical_grounding_supported=False,
    )
    score = _score(task_id, sub_dir, judge_payload=judge)
    verdict = score["study_verdict"]
    assert verdict["truth_correct"] is True
    assert verdict["evidence_verified"] is True
    assert verdict["reasoning_grounded"] is False
    assert verdict["grounded_correct"] is False
    assert score["weighted_total"] == 0.0


def test_md_supported_but_truth_wrong_cannot_pass(tmp_path: Path):
    """Logically following one's own MD is necessary, but not sufficient."""
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id, mode="faithful_wrong")
    judge = fakes.grounded_judge_payload(sub_dir, task_id)
    score = _score(task_id, sub_dir, judge_payload=judge)
    verdict = score["study_verdict"]
    assert verdict["valid_md"] is True
    assert verdict["evidence_verified"] is True
    assert verdict["reasoning_grounded"] is True
    assert verdict["truth_correct"] is False
    assert verdict["grounded_correct"] is False
    assert score["weighted_total"] == 0.0


def test_honest_inconclusive_result_cannot_be_counted_as_correct(tmp_path: Path):
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id, mode="inconclusive")
    judge = fakes.grounded_judge_payload(
        sub_dir,
        task_id,
        support_verdict="inconclusive",
        logical_grounding_supported=False,
    )
    score = _score(task_id, sub_dir, judge_payload=judge)
    verdict = score["study_verdict"]
    assert verdict["evidence_status"] == "inconclusive"
    assert verdict["reasoning_grounded"] is False
    assert verdict["grounded_correct"] is False
    assert score["weighted_total"] == 0.0


@pytest.mark.parametrize("failure", ["hash_mismatch", "hash_missing", "judge_missing"])
def test_missing_or_unbound_judge_cannot_pass(tmp_path: Path, failure: str):
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id)
    if failure == "judge_missing":
        score = _score(task_id, sub_dir, include_judge=False)
    else:
        judge = fakes.grounded_judge_payload(sub_dir, task_id)
        if failure == "hash_mismatch":
            judge["evidence_packet_hash"] = "0" * 64
        else:
            judge.pop("evidence_packet_hash")
        score = _score(task_id, sub_dir, judge_payload=judge)
    verdict = score["study_verdict"]
    assert verdict["evaluation_complete"] is False
    assert verdict["grounded_correct"] is False
    assert score["weighted_total"] == 0.0


@pytest.mark.parametrize(
    "corruption",
    ["zero_logic", "unverified_citation", "unresolved_citation"],
)
def test_internally_inconsistent_or_mixed_citation_judge_cannot_pass(
    tmp_path: Path,
    corruption: str,
):
    task_id = "S01_stability_t4l_l99a"
    mode = "inconclusive" if corruption == "unresolved_citation" else "honest"
    sub_dir = _make(tmp_path, task_id, mode=mode)
    judge = fakes.grounded_judge_payload(sub_dir, task_id)
    if corruption == "zero_logic":
        judge["scores"]["reasoning_logic"] = 0.0
    elif corruption == "unverified_citation":
        judge["cited_evidence_ids"].append("not-a-verified-item")

    score = _score(task_id, sub_dir, judge_payload=judge)

    assert score["study_verdict"]["reasoning_grounded"] is False
    assert score["study_verdict"]["grounded_correct"] is False
    assert score["weighted_total"] == 0.0
