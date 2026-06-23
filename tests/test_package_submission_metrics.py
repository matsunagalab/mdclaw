"""Guard the package_openmm_submission metrics contract.

The packager is the intended one-command path to a scorer-valid submission, so
its metrics.json must match the scorer's canonical preparation.* json_paths.
Force-field/water choices are now provenance declarations plus artifact
rescans, not scored ``metrics.preparation`` self-report.

These tests build a minimal real OpenMM triple, run the packager, and assert the
emitted metrics. Run with: pytest tests/test_package_submission_metrics.py -v
"""

import json
import shutil
from pathlib import Path

import pytest


def _make_openmm_triple(tmp_path):
    pytest.importorskip("openmm")
    from openmm import (
        Context,
        NonbondedForce,
        System,
        VerletIntegrator,
        Vec3,
        XmlSerializer,
        unit,
    )
    from openmm.app import Element, PDBFile, Topology

    system = System()
    system.addParticle(12.0)
    nb = NonbondedForce()
    nb.addParticle(0.0, 0.1, 0.0)
    system.addForce(nb)
    integrator = VerletIntegrator(1.0 * unit.femtosecond)
    context = Context(system, integrator)
    context.setPositions([Vec3(0, 0, 0)] * unit.nanometer)
    state = context.getState(getPositions=True)

    sys_xml = tmp_path / "system.xml"
    sys_xml.write_text(XmlSerializer.serialize(system))
    state_xml = tmp_path / "state.xml"
    state_xml.write_text(XmlSerializer.serialize(state))

    top = Topology()
    chain = top.addChain()
    res = top.addResidue("ALA", chain)
    top.addAtom("CA", Element.getBySymbol("C"), res)
    topo_pdb = tmp_path / "topology.pdb"
    with open(topo_pdb, "w") as fh:
        PDBFile.writeFile(top, [Vec3(0, 0, 0)] * unit.nanometer, fh)
    return str(sys_xml), str(state_xml), str(topo_pdb)


def _package(tmp_path, **kwargs):
    from mdclaw.benchmark.cli import package_openmm_submission

    sys_xml, state_xml, topo_pdb = _make_openmm_triple(tmp_path)
    sub = tmp_path / "submission"
    res = package_openmm_submission(
        submission_dir=str(sub),
        task_id="P10_prep_bpti_disulfides",
        system_xml_file=sys_xml,
        topology_pdb_file=topo_pdb,
        state_xml_file=state_xml,
        **kwargs,
    )
    assert res["success"], res
    return {
        "metrics": json.loads((sub / "metrics.json").read_text()),
        "provenance": json.loads((sub / "provenance.json").read_text()),
        "result": res,
        "submission": sub,
    }


def test_packager_keeps_forcefield_declarations_out_of_scored_metrics(tmp_path):
    packaged = _package(tmp_path, force_field="ff19SB", water_model="opc")
    prep = packaged["metrics"]["preparation"]
    assert "forcefield" not in prep
    assert "force_field" not in prep
    assert "water_model" not in prep
    assert packaged["provenance"]["declared_preparation"] == {
        "force_field": "ff19SB",
        "solvent_model": "unspecified",
        "water_model": "opc",
    }


def test_packager_derives_only_scorer_consumed_preparation_keys(tmp_path):
    summary = tmp_path / "prep_summary.json"
    summary.write_text(json.dumps({
        "preparation_summary": {
            "assembly_id": "1",
            "assembly_chain_identity_map": [
                {
                    "source_pdb_id": "1STP",
                    "assembly_id": "1",
                    "source_auth_asym_id": "A",
                    "source_label_asym_id": "A",
                    "operator_id": "1",
                    "output_chain_id": "A",
                    "naming_policy": "preserved",
                },
            ],
            "disulfide_pairs": [{"cys1": 5, "cys2": 55}],
            "component_disposition_recorded": True,
        }
    }))
    packaged = _package(
        tmp_path, force_field="ff19SB", water_model="opc",
        solvent_model="explicit", preparation_summary_file=str(summary),
    )
    prep = packaged["metrics"]["preparation"]
    assert prep == {}
    assert "disulfide_pairs" not in prep
    assert "component_disposition_recorded" not in prep
    assert any(
        "ignored non-scored preparation_summary" in warning
        for warning in packaged["result"]["warnings"]
    )


def test_packager_metrics_keys_are_scorer_canonical(tmp_path):
    # every preparation key the packager emits must be a known scorer json_path
    # key (catches a future force_field-style drift). Derived from the public
    # task_specs preparation.* paths.
    import re
    from pathlib import Path
    specs = Path(__file__).resolve().parent.parent / (
        "benchmarks/mdprepbench/task_specs/tasks"
    )
    if not specs.is_dir():
        pytest.skip("task_specs not present in this checkout")
    canonical = set()
    for f in specs.glob("*.json"):
        canonical |= set(re.findall(r"preparation\.([A-Za-z_]+)", f.read_text()))
    summary = tmp_path / "prep_summary.json"
    summary.write_text(json.dumps({
        "preparation_summary": {
            "net_charge": 0.0,
            "unknown_benchmark_flag": True,
        }
    }))
    metrics = _package(
        tmp_path, force_field="ff19SB", water_model="opc",
        preparation_summary_file=str(summary),
    )["metrics"]
    assert metrics["preparation"] == {}
    for key in metrics["preparation"]:
        assert key in canonical, (
            f"packager emits preparation.{key!r} which is not a scorer "
            "json_path key - fix the key name or the scorer contract"
        )


