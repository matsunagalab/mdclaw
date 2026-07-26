#!/usr/bin/env python
"""Generate synthetic submissions for the study-level benchmark task set.

These fixtures exercise validation, scoring, and aggregation without running
real MD. They are CI fixtures, not leaderboard evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"

V2_PRESSURE_TASK_ID = "S01_pressure_hydration_t4l_l99a"
T4L_C54T_C97A_L99A_SEQUENCE = (
    "MNIFEMLRIDEGLRLKIYKDTEGYYTIGIGHLLTKSPSLNAAKSELDKAIGRNTNGVITKDEAE"
    "KLFNQDVDAAVRGILRNAKLKPVYDSLDAVRRAAAINMVFQMGETGVAGFTNSLRMLQQKRWDEA"
    "AVNLAKSRWYNQTPNRAKRVITTFRTGTWDAYKNL"
)
_ONE_TO_THREE = {
    "A": "ALA",
    "C": "CYS",
    "D": "ASP",
    "E": "GLU",
    "F": "PHE",
    "G": "GLY",
    "H": "HIS",
    "I": "ILE",
    "K": "LYS",
    "L": "LEU",
    "M": "MET",
    "N": "ASN",
    "P": "PRO",
    "Q": "GLN",
    "R": "ARG",
    "S": "SER",
    "T": "THR",
    "V": "VAL",
    "W": "TRP",
    "Y": "TYR",
}


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
                "run_id": run_id,
                "task_id": task_id,
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


# Per-task citation-shaped prior context. v0.3 keeps this separate from MD
# evidence and does not restrict agents to a curator-provided citation pool.
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
    fixture_spec = _fixture_evidence_spec(task_id)
    prior_knowledge: dict[str, Any] = {
        "citations": [_study_citation(task_id)],
        "summary": (
            "Synthetic prior context is recorded separately from the MD-derived "
            "conclusion and is not used as substitute evidence."
        ),
    }
    evidence_items: list[dict[str, Any]] = []
    for observable in observables:
        item = {
            "id": str(observable.get("name") or observable.get("metric")),
            "metric": str(observable.get("metric")),
            "selection": str(
                observable.get("selection")
                or fixture_spec["observable_selection"]
                or "protein and name CA"
            ),
            "reference": float(observable["wt_value"]),
            "variant": float(observable["mutant_value"]),
            "uncertainty": float(observable.get("uncertainty") or 0.0),
            "unit": str(observable.get("unit") or "arbitrary"),
        }
        selection_b = fixture_spec.get("observable_selection_b")
        if selection_b:
            item["selection_b"] = selection_b
        cutoff = fixture_spec.get("contact_cutoff_nm")
        if cutoff is not None:
            item["contact_cutoff_nm"] = float(cutoff)
        evidence_items.append(item)

    evidence_status = "inconclusive" if mode == "inconclusive" else "supported"
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": (
            "Synthetic study fixture with complete evidence-contract fields. "
            "This text is deliberately long enough to pass the byte-floor "
            "integrity check while remaining clear that it is not a scientific "
            "or leaderboard submission."
        ),
        "conclusion": {
            "direction": direction,
            "evidence_status": evidence_status,
            "confidence": 0.8 if mode == "honest" else 0.3,
        },
        # Kept as a v0.2 compatibility diagnostic.  The v0.3 official verdict
        # reads conclusion and the independently verified evidence list below.
        "effect": {"direction": direction, "confidence": "medium"},
        "observables": observables,
        "reasoning": (
            "The sign and uncertainty of the selected observable are compared "
            "between every declared reference and variant replica. The final "
            "direction is claimed only from those submitted MD artifacts; see "
            "evidence[] for the values independently recomputed by the scorer."
        ),
        "evidence": evidence_items,
        "prior_knowledge": prior_knowledge,
        "supplemental_metrics": md_metrics or {},
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

# Test-only, agent-selected observables used to make raw-verifiable CI fixtures.
# These choices follow from the public systems and contain no held-out answer.
# Production task specs intentionally prescribe none of these metrics,
# selections, or interpretation maps.
_FIXTURE_EVIDENCE_SPECS: dict[str, dict[str, Any]] = {
    "S01_stability_t4l_l99a": {
        "id": "ca_rmsf_near_99",
        "observable_metric": "ca_rmsf",
        "observable_selection": "resSeq 95 to 103 and name CA",
        "sign_to_direction": {
            "increase": "destabilizing",
            "decrease": "stabilizing",
        },
    },
    "S02_ppi_hotspot_barnase_d39a": {
        "id": "interface_contacts",
        "observable_metric": "contact_count",
        "observable_selection": "chainid 0 and protein",
        "observable_selection_b": "chainid 1 and protein",
        "contact_cutoff_nm": 0.45,
        "sign_to_direction": {
            "increase": "strengthened_binding",
            "decrease": "weakened_binding",
        },
    },
    "S03_stability_nuclease_h124l": {
        "id": "ca_rmsf_near_124",
        "observable_metric": "ca_rmsf",
        "observable_selection": "resSeq 120 to 128 and name CA",
        "sign_to_direction": {
            "increase": "destabilizing",
            "decrease": "stabilizing",
        },
    },
    "S04_affinity_t4l_l99a_alkylbenzene": {
        "id": "ligand_cavity_contacts",
        "observable_metric": "contact_count",
        "observable_selection": (
            "not protein and not water and not resname NA CL K MG ZN CA"
        ),
        "observable_selection_b": "protein and resSeq 78 to 121",
        "contact_cutoff_nm": 0.45,
        "sign_to_direction": {
            "increase": "stronger_binding",
            "decrease": "weaker_binding",
        },
    },
}

# Per-task synthetic system layout. Each system is a list of chains, and each
# chain is a list of (residue_name, resSeq). The reference and variant differ by
# exactly one residue at the comparative site so paired_mutation_topology
# passes. The test fixture independently chooses an observable compatible with
# each synthetic topology; production task specs intentionally do not.
#
# ``metric`` identifies the test fixture's chosen generic recomputation.
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


def _fixture_evidence_spec(task_id: str) -> dict[str, Any]:
    try:
        return _FIXTURE_EVIDENCE_SPECS[task_id]
    except KeyError as exc:
        raise ValueError(f"no synthetic evidence fixture for {task_id}") from exc


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
    sign_to_direction = _fixture_evidence_spec(task_id)["sign_to_direction"]
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
    """Recompute the test fixture's agent-selected generic observable."""
    from mdclaw.benchmark import scoring
    from mdclaw.benchmark.models import DeterministicCheck

    spec = _fixture_evidence_spec(str(task["task_id"]))
    raw = {
        "check_id": f"{spec['id']}_fixture_recompute",
        "check_type": "direction_grounding",
        "weight": 0.0,
        "observable_metric": spec["observable_metric"],
        "observable_selection": spec["observable_selection"],
        "observable_selection_b": spec.get("observable_selection_b"),
        "contact_cutoff_nm": spec.get("contact_cutoff_nm", 0.45),
        "sign_to_direction": spec["sign_to_direction"],
        "report_observable_name": spec["id"],
    }
    check = DeterministicCheck.model_validate(raw)
    ref, var, sigma, message = scoring._recompute_paired_observable(
        check,
        sub_dir,
        manifest,
    )
    if ref is None:
        raise RuntimeError(f"fixture failed to recompute observable: {message}")
    if fabricate:
        ref += 1000.0
        var += 1000.0
    metric = str(spec["observable_metric"])
    return [{
        "name": str(spec["id"]),
        "metric": metric,
        "selection": str(spec["observable_selection"]),
        "wt_value": round(float(ref), 6),
        "mutant_value": round(float(var), 6),
        "unit": "angstrom" if metric == "ca_rmsf" else "count",
        "uncertainty": round(float(sigma or 0.0), 6),
        "uncertainty_method": "block_average",
        "supports_direction": spec["sign_to_direction"].get(
            "increase" if var >= ref else "decrease"
        ),
        "source": "recomputed_from_trajectory",
    }]


