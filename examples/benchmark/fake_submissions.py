#!/usr/bin/env python
"""Generate hand-built submissions for all 9 v1.0 pilot tasks.

Used by Phase 5 validation to verify the framework end-to-end without
requiring real MD compute. Two modes:

    --mode honest   passing answers where deterministic alone is enough
                    (T01, T05, T08 still require real MD compute and are
                    marked status=partial)
    --mode wrong    deliberate wrong answers + missing artifacts
                    (the framework must score these low)

Usage:
    python examples/benchmark/fake_submissions.py \\
        --run-dir benchmark_runs/v10_dryrun_container \\
        --mode honest
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


def _write(path: Path, payload: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    else:
        path.write_text(str(payload))


def _common_provenance(run_id: str, task_id: str, mode: str) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "agent": {"name": "fake_submissions.py", "mode": mode},
        "backend": {"name": "synthetic-fixture", "version": "1.0"},
        "harness": {"name": "fake_submissions.py"},
        "scripts": [],
        "raw_outputs": [],
    }


def make_t01(sub_dir: Path, run_id: str, mode: str):
    has_evidence = (mode == "honest")
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T01_engine_smoke",
        "status": "partial" if has_evidence else "completed",
        "outputs": {"trajectories": ["../work/traj.dcd"]},
        "limitations": [
            "fake_submissions.py — no real MD trajectory was emitted; "
            "trajectory_rescan check will fail by design.",
        ],
    })
    _write(sub_dir / "metrics.json", {
        "schema_version": "1.0", "task_id": "T01_engine_smoke",
        "execution": {
            "completed": has_evidence,
            "finite_energy": has_evidence,
            "no_nan": has_evidence,
        },
    })
    _write(sub_dir / "provenance.json", _common_provenance(run_id, "T01_engine_smoke", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0", "task_id": "T01_engine_smoke",
        "summary": "Synthetic submission — no real MD performed.",
        "limitations": ["No trajectory artifact; trajectory_rescan will fail."],
    })


def make_t02(sub_dir: Path, run_id: str, mode: str):
    code = ("metal_containing_ligand_blocked" if mode == "honest"
            else "low_confidence_charge")  # wrong code on purpose
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T02_prep_metalloenzyme_guardrail",
        "status": "failed",
        "outputs": {},
        "limitations": ["intentional structured refusal"],
    })
    _write(sub_dir / "metrics.json", {
        "schema_version": "1.0",
        "preparation": {"guardrail_code": code, "status": "blocked_by_guardrail"},
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T02_prep_metalloenzyme_guardrail", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "summary": (
            "Refused to parameterize the Zn metalloenzyme PDB 2CBA under GAFF. "
            "Zn coordination cannot be represented correctly without ZAFF or "
            "MCPB.py, so a silent build would emit a chemically wrong system."
        ),
        "decision": "structured_refusal",
        "guardrail_code": code,
        "effect": {"direction": None, "confidence": None},
        "limitations": [
            "No prepared_structure.pdb is emitted by design.",
            "Re-run with explicit metal parameterization to lift this guardrail.",
        ],
    })


def make_t03(sub_dir: Path, run_id: str, mode: str):
    rmsd = 0.42 if mode == "honest" else 1.5
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T03_prep_ligand_pose_t4l_benzene",
        "status": "partial",
        "outputs": {"prepared_structure": "prepared_structure.pdb"},
        "limitations": ["fake submission; rmsd reported but no real prep run"],
    })
    _write(sub_dir / "metrics.json", {
        "schema_version": "1.0",
        "preparation": {"ligand_heavy_atom_rmsd_angstrom": rmsd,
                        "ligand_id": "BNZ"},
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T03_prep_ligand_pose_t4l_benzene", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "summary": "Synthetic prep; rmsd reported by hand.",
        "limitations": ["No real prep ran; rmsd_recompute will fail (no prepared_structure)."],
    })
    # provide a trivial PDB so the file-presence portion of any check passes,
    # but the rmsd_recompute scorer side will not find BNZ atoms (intentional).
    _write(sub_dir / "prepared_structure.pdb",
           "REMARK 1 Synthetic placeholder for fake_submissions.py\nEND\n")


def make_t04(sub_dir: Path, run_id: str, mode: str):
    has_evidence = (mode == "honest")
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T04_exec_short_protein_md",
        "status": "partial" if has_evidence else "completed",
        "outputs": {"trajectories": ["../work/traj_production.dcd"]},
        "limitations": ["fake submission — no production trajectory exists; rescan will fail"],
    })
    _write(sub_dir / "metrics.json", {
        "schema_version": "1.0",
        "execution": {
            "completed": has_evidence, "finite_energy": has_evidence,
            "no_nan": has_evidence, "simulated_time_ps": 100.0,
        },
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T04_exec_short_protein_md", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "summary": "Synthetic; no MD run.",
        "limitations": ["No real trajectory."],
    })


def make_t05(sub_dir: Path, run_id: str, mode: str):
    has_evidence = (mode == "honest")
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T05_exec_restart_continue",
        "status": "partial" if has_evidence else "completed",
        "outputs": {"trajectories": ["../work/traj_concat.dcd"]},
        "limitations": ["fake submission; trajectory rescan will fail"],
    })
    _write(sub_dir / "metrics.json", {
        "schema_version": "1.0",
        "execution": {"restart_steps_contiguous": has_evidence},
        "analysis": {"concat_frames_match_sources": has_evidence},
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T05_exec_restart_continue", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "summary": "Synthetic restart submission.",
        "limitations": ["No real trajectory pair."],
    })


def make_t06(sub_dir: Path, run_id: str, mode: str):
    direction = "destabilizing" if mode == "honest" else "stabilizing"
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
        "outputs": {"evidence_report": "evidence_report.json"},
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T06_answer_stability_t4l_l99a", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0", "task_id": "T06_answer_stability_t4l_l99a",
        "summary": "Literature-anchored answer for T4L L99A vs WT.",
        "effect": {"direction": direction, "confidence": "high"},
        "evidence": {
            "reasoning": (
                "L99A is a canonical cavity-creating mutation in the buried "
                "hydrophobic core of T4 lysozyme. Cavity-creating mutations "
                "lose ~2-4 kcal/mol of packing stability unless rescued by a "
                "bound ligand; the WT vs L99A comparison is one of the most "
                "thoroughly characterized stability benchmarks in the "
                "structural-biology literature."
            ),
            "citations": [
                {
                    "doi": "10.1126/science.1553543",
                    "citation": (
                        "Eriksson AE et al. Science 1992 — cavity-creating "
                        "mutation, +4-5 kcal/mol destabilization."
                    ),
                },
                {"source": "FireProtDB",
                 "note": "single-mutation ΔΔG records confirm destabilization"},
            ],
        },
        "limitations": [
            "No fresh MD run performed; answer is literature-anchored.",
            "Confidence reflects the experimental literature, not new simulation.",
        ],
    })


def make_t07(sub_dir: Path, run_id: str, mode: str):
    direction = "weakened_binding" if mode == "honest" else "strengthened_binding"
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T07_answer_ppi_hotspot_barnase_d39a",
        "status": "completed",
        "outputs": {"evidence_report": "evidence_report.json"},
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T07_answer_ppi_hotspot_barnase_d39a", mode))
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "summary": "Literature-anchored answer for barnase D39A vs WT against barstar.",
        "effect": {"direction": direction, "confidence": "high"},
        "evidence": {
            "reasoning": (
                "D39 is the canonical hot-spot residue at the barnase-barstar "
                "interface. The Schreiber & Fersht 1995 alanine scan reports "
                "the largest single-mutation ΔΔG_binding at this position, "
                "and SKEMPI's curated records confirm a substantial loss of "
                "affinity for D39A."
            ),
            "citations": [
                {
                    "doi": "10.1006/jmbi.1995.0237",
                    "citation": (
                        "Schreiber & Fersht 1995 — alanine-scan of the "
                        "barnase-barstar interface."
                    ),
                },
                {"source": "SKEMPI",
                 "note": "curated mutation effects on PPI binding"},
            ],
        },
        "limitations": [
            "No MM/PBSA or FEP run; answer is literature-anchored only.",
            "Confidence reflects published alanine-scan, not new computation.",
        ],
    })


def make_t08(sub_dir: Path, run_id: str, mode: str):
    fig_dir = sub_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    # PNG magic + padding so each file passes the >= 1024 byte integrity check.
    # The bytes after the header are ignored by the scorer (which only checks
    # the magic and the file size); the figure isn't actually rendered.
    stub_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
    for name in ("rmsd.png", "rmsf.png", "contacts.png"):
        (fig_dir / name).write_bytes(stub_png)
    captions_pass = mode == "honest"
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T08_communicate_t4l_dynamics",
        "status": "partial",
        "outputs": {"figures": ["figures/rmsd.png", "figures/rmsf.png", "figures/contacts.png"]},
        "limitations": ["No real trajectory analyzed."],
    })
    _write(sub_dir / "metrics.json", {
        "schema_version": "1.0",
        "analysis": {
            "rmsd": {"mean_angstrom": 1.21},
            "rmsf": {"mean_angstrom": 0.57},
            "contacts": {"high_occupancy_pairs_above_0.5": 476},
        },
    })
    _write(sub_dir / "provenance.json", _common_provenance(
        run_id, "T08_communicate_t4l_dynamics", mode))
    captions = (
        [{"path": "figures/rmsd.png", "caption": "RMSD mean 1.21 angstrom"},
         {"path": "figures/rmsf.png", "caption": "RMSF mean 0.57 angstrom"},
         {"path": "figures/contacts.png", "caption": "476 contact pairs"}]
        if captions_pass else
        [{"path": "figures/rmsd.png", "caption": "RMSD mean 9.99 angstrom"}]
    )
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "summary": (
            "Synthetic dynamics figures for T4L WT. RMSD, RMSF, and CA-CA "
            "contact metrics are reported in metrics.json; captions reference "
            "the matching numeric values."
        ),
        "figure_captions": captions,
        "limitations": [
            "Stub PNG headers, not real rendered figures.",
            "Metrics are synthetic placeholders, not derived from new MD.",
        ],
    })


def make_t09(sub_dir: Path, run_id: str, mode: str):
    direction = "destabilizing" if mode == "honest" else "stabilizing"
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0", "run_id": run_id,
        "task_id": "T09_study_t4l_wt_vs_l99a_methods",
        "status": "partial",
        "outputs": {
            "methods": "methods.md",
            "evidence_report": "evidence_report.json",
            "decision_log": "decision_log.jsonl",
        },
        "limitations": ["dry_run; no real MD run"],
    })
    _write(sub_dir / "methods.md", (
        "# T4L WT vs L99A — methods plan\n"
        "\n"
        "## Methods\n"
        "\n"
        "Synthetic methods bundle for the v1.0 dry-run fixture. The WT system "
        "starts from PDB 2LZM; the L99A mutant is generated by an in-silico "
        "substitution at residue 99. Both systems would be parameterized with "
        "ff14SB / TIP3P, neutralized with 0.15 M NaCl in a truncated "
        "octahedral box, minimized for 200 steps, equilibrated 100 ps NVT "
        "then 100 ps NPT, and run for 100 ns of production NPT MD per role.\n"
        "\n"
        "## Limitations\n"
        "\n"
        "No real simulation ran in this submission; the bundle is a methods "
        "draft only. Downstream analysis (RMSF, contact maps, B-factor "
        "comparison) is described but not executed. effect.direction is "
        "anchored to the Eriksson 1992 destabilization literature.\n"
    ))
    _write(sub_dir / "decision_log.jsonl", "")
    _write(sub_dir / "provenance.json", {
        **_common_provenance(run_id, "T09_study_t4l_wt_vs_l99a_methods", mode),
        "study": {
            "name": "T4L WT vs L99A (synthetic)",
            "roles": [
                {"role": "wt", "label": "T4L WT", "executed_in_this_submission": False},
                {"role": "mutant", "label": "T4L L99A", "executed_in_this_submission": False},
            ],
        },
    })
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0", "task_id": "T09_study_t4l_wt_vs_l99a_methods",
        "summary": (
            "Synthetic methods bundle for the T4L WT vs L99A comparison. "
            "Provenance lists both wt and mutant roles; methods.md describes "
            "the planned protocol; effect.direction is literature-anchored."
        ),
        "effect": {"direction": direction, "confidence": "high"},
        "evidence": {
            "citations": [
                {
                    "doi": "10.1126/science.1553543",
                    "citation": "Eriksson AE et al. Science 1992 — L99A destabilization.",
                },
            ],
        },
        "limitations": [
            "No MD ran in this submission.",
            "effect.direction is literature-anchored, not derived from new simulation.",
        ],
    })


GENERATORS = {
    "T01_engine_smoke": make_t01,
    "T02_prep_metalloenzyme_guardrail": make_t02,
    "T03_prep_ligand_pose_t4l_benzene": make_t03,
    "T04_exec_short_protein_md": make_t04,
    "T05_exec_restart_continue": make_t05,
    "T06_answer_stability_t4l_l99a": make_t06,
    "T07_answer_ppi_hotspot_barnase_d39a": make_t07,
    "T08_communicate_t4l_dynamics": make_t08,
    "T09_study_t4l_wt_vs_l99a_methods": make_t09,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=("honest", "wrong"), default="honest")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    tasks_dir = run_dir / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)
    tasks_dir.mkdir(parents=True)

    for task_id, fn in GENERATORS.items():
        sub_dir = tasks_dir / task_id / "submission"
        fn(sub_dir, run_id=run_dir.name, mode=args.mode)

    print(f"[ok] {len(GENERATORS)} fake submissions written under {tasks_dir} (mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
