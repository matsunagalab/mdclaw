"""Opt-in SMO acceptance: immutable fixture import, then ordinary MDClaw CLI.

Run inside the dependency SIF with checkout PYTHONPATH on an allocated GPU.
No original node, event or shared cache is modified. The oriented prep fixture
is explicitly recorded as a copied/rotated baseline, never as a new prep run.
"""

from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-job", type=Path, required=True)
    parser.add_argument("--study-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=["setup", "run"], required=True)
    args = parser.parse_args()
    study = args.study_dir.resolve()
    logs = study / "validation"
    logs.mkdir(parents=True, exist_ok=True)
    counter = len(list(logs.glob("cli-*.json")))

    def cli(tool, **kw):
        nonlocal counter
        counter += 1
        cmd = [sys.executable, "-m", "mdclaw._cli", tool]
        for key, value in kw.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                cmd.append(flag if value else "--no-" + key.replace("_", "-"))
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                if value:
                    cmd.extend([flag, *value])
            elif value is not None:
                cmd.extend(
                    [flag, json.dumps(value) if isinstance(value, (list, dict)) else str(value)]
                )
        result = subprocess.run(cmd, capture_output=True, text=True)
        prefix = logs / f"cli-{counter:03d}-{tool}"
        prefix.with_suffix(".stderr").write_text(result.stderr)
        prefix.with_suffix(".json").write_text(result.stdout)
        with (logs / "commands.jsonl").open("a") as f:
            f.write(json.dumps(cmd) + "\n")
        r = json.loads(result.stdout)
        print(tool, {k: r.get(k) for k in ("success", "code", "node_id")}, flush=True)
        assert result.returncode == 0 and r.get("success"), r
        return r

    if args.phase == "setup":
        assert not (study / "fixture.json").exists(), "Use a fresh study for a new test"
        r = cli(
            "bootstrap_md_workflow",
            study_dir=study,
            question="Validate sulfur chemistry handoff on the fixed SMO fixture",
            solvent_regime="membrane",
            execution_mode="autonomous",
        )
        job = Path(r["job_dir"])
        cli("inspect_job", job_dir=job)
        from mdclaw._node import complete_node

        copied = {}
        parent = []
        for original, kind in [("source_001", "source"), ("prep_008", "prep")]:
            src = args.source_job / "nodes" / original
            saved = json.loads((src / "node.json").read_text())
            node = cli(
                "create_node",
                job_dir=job,
                node_type=kind,
                parent_node_ids=parent,
                label="Imported test fixture " + original,
            )["node_id"]
            dst = job / "nodes" / node
            for key, rel in saved["artifacts"].items():
                path = src / rel
                if not path.is_file():
                    continue
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                copied[str(path)] = {"sha256": digest(path), "copy": str(target)}
            # Source bundle candidate references must be local as well.
            if kind == "source":
                shutil.copytree(
                    src / "artifacts/candidates", dst / "artifacts/candidates", dirs_exist_ok=True
                )
            complete_node(
                str(job),
                node,
                saved["artifacts"],
                metadata={
                    **saved.get("metadata", {}),
                    "fixture_import": {"source_node": str(src), "reexecuted": False},
                },
            )
            parent = [node]
        # Make orientation a distinct prepared fixture branch, with identical
        # atom/chemical identities. Packing then uses the normal solv resolver.
        oriented = args.source_job / "nodes/solv_005/artifacts/oriented_protein.pdb"
        n = cli(
            "create_node",
            job_dir=job,
            node_type="prep",
            parent_node_ids=parent,
            label="Imported oriented prep fixture",
        )["node_id"]
        dest = job / "nodes" / n
        baseline = job / "nodes" / parent[0]
        shutil.copytree(baseline / "artifacts", dest / "artifacts", dirs_exist_ok=True)
        shutil.copy2(oriented, dest / "artifacts/merge/merged.pdb")
        copied[str(oriented)] = {
            "sha256": digest(oriented),
            "copy": str(dest / "artifacts/merge/merged.pdb"),
        }
        from openmm.app import PDBFile
        from mdclaw.amber.topology_validation import validate_loader_conservation

        conservation = validate_loader_conservation(
            PDBFile(str(baseline / "artifacts/merge/merged.pdb")).topology,
            PDBFile(str(oriented)).topology,
        )
        assert conservation["status"] == "passed", conservation
        complete_node(
            str(job),
            n,
            saved["artifacts"],
            metadata={
                "fixture_import": {
                    "source_node": str(oriented),
                    "reexecuted": False,
                    "orientation_only": True,
                },
                "source_structure_id": "candidate_001",
            },
        )
        fixture = {
            "job_dir": str(job),
            "prep_id": n,
            "inputs": copied,
            "orientation_inventory": conservation,
            "source_job": str(args.source_job),
            "scope": "solv -> topo -> min -> 0.1 ns NVT + 2 ns NPT",
            "thresholds": {
                "sg_angstrom": [1.8, 2.4],
                "backbone_cn_angstrom": [1.15, 1.7],
                "charge_abs_e": 0.001,
                "temperature_last10_mean_K": [290, 310],
                "density_last10_mean_g_ml": [0.95, 1.15],
            },
        }
        (study / "fixture.json").write_text(json.dumps(fixture, indent=2))
        cli("inspect_job", job_dir=job)
        return
    fixture = json.loads((study / "fixture.json").read_text())
    job = Path(fixture["job_dir"])
    cli("inspect_job", job_dir=job)
    import mdclaw

    repo = Path(mdclaw.__file__).resolve().parent.parent
    manifest = {
        "python": sys.executable,
        "mdclaw_import": mdclaw.__file__,
        "sif": os.environ.get("MDCLAW_SIF"),
        "versions": {
            p: importlib.metadata.version(p)
            for p in ("mdclaw", "openmm", "openff-pablo", "openmmforcefields")
        },
        "source_hashes": {
            str(p.relative_to(repo)): digest(p) for p in (repo / "mdclaw").rglob("*.py")
        },
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
    }
    (logs / "runtime.json").write_text(json.dumps(manifest, indent=2))
    parent = fixture["prep_id"]
    stages = [
        (
            "solv",
            "embed_in_membrane",
            dict(
                preoriented=True,
                lipids="POPC",
                ratio="1",
                dist=15.0,
                dist_wat=17.5,
                leaflet=23.0,
                salt=True,
                saltcon=0.15,
                water_model="opc",
                membrane_cache_mode="read-only",
            ),
        ),
        (
            "topo",
            "build_amber_system",
            dict(forcefield="ff19SB", water_model="opc", hmr=False, pablo_auto_download=False),
        ),
        (
            "min",
            "run_minimization",
            dict(
                max_iterations=5000,
                restraint_atoms="solute_heavy",
                restraint_force_constant=100.0,
                platform="CUDA",
                hmr=False,
            ),
        ),
        (
            "eq",
            "run_equilibration",
            dict(
                nvt_time_ns=0.1,
                npt_time_ns=2.0,
                temperature_kelvin=300.0,
                pressure_bar=1.0,
                timestep_fs=2.0,
                hmr=False,
                restraint_atoms="solute_heavy",
                restraint_force_constant=100.0,
                platform="CUDA",
                random_seed=20260908,
            ),
        ),
    ]
    ids = {}
    for kind, tool, params in stages:
        node = cli(
            "create_node",
            job_dir=job,
            node_type=kind,
            parent_node_ids=[parent],
            conditions=params,
            label="SMO fix acceptance",
        )["node_id"]
        ready = cli("explain_node", job_dir=job, node_id=node)
        assert ready.get("ready_to_run"), ready
        r = cli(tool, job_dir=job, node_id=node, **params)
        ids[kind] = node
        (logs / "stage_nodes.json").write_text(json.dumps(ids, indent=2))
        if kind == "solv":
            charge = r["statistics"]["neutralization"]
            assert charge["net_charge"] == 2, charge
            assert charge["anions_added"] - charge["cations_added"] == 2, charge
        if kind == "topo":
            assert abs(r["system_net_charge_e"]) < 0.001, r
            assert r["topology_validation"]["disulfides"]["expected_count"] == 9, r
        parent = node
    cli("inspect_job", job_dir=job)


if __name__ == "__main__":
    main()