def _v2_pressure_mode_plan(mode: str) -> dict[str, Any]:
    """Describe a v2 fixture without weakening any verifier requirement."""

    plans = {
        "honest": {
            "raw_direction": "increase",
            "status": "resolved",
            "claim": "increased_hydration",
        },
        "wrong": {
            "raw_direction": "increase",
            "status": "resolved",
            "claim": "decreased_hydration",
        },
        "faithful_wrong": {
            "raw_direction": "decrease",
            "status": "resolved",
            "claim": "decreased_hydration",
        },
        "guess": {
            "raw_direction": "decrease",
            "status": "resolved",
            "claim": "increased_hydration",
        },
        "inconclusive": {
            "raw_direction": "ambiguous",
            "status": "unresolved",
            "claim": None,
        },
    }
    return plans[mode]


def _v2_occupancy_pattern(regime: str) -> list[bool]:
    """Return 30 frames with post-burn-in occupancy round trips.

    The low/high patterns resolve a directional contrast.  The two ambiguous
    patterns have heterogeneous block means, so their confidence interval
    crosses both the zero-equivalence band and a material-change boundary.
    """

    if regime == "low":
        occupied = {0, 1, 5, 6, 15, 16, 25, 26}
    elif regime == "high":
        occupied = set(range(30)) - {5, 6, 15, 16, 25, 26}
    elif regime == "ambiguous_reference":
        occupied = {
            1, 6, 7, 8, 9, 10, 11, 17, 18, 19, 20, 21, 22, 27, 28,
        }
    elif regime == "ambiguous_variant":
        occupied = {
            1, 2, 3, 4, 5, 7, 12, 13, 14, 15, 16, 18, 23, 24, 25, 26, 27,
        }
    else:
        raise ValueError(f"unknown occupancy regime: {regime}")
    return [frame in occupied for frame in range(30)]


