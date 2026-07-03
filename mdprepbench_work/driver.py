#!/usr/bin/env python3
"""MDPrepBench reference solver driver (MDClaw skills+CLI condition).

Runs one MDPrepBench task end-to-end through the MDClaw DAG CLI:
  source -> prep (-> mutation/PTM branch) -> solv|embed|skip -> topo -> min
then packages a submission (package_mdprep_submission), writes a harness
execution record (measured walltime per stage), and scores it
(score_benchmark_submission).

Run under the mdclaw conda env:
  conda run -n mdclaw python mdprepbench_work/driver.py --task P01 [P02 ...]
  conda run -n mdclaw python mdprepbench_work/driver.py --all
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / "mdprepbench_work"
TASKS_DIR = REPO / "benchmarks" / "mdprepbench" / "tasks"
RUN_ID = "mdclaw_ref_local"

# ---------------------------------------------------------------------------
# Per-task configuration.
# stages: keys consumed below. Missing keys use defaults.
#   pdb_id, fmt ("pdb"/"cif"), assembly_ids (list)
#   prep: dict of prepare_complex CLI kwargs (flags without values use True)
#   mutations: str -> create a mutant prep branch via create_mutated_structure
#   phosphorylate: dict of phosphorylate_residues kwargs (branch) 
#   regime: "explicit" (default) | "implicit" | "membrane"
#   solvate: dict of solvate_structure kwargs (explicit only)
#   membrane: dict of embed_in_membrane kwargs (membrane only)
#   topo: dict of build_amber_system kwargs
#   task_full_id: full task dir name
#   extra: task-specific handling flag
TASKS: dict[str, dict] = {
    "P01": {
        "task_full_id": "P01_prep_simple_monomer_t4l",
        "pdb_id": "2LZM", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P02": {
        "task_full_id": "P02_prep_1ake_chain_ap5",
        "pdb_id": "1AKE", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "include-ligand-resnames": ["AP5"],
                 "solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P03": {
        "task_full_id": "P03_prep_ligand_pose_t4l_benzene",
        "pdb_id": "181L", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "include-ligand-resnames": ["BNZ"],
                 "solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P04": {
        "task_full_id": "P04_prep_multi_ligand_filter_3pwb",
        "pdb_id": "3PWB", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "include-ligand-resnames": ["BEN", "GOL"],
                 "solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P05": {
        "task_full_id": "P05_prep_dap_dehydrogenase_nadp",
        "pdb_id": "1DAP", "fmt": "pdb",
        "prep": {"include-ligand-resnames": ["NDP"], "solvent-type": "implicit"},
        "regime": "implicit",
        "topo": {"forcefield": "ff14SB", "implicit-solvent": "GBn2"},
    },
    "P06": {
        "task_full_id": "P06_prep_calmodulin_ca_ions",
        "pdb_id": "1CLL", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "include-types": ["protein", "ion"]},
        "regime": "explicit",
    },
    "P07": {
        "task_full_id": "P07_prep_rna_crystallographic_ions",
        "pdb_id": "4RBQ", "fmt": "pdb",
        "prep": {"solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P08": {
        "task_full_id": "P08_prep_t4l_l99a_branch",
        "pdb_id": "2LZM", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "mutations": ["L99A"],
        "regime": "explicit",
        "extra": "wt_parent",
    },
    "P09": {
        "task_full_id": "P09_prep_t4l_double_mutant",
        "pdb_id": "2LZM", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "mutations": ["L99A", "M102Q"],
        "regime": "explicit",
    },
    "P10": {
        "task_full_id": "P10_prep_bpti_disulfides",
        "pdb_id": "5PTI", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "include-types": ["protein"]},
        "regime": "explicit",
        "extra": "disposition",
    },
    "P11": {
        "task_full_id": "P11_prep_site_protonation_t4l_glu11",
        "pdb_id": "2LZM", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "protonation-states": {"A:11": "GLH"}},
        "regime": "explicit",
    },
    "P12": {
        "task_full_id": "P12_prep_restore_deposited_sep",
        "pdb_id": "5K9P", "fmt": "pdb",
        "prep": {"solvent-type": "explicit"},
        "phosphorylate": {"restore-from-detection": True},
        "regime": "explicit",
    },
    "P13": {
        "task_full_id": "P13_prep_user_requested_sep",
        "pdb_id": "1UBQ", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "phosphorylate": {"sites-str": "A:20:SEP"},
        "regime": "explicit",
    },
    "P14": {
        "task_full_id": "P14_prep_glycoprotein_glycan",
        "pdb_id": "6YA2", "fmt": "cif",
        "prep": {"solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P15": {
        "task_full_id": "P15_prep_standard_dna",
        "pdb_id": "5MVQ", "fmt": "pdb",
        "prep": {"solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P16": {
        "task_full_id": "P16_prep_standard_rna",
        "pdb_id": "4RBQ", "fmt": "pdb",
        "prep": {"solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P17": {
        "task_full_id": "P17_prep_dna_duplex_neutralization",
        "pdb_id": "1BNA", "fmt": "pdb",
        "prep": {"solvent-type": "explicit"},
        "regime": "explicit",
    },
    "P18": {
        "task_full_id": "P18_prep_membrane_mixed_lipids",
        "pdb_id": "2LOP", "fmt": "pdb",
        "prep": {"solvent-type": "explicit", "source-model-index": 1},
        "regime": "membrane",
        "membrane": {"lipids": "POPC:POPE:CHL1", "ratio": "2:1:1",
                     "dist": "15.0", "dist-wat": "17.5", "saltcon": "0.15"},
        "topo": {"is-membrane": True},
    },
    "P19": {
        "task_full_id": "P19_prep_nmr_model_selection",
        "pdb_id": "2K39", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "source-model-index": 5},
        "regime": "explicit",
    },
    "P20": {
        "task_full_id": "P20_prep_terminal_capping",
        "pdb_id": "5AWL", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "n-terminal-cap": "ACE", "c-terminal-cap": "NME"},
        "regime": "explicit",
    },
    "P21": {
        "task_full_id": "P21_prep_cleanup_altloc_mse_numbering",
        "pdb_id": "4Q5T", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "include-types": ["protein"]},
        "regime": "explicit",
    },
    "P22": {
        "task_full_id": "P22_prep_forcefield_water_fidelity",
        "pdb_id": "2LZM", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "regime": "explicit",
        "solvate": {"water-model": "opc"},
        "topo": {"forcefield": "ff19SB", "water-model": "opc"},
        "ff": "ff19SB", "water": "opc",
    },
    "P23": {
        "task_full_id": "P23_prep_implicit_solvent_chignolin",
        "pdb_id": "5AWL", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "implicit"},
        "regime": "implicit",
        "topo": {"forcefield": "ff14SB", "implicit-solvent": "GBn2"},
    },
    "P24": {
        "task_full_id": "P24_prep_biological_assembly",
        "pdb_id": "1STP", "fmt": "cif", "assembly_ids": ["1"],
        "prep": {"solvent-type": "explicit",
                 "source-candidate-id": "candidate_002"},
        "regime": "explicit",
    },
    "P25": {
        "task_full_id": "P25_prep_kcl_ion_concentration",
        "pdb_id": "5AWL", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "regime": "explicit",
        "solvate": {"salt-c": "K+", "salt-a": "Cl-", "saltcon": "0.30"},
    },
    "P26": {
        "task_full_id": "P26_prep_zinc_metalloenzyme_2cba",
        "pdb_id": "2CBA", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "include-types": ["protein", "ion"]},
        "regime": "explicit",
    },
    "P27": {
        "task_full_id": "P27_prep_manganese_metalloenzyme_3cna",
        "pdb_id": "3CNA", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "include-types": ["protein", "ion"]},
        "regime": "explicit",
    },
    "P28": {
        "task_full_id": "P28_prep_kinase_inhibitor_gaff_1iep",
        "pdb_id": "1IEP", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "include-ligand-resnames": ["STI"],
                 "solvent-type": "explicit",
                 "ligand-smiles": {
                     "STI": "Cc1ccc(cc1Nc1nccc(n1)-c1cccnc1)NC(=O)"
                            "c1ccc(cc1)CN1CCN(C)CC1"}},
        "regime": "explicit",
    },
    "P29": {
        "task_full_id": "P29_prep_protein_protein_interface_1emv",
        "pdb_id": "1EMV", "fmt": "pdb",
        "prep": {"select-chains": ["A", "B"], "solvent-type": "explicit",
                 "include-types": ["protein"]},
        "regime": "explicit",
    },
    "P30": {
        "task_full_id": "P30_prep_protein_dna_zinc_1aay",
        "pdb_id": "1AAY", "fmt": "pdb",
        "prep": {"solvent-type": "explicit",
                 "include-types": ["protein", "nucleic", "ion"]},
        "regime": "explicit",
    },
    "P31": {
        "task_full_id": "P31_prep_his_protonation_hip_t4l",
        "pdb_id": "2LZM", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "protonation-states": {"A:31": "HIP"}},
        "regime": "explicit",
    },
    "P32": {
        "task_full_id": "P32_prep_sidechain_completion_1csp",
        "pdb_id": "1CSP", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit",
                 "include-types": ["protein"]},
        "regime": "explicit",
    },
    "P33": {
        "task_full_id": "P33_prep_physiological_nacl_ubiquitin",
        "pdb_id": "1UBQ", "fmt": "pdb",
        "prep": {"select-chains": ["A"], "solvent-type": "explicit"},
        "regime": "explicit",
        "solvate": {"salt-c": "Na+", "salt-a": "Cl-", "saltcon": "0.15"},
    },
    "P34": {
        "task_full_id": "P34_prep_anionic_lipid_membrane_2lop",
        "pdb_id": "2LOP", "fmt": "pdb",
        "prep": {"solvent-type": "explicit", "source-model-index": 1},
        "regime": "membrane",
        "membrane": {"lipids": "POPC:POPG", "ratio": "3:1",
                     "dist": "15.0", "dist-wat": "17.5", "saltcon": "0.15"},
        "topo": {"is-membrane": True},
    },
}


class StageError(Exception):
    pass


def _flags(kwargs: dict) -> list[str]:
    out: list[str] = []
    for k, v in kwargs.items():
        flag = f"--{k}"
        if v is True:
            out.append(flag)
        elif isinstance(v, list):
            out.append(flag)
            out.extend(str(x) for x in v)
        elif isinstance(v, dict):
            out.append(flag)
            out.append(json.dumps(v))
        else:
            out.append(flag)
            out.append(str(v))
    return out


class Runner:
    def __init__(self, task_key: str, log_dir: Path):
        self.task_key = task_key
        self.log_dir = log_dir
        self.records: list[dict] = []

    def run(self, stage: str, args: list[str]) -> dict:
        cmd = ["mdclaw", *args]
        t0 = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        dt = round(time.monotonic() - t0, 3)
        self.records.append({
            "stage": stage,
            "command": " ".join(cmd),
            "exit_code": proc.returncode,
            "walltime_seconds": dt,
        })
        (self.log_dir / f"{stage}.stdout").write_text(proc.stdout)
        (self.log_dir / f"{stage}.stderr").write_text(proc.stderr)
        # Parse trailing JSON object from stdout.
        payload = _parse_json(proc.stdout)
        if proc.returncode != 0 or (isinstance(payload, dict)
                                    and payload.get("success") is False):
            raise StageError(
                f"[{self.task_key}] stage {stage} failed (exit={proc.returncode}): "
                f"{(payload or {}).get('code') or (payload or {}).get('errors')}"
            )
        return payload or {}


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    # Find the last top-level JSON object.
    depth = 0
    end = None
    for i in range(len(text) - 1, -1, -1):
        c = text[i]
        if c == "}":
            if depth == 0:
                end = i
            depth += 1
        elif c == "{":
            depth -= 1
            if depth == 0 and end is not None:
                try:
                    return json.loads(text[i:end + 1])
                except json.JSONDecodeError:
                    end = None
                    depth = 0
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def solve_task(task_key: str) -> dict:
    cfg = TASKS[task_key]
    task_full = cfg["task_full_id"]
    study_dir = WORK / task_key
    if study_dir.exists():
        shutil.rmtree(study_dir)
    log_dir = WORK / "logs" / task_key
    log_dir.mkdir(parents=True, exist_ok=True)
    r = Runner(task_key, log_dir)
    result = {"task": task_key, "task_full_id": task_full, "status": "running"}

    try:
        # 1. bootstrap study
        regime = cfg.get("regime", "explicit")
        boot_regime = "membrane" if regime == "membrane" else regime
        r.run("bootstrap", [
            "bootstrap_md_workflow",
            "--study-dir", str(study_dir),
            "--question", f"MDPrepBench {task_full}",
            "--md-goal", f"MD-ready topology for {task_full}",
            "--solvent-regime", boot_regime,
            "--execution-mode", "autonomous",
        ])
        jd = study_dir / "jobs" / "main"

        # 2. source node + fetch
        src = r.run("create_source", [
            "create_node", "--job-dir", str(jd), "--node-type", "source",
        ])["node_id"]
        fetch_args = [
            "fetch_structure", "--source", "pdb",
            "--pdb-id", cfg["pdb_id"], "--format", cfg["fmt"],
            "--job-dir", str(jd), "--node-id", src,
        ]
        if cfg.get("assembly_ids"):
            fetch_args += ["--assembly-mode", "ids", "--assembly-ids",
                           *cfg["assembly_ids"]]
        r.run("source", fetch_args)

        # 3. prep node
        prep = r.run("create_prep", [
            "create_node", "--job-dir", str(jd), "--node-type", "prep",
        ])["node_id"]
        prep_out = r.run("prep", [
            "prepare_complex", "--job-dir", str(jd), "--node-id", prep,
            *_flags(cfg.get("prep", {})),
        ])
        base_prep = prep
        wt_merged = prep_out.get("merged_pdb")

        # 3b. mutation branch
        if cfg.get("mutations"):
            mut_prep = r.run("create_mut_prep", [
                "create_node", "--job-dir", str(jd), "--node-type", "prep",
                "--parent-node-ids", base_prep,
            ])["node_id"]
            r.run("mutate", [
                "create_mutated_structure", "--job-dir", str(jd),
                "--node-id", mut_prep, *_flags({"mutations": cfg["mutations"]}),
            ])
            base_prep = mut_prep

        # 3c. phosphorylation branch
        if cfg.get("phosphorylate"):
            ptm_prep = r.run("create_ptm_prep", [
                "create_node", "--job-dir", str(jd), "--node-type", "prep",
                "--parent-node-ids", base_prep,
            ])["node_id"]
            r.run("phosphorylate", [
                "phosphorylate_residues", "--job-dir", str(jd),
                "--node-id", ptm_prep, *_flags(cfg["phosphorylate"]),
            ])
            base_prep = ptm_prep

        # 4. solvation / membrane / skip
        box_dims = None
        if regime == "explicit":
            solv = r.run("create_solv", [
                "create_node", "--job-dir", str(jd), "--node-type", "solv",
                "--parent-node-ids", base_prep,
            ])["node_id"]
            solv_out = r.run("solv", [
                "solvate_structure", "--job-dir", str(jd), "--node-id", solv,
                *_flags(cfg.get("solvate", {})),
            ])
            box_dims = solv_out.get("box_dimensions")
        elif regime == "membrane":
            solv = r.run("create_solv", [
                "create_node", "--job-dir", str(jd), "--node-type", "solv",
                "--parent-node-ids", base_prep,
            ])["node_id"]
            r.run("solv", [
                "embed_in_membrane", "--job-dir", str(jd), "--node-id", solv,
                *_flags(cfg.get("membrane", {})),
            ])
        # implicit: no solv node; topo parents prep directly

        # 5. topo node
        topo_parent = solv if regime in ("explicit", "membrane") else base_prep
        topo = r.run("create_topo", [
            "create_node", "--job-dir", str(jd), "--node-type", "topo",
            "--parent-node-ids", topo_parent,
        ])["node_id"]
        topo_args = ["build_amber_system", "--job-dir", str(jd),
                     "--node-id", topo, *_flags(cfg.get("topo", {}))]
        if box_dims and regime == "explicit":
            topo_args += ["--box-dimensions", json.dumps(box_dims)]
        r.run("topo", topo_args)

        # 6. min node
        minn = r.run("create_min", [
            "create_node", "--job-dir", str(jd), "--node-type", "min",
            "--parent-node-ids", topo,
        ])["node_id"]
        min_kwargs = {}
        if regime == "membrane":
            min_kwargs["is-membrane"] = True
        if regime == "implicit":
            min_kwargs["implicit-solvent"] = cfg["topo"]["implicit-solvent"]
        r.run("min", [
            "run_minimization", "--job-dir", str(jd), "--node-id", minn,
            *_flags(min_kwargs),
        ])

        # 7. harness record
        sub_parent = WORK / "submissions" / task_key
        sub_parent.mkdir(parents=True, exist_ok=True)
        harness = {
            "schema_version": "1.0",
            "run_id": RUN_ID,
            "task_id": task_full,
            "records": r.records,
        }
        (sub_parent / "harness_execution.json").write_text(
            json.dumps(harness, indent=2) + "\n")

        # 8. package submission
        sub_dir = sub_parent / "submission"
        pkg_args = [
            "package_mdprep_submission",
            "--submission-dir", str(sub_dir),
            "--task-id", task_full,
            "--job-dir", str(jd), "--node-id", minn,
            "--run-id", RUN_ID,
            "--agent", "cursor-mdclaw-ref",
            "--backend", "mdclaw-dag",
            "--harness", "cursor-driver",
            "--force-field", cfg.get("ff", "ff19SB" if regime == "explicit" else "ff14SB"),
            "--water-model", cfg.get("water", "opc" if regime == "explicit" else "none"),
            "--solvent-model", regime,
        ]
        # task-specific extra outputs
        if cfg.get("extra") == "wt_parent" and wt_merged:
            wt_file = sub_parent / "wt_prepared_structure.pdb"
            shutil.copy2(wt_merged, wt_file)
            pkg_args += ["--extra-output-files",
                         f"parent_prepared_structure={wt_file}"]
        if cfg.get("extra") == "disposition":
            extras = _collect_disposition(jd, base_prep, sub_parent)
            if extras:
                pkg_args += ["--extra-output-files", *extras]
        r.run("package", pkg_args)

        # 9. score
        r.run("score", [
            "score_benchmark_submission",
            "--task-file", str(TASKS_DIR / task_full / "task.json"),
            "--submission-dir", str(sub_dir),
            "--run-id", RUN_ID,
            "--harness-record-file", str(sub_parent / "harness_execution.json"),
            "--output-file", str(sub_parent / "score.json"),
        ])
        sc = json.loads((sub_parent / "score.json").read_text())
        det = {c["check_id"]: c for c in sc.get("deterministic_checks", [])}
        failed = [cid for cid, c in det.items() if not c.get("passed")]
        # corrected clash count: skip non-interacting (eps<=0) particles.
        corrected = _corrected_clashes(sub_dir)
        result["status"] = "scored"
        result["stock_status"] = sc.get("status")
        result["stock_weighted_total"] = sc.get("weighted_total")
        result["stock_preparation"] = sc.get("scores", {}).get("preparation")
        result["capability_scores"] = sc.get("capability_scores")
        result["failed_checks"] = failed
        result["corrected_clashes"] = corrected
        result["corrected_gate_pass"] = (corrected == 0)
    except StageError as e:
        result["status"] = "failed"
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
    result["records"] = r.records
    (WORK / "logs" / task_key / "result.json").write_text(
        json.dumps(result, indent=2) + "\n")
    return result


_TWO16 = 2 ** (1 / 6)


def _corrected_clashes(sub_dir: Path, frac: float = 0.6) -> int | None:
    """Scorer-style clash count but skipping non-interacting (eps<=0) atoms.

    The stock scorer counts eps=0 polar hydrogens (OpenMM placeholder
    sigma=1.0 nm) as clashes; those atoms have no van der Waals radius, so
    this recomputation excludes them to show the physically meaningful count.
    """
    import math
    from openmm import NonbondedForce, XmlSerializer, unit
    try:
        system = XmlSerializer.deserialize(
            (sub_dir / "topology" / "system.xml").read_text())
        state = XmlSerializer.deserialize(
            (sub_dir / "topology" / "state.xml").read_text())
    except Exception:  # noqa: BLE001
        return None
    coords = state.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
    nb = next((f for f in system.getForces()
               if isinstance(f, NonbondedForce)), None)
    if nb is None:
        return None
    n = system.getNumParticles()
    sig = [0.0] * n
    active = [False] * n
    for i in range(n):
        _q, s, e = nb.getParticleParameters(i)
        sv = s.value_in_unit(unit.nanometer)
        ev = e.value_in_unit(unit.kilojoule_per_mole)
        virt = system.isVirtualSite(i)
        sig[i] = sv
        active[i] = (not virt) and sv > 0.0 and ev > 0.0
    exc = set()
    for k in range(nb.getNumExceptions()):
        p1, p2, *_ = nb.getExceptionParameters(k)
        exc.add((min(p1, p2), max(p1, p2)))
    max_sig = max((sig[i] for i in range(n) if active[i]), default=0.0)
    cell = frac * max_sig * _TWO16
    if cell <= 0.0:
        return 0
    inv = 1.0 / cell
    grid: dict = {}
    for i in range(n):
        if not active[i]:
            continue
        x, y, z = coords[i]
        key = (math.floor(x * inv), math.floor(y * inv), math.floor(z * inv))
        grid.setdefault(key, []).append(i)
    offs = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1)
            for c in (-1, 0, 1)]
    clashes = 0
    for (cx, cy, cz), mem in grid.items():
        for o in offs:
            nb2 = grid.get((cx + o[0], cy + o[1], cz + o[2]))
            if not nb2:
                continue
            for i in mem:
                for j in nb2:
                    if j <= i or (i, j) in exc:
                        continue
                    xi, yi, zi = coords[i]
                    xj, yj, zj = coords[j]
                    d2 = (xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2
                    rmin = ((sig[i] + sig[j]) * 0.5) * _TWO16
                    th = frac * rmin
                    if d2 < th * th:
                        clashes += 1
    return clashes


def _collect_disposition(jd: Path, prep_node: str, dest: Path) -> list[str]:
    """Find component_disposition.json / excluded_components.json in prep artifacts."""
    out = []
    art = jd / "nodes" / prep_node / "artifacts"
    cd = list(art.rglob("component_disposition.json"))
    if cd:
        d = dest / "component_disposition.json"
        shutil.copy2(cd[0], d)
        out.append(f"component_disposition={d}")
    ec = list(art.rglob("excluded_components.json"))
    if ec:
        d = dest / "excluded_components.json"
        shutil.copy2(ec[0], d)
        out.append(f"excluded_components={d}")
    elif cd:
        # synthesize excluded_components from disposition
        try:
            disp = json.loads(cd[0].read_text())
        except Exception:  # noqa: BLE001
            disp = {}
        excluded = []
        if isinstance(disp, dict):
            comps = disp.get("components") or disp.get("dispositions") or []
            if isinstance(comps, list):
                excluded = [c for c in comps
                            if isinstance(c, dict) and str(c.get("disposition", "")).lower()
                            in ("excluded", "dropped", "removed")]
        d = dest / "excluded_components.json"
        d.write_text(json.dumps({"schema_version": "1.0",
                                 "excluded_components": excluded}, indent=2) + "\n")
        out.append(f"excluded_components={d}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", nargs="*", default=[])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    keys = list(TASKS.keys()) if args.all else args.task
    if not keys:
        print("no tasks specified", file=sys.stderr)
        return 2
    results = []
    for k in keys:
        print(f"=== {k} ===", flush=True)
        res = solve_task(k)
        print(f"    -> {res['status']} stock_prep={res.get('stock_preparation')} "
              f"corrected_clashes={res.get('corrected_clashes')} "
              f"failed={res.get('failed_checks')} {res.get('error','')}",
              flush=True)
        results.append(res)
    (WORK / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
