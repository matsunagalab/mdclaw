"""Guard the package_openmm_submission metrics contract.

The packager is the intended one-command path to a scorer-valid submission, so
its metrics.json must match the scorer's canonical preparation.* json_paths:

- the key is ``preparation.forcefield`` (NOT ``force_field``); writing the
  wrong key silently failed P22-type fidelity checks, and
- task-specific fields ingested from the prep summary keep their TYPE
  (``disulfide_pairs`` must stay a list, not collapse to a count).

These tests build a minimal real OpenMM triple, run the packager, and assert the
emitted metrics. Run with: pytest tests/test_package_submission_metrics.py -v
"""

import json

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
    return json.loads((sub / "metrics.json").read_text())


def test_packager_uses_canonical_forcefield_key(tmp_path):
    metrics = _package(tmp_path, force_field="ff19SB", water_model="opc")
    prep = metrics["preparation"]
    assert prep.get("forcefield") == "ff19SB"   # canonical scorer json_path
    assert "force_field" not in prep            # the old wrong key is gone
    assert prep.get("water_model") == "opc"


def test_packager_ingests_prep_summary_preserving_types(tmp_path):
    # the prep summary carries disulfide_pairs as a LIST and the recorded flags
    summary = tmp_path / "prep_summary.json"
    summary.write_text(json.dumps({
        "preparation_summary": {
            "disulfide_pairs": [
                {"cys1": {"chain": "A", "resnum": 5},
                 "cys2": {"chain": "A", "resnum": 55}},
                {"cys1": {"chain": "A", "resnum": 14},
                 "cys2": {"chain": "A", "resnum": 38}},
            ],
            "disulfide_detection_recorded": True,
            "component_disposition_recorded": True,
        }
    }))
    metrics = _package(
        tmp_path, force_field="ff19SB", water_model="opc",
        solvent_model="explicit", preparation_summary_file=str(summary),
    )
    prep = metrics["preparation"]
    assert isinstance(prep["disulfide_pairs"], list)          # stays a list
    assert len(prep["disulfide_pairs"]) == 2
    assert prep["disulfide_detection_recorded"] is True
    assert prep["component_disposition_recorded"] is True
    assert prep["forcefield"] == "ff19SB"                     # layered on top
    assert prep["solvent_model"] == "explicit"


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
    metrics = _package(tmp_path, force_field="ff19SB", water_model="opc")
    for key in metrics["preparation"]:
        assert key in canonical, (
            f"packager emits preparation.{key!r} which is not a scorer "
            f"json_path key — fix the key name or the scorer contract"
        )