def _write_v2_pressure_run(
    sub_dir: Path,
    *,
    system_id: str,
    occupancy: list[bool],
) -> tuple[str, str]:
    """Write a full-construct, loadable trajectory for native v2 verifiers."""

    import mdtraj as md
    import numpy as np
    from mdtraj.core import element

    if len(occupancy) < 30:
        raise ValueError("v2 pressure fixture requires at least 30 frames")

    topology = md.Topology()
    protein_chain = topology.add_chain()
    ca_indices: list[int] = []
    cavity_cb_index: int | None = None
    for residue_number, one_letter in enumerate(
        T4L_C54T_C97A_L99A_SEQUENCE,
        start=1,
    ):
        residue = topology.add_residue(
            _ONE_TO_THREE[one_letter],
            protein_chain,
            resSeq=residue_number,
        )
        ca_indices.append(
            int(topology.add_atom("CA", element.carbon, residue).index)
        )
        if residue_number == 99:
            cavity_cb_index = int(
                topology.add_atom("CB", element.carbon, residue).index
            )
    assert cavity_cb_index is not None
    water_chain = topology.add_chain()
    water = topology.add_residue("HOH", water_chain, resSeq=1000)
    topology.add_atom("O", element.oxygen, water)

    frame_count = len(occupancy)
    coordinates = np.zeros(
        (frame_count, topology.n_atoms, 3),
        dtype=np.float32,
    )
    parameter = np.linspace(
        0.0,
        8.0 * np.pi,
        len(T4L_C54T_C97A_L99A_SEQUENCE),
    )
    protein_coordinates = np.column_stack(
        (
            1.2 * np.cos(parameter),
            1.2 * np.sin(parameter),
            np.linspace(-1.0, 1.0, len(T4L_C54T_C97A_L99A_SEQUENCE)),
        )
    ).astype(np.float32)
    for frame in range(frame_count):
        coordinates[frame, ca_indices, :] = protein_coordinates
        # A tiny internal motion avoids a byte-identical all-static trajectory
        # while remaining far below the folded-state RMSD threshold.
        coordinates[frame, ca_indices[10], 2] += 0.005 * np.sin(frame / 3.0)

    cavity_center = protein_coordinates[98] + np.array(
        (0.05, 0.0, 0.0), dtype=np.float32
    )
    coordinates[:, cavity_cb_index, :] = cavity_center
    near = cavity_center + np.array((0.0, 0.0, 0.2), dtype=np.float32)
    far = np.array((4.0, 4.0, 4.0), dtype=np.float32)
    coordinates[:, -1, :] = far
    coordinates[np.asarray(occupancy, dtype=bool), -1, :] = near
    # The public S01 observable contract requires minimum-image distances.
    # Keep the synthetic coordinates in a generous orthorhombic box so the
    # native verifier exercises its periodic path without changing the
    # deliberately controlled occupancy pattern.
    unitcell_lengths = np.full((frame_count, 3), 8.0, dtype=np.float32)
    unitcell_angles = np.full((frame_count, 3), 90.0, dtype=np.float32)
    trajectory = md.Trajectory(
        coordinates,
        topology,
        unitcell_lengths=unitcell_lengths,
        unitcell_angles=unitcell_angles,
    )

    system_dir = sub_dir / "systems" / system_id
    system_dir.mkdir(parents=True, exist_ok=True)
    topology_path = system_dir / "topology.pdb"
    trajectory_path = system_dir / "confirmatory.dcd"
    trajectory[0].save_pdb(str(topology_path))
    trajectory.save_dcd(str(trajectory_path))
    return (
        topology_path.relative_to(sub_dir).as_posix(),
        trajectory_path.relative_to(sub_dir).as_posix(),
    )