def test_packager_writes_raw_output_hashes(tmp_path):
    from mdclaw.benchmark import integrity

    packaged = _package(tmp_path)
    sub = packaged["submission"]
    manifest = json.loads((sub / "manifest.json").read_text())
    provenance = json.loads((sub / "provenance.json").read_text())

    warnings = integrity.required_raw_output_hash_warnings(
        sub,
        manifest,
        provenance,
    )
    warnings.extend(integrity.verify_provenance_hashes(sub, provenance))

    assert warnings == []
    hashed_paths = {entry["path"] for entry in provenance["raw_outputs"]}
    assert "manifest.json" in hashed_paths
    assert "topology/state.xml" in hashed_paths
    assert "minimized_structure.pdb" in hashed_paths


def test_hash_integrity_detects_post_package_edits(tmp_path):
    from mdclaw.benchmark import integrity

    packaged = _package(tmp_path)
    sub = packaged["submission"]
    (sub / "minimization_report.json").write_text("{}\n")
    manifest = json.loads((sub / "manifest.json").read_text())
    provenance = json.loads((sub / "provenance.json").read_text())

    warnings = integrity.required_raw_output_hash_warnings(
        sub,
        manifest,
        provenance,
    )
    warnings.extend(integrity.verify_provenance_hashes(sub, provenance))

    assert any("md5 mismatch" in warning for warning in warnings)


def test_state_consistency_detects_mixed_minimized_structure(tmp_path):
    from mdclaw.benchmark import integrity

    packaged = _package(tmp_path)
    sub = packaged["submission"]
    (sub / "minimized_structure.pdb").write_text(
        "ATOM      1  CA  ALA A   1      10.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    manifest = json.loads((sub / "manifest.json").read_text())

    warnings = integrity.openmm_minimized_state_consistency_warnings(
        sub,
        manifest,
    )

    assert any("not exported from topology/state.xml" in warning for warning in warnings)


def test_package_mdprep_submission_uses_min_node_state(tmp_path):
    from mdclaw._node import complete_node, create_node
    from mdclaw.benchmark.cli import package_mdprep_submission

    sys_xml, state_xml, topo_pdb = _make_openmm_triple(tmp_path)
    job_dir = tmp_path / "study" / "jobs" / "main"
    prep = create_node(str(job_dir), "prep")
    assert prep["success"], prep
    prep_dir = job_dir / "nodes" / prep["node_id"]
    prep_merged = prep_dir / "artifacts" / "merge" / "merged.pdb"
    prep_merged.parent.mkdir(parents=True, exist_ok=True)
    prep_merged.write_text(Path(topo_pdb).read_text())
    complete_node(
        str(job_dir),
        prep["node_id"],
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
    )

    topo = create_node(
        str(job_dir),
        "topo",
        parent_node_ids=[prep["node_id"]],
    )
    assert topo["success"], topo
    topo_dir = job_dir / "nodes" / topo["node_id"] / "artifacts"
    topo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sys_xml, topo_dir / "system.xml")
    shutil.copy2(topo_pdb, topo_dir / "topology.pdb")
    complete_node(
        str(job_dir),
        topo["node_id"],
        artifacts={
            "system_xml": "artifacts/system.xml",
            "topology_pdb": "artifacts/topology.pdb",
        },
    )

    min_node = create_node(
        str(job_dir),
        "min",
        parent_node_ids=[topo["node_id"]],
    )
    assert min_node["success"], min_node
    min_dir = job_dir / "nodes" / min_node["node_id"] / "artifacts"
    min_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state_xml, min_dir / "minimized.xml")
    shutil.copy2(topo_pdb, min_dir / "minimized_structure.pdb")
    (min_dir / "minimization_report.json").write_text(json.dumps({
        "schema_version": "1.0",
        "minimization": {
            "attempted": True,
            "completed": True,
            "energy_is_finite": True,
            "positions_are_finite": True,
            "atom_count_preserved": True,
            "energy_initial_kj_mol": 0.0,
            "energy_final_kj_mol": 0.0,
        },
    }))
    complete_node(
        str(job_dir),
        min_node["node_id"],
        artifacts={
            "state": "artifacts/minimized.xml",
            "minimized_structure": "artifacts/minimized_structure.pdb",
            "minimization_report": "artifacts/minimization_report.json",
        },
    )

    command_log = tmp_path / "command_log.json"
    command_log.write_text(json.dumps({"command_log": []}))
    sub = tmp_path / "mdprep_submission"
    res = package_mdprep_submission(
        submission_dir=str(sub),
        task_id="P01_demo",
        job_dir=str(job_dir),
        node_id=min_node["node_id"],
        command_log_file=str(command_log),
    )

    assert res["success"], res
    assert (sub / "topology" / "state.xml").read_text() == Path(state_xml).read_text()
    provenance = json.loads((sub / "provenance.json").read_text())
    assert provenance["mdclaw_dag"]["min_node_id"] == min_node["node_id"]
    assert provenance["raw_outputs"]
