"""run_production takes its temperature from the node it restarts from.

MDDataBench glm-5.3-flash 3cond, 010_membrane_6kux cli_skill_sif r3
(2026-09-26): the equilibration ran at 310 K as asked, production was called
without --temperature-kelvin and ran at the 300 K default. A real
run_equilibration node and real run_production nodes on a two-carbon periodic
triple (the tests/test_rounds.py fixture), Reference platform, a few hundred
steps each.
"""
import json
import re
import shlex
import shutil
from pathlib import Path

import pytest

from mdclaw._node import create_node, init_progress_v3, read_node
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node

STAGE = dict(simulation_time_ns=0.0004, output_frequency_ps=0.1, platform="Reference", hmr=False)


@pytest.fixture(scope="module")
def periodic_triple(tmp_path_factory):
    import openmm
    from openmm import unit
    from openmm.app import PDBFile, Topology, element

    d = tmp_path_factory.mktemp("triple")
    box = [openmm.Vec3(3.0, 0, 0), openmm.Vec3(0, 3.0, 0), openmm.Vec3(0, 0, 3.0)] * unit.nanometer
    top = Topology()
    res = top.addResidue("DUM", top.addChain())
    a = top.addAtom("C1", element.carbon, res)
    b = top.addAtom("C2", element.carbon, res)
    top.addBond(a, b)
    top.setPeriodicBoxVectors(box)
    system = openmm.System()
    system.addParticle(12.0)
    system.addParticle(12.0)
    system.setDefaultPeriodicBoxVectors(*box)
    bond = openmm.HarmonicBondForce()
    bond.addBond(0, 1, 1.0, 100.0)
    system.addForce(bond)
    nb = openmm.NonbondedForce()
    nb.setNonbondedMethod(openmm.NonbondedForce.CutoffPeriodic)
    nb.setCutoffDistance(1.2 * unit.nanometer)
    nb.addParticle(0.0, 0.3, 0.0)
    nb.addParticle(0.0, 0.3, 0.0)
    nb.addException(0, 1, 0.0, 0.3, 0.0)
    system.addForce(nb)
    (d / "system.xml").write_text(openmm.XmlSerializer.serialize(system))
    pos = [[1.0, 1.0, 1.0], [2.0, 1.0, 1.0]] * unit.nanometer
    with open(d / "topology.pdb", "w") as fh:
        PDBFile.writeFile(top, pos, fh, keepIds=True)
    ctx = openmm.Context(system, openmm.VerletIntegrator(0.001),
                         openmm.Platform.getPlatformByName("Reference"))
    ctx.setPositions(pos)
    ctx.setVelocitiesToTemperature(300 * unit.kelvin, 3)
    (d / "state.xml").write_text(openmm.XmlSerializer.serialize(
        ctx.getState(getPositions=True, getVelocities=True)))
    return d


def _job_with_topo(tmp_path, tri):
    jd = tmp_path / "job"
    jd.mkdir()
    init_progress_v3(str(jd))
    src = create_node(str(jd), "source")["node_id"]
    complete_node(str(jd), src, {"structure_file": "artifacts/x.cif"})
    prep = create_node(str(jd), "prep", parent_node_ids=[src])["node_id"]
    complete_node(str(jd), prep, {"merged_pdb": "artifacts/x.pdb"})
    solv = create_node(str(jd), "solv", parent_node_ids=[prep])["node_id"]
    complete_node(str(jd), solv, {"solvated_pdb": "artifacts/x.pdb", "box_dimensions": "artifacts/x.json"})
    topo = create_node(str(jd), "topo", parent_node_ids=[solv])["node_id"]
    for n in ("system.xml", "topology.pdb", "state.xml"):
        shutil.copy(tri / n, jd / "nodes" / topo / "artifacts" / n)
    complete_node(str(jd), topo, {"system_xml": "artifacts/system.xml",
                                  "topology_pdb": "artifacts/topology.pdb",
                                  "state_xml": "artifacts/state.xml"}, metadata={"hmr": False})
    return str(jd), topo


def _eq(jd, parent, temperature):
    from mdclaw.simulation.equilibrate import run_equilibration
    eq = create_node(jd, "eq", parent_node_ids=[parent])["node_id"]
    r = run_equilibration(job_dir=jd, node_id=eq, temperature_kelvin=temperature, pressure_bar=0,
                          nvt_steps=200, npt_steps=0, restraint_force_constant=0.0,
                          platform="Reference", hmr=False, random_seed=1)
    assert r["success"], r["errors"]
    return eq