def _v2_analysis_intent(task: dict[str, Any]) -> dict[str, Any]:
    primary_contract = task["scientific_target"]["primary_evidence_contract"]
    control_contract = task["scientific_target"]["control_evidence_contracts"][0]
    primary_parameters = {
        "region_selection": "resid 98 and name CB",
        **primary_contract["fixed_observable_parameters"],
    }
    return {
        "schema_version": "1.0",
        "task_id": V2_PRESSURE_TASK_ID,
        "intent_id": "fixture-pressure-intent-1",
        "target_estimand": task["scientific_target"]["estimand"],
        "primary_analyses": [
            {
                "analysis_id": "hydration-primary",
                "analysis_role": "estimand",
                "comparison_id": "pressure-effect",
                "verifier_id": primary_contract["verifier_id"],
                "observable": {
                    "parameters": primary_parameters,
                },
                "outcome_mapping": dict(primary_contract["outcome_mapping"]),
                "decision_rule": dict(primary_contract["decision_rule"]),
                "estimand_link": (
                    "The direct 200 MPa minus 0.1 MPa contrast estimates the "
                    "public equilibrium cavity-hydration estimand."
                ),
                "alternative_explanations": [
                    "global unfolding",
                    "initialization-dependent wetting",
                ],
            },
            {
                "analysis_id": "folded-control",
                "analysis_role": "validity_control",
                "comparison_id": "pressure-effect",
                "verifier_id": "folded_state_retention@1",
                "observable": {
                    "parameters": dict(
                        control_contract["fixed_observable_parameters"]
                    )
                },
                "outcome_mapping": dict(control_contract["outcome_mapping"]),
                "decision_rule": dict(control_contract["decision_rule"]),
                "estimand_link": (
                    "The target estimand is conditional on both pressure "
                    "systems retaining the folded construct."
                ),
                "alternative_explanations": ["pressure-induced unfolding"],
            },
        ],
    }


def _v2_study_index(
    *,
    reference_topology: str,
    reference_trajectory: str,
    variant_topology: str,
    variant_trajectory: str,
    reference_start: float,
    variant_start: float,
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "task_id": V2_PRESSURE_TASK_ID,
        "conditions": {
            "temperature_k": 300.0,
            "ph": 7.0,
            "reference_pressure_mpa": 0.1,
            "test_pressure_mpa": 200.0,
        },
        "systems": [
            {
                "system_id": "ambient",
                "source": {
                    "type": "synthetic_fixture",
                    "id": "full-t4l-c54t-c97a-l99a-ambient",
                },
                "conditions": {
                    "temperature_k": 300.0,
                    "ph": 7.0,
                    "pressure_mpa": 0.1,
                },
                "runs": [
                    {
                        "run_id": "ambient-confirmatory-1",
                        "phase": "confirmatory",
                        "intent_id": "fixture-pressure-intent-1",
                        "production_event_id": "prod-ambient-1",
                        "topology": reference_topology,
                        "trajectory": reference_trajectory,
                        "declared_starting_occupancy": reference_start,
                        "metadata": {"sampling_mode": "unbiased"},
                    }
                ],
            },
            {
                "system_id": "high-pressure",
                "source": {
                    "type": "synthetic_fixture",
                    "id": "full-t4l-c54t-c97a-l99a-high-pressure",
                },
                "conditions": {
                    "temperature_k": 300.0,
                    "ph": 7.0,
                    "pressure_mpa": 200.0,
                },
                "runs": [
                    {
                        "run_id": "pressure-confirmatory-1",
                        "phase": "confirmatory",
                        "intent_id": "fixture-pressure-intent-1",
                        "production_event_id": "prod-pressure-1",
                        "topology": variant_topology,
                        "trajectory": variant_trajectory,
                        "declared_starting_occupancy": variant_start,
                        "metadata": {"sampling_mode": "unbiased"},
                    }
                ],
            },
        ],
        "comparisons": [
            {
                "comparison_id": "pressure-effect",
                "reference_system_ids": ["ambient"],
                "variant_system_ids": ["high-pressure"],
                "matched_except": ["pressure_mpa"],
            }
        ],
    }


