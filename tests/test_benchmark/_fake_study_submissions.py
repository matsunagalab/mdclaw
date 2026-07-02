#!/usr/bin/env python
"""Generate synthetic submissions for the study-level benchmark task set.

These fixtures exercise validation, scoring, and aggregation without running
real MD. They are CI fixtures, not leaderboard evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


def _write(path: Path, payload: dict | str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, dict):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        path.write_text(str(payload))


def _truth_direction(task_id: str) -> str:
    truth_file = DATASET_DIR / "tasks" / task_id / "truth" / "experimental_truth.json"
    return str(json.loads(truth_file.read_text())["expected_direction"])


def _common_provenance(
    run_id: str,
    task_id: str,
    mode: str,
    stages: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "agent": {"name": "fake_study_submissions.py", "mode": mode},
        "backend": {"name": "synthetic-fixture", "version": "study-v0.1"},
        "harness": {"name": "fake_study_submissions.py"},
        "command_log": [
            {
                "stage": stage,
                "command": f"synthetic fixture {stage} action for {task_id}",
                "exit_code": 0,
                "walltime_seconds": 0.1,
            }
            for stage in stages
        ],
        "scripts": [],
        "raw_outputs": [],
    }


def _write_harness_record(sub_dir: Path, provenance: dict[str, Any]) -> None:
    command_log = provenance.get("command_log") or []
    _write(sub_dir.parent / "harness_execution.json", {
        "schema_version": "1.0",
        "run_id": provenance.get("run_id"),
        "task_id": provenance.get("task_id"),
        "recorded_by": "fake_study_submissions.py",
        "records": command_log,
    })


def _runtime_metrics() -> dict[str, Any]:
    return {
        "runtime": {
            "walltime_minutes": 3.0,
            "tokens": 0,
            "gpu_hours": 0.0,
        }
    }


# Per-task synthetic citation that satisfies each task's citation pool (either
# an allowed pool name + anchor, or the pool's primary-reference DOI).
_FIXTURE_CITATIONS: dict[str, dict[str, str]] = {
    "S01_stability_t4l_l99a": {
        "pool": "FireProtDB", "record_id": "synthetic-FireProtDB-2LZM-L99A",
        "doi": "10.1126/science.1553543",
    },
    "S02_ppi_hotspot_barnase_d39a": {
        "pool": "SKEMPI", "record_id": "synthetic-SKEMPI-1BRS-D39A",
        "pmid": "7540270",
    },
    "S03_stability_nuclease_h124l": {
        "pool": "ProThermDB", "record_id": "synthetic-ProThermDB-1STN-H124L",
        "doi": "10.1002/pro.5560050917",
    },
    "S04_affinity_t4l_l99a_alkylbenzene": {
        "pool": "PDBbind", "record_id": "synthetic-PDBbind-L99A-butylbenzene",
        "doi": "10.1021/bi00027a006",
    },
}


def _study_citation(task_id: str) -> dict[str, str]:
    return _FIXTURE_CITATIONS.get(task_id, _FIXTURE_CITATIONS["S01_stability_t4l_l99a"])


def _comparative_metrics(task_id: str, mode: str) -> dict[str, Any]:
    base = {"production_time_ns": 1.0}
    if task_id == "S02_ppi_hotspot_barnase_d39a":
        md_analysis = {
            **base,
            "systems": ["barnase_barstar_wt", "barnase_barstar_d39a"],
            "delta_interface_sasa_angstrom2": 240.0,
            "delta_inter_chain_contact_count": -5,
            "delta_hydrogen_bond_count": -2,
            "delta_salt_bridge_count": -1,
            "interpretation": "D39A removes interface polar contacts.",
        }
    elif task_id == "S03_stability_nuclease_h124l":
        md_analysis = {
            **base,
            "systems": ["nuclease_wt", "nuclease_h124l"],
            "delta_residue124_rmsf_angstrom": -0.30,
            "delta_local_sasa_angstrom2": -45.0,
            "delta_secondary_structure_fraction": 0.03,
            "interpretation": "H124L improves local packing around residue 124.",
        }
    elif task_id == "S04_affinity_t4l_l99a_alkylbenzene":
        md_analysis = {
            **base,
            "systems": ["l99a_benzene", "l99a_n_butylbenzene"],
            "delta_ligand_cavity_contacts": 8,
            "delta_buried_apolar_surface_angstrom2": 95.0,
            "delta_ligand_occupancy_fraction": 0.07,
            "interpretation": "n-butylbenzene buries more apolar surface in the cavity.",
        }
    else:
        md_analysis = {
            **base,
            "systems": ["t4l_wt", "t4l_l99a"],
            "delta_core_sasa_angstrom2": 180.0,
            "delta_cavity_volume_angstrom3": 120.0,
            "delta_packing_density": -0.08,
            "delta_mutation_region_rmsf_angstrom": 0.45,
            "interpretation": "L99A creates a hydrophobic core cavity.",
        }
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "md_analysis": md_analysis,
        **_runtime_metrics(),
    }


def _evidence_report(
    task: dict[str, Any],
    mode: str,
    *,
    direction: str,
    observables: list[dict[str, Any]],
    md_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    evidence: dict[str, Any] = {
        "citations": [_study_citation(task_id)],
        "md_metrics": md_metrics or {
            "direction_basis": "methods-only literature-backed fixture",
            "confidence_calibration": "synthetic dry-run evidence bundle",
        },
        "rationale": [
            "Synthetic fixture used to exercise the StudyBench scorer path.",
            "Observable values below are recomputed from the submitted synthetic "
            "trajectories so the recompute-consistency check passes.",
        ],
    }
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": (
            "Synthetic study fixture with complete evidence-contract fields. "
            "This text is deliberately long enough to pass the byte-floor "
            "integrity check while remaining clear that it is not a scientific "
            "or leaderboard submission."
        ),
        "effect": {
            "direction": direction,
            "confidence": "medium" if mode == "honest" else "low",
        },
        "observables": observables,
        "reasoning": (
            "The discriminating observable's sign between the reference and "
            "variant systems is used to support the reported effect.direction; "
            "see observables[] for the recomputed wild-type and mutant values "
            "and their block-average uncertainty."
        ),
        "evidence": evidence,
        "limitations": [
            "CI fixture only; synthetic trajectories with controlled geometry.",
            "Synthetic files are present solely to exercise validator and scorer logic.",
        ],
    }


# Minimal residue -> (atom_name, element_symbol) templates used to build tiny
# but genuinely loadable topologies. The mutation site uses the wild-type
# residue in the WT system and the mutant residue in the mutant system so the
# scorer's paired_mutation_topology check sees exactly one substitution.
_RES_ATOMS: dict[str, list[tuple[str, str]]] = {
    "ALA": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C")],
    "GLY": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")],
    "VAL": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("CG1", "C"), ("CG2", "C")],
    "SER": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("OG", "O")],
    "LEU": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("CG", "C"), ("CD1", "C"), ("CD2", "C")],
    "ASP": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("CG", "C"), ("OD1", "O"), ("OD2", "O")],
    "HIS": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C"),
            ("CG", "C"), ("ND1", "N"), ("CD2", "C"), ("CE1", "C"), ("NE2", "N")],
    # ligand residues for the affinity task (atom counts differ; names differ)
    "BNZ": [(f"C{i}", "C") for i in range(1, 7)],
    "NBB": [(f"C{i}", "C") for i in range(1, 11)],
}

COMPARATIVE_TASKS = {
    "S01_stability_t4l_l99a",
    "S02_ppi_hotspot_barnase_d39a",
    "S03_stability_nuclease_h124l",
    "S04_affinity_t4l_l99a_alkylbenzene",
}

# Per-task synthetic system layout. Each system is a list of chains, and each
# chain is a list of (residue_name, resSeq). The reference and variant differ by
# exactly one residue at the comparative site so paired_mutation_topology
# passes, and the residue numbering / chain layout matches the observable
# selection strings in the task specs.
#
# ``metric`` is the observable the direction_grounding / consistency checks
# recompute for that task; ``site`` names the (reference, variant) residue at
# the swapped position.
_N_FRAMES = 10


def _ca_chain(resseqs: list[int], site_index: int, site_res: str) -> list[tuple[str, int]]:
    chain: list[tuple[str, int]] = []
    for i, rs in enumerate(resseqs):
        chain.append((site_res if i == site_index else "ALA", rs))
    return chain


def _study_systems(task_id: str) -> dict[str, Any]:
    if task_id == "S01_stability_t4l_l99a":
        resseqs = list(range(95, 104))
        idx = resseqs.index(99)
        return {
            "metric": "ca_rmsf",
            "reference": [_ca_chain(resseqs, idx, "LEU")],
            "variant": [_ca_chain(resseqs, idx, "ALA")],
        }
    if task_id == "S03_stability_nuclease_h124l":
        resseqs = list(range(120, 129))
        idx = resseqs.index(124)
        return {
            "metric": "ca_rmsf",
            "reference": [_ca_chain(resseqs, idx, "HIS")],
            "variant": [_ca_chain(resseqs, idx, "LEU")],
        }
    if task_id == "S02_ppi_hotspot_barnase_d39a":
        barnase = [("ALA", rs) for rs in range(1, 6)]
        barstar_ref = [("ALA", 37), ("ALA", 38), ("ASP", 39), ("ALA", 40), ("ALA", 41)]
        barstar_var = [("ALA", 37), ("ALA", 38), ("ALA", 39), ("ALA", 40), ("ALA", 41)]
        return {
            "metric": "contact_count",
            "reference": [barnase, barstar_ref],
            "variant": [barnase, barstar_var],
        }
    if task_id == "S04_affinity_t4l_l99a_alkylbenzene":
        cavity = [("ALA", rs) for rs in range(78, 91)]
        return {
            "metric": "contact_count",
            "reference": [cavity, [("BNZ", 201)]],
            "variant": [cavity, [("NBB", 201)]],
        }
    raise ValueError(f"unknown MDStudyBench task_id: {task_id}")


def _new_topology(chains: list[list[tuple[str, int]]]):
    import mdtraj as md
    from mdtraj.core import element as elem

    top = md.Topology()
    for chain_res in chains:
        ch = top.add_chain()
        for resname, resseq in chain_res:
            res = top.add_residue(resname, ch, resSeq=resseq)
            for aname, symbol in _RES_ATOMS[resname]:
                top.add_atom(aname, elem.get_by_symbol(symbol), res)
    return top


def _build_rmsf_traj(chains, ca_flex_nm: float, seed: int):
    """Single-chain system whose CA atoms fluctuate with amplitude ``ca_flex_nm``.

    Mean CA RMSF is controlled by ca_flex_nm, so making the variant more or less
    flexible than the reference sets the sign of the recomputed observable.
    """
    import numpy as np

    top = _new_topology(chains)
    rng = np.random.RandomState(seed)
    n = top.n_atoms
    base = np.zeros((n, 3), dtype="float32")
    for i, atom in enumerate(top.atoms):
        base[i] = [0.38 * atom.residue.resSeq, 0.1 * (i % 3), 0.05 * (i % 2)]
    ca_idx = [a.index for a in top.atoms if a.name == "CA"]
    xyz = np.repeat(base[None, :, :], _N_FRAMES, axis=0).copy()
    noise = rng.randn(_N_FRAMES, len(ca_idx), 3).astype("float32") * ca_flex_nm
    xyz[:, ca_idx, :] += noise
    import mdtraj as md
    return md.Trajectory(xyz, top)


def _build_contact_traj(chains, separation_nm: float, seed: int):
    """Two-group system. Group A (chain 0 / cavity) clusters near the origin and
    group B (chain 1 / ligand) clusters ``separation_nm`` away, so a small
    separation yields many heavy-atom contacts and a large one yields none."""
    import numpy as np

    top = _new_topology(chains)
    rng = np.random.RandomState(seed)
    n = top.n_atoms
    base = np.zeros((n, 3), dtype="float32")
    for i, atom in enumerate(top.atoms):
        if atom.residue.chain.index == 0:
            centre = np.array([0.0, 0.0, 0.0])
        else:
            centre = np.array([separation_nm, 0.0, 0.0])
        jitter = rng.uniform(-0.08, 0.08, size=3)
        base[i] = (centre + jitter).astype("float32")
    xyz = np.repeat(base[None, :, :], _N_FRAMES, axis=0).copy()
    xyz += rng.randn(_N_FRAMES, n, 3).astype("float32") * 0.004
    import mdtraj as md
    return md.Trajectory(xyz, top)


# Contact regimes: a close separation puts every A-B heavy-atom pair inside the
# 0.45 nm cutoff (many contacts); a far separation puts them all outside (none).
_CONTACT_CLOSE_NM = 0.30
_CONTACT_FAR_NM = 0.75
# RMSF regimes (nm): a flexible system has a larger mean CA RMSF than a rigid one.
_RMSF_FLEX_NM = 0.09
_RMSF_RIGID_NM = 0.015


def _grounding_check(task: dict[str, Any]) -> dict[str, Any]:
    for check in task["scoring"]["deterministic_checks"]:
        if check.get("check_type") == "direction_grounding":
            return check
    raise ValueError("task has no direction_grounding check")


def _mode_plan(task: dict[str, Any], mode: str) -> dict[str, Any]:
    """Return the geometry favouring + claimed direction + fabricate flag for a
    mode.

    ``favor`` is the label ("increase"/"decrease"/"flat") the synthetic geometry
    should express; ``claim`` is the direction written into evidence_report;
    ``fabricate`` overwrites reported observable values so the
    recompute-consistency check fails.
    """
    task_id = str(task["task_id"])
    truth = _truth_direction(task_id)
    sign_to_direction = _grounding_check(task)["sign_to_direction"]
    label_for = {direction: label for label, direction in sign_to_direction.items()}
    truth_label = label_for[truth]
    opposite = {"increase": "decrease", "decrease": "increase"}
    wrong_label = opposite[truth_label]
    wrong_direction = sign_to_direction[wrong_label]
    plans = {
        # geometry favours truth, agent claims truth -> full credit
        "honest": {"favor": truth_label, "claim": truth, "fabricate": False},
        # geometry favours truth, agent claims a non-truth direction -> the claim
        # contradicts the agent's own MD, so grounding fails (only recompute
        # consistency credit remains)
        "wrong": {"favor": truth_label, "claim": wrong_direction, "fabricate": False},
        # geometry favours the wrong direction and the agent claims it -> the
        # answer is faithful to its own MD but disagrees with the literature
        "faithful_wrong": {
            "favor": wrong_label, "claim": wrong_direction, "fabricate": False,
        },
        # literature guess: claims truth but its own MD points the other way and
        # the reported numbers are fabricated -> grounding + consistency fail
        "guess": {"favor": wrong_label, "claim": truth, "fabricate": True},
        # honest inconclusive: geometry shows no meaningful separation
        "inconclusive": {"favor": "flat", "claim": truth, "fabricate": False},
    }
    return plans[mode]


def _write_comparative_systems(sub_dir: Path, task_id: str,
                               favor: str) -> tuple[dict[str, list[str]], str]:
    """Write loadable reference/variant topologies + trajectories with geometry
    that expresses ``favor`` (increase/decrease/flat) for the task observable."""
    spec = _study_systems(task_id)
    metric = spec["metric"]

    if metric == "ca_rmsf":
        if favor == "increase":
            ref_p, var_p = _RMSF_RIGID_NM, _RMSF_FLEX_NM
        elif favor == "decrease":
            ref_p, var_p = _RMSF_FLEX_NM, _RMSF_RIGID_NM
        else:
            ref_p = var_p = _RMSF_RIGID_NM
        ref_traj = _build_rmsf_traj(spec["reference"], ref_p, seed=11)
        var_traj = _build_rmsf_traj(spec["variant"], var_p, seed=11)
    else:
        if favor == "increase":
            ref_s, var_s = _CONTACT_FAR_NM, _CONTACT_CLOSE_NM
        elif favor == "decrease":
            ref_s, var_s = _CONTACT_CLOSE_NM, _CONTACT_FAR_NM
        else:
            ref_s = var_s = _CONTACT_CLOSE_NM
        ref_traj = _build_contact_traj(spec["reference"], ref_s, seed=23)
        var_traj = _build_contact_traj(spec["variant"], var_s, seed=23)

    trajectories: list[str] = []
    topologies: list[str] = []
    for name, traj in (("wt", ref_traj), ("mutant", var_traj)):
        top_rel = f"topology/{name}.pdb"
        traj_rel = f"trajectories/{name}.dcd"
        (sub_dir / top_rel).parent.mkdir(parents=True, exist_ok=True)
        (sub_dir / traj_rel).parent.mkdir(parents=True, exist_ok=True)
        traj.save_pdb(str(sub_dir / top_rel))
        traj.save_dcd(str(sub_dir / traj_rel))
        topologies.append(top_rel)
        trajectories.append(traj_rel)
    return {"trajectories": trajectories, "topology": topologies}, metric


def _recomputed_observables(task: dict[str, Any], sub_dir: Path,
                            manifest: dict[str, Any],
                            fabricate: bool) -> list[dict[str, Any]]:
    """Build the evidence_report.observables[] entries by recomputing each
    task observable exactly the way the scorer will, so the honest report agrees
    with the scorer. ``fabricate`` perturbs the values to fail consistency."""
    from mdclaw.benchmark import scoring
    from mdclaw.benchmark.models import DeterministicCheck

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in task["scoring"]["deterministic_checks"]:
        if raw.get("check_type") not in (
            "direction_grounding", "observable_recompute_consistency",
        ):
            continue
        name = raw.get("report_observable_name") or raw.get("observable_metric")
        if name in seen:
            continue
        seen.add(name)
        check = DeterministicCheck.model_validate(raw)
        ref, var, sigma, _msg = scoring._recompute_paired_observable(
            check, sub_dir, manifest,
        )
        if ref is None:
            raise RuntimeError(f"fixture failed to recompute observable: {_msg}")
        if fabricate:
            ref = ref + 1000.0
            var = var + 1000.0
        entries.append({
            "name": name,
            "metric": raw.get("observable_metric"),
            "selection": raw.get("observable_selection"),
            "wt_value": round(float(ref), 6),
            "mutant_value": round(float(var), 6),
            "unit": "angstrom" if raw.get("observable_metric") == "ca_rmsf" else "count",
            "uncertainty": round(float(sigma or 0.0), 6),
            "uncertainty_method": "block_average",
            "supports_direction": raw.get("sign_to_direction", {}).get(
                "increase" if var >= ref else "decrease"
            ),
            "source": "recomputed_from_trajectory",
        })
    return entries


def make_study_submission(
    sub_dir: Path,
    run_id: str,
    mode: str,
    task_id: str,
) -> None:
    task_dir = DATASET_DIR / "tasks" / task_id
    task = json.loads((task_dir / "task.json").read_text())

    if task_id not in COMPARATIVE_TASKS:
        raise ValueError(f"unknown MDStudyBench task_id: {task_id}")

    plan = _mode_plan(task, mode)

    systems, _metric = _write_comparative_systems(sub_dir, task_id, plan["favor"])
    metrics = _comparative_metrics(task_id, mode)
    provenance = _common_provenance(
        run_id,
        task_id,
        mode,
        ["source", "prep", "prod", "analysis", "report"],
    )
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
            "trajectories": systems["trajectories"],
            "topology": systems["topology"],
        },
        "limitations": [
            "Synthetic CI fixture; trajectory geometry is controlled, not real MD.",
        ],
    }
    _write(sub_dir / "manifest.json", manifest)
    _write(sub_dir / "metrics.json", metrics)
    _write(sub_dir / "provenance.json", provenance)
    _write_harness_record(sub_dir, provenance)

    observables = _recomputed_observables(task, sub_dir, manifest, plan["fabricate"])
    evidence = _evidence_report(
        task,
        mode,
        direction=plan["claim"],
        observables=observables,
        md_metrics=metrics["md_analysis"],
    )
    _write(sub_dir / "evidence_report.json", evidence)


def _load_task_ids() -> list[str]:
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    return [str(task_id) for task_id in dataset["task_ids"]]


def _make_generator(task_id: str):
    return lambda sub_dir, run_id, mode: make_study_submission(
        sub_dir,
        run_id,
        mode,
        task_id,
    )


GENERATORS = {task_id: _make_generator(task_id) for task_id in _load_task_ids()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--mode",
        choices=("honest", "wrong", "faithful_wrong", "guess", "inconclusive"),
        default="honest",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    tasks_dir = run_dir / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)
    tasks_dir.mkdir(parents=True)

    for task_id, fn in GENERATORS.items():
        sub_dir = tasks_dir / task_id / "submission"
        fn(sub_dir, run_id=run_dir.name, mode=args.mode)

    print(
        f"[ok] {len(GENERATORS)} fake study submissions written under "
        f"{tasks_dir} (mode={args.mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