def _prod(jd, *, parent=None, continue_from=None, seed, conditions=None, **kw):
    from mdclaw.simulation.production import run_production
    node = (create_node(jd, "prod", continue_from=continue_from, conditions=conditions)
            if continue_from else
            create_node(jd, "prod", parent_node_ids=[parent], conditions=conditions))["node_id"]
    return node, run_production(job_dir=jd, node_id=node, random_seed=seed, **STAGE, **kw)


def _ran_at(result):
    """The temperature the Langevin integrator actually used."""
    m = re.search(r'temperature="([0-9.eE+-]+)"', Path(result["integrator_file"]).read_text())
    return float(m.group(1))


def _assert_ran_at(jd, node, result, t):
    assert result["success"], result.get("errors")
    assert result["temperature_kelvin"] == pytest.approx(t)
    assert _ran_at(result) == pytest.approx(t)
    meta = read_node(jd, node)["metadata"]
    assert meta["temperature_kelvin"] == pytest.approx(t)
    assert meta["integrator_signature"]["temperature_kelvin"] == pytest.approx(t)


def test_eq_to_prod_without_flag_inherits_the_eq_temperature(tmp_path, periodic_triple):
    """010_membrane_6kux cli_skill_sif r3: eq at 310 K, prod without the flag ran at 300 K."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    node, r = _prod(jd, parent=eq, seed=2)
    _assert_ran_at(jd, node, r, 310.0)
    assert not any("temperature_kelvin" in c for c in r.get("restart_integrator_changes") or [])
    assert r["temperature_kelvin_source"] == "inherited"
    assert r["temperature_kelvin_inherited_from"] == eq
    assert any(f"inherited from eq '{eq}'" in w for w in r["warnings"])
    meta = read_node(jd, node)["metadata"]
    assert meta["temperature_kelvin_source"] == "inherited"
    assert meta["temperature_kelvin_inherited_from"] == eq


def test_prod_to_prod_without_flag_inherits_the_prod_parent_not_the_eq(tmp_path, periodic_triple):
    """The continuation restarts from the prod parent: an explicit 320 K first
    segment (a warned change from the 310 K eq) is continued at 320 K."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    p1, r1 = _prod(jd, parent=eq, seed=2, temperature_kelvin=320.0)
    _assert_ran_at(jd, p1, r1, 320.0)
    p2, r2 = _prod(jd, continue_from=p1, seed=3)
    _assert_ran_at(jd, p2, r2, 320.0)
    # and an inherited segment is itself inherited from
    p3, r3 = _prod(jd, continue_from=p2, seed=4)
    _assert_ran_at(jd, p3, r3, 320.0)


def test_explicit_different_temperature_from_eq_warns(tmp_path, periodic_triple):
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    node, r = _prod(jd, parent=eq, seed=2, temperature_kelvin=300.0)
    _assert_ran_at(jd, node, r, 300.0)
    assert r["restart_integrator_changes"] == ["temperature_kelvin: restart=310.0, current=300.0"]


def test_explicit_different_temperature_on_continuation_is_refused(tmp_path, periodic_triple):
    """Refused before anything runs, with a code, and the node stays pending."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    p1, r1 = _prod(jd, parent=eq, seed=2)
    p2, r2 = _prod(jd, continue_from=p1, seed=3, temperature_kelvin=300.0)
    assert r2["success"] is False
    assert r2["code"] == "production_restart_integrator_mismatch"
    assert "temperature_kelvin: parent=310.0, this run=300.0" in r2["errors"][0]
    assert read_node(jd, p2)["status"] == "pending"
    # Omitting the flag on the same node then runs at the parent's temperature.
    from mdclaw.simulation.production import run_production
    r3 = run_production(job_dir=jd, node_id=p2, random_seed=3, **STAGE)
    _assert_ran_at(jd, p2, r3, 310.0)


def test_declared_condition_is_checked_against_the_inherited_temperature(tmp_path, periodic_triple):
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    node, r = _prod(jd, parent=eq, seed=2, conditions={"temperature_kelvin": 310.0})
    _assert_ran_at(jd, node, r, 310.0)
    bad, rb = _prod(jd, parent=eq, seed=3, conditions={"temperature_kelvin": 300.0})
    assert rb["success"] is False and "condition_mismatch" in rb["blocking_codes"]
    assert "declared 300.0, actual 310.0" in rb["errors"][0]
    assert read_node(jd, bad)["status"] == "pending"


def test_legacy_eq_without_temperature_metadata_keeps_300(tmp_path, periodic_triple):
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = create_node(jd, "eq", parent_node_ids=[topo])["node_id"]
    shutil.copy(periodic_triple / "state.xml", Path(jd) / "nodes" / eq / "artifacts" / "equilibrated.xml")
    complete_node(jd, eq, {"state": "artifacts/equilibrated.xml"},
                  metadata={"final_step": 0, "final_ensemble": "NVT"})
    node, r = _prod(jd, parent=eq, seed=2)
    _assert_ran_at(jd, node, r, 300.0)
    assert r["temperature_kelvin_source"] == "default"
    assert any("records no temperature to inherit" in w for w in r["warnings"])


def test_explicit_300_is_not_overridden_by_inheritance(tmp_path, periodic_triple):
    """An explicit value is the caller's decision, even when it equals the old default."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    node, r = _prod(jd, parent=eq, seed=2, temperature_kelvin=300.0)
    _assert_ran_at(jd, node, r, 300.0)
    assert r["temperature_kelvin_source"] == "explicit"