def _v2_evidence_report(mode: str, plan: dict[str, Any]) -> dict[str, Any]:
    unresolved = plan["status"] == "unresolved"
    prior_outcome = "increased_hydration" if mode == "guess" else None
    return {
        "schema_version": "2.0",
        "task_id": V2_PRESSURE_TASK_ID,
        "prior_expectation": {
            "outcome": prior_outcome,
            "confidence": 0.8 if mode == "guess" else None,
            "rationale": (
                "The guess-mode fixture records an open-book expectation, "
                "which is not used as MD evidence."
                if mode == "guess"
                else "No prior expectation is used for this synthetic MD conclusion."
            ),
            "sources": [],
        },
        "md_verdict": {
            "status": plan["status"],
            "outcome": plan["claim"],
            "basis": "insufficient" if unresolved else "direct_estimator",
            "confidence": 0.35 if unresolved else 0.8,
            "cited_evidence_ids": [
                "hydration-primary-result",
                "folded-control-result",
            ],
            "unresolved_reasons": (
                ["The preregistered confidence interval crosses a decision boundary."]
                if unresolved
                else []
            ),
        },
        "evidence": [
            {
                "id": "hydration-primary-result",
                "intent_id": "fixture-pressure-intent-1",
                "analysis_id": "hydration-primary",
                "comparison_id": "pressure-effect",
                "verifier_id": "region_water_occupancy@1",
                "claim_role": "direct_estimator",
                "estimand_link": (
                    "Direct pressure contrast in equilibrium cavity-water occupancy."
                ),
                "reported": {"estimate": 0.0, "unit": "water_count"},
                "uncertainty": 0.0,
                "artifacts": ["analysis/hydration.json"],
            },
            {
                "id": "folded-control-result",
                "intent_id": "fixture-pressure-intent-1",
                "analysis_id": "folded-control",
                "comparison_id": "pressure-effect",
                "verifier_id": "folded_state_retention@1",
                "claim_role": "validity_control",
                "estimand_link": (
                    "Confirms that both pressure conditions remain folded."
                ),
                "reported": {"folded_state_retained": True},
                "uncertainty": 0.0,
                "artifacts": ["analysis/folded-control.json"],
            },
        ],
        "reasoning": (
            "The conclusion follows the preregistered equivalence-interval "
            "classification of the evaluator-recomputed pressure contrast; "
            "the separate folded-state control checks the conditional estimand."
        ),
        "limitations": [
            "Synthetic CI fixture with controlled occupancy geometry, not real MD.",
            "Its purpose is to test raw recomputation, lineage, and taxonomy.",
        ],
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_v2_generic_harness_record(
    sub_dir: Path,
    *,
    run_id: str,
    intent: dict[str, Any],
    study_index: dict[str, Any],
) -> None:
    """Write legacy stage provenance that is deliberately not a v2 ledger.

    The raw-evidence fixture still needs an external harness file, but a
    generic command wrapper must never certify that synthetic DCD bytes came
    from runner-controlled OpenMM production.
    """

    del intent  # The attested digest is deliberately over exact file bytes.
    intent_path = sub_dir / "analysis_intent.json"
    trajectory_by_event = {
        str(run["production_event_id"]): sub_dir / str(run["trajectory"])
        for system in study_index["systems"]
        for run in system["runs"]
    }
    topology_by_event = {
        str(run["production_event_id"]): sub_dir / str(run["topology"])
        for system in study_index["systems"]
        for run in system["runs"]
    }

    def base_record(event_id: str, stage: str, timestamp: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "task_id": V2_PRESSURE_TASK_ID,
            "stage": stage,
            "command": f"synthetic v2 fixture {stage}",
            "exit_code": 0,
            "walltime_seconds": 0.1,
            "started_at": timestamp,
            "completed_at": timestamp,
        }

    records = [
        base_record("source-1", "source", "2026-07-21T00:58:00+00:00"),
        base_record("prep-1", "prep", "2026-07-21T00:59:00+00:00"),
    ]
    registration = base_record(
        "register-intent-1",
        "register_analysis_intent",
        "2026-07-21T01:00:00+00:00",
    )
    registration.update(
        {
            "intent_id": "fixture-pressure-intent-1",
            "intent_sha256": _sha256_file(intent_path),
        }
    )
    records.append(registration)
    for event_id, timestamp in (
        ("prod-ambient-1", "2026-07-21T01:01:00+00:00"),
        ("prod-pressure-1", "2026-07-21T01:02:00+00:00"),
    ):
        trajectory = trajectory_by_event[event_id]
        trajectory_hash = _sha256_file(trajectory)
        trajectory_size = trajectory.stat().st_size
        topology = topology_by_event[event_id]
        topology_hash = _sha256_file(topology)
        topology_size = topology.stat().st_size
        event = base_record(event_id, "prod", timestamp)
        event.update(
            {
                "phase": "confirmatory",
                "intent_id": "fixture-pressure-intent-1",
                "artifacts": [
                    {
                        "path": trajectory.relative_to(sub_dir).as_posix(),
                        "before": {"exists": False, "is_file": False},
                        "after": {
                            "exists": True,
                            "is_file": True,
                            "sha256": trajectory_hash,
                            "bytes": trajectory_size,
                        },
                        "sha256": trajectory_hash,
                        "bytes": trajectory_size,
                    }
                ],
                "input_artifacts": [
                    {
                        "path": topology.relative_to(sub_dir).as_posix(),
                        "before": {
                            "exists": True,
                            "is_file": True,
                            "sha256": topology_hash,
                            "bytes": topology_size,
                        },
                        "after": {
                            "exists": True,
                            "is_file": True,
                            "sha256": topology_hash,
                            "bytes": topology_size,
                        },
                        "sha256": topology_hash,
                        "bytes": topology_size,
                    }
                ],
            }
        )
        records.append(event)
    records.extend(
        [
            base_record("analysis-1", "analysis", "2026-07-21T01:03:00+00:00"),
            base_record("report-1", "report", "2026-07-21T01:04:00+00:00"),
        ]
    )
    _write(
        sub_dir.parent / "harness_execution.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": V2_PRESSURE_TASK_ID,
            "recorded_by": "fake_study_submissions.py",
            "records": records,
        },
    )


