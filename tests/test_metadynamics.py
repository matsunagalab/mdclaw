"""run_metadynamics: well-tempered metadynamics on a COM distance as a production node."""

import csv
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from mdclaw.simulation.metadynamics import (
    MetadynamicsToolError,
    _validate_cv,
    _validate_grid,
    run_metadynamics,
)

CV = {"name": "d", "selection_group1": "index 0", "selection_group2": "index 1"}
SHORT = dict(
    distance_cv=CV,
    cv_min_nm=0.6,
    cv_max_nm=1.4,
    bias_width_nm=0.05,
    bias_height_kj_mol=1.0,
    bias_factor=5.0,
    deposition_interval_ps=0.02,    # 10 steps at 2 fs
    save_interval_ps=0.1,
    simulation_time_ns=0.004,       # 200 depositions
    output_frequency_ps=0.1,
    timestep_fs=2.0,
    hmr=False,
    platform="Reference",
    random_seed=7,
)


@pytest.fixture(scope="module")
def xml_triple(tmp_path_factory):
    """Two carbons on a harmonic bond (k=100 kJ/mol/nm^2, r=1 nm), in vacuum."""
    import openmm
    from openmm import unit
    from openmm.app import PDBFile, Topology, element

    d = tmp_path_factory.mktemp("triple")
    top = Topology()
    res = top.addResidue("DUM", top.addChain())
    a = top.addAtom("C1", element.carbon, res)
    b = top.addAtom("C2", element.carbon, res)
    top.addBond(a, b)
    system = openmm.System()
    system.addParticle(12.0)
    system.addParticle(12.0)
    bond = openmm.HarmonicBondForce()
    bond.addBond(0, 1, 1.0, 100.0)
    system.addForce(bond)
    (d / "system.xml").write_text(openmm.XmlSerializer.serialize(system))
    positions = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]] * unit.nanometer
    with open(d / "topology.pdb", "w") as fh:
        PDBFile.writeFile(top, positions, fh, keepIds=True)
    ctx = openmm.Context(system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
    ctx.setPositions(positions)
    ctx.setVelocitiesToTemperature(300 * unit.kelvin, 3)
    (d / "state.xml").write_text(openmm.XmlSerializer.serialize(ctx.getState(getPositions=True, getVelocities=True)))
    return d


def _rows(path):
    with open(path) as fh:
        return list(csv.DictReader(fh))


class TestValidation:
    def test_registered_as_prod_tool(self):
        from mdclaw.simulation import TOOLS
        from mdclaw._tool_meta import tool_node_type

        assert TOOLS["run_metadynamics"] is run_metadynamics
        assert tool_node_type(run_metadynamics) == "prod"

    def test_grid_rules(self):
        g = _validate_grid(0.4, 1.4, 0.05, None)
        assert g["grid_min_nm"] == pytest.approx(0.2) and g["grid_max_nm"] == pytest.approx(1.6)   # walls +- 4 sigma
        assert g["grid_points"] == 141
        for args in ((1.0, 0.5, 0.05, None), (0.4, 1.4, 0.6, None), (0.4, 1.4, 0.05, 5), (-1, 1, 0.05, None)):
            with pytest.raises(MetadynamicsToolError) as exc:
                _validate_grid(*args)
            assert exc.value.code in ("metadynamics_grid_invalid", "metadynamics_parameters_invalid")

    def test_cv_rules(self):
        assert _validate_cv(CV) == CV
        for bad in (None, {"name": "d"}, {**CV, "target_distance_nm": 1.0}):
            with pytest.raises(MetadynamicsToolError) as exc:
                _validate_cv(bad)
            assert exc.value.code == "metadynamics_cv_invalid"

    def test_missing_inputs_fail_cleanly(self, tmp_path):
        res = run_metadynamics(system_xml_file=str(tmp_path / "nope.xml"), topology_pdb_file=str(tmp_path / "nope.pdb"),
                               output_dir=str(tmp_path), **SHORT)
        assert res["success"] is False and res["code"] == "file_not_found"

    def test_bad_bias_factor_is_refused(self, xml_triple, tmp_path):
        res = run_metadynamics(system_xml_file=str(xml_triple / "system.xml"),
                               topology_pdb_file=str(xml_triple / "topology.pdb"),
                               output_dir=str(tmp_path), **{**SHORT, "bias_factor": 1.0})
        assert res["success"] is False and res["code"] == "metadynamics_parameters_invalid"


def test_run_metadynamics_standalone_fills_the_bond_well(xml_triple, tmp_path):
    res = run_metadynamics(
        system_xml_file=str(xml_triple / "system.xml"),
        topology_pdb_file=str(xml_triple / "topology.pdb"),
        state_xml_file=str(xml_triple / "state.xml"),
        output_dir=str(tmp_path),
        **SHORT,
    )
    assert res["success"], res
    assert res["temperature_kelvin"] == 300.0 and res["temperature_kelvin_source"] == "default"
    out = Path(res["output_dir"])
    for f in ("trajectory.dcd", "energy.dat", "state.xml", "final_structure.pdb", "metadynamics.csv",
              "metadynamics.json", "free_energy.csv", "metadynamics_total_bias.npy", "metadynamics_self_bias.npy",
              "collective_variables.csv", "collective_variables.meta.json", "runtime_system.xml", "integrator.xml"):
        assert (out / f).is_file(), f
    m = res["metadynamics"]
    assert m["depositions"] == 200 and m["grid_points"] == 121 and m["shared_bias_dir"] is False
    rows = _rows(out / "metadynamics.csv")
    assert len(rows) == 200
    heights = [float(r["gaussian_height_kj_mol"]) for r in rows]
    assert heights[0] <= 1.0 + 1e-9 and heights[-1] < heights[0]      # well-tempered decay
    total = np.load(out / "metadynamics_total_bias.npy")
    assert total.shape == (121,) and total.max() > 0
    F = np.loadtxt(out / "free_energy.csv", delimiter=",", skiprows=1)
    assert F[0, 0] == pytest.approx(0.6, abs=1e-6) and F[-1, 0] == pytest.approx(1.4, abs=1e-6)   # reported between the walls
    # the bias (and so the free-energy minimum) lies where the walker actually went
    x_min = F[np.argmin(F[:, 1]), 0]
    assert m["cv_visited_min_nm"] - 0.05 <= x_min <= m["cv_visited_max_nm"] + 0.05
    assert 0 < m["free_energy_range_kj_mol"] <= F[:, 1].max() + 1e-9
    cv_rows = _rows(out / "collective_variables.csv")
    assert len(cv_rows) == 40 and "d" in cv_rows[0]
    assert float(cv_rows[-1]["bias_energy_kj_mol"]) > 0


def _job(xml_triple, tmp_path, eq_temperature=None):
    from mdclaw._node import complete_node, create_node, init_progress_v3

    jd = tmp_path / "job"
    jd.mkdir()
    init_progress_v3(str(jd))

    def _node(node_type, **kw):
        return create_node(str(jd), node_type, **kw)["node_id"]

    def _touch(node_id, rel):
        p = jd / "nodes" / node_id / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("placeholder")
        return rel

    src = _node("source")
    complete_node(str(jd), src, {"structure_file": _touch(src, "artifacts/x.cif")})
    prep = _node("prep", parent_node_ids=[src])
    complete_node(str(jd), prep, {"merged_pdb": _touch(prep, "artifacts/x.pdb")})
    solv = _node("solv", parent_node_ids=[prep])
    complete_node(str(jd), solv, {"solvated_pdb": _touch(solv, "artifacts/x.pdb"),
                                   "box_dimensions": _touch(solv, "artifacts/x.json")})
    topo = _node("topo", parent_node_ids=[solv])
    tart = jd / "nodes" / topo / "artifacts"
    tart.mkdir(parents=True, exist_ok=True)
    for f in ("system.xml", "topology.pdb", "state.xml"):
        shutil.copy(xml_triple / f, tart / f)
    (tart / "amber_metadata.json").write_text(json.dumps({"parameters": {"hmr": False}, "forcefield_provenance": {}}))
    complete_node(str(jd), topo, {"system_xml": "artifacts/system.xml", "topology_pdb": "artifacts/topology.pdb",
                                   "state_xml": "artifacts/state.xml"}, metadata={"hmr": False})
    eq = _node("eq", parent_node_ids=[topo])
    eart = jd / "nodes" / eq / "artifacts"
    eart.mkdir(parents=True, exist_ok=True)
    shutil.copy(xml_triple / "state.xml", eart / "equilibrated.xml")
    eq_meta = {"final_step": 0}
    if eq_temperature is not None:
        from mdclaw.simulation.xml_contract import _integrator_signature

        eq_meta.update(temperature_kelvin=eq_temperature,
                       integrator_signature=_integrator_signature(temperature_kelvin=eq_temperature,
                                                                  timestep_fs=2.0))
    complete_node(str(jd), eq, {"state": "artifacts/equilibrated.xml"}, metadata=eq_meta)
    return jd, eq, _node


def test_node_mode_continuation_carries_the_bias(xml_triple, tmp_path):
    from mdclaw._node import read_node

    jd, eq, _node = _job(xml_triple, tmp_path)
    prod1 = _node("prod", parent_node_ids=[eq])
    res1 = run_metadynamics(job_dir=str(jd), node_id=prod1, **SHORT)
    assert res1["success"], res1
    n1 = read_node(str(jd), prod1)
    assert n1["status"] == "completed"
    assert n1["artifacts"]["metadynamics_total_bias"] == "artifacts/metadynamics_total_bias.npy"
    assert n1["metadata"]["sampling_method"] == "metadynamics" and n1["metadata"]["final_step"] == 2000
    total1 = np.load(jd / "nodes" / prod1 / "artifacts" / "metadynamics_total_bias.npy")

    prod2 = _node("prod", continue_from=prod1)
    res2 = run_metadynamics(job_dir=str(jd), node_id=prod2, **SHORT)
    assert res2["success"], res2
    n2 = read_node(str(jd), prod2)
    assert n2["metadata"]["start_step"] == 2000 and n2["metadata"]["final_step"] == 4000
    assert res2["metadynamics"]["loaded_walkers_at_start"] == [0]      # the parent's bias, loaded as walker 0
    total2 = np.load(jd / "nodes" / prod2 / "artifacts" / "metadynamics_total_bias.npy")
    own2 = np.load(jd / "nodes" / prod2 / "artifacts" / "metadynamics_self_bias.npy")
    assert np.allclose(total2, total1 + own2)
    assert int(_rows(jd / "nodes" / prod2 / "artifacts" / "metadynamics.csv")[0]["step"]) == 2010

    # changed settings cannot continue a walker
    prod3 = _node("prod", continue_from=prod2)
    res3 = run_metadynamics(job_dir=str(jd), node_id=prod3, **{**SHORT, "bias_factor": 8.0})
    assert res3["success"] is False and res3["code"] == "metadynamics_restart_mismatch"
    assert read_node(str(jd), prod3)["status"] == "failed"


def _integrator_temperature(result):
    import re

    m = re.search(r'temperature="([0-9.eE+-]+)"', Path(result["integrator_file"]).read_text())
    return float(m.group(1))


def test_walker_without_the_flag_runs_at_the_eq_temperature_and_continues(xml_triple, tmp_path):
    """A walker after a 310 K eq used to run at 300 K without a warning, and its
    no-flag continuation then failed with metadynamics_restart_mismatch (the
    temperature is in the manifest). Omitted, the temperature now follows the
    node the walker starts from, as run_production does after 010_membrane_6kux."""
    from mdclaw._node import read_node

    jd, eq, _node = _job(xml_triple, tmp_path, eq_temperature=310.0)
    w1 = _node("prod", parent_node_ids=[eq])
    r1 = run_metadynamics(job_dir=str(jd), node_id=w1, **SHORT)
    assert r1["success"], r1
    assert r1["temperature_kelvin"] == 310.0 and _integrator_temperature(r1) == pytest.approx(310.0)
    assert r1["temperature_kelvin_source"] == "inherited" and r1["temperature_kelvin_inherited_from"] == eq
    assert any(f"inherited from eq '{eq}'" in w for w in r1["warnings"])
    meta1 = read_node(str(jd), w1)["metadata"]
    assert meta1["temperature_kelvin"] == 310.0
    assert meta1["integrator_signature"]["temperature_kelvin"] == 310.0
    assert meta1["temperature_kelvin_source"] == "inherited" and meta1["temperature_kelvin_inherited_from"] == eq
    side = json.loads(Path(r1["metadynamics_state_file"]).read_text())
    assert side["manifest"]["temperature_kelvin"] == 310.0
    assert side["kT_kj_mol"] == pytest.approx(0.0083144626 * 310.0, rel=1e-6)

    # a continuation without the flag keeps the walker's temperature and its bias
    w2 = _node("prod", continue_from=w1)
    r2 = run_metadynamics(job_dir=str(jd), node_id=w2, **SHORT)
    assert r2["success"], r2
    assert r2["temperature_kelvin"] == 310.0 and _integrator_temperature(r2) == pytest.approx(310.0)
    assert r2["temperature_kelvin_inherited_from"] == w1
    assert r2["metadynamics"]["loaded_walkers_at_start"] == [0]

    # an explicit different temperature still cannot continue the walker
    w3 = _node("prod", continue_from=w2)
    r3 = run_metadynamics(job_dir=str(jd), node_id=w3, **{**SHORT, "temperature_kelvin": 300.0})
    assert r3["success"] is False and r3["code"] == "metadynamics_restart_mismatch"
    assert read_node(str(jd), w3)["status"] == "failed"


def test_explicit_temperature_wins_and_eq_without_temperature_keeps_300(xml_triple, tmp_path):
    from mdclaw._node import read_node

    jd, eq, _node = _job(xml_triple, tmp_path, eq_temperature=310.0)
    w = _node("prod", parent_node_ids=[eq])
    r = run_metadynamics(job_dir=str(jd), node_id=w, **{**SHORT, "temperature_kelvin": 320.0})
    assert r["success"], r
    assert r["temperature_kelvin"] == 320.0 and _integrator_temperature(r) == pytest.approx(320.0)
    assert r["temperature_kelvin_source"] == "explicit" and r["temperature_kelvin_inherited_from"] is None

    (tmp_path / "legacy").mkdir()
    jd2, eq2, _node2 = _job(xml_triple, tmp_path / "legacy")
    w2 = _node2("prod", parent_node_ids=[eq2])
    r2 = run_metadynamics(job_dir=str(jd2), node_id=w2, **SHORT)
    assert r2["success"], r2
    assert r2["temperature_kelvin"] == 300.0 and r2["temperature_kelvin_source"] == "default"
    assert any("records no temperature to inherit" in w for w in r2["warnings"])
    assert read_node(str(jd2), w2)["metadata"]["temperature_kelvin_source"] == "default"


def test_declared_temperature_is_checked_against_the_inherited_value(xml_triple, tmp_path):
    from mdclaw._node import create_node, read_node

    jd, eq, _node = _job(xml_triple, tmp_path, eq_temperature=310.0)
    ok = create_node(str(jd), "prod", parent_node_ids=[eq], conditions={"temperature_kelvin": 310.0})["node_id"]
    assert run_metadynamics(job_dir=str(jd), node_id=ok, **SHORT)["success"]
    bad = create_node(str(jd), "prod", parent_node_ids=[eq], conditions={"temperature_kelvin": 300.0})["node_id"]
    rb = run_metadynamics(job_dir=str(jd), node_id=bad, **SHORT)
    assert rb["success"] is False and "condition_mismatch" in rb["blocking_codes"]
    assert read_node(str(jd), bad)["status"] == "pending"


def test_shared_bias_dir_joins_walkers_and_refuses_other_settings(xml_triple, tmp_path):
    from mdclaw._node import read_node

    jd, eq, _node = _job(xml_triple, tmp_path)
    shared = tmp_path / "shared_bias"
    w1 = _node("prod", parent_node_ids=[eq])
    r1 = run_metadynamics(job_dir=str(jd), node_id=w1, bias_dir=str(shared), **{**SHORT, "random_seed": 1})
    assert r1["success"], r1
    assert r1["metadynamics"]["shared_manifest"]["created"] is True
    w2 = _node("prod", parent_node_ids=[eq])
    r2 = run_metadynamics(job_dir=str(jd), node_id=w2, bias_dir=str(shared), **{**SHORT, "random_seed": 2})
    assert r2["success"], r2
    assert r2["metadynamics"]["shared_manifest"]["created"] is False
    assert r1["metadynamics"]["walker_id"] in r2["metadynamics"]["loaded_walkers_at_start"]
    own1 = np.load(jd / "nodes" / w1 / "artifacts" / "metadynamics_self_bias.npy")
    own2 = np.load(jd / "nodes" / w2 / "artifacts" / "metadynamics_self_bias.npy")
    total2 = np.load(jd / "nodes" / w2 / "artifacts" / "metadynamics_total_bias.npy")
    assert np.allclose(total2, own1 + own2)
    assert sorted(p.name for p in shared.iterdir() if p.suffix == ".npy") and (shared / "metadynamics_manifest.json").is_file()
    w3 = _node("prod", parent_node_ids=[eq])
    r3 = run_metadynamics(job_dir=str(jd), node_id=w3, bias_dir=str(shared), **{**SHORT, "bias_width_nm": 0.1})
    assert r3["success"] is False and r3["code"] == "metadynamics_shared_bias_mismatch"
    assert read_node(str(jd), w3)["status"] == "failed"


def test_shared_manifest_tolerates_a_concurrent_writer(tmp_path):
    """Two walkers start together: the second may see the manifest before it is
    complete; it must wait for it instead of refusing."""
    import threading
    import time

    from mdclaw.simulation.metadynamics import MANIFEST_NAME, _check_shared_dir, _manifest, _validate_grid

    manifest = _manifest(CV, _validate_grid(0.6, 1.4, 0.05, None), 300.0, 5.0, 1.0, 0.02)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / MANIFEST_NAME).write_text("")          # a half-written file

    def finish():
        time.sleep(0.8)
        (shared / MANIFEST_NAME).write_text(json.dumps(manifest))

    threading.Thread(target=finish).start()
    assert _check_shared_dir(shared, manifest, retry_seconds=10)["created"] is False
    # and a genuinely different manifest is still refused
    with pytest.raises(MetadynamicsToolError) as exc:
        _check_shared_dir(shared, {**manifest, "bias_factor": 9.0}, retry_seconds=1)
    assert exc.value.code == "metadynamics_shared_bias_mismatch"
    # the first walker creates it atomically
    fresh = tmp_path / "fresh"
    assert _check_shared_dir(fresh, manifest)["created"] is True
    assert json.loads((fresh / MANIFEST_NAME).read_text()) == manifest
