"""run_equilibration refuses an eq -> eq restart whose temperature is unstated.

Production now inherits the temperature of the node it restarts from
(MDDataBench glm-5.3-flash 3cond 010_membrane_6kux r3: eq at 310 K, production
without the flag at 300 K). An eq stage does not inherit: after a deliberate
hot stage (md-we's 500 K eq_002) a forgotten flag must not silently stay hot.
It keeps the 300 K default, except that an omitted flag after an eq that ran
away from 300 K is refused before anything runs, node pending. Real
run_equilibration nodes on the two-carbon periodic triple of
tests/test_production_temperature_inheritance.py, Reference platform.
"""
import shutil
from pathlib import Path

import pytest

from mdclaw._node import create_node, read_node
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node
from tests.test_production_temperature_inheritance import _eq, _job_with_topo
from tests.test_production_temperature_inheritance import periodic_triple as _periodic_triple

periodic_triple = _periodic_triple

EQ = dict(pressure_bar=0, nvt_steps=200, npt_steps=0, restraint_force_constant=0.0,
          platform="Reference", hmr=False, random_seed=5)


def _run_eq(jd, node, **kw):
    from mdclaw.simulation.equilibrate import run_equilibration
    return run_equilibration(job_dir=jd, node_id=node, **EQ, **kw)


def _assert_eq_ran_at(jd, node, result, t):
    assert result["success"], result.get("errors")
    assert result["integrator_signature"]["temperature_kelvin"] == pytest.approx(t)
    meta = read_node(jd, node)["metadata"]
    assert meta["temperature_kelvin"] == pytest.approx(t)
    assert meta["integrator_signature"]["temperature_kelvin"] == pytest.approx(t)


def test_eq_after_a_310_eq_without_the_flag_is_refused_and_stays_pending(tmp_path, periodic_triple):
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq1 = _eq(jd, topo, 310.0)
    eq2 = create_node(jd, "eq", parent_node_ids=[eq1])["node_id"]
    r = _run_eq(jd, eq2)
    assert r["success"] is False
    assert r["code"] == "eq_restart_temperature_unstated"
    assert f"eq '{eq1}', which ran at 310.0 K" in r["errors"][0]
    assert any("--temperature-kelvin 310" in h for h in r["hints"])
    assert r["context"]["restart_temperature_kelvin"] == pytest.approx(310.0)
    node = read_node(jd, eq2)
    assert node["status"] == "pending"
    assert "temperature_kelvin" not in (node.get("metadata") or {})
    # Stating the parent's temperature on the same node then runs it.
    r2 = _run_eq(jd, eq2, temperature_kelvin=310.0)
    _assert_eq_ran_at(jd, eq2, r2, 310.0)


def test_explicit_temperature_after_a_310_eq_always_wins(tmp_path, periodic_triple):
    """Cooling back to 300 K (or heating further) on purpose is the caller's decision."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq1 = _eq(jd, topo, 310.0)
    for t in (300.0, 340.0):
        node = create_node(jd, "eq", parent_node_ids=[eq1])["node_id"]
        _assert_eq_ran_at(jd, node, _run_eq(jd, node, temperature_kelvin=t), t)


def test_eq_after_a_300_eq_without_the_flag_runs_at_300(tmp_path, periodic_triple):
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq1 = _eq(jd, topo, 300.0)
    eq2 = create_node(jd, "eq", parent_node_ids=[eq1])["node_id"]
    r = _run_eq(jd, eq2)
    _assert_eq_ran_at(jd, eq2, r, 300.0)
    assert r["restart_from_node_id"] == eq1


def test_eq_after_a_legacy_eq_without_temperature_metadata_runs_at_300(tmp_path, periodic_triple):
    """Nothing recorded, nothing ambiguous to refuse: today's 300 K default."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    eq1 = create_node(jd, "eq", parent_node_ids=[topo])["node_id"]
    shutil.copy(periodic_triple / "state.xml", Path(jd) / "nodes" / eq1 / "artifacts" / "equilibrated.xml")
    complete_node(jd, eq1, {"state": "artifacts/equilibrated.xml"},
                  metadata={"final_step": 0, "final_ensemble": "NVT"})
    eq2 = create_node(jd, "eq", parent_node_ids=[eq1])["node_id"]
    _assert_eq_ran_at(jd, eq2, _run_eq(jd, eq2), 300.0)


def test_min_to_eq_without_the_flag_runs_at_300(tmp_path, periodic_triple):
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    mn = create_node(jd, "min", parent_node_ids=[topo])["node_id"]
    shutil.copy(periodic_triple / "state.xml", Path(jd) / "nodes" / mn / "artifacts" / "minimized.xml")
    complete_node(jd, mn, {"state": "artifacts/minimized.xml"}, metadata={"final_step": 0})
    eq = create_node(jd, "eq", parent_node_ids=[mn])["node_id"]
    r = _run_eq(jd, eq)
    _assert_eq_ran_at(jd, eq, r, 300.0)
    assert r["restart_from_node_type"] == "min"


def test_declared_temperature_is_checked_against_the_300_default(tmp_path, periodic_triple):
    """The default is resolved before the declared-condition check: a node that
    declares 300 K and omits the flag runs; one that declares 310 K is refused."""
    jd, topo = _job_with_topo(tmp_path, periodic_triple)
    ok = create_node(jd, "eq", parent_node_ids=[topo], conditions={"temperature_kelvin": 300.0})["node_id"]
    _assert_eq_ran_at(jd, ok, _run_eq(jd, ok), 300.0)
    bad = create_node(jd, "eq", parent_node_ids=[topo], conditions={"temperature_kelvin": 310.0})["node_id"]
    rb = _run_eq(jd, bad)
    assert rb["success"] is False and "condition_mismatch" in rb["blocking_codes"]
    assert read_node(jd, bad)["status"] == "pending"


def test_standalone_equilibration_keeps_300(tmp_path, periodic_triple):
    from mdclaw.simulation.equilibrate import run_equilibration
    r = run_equilibration(system_xml_file=str(periodic_triple / "system.xml"),
                          topology_pdb_file=str(periodic_triple / "topology.pdb"),
                          state_xml_file=str(periodic_triple / "state.xml"),
                          output_dir=str(tmp_path / "eq"), **EQ)
    assert r["success"], r["errors"]
    assert r["integrator_signature"]["temperature_kelvin"] == 300.0