def _finalize_v2_report_from_raw(
    sub_dir: Path,
    *,
    task: dict[str, Any],
    report: dict[str, Any],
    mode: str,
) -> None:
    """Fill diagnostic report values from the same native raw verifier."""

    from mdclaw.benchmark.grounded_v2 import build_truth_blind_bundle_v2

    harness = json.loads((sub_dir.parent / "harness_execution.json").read_text())
    bundle = build_truth_blind_bundle_v2(
        submission_dir=sub_dir,
        scientific_target=task["scientific_target"],
        harness_record=harness,
    )
    packet_items = {
        item["id"]: item
        for item in bundle["verified_evidence"]["evidence"]
    }
    hydration = packet_items["hydration-primary-result"]
    folded = packet_items["folded-control-result"]
    hydration_raw = hydration.get("raw_recomputed") or {}
    folded_raw = folded.get("raw_recomputed") or {}
    report["evidence"][0]["reported"] = {
        "estimate": hydration_raw.get("variant_minus_reference"),
        "unit": hydration_raw.get("unit", "water_count"),
    }
    report["evidence"][0]["uncertainty"] = hydration_raw.get("uncertainty")
    report["evidence"][1]["reported"] = {
        "folded_state_retained": folded_raw.get("folded_state_retained")
    }

    summary = bundle.get("summary") or {}
    preregistration = bundle.get("preregistration_certificate") or {}
    execution = preregistration.get("execution_certificate") or {}
    expected_primary_status = (
        "inconclusive" if mode == "inconclusive" else "resolved"
    )
    failures = []
    if not summary.get("artifact_valid"):
        failures.append("artifact_valid")
    if not summary.get("entity_condition_valid"):
        failures.append("entity_condition_valid")
    if not preregistration.get("authored_contract_valid"):
        failures.append("authored_contract_valid")
    # Generic stage provenance can accompany raw-evidence recomputation, but
    # it must never satisfy the runner-owned execution boundary.
    if summary.get("execution_attested"):
        failures.append("generic_wrapper_was_execution_attested")
    if summary.get("preregistration_valid"):
        failures.append("generic_wrapper_was_preregistered")
    execution_reason_codes = {
        str(item.get("code"))
        for item in execution.get("errors") or []
        if isinstance(item, dict)
    }
    if "runner_execution_ledger_missing" not in execution_reason_codes:
        failures.append("runner_execution_ledger_missing_not_reported")
    if not summary.get("required_controls_evaluated"):
        failures.append("required_controls_not_recomputed")
    if summary.get("required_controls_passed"):
        failures.append("generic_wrapper_control_became_support_eligible")
    if hydration.get("statistical_status") != expected_primary_status:
        failures.append(
            "hydration_status=" + str(hydration.get("statistical_status"))
        )
    if not hydration_raw:
        failures.append("hydration_raw_recomputed")
    if not folded_raw:
        failures.append("folded_raw_recomputed")
    if hydration.get("support_eligible"):
        failures.append("generic_wrapper_evidence_became_support_eligible")
    if failures:
        raise RuntimeError(
            "v2 pressure fixture failed its native contracts: "
            + ", ".join(failures)
            + "; identity="
            + repr(bundle.get("entity_condition_certificate", {}).get("errors"))
            + "; prereg="
            + repr(bundle.get("preregistration_certificate", {}).get("errors"))
            + "; hydration="
            + repr(hydration.get("reason_codes"))
            + "; folded="
            + repr(folded.get("reason_codes"))
        )