def test_explain_node_shows_the_temperature_to_inherit(tmp_path, periodic_triple):
    from mdclaw._node import resolve_node_inputs
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    node = create_node(jd, "prod", parent_node_ids=[eq])["node_id"]
    inputs = resolve_node_inputs(jd, node, "prod")
    assert inputs["restart_temperature_kelvin"] == pytest.approx(310.0)
    assert inputs["restart_temperature_node_id"] == eq
    assert inputs["restart_temperature_node_type"] == "eq"


def test_standalone_run_keeps_300(tmp_path, periodic_triple):
    from mdclaw.simulation.production import run_production
    r = run_production(system_xml_file=str(periodic_triple / "system.xml"),
                       topology_pdb_file=str(periodic_triple / "topology.pdb"),
                       state_xml_file=str(periodic_triple / "state.xml"),
                       output_dir=str(tmp_path / "md"), random_seed=9, **STAGE)
    assert r["success"], r["errors"]
    assert r["temperature_kelvin"] == 300.0 and _ran_at(r) == 300.0
    assert r["temperature_kelvin_source"] == "default"


def test_slurm_preflight_defers_an_omitted_temperature(tmp_path):
    """A literal command without --temperature-kelvin cannot know the inherited
    value before the parents finish; it must be deferred, not refused."""
    from mdclaw.slurm.preflight import production_preflight
    node = tmp_path / "nodes/prod_001"
    node.mkdir(parents=True)
    (node / "node.json").write_text(json.dumps({
        "node_id": "prod_001", "node_type": "prod", "status": "pending",
        "conditions": {"temperature_kelvin": 310.0}, "parent_node_ids": ["eq_001"],
        "metadata": {}, "artifacts": {}}))
    cmd = f"mdclaw --job-dir {shlex.quote(str(tmp_path))} --node-id prod_001 run_production"
    r = production_preflight(cmd, str(tmp_path), "prod_001")
    assert r["success"], r
    assert "temperature_kelvin" in r["deferred_conditions"]
    explicit = production_preflight(cmd + " --temperature-kelvin 300", str(tmp_path), "prod_001")
    assert explicit["status"] == "failed"
    matching = production_preflight(cmd + " --temperature-kelvin 310", str(tmp_path), "prod_001")
    assert matching["status"] == "checked" and "temperature_kelvin" in matching["checked_conditions"]


def test_a_terminal_node_gets_node_terminal_not_the_integrator_refusal(tmp_path, periodic_triple):
    """The prod -> prod check runs after the node-context check."""
    from mdclaw.simulation.production import run_production
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq = _eq(jd, topo, 310.0)
    p1, r1 = _prod(jd, parent=eq, seed=2)
    p2, r2 = _prod(jd, continue_from=p1, seed=3)
    assert r2["success"]
    again = run_production(job_dir=jd, node_id=p2, random_seed=3, temperature_kelvin=300.0, **STAGE)
    assert again["success"] is False
    assert "node_terminal" in (again.get("blocking_codes") or [again.get("code")])
    assert not (Path(jd) / "nodes" / p2 / "artifacts" / "failure").exists()


def test_new_refusal_texts_do_not_promise_a_pending_node():
    """A submitted (queued) node is recorded failed by the same refusal; the
    envelope says which, so the registered action must not claim either."""
    from mdclaw.guardrail_codes import GUARDRAIL_CODES
    for code in ("production_restart_integrator_mismatch", "eq_restart_temperature_unstated",
                 "rounds_start_temperature_mismatch"):
        assert "still pending" not in GUARDRAIL_CODES[code]