def make_v2_pressure_submission(sub_dir: Path, run_id: str, mode: str) -> None:
    task = json.loads(
        (DATASET_DIR / "tasks" / V2_PRESSURE_TASK_ID / "task.json").read_text()
    )
    reference_sequence = task["scientific_target"]["entity"]["reference_sequence"]
    if reference_sequence != T4L_C54T_C97A_L99A_SEQUENCE:
        raise RuntimeError("fixture construct sequence drifted from the public task")
    plan = _v2_pressure_mode_plan(mode)
    if plan["raw_direction"] == "increase":
        reference_pattern = _v2_occupancy_pattern("low")
        variant_pattern = _v2_occupancy_pattern("high")
    elif plan["raw_direction"] == "decrease":
        reference_pattern = _v2_occupancy_pattern("high")
        variant_pattern = _v2_occupancy_pattern("low")
    else:
        reference_pattern = _v2_occupancy_pattern("ambiguous_reference")
        variant_pattern = _v2_occupancy_pattern("ambiguous_variant")

    reference_topology, reference_trajectory = _write_v2_pressure_run(
        sub_dir,
        system_id="ambient",
        occupancy=reference_pattern,
    )
    variant_topology, variant_trajectory = _write_v2_pressure_run(
        sub_dir,
        system_id="high-pressure",
        occupancy=variant_pattern,
    )
    intent = _v2_analysis_intent(task)
    study_index = _v2_study_index(
        reference_topology=reference_topology,
        reference_trajectory=reference_trajectory,
        variant_topology=variant_topology,
        variant_trajectory=variant_trajectory,
        reference_start=float(reference_pattern[0]),
        variant_start=float(variant_pattern[0]),
    )
    report = _v2_evidence_report(mode, plan)
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": V2_PRESSURE_TASK_ID,
        "status": "completed",
        "outputs": {
            "analysis_intent": "analysis_intent.json",
            "study_index": "study_index.json",
            "evidence_report": "evidence_report.json",
        },
        "limitations": [
            "Synthetic raw-verification fixture; no scientific result is claimed."
        ],
    }
    _write(sub_dir / "manifest.json", manifest)
    _write(sub_dir / "analysis_intent.json", intent)
    _write(sub_dir / "study_index.json", study_index)
    _write(sub_dir / "evidence_report.json", report)
    _write(
        sub_dir / "analysis" / "hydration.json",
        {"fixture": True, "native_verifier": "region_water_occupancy@1"},
    )
    _write(
        sub_dir / "analysis" / "folded-control.json",
        {"fixture": True, "native_verifier": "folded_state_retention@1"},
    )
    _write_v2_generic_harness_record(
        sub_dir,
        run_id=run_id,
        intent=intent,
        study_index=study_index,
    )
    _finalize_v2_report_from_raw(
        sub_dir,
        task=task,
        report=report,
        mode=mode,
    )
    _write(sub_dir / "evidence_report.json", report)


def make_study_submission(
    sub_dir: Path,
    run_id: str,
    mode: str,
    task_id: str,
) -> None:
    if task_id == V2_PRESSURE_TASK_ID:
        make_v2_pressure_submission(sub_dir, run_id, mode)
        return

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
            "study_index": "study_index.json",
            # Flat arrays remain solely for v0.2 deterministic-check coverage.
            "trajectories": systems["trajectories"],
            "topology": systems["topology"],
        },
        "limitations": [
            "Synthetic CI fixture; trajectory geometry is controlled, not real MD.",
        ],
    }
    study_index = {
        "schema_version": "1.0",
        "task_id": task_id,
        "systems": [
            {
                "role": "reference",
                "source": {
                    "type": "synthetic_fixture",
                    "id": f"{task_id}:reference",
                },
                "replicas": [
                    {
                        "replica_id": "reference_r1",
                        "topology": systems["topology"][0],
                        "trajectory": systems["trajectories"][0],
                    }
                ],
            },
            {
                "role": "variant",
                "source": {
                    "type": "synthetic_fixture",
                    "id": f"{task_id}:variant",
                },
                "replicas": [
                    {
                        "replica_id": "variant_r1",
                        "topology": systems["topology"][1],
                        "trajectory": systems["trajectories"][1],
                    }
                ],
            },
        ],
    }
    _write(sub_dir / "manifest.json", manifest)
    _write(sub_dir / "study_index.json", study_index)
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
    if not plan["fabricate"]:
        # Bind the fixture's reported values to the v0.3 verifier's exact
        # all-replica/block-average recomputation (not the legacy scorer's
        # numerically similar but independently implemented calculation).
        from mdclaw.benchmark.study_evidence import build_verified_evidence_packet

        packet = build_verified_evidence_packet(sub_dir, manifest, evidence)
        recomputed_by_id = {
            item["id"]: item.get("recomputed")
            for item in packet["evidence"]
            if item.get("recomputed") is not None
        }
        for item in evidence["evidence"]:
            recomputed = recomputed_by_id.get(item["id"])
            if recomputed:
                item["reference"] = recomputed["reference"]
                item["variant"] = recomputed["variant"]
                item["uncertainty"] = recomputed["uncertainty"]
                if item["metric"] == "ca_rmsf":
                    item["unit"] = "nm"
    _write(sub_dir / "evidence_report.json", evidence)


def grounded_judge_payload(
    sub_dir: Path,
    task_id: str,
    *,
    support_verdict: str = "supported",
    logical_grounding_supported: bool = True,
    evidence_packet_hash: str | None = None,
    cite_verified_evidence: bool = True,
) -> dict[str, Any]:
    """Build a scorer-consumable v1 packet-bound fake judge response."""
    task = json.loads((DATASET_DIR / "tasks" / task_id / "task.json").read_text())
    if task.get("evaluation_protocol") == "grounded_correct_v2":
        raise ValueError(
            "grounded_correct_v2 claim support is deterministic; "
            "do not synthesize an LLM judge payload"
        )

    from mdclaw.benchmark.study_evidence import (
        build_verified_evidence_packet,
        verified_evidence_hash,
    )

    manifest = json.loads((sub_dir / "manifest.json").read_text())
    evidence = json.loads((sub_dir / "evidence_report.json").read_text())
    packet = build_verified_evidence_packet(sub_dir, manifest, evidence)
    verified_ids = [
        str(item["id"])
        for item in packet["evidence"]
        if item.get("verification_status") == "verified"
    ]
    return {
        "enabled": True,
        "judge_model": "synthetic-fixture-judge",
        "temperature": 0.0,
        "rubric_version": "2.0",
        "scores": {
            rubric: 1.0
            for rubric in task["scoring"].get("llm_judge_rubrics", [])
        },
        "support_verdict": support_verdict,
        "logical_grounding_supported": logical_grounding_supported,
        "cited_evidence_ids": verified_ids[:1] if cite_verified_evidence else [],
        "evidence_packet_hash": (
            evidence_packet_hash
            if evidence_packet_hash is not None
            else verified_evidence_hash(packet)
        ),
        "violations": [],
        "rationale": {"reasoning_logic": "Synthetic fixture judgment."},
    }


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
