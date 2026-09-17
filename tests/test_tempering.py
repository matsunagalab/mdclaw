"""run_sst2: solute tempering as a production node (SST2 driver subprocess)."""

import json
import os
from pathlib import Path

import pytest

from mdclaw.simulation.tempering import (
    SST2ToolError,
    _resolve_solute_indices,
    _sst2_environment,
    _validate_ladder,
    run_sst2,
)


def _sst2_home():
    home = os.environ.get("MDCLAW_SST2_HOME")
    if home and (Path(home) / "src" / "SST2" / "driver.py").is_file():
        return Path(home)
    return None


def _sst2_test_pdb():
    """The 2HPL test system of SST2: from the checkout on MDCLAW_SST2_HOME, or
    from the packaged copy (the images ship the fork pinned by commit)."""
    home = _sst2_home()
    if home is not None:
        return home / "src" / "SST2" / "tests" / "inputs" / "2HPL_equi_water.pdb"
    try:
        from SST2.tests.datafiles import PDB_PROT_PEP_SOL  # noqa: PLC0415
        import SST2.driver  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001
        return None
    pdb = Path(PDB_PROT_PEP_SOL)
    return pdb if pdb.is_file() else None


needs_sst2 = pytest.mark.skipif(
    _sst2_test_pdb() is None, reason="SST2 fork not importable and MDCLAW_SST2_HOME not set"
)


class TestValidation:
    def test_registered_as_prod_tool(self):
        from mdclaw.simulation import TOOLS
        from mdclaw._tool_meta import tool_node_type

        assert TOOLS["run_sst2"] is run_sst2
        assert tool_node_type(run_sst2) == "prod"

    def test_ladder_rules(self):
        assert _validate_ladder([300, 330, 360], None) == ([300.0, 330.0, 360.0], 300.0)
        assert _validate_ladder(["300", "330", "360"], "330") == ([300.0, 330.0, 360.0], 330.0)
        with pytest.raises(SST2ToolError):
            _validate_ladder(["300", "warm"], None)
        assert _validate_ladder([280, 300, 330], 300) == ([280.0, 300.0, 330.0], 300.0)
        for bad, ref in (([300], None), ([300, 300], None), ([330, 300], None), ([300, 330], 310)):
            with pytest.raises(SST2ToolError) as exc:
                _validate_ladder(bad, ref)
            assert exc.value.code == "sst2_ladder_invalid"

    def test_solute_needs_exactly_one_source(self, tmp_path):
        with pytest.raises(SST2ToolError) as exc:
            _resolve_solute_indices("x.pdb", solute_selection=None, solute_indices_file=None)
        assert exc.value.code == "sst2_solute_required"

    def test_solvent_in_selection_is_refused(self, tmp_path):
        """A selection that reaches water or ions is refused with a stable code."""
        pdb_in = _sst2_test_pdb()
        if pdb_in is None:
            pytest.skip("SST2 test system not available")
        with pytest.raises(SST2ToolError) as exc:
            _resolve_solute_indices(str(pdb_in), solute_selection="resid 101 to 103 or water",
                                    solute_indices_file=None)
        assert exc.value.code == "sst2_solute_includes_solvent"
        assert "HOH" in str(exc.value)
        ok, prov = _resolve_solute_indices(str(pdb_in), solute_selection="chainid 1 and resid 101 to 103",
                                           solute_indices_file=None)
        assert len(ok) == 52 and "HOH" not in prov["solute_residue_names"]
        bad = tmp_path / "idx.json"
        bad.write_text("[0, 1, 2, 11000]")   # 11000 is a water atom in the 2HPL system
        with pytest.raises(SST2ToolError) as exc:
            _resolve_solute_indices(str(pdb_in), solute_selection=None, solute_indices_file=str(bad))
        assert exc.value.code == "sst2_solute_includes_solvent"

    def test_bad_home_reports_not_installed(self, tmp_path):
        with pytest.raises(SST2ToolError) as exc:
            _sst2_environment(str(tmp_path))
        assert exc.value.code == "sst2_not_installed"

    def test_missing_inputs_fail_cleanly(self, tmp_path):
        res = run_sst2(
            system_xml_file=str(tmp_path / "nope.xml"),
            topology_pdb_file=str(tmp_path / "nope.pdb"),
            temperatures_kelvin=[300, 330],
            solute_selection="resid 0",
            output_dir=str(tmp_path),
        )
        assert res["success"] is False
        assert res["code"] == "file_not_found"


@pytest.fixture(scope="module")
def xml_triple(tmp_path_factory):
    """system.xml / topology.pdb / state.xml of the SST2 test system (2HPL)."""
    pdb_in = _sst2_test_pdb()
    if pdb_in is None:
        pytest.skip("SST2 fork not available")
    import openmm
    from openmm import unit
    import openmm.app as app

    d = tmp_path_factory.mktemp("triple")
    ff = app.ForceField("amber14-all.xml", "amber14/tip3pfb.xml")
    pdb = app.PDBFile(str(pdb_in))
    system = ff.createSystem(
        pdb.topology, nonbondedMethod=app.PME, nonbondedCutoff=1 * unit.nanometers,
        constraints=app.HBonds, hydrogenMass=1.5 * unit.amu,
    )
    (d / "system.xml").write_text(openmm.XmlSerializer.serialize(system))
    with open(d / "topology.pdb", "w") as fh:
        app.PDBFile.writeFile(pdb.topology, pdb.positions, fh, keepIds=True)
    ctx = openmm.Context(system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("CPU"))
    ctx.setPositions(pdb.positions)
    ctx.setVelocitiesToTemperature(300 * unit.kelvin, 3)
    (d / "state.xml").write_text(openmm.XmlSerializer.serialize(ctx.getState(getPositions=True, getVelocities=True)))
    return d


SHORT = dict(
    temperatures_kelvin=[300.0, 330.0, 360.0],
    solute_selection="chainid 1 and resid 101 to 103",   # chain B residues 2-4
    simulation_time_ns=0.0002,      # 100 steps at 2 fs
    exchange_interval_ps=0.02,      # every 10 steps
    output_frequency_ps=0.1,
    timestep_fs=2.0,
    hmr=False,
    platform="CPU",
    random_seed=1,
)


@needs_sst2
def test_run_sst2_standalone(xml_triple, tmp_path):
    res = run_sst2(
        system_xml_file=str(xml_triple / "system.xml"),
        topology_pdb_file=str(xml_triple / "topology.pdb"),
        state_xml_file=str(xml_triple / "state.xml"),
        output_dir=str(tmp_path),
        **SHORT,
    )
    assert res["success"], res
    out = Path(res["output_dir"])
    for f in ("trajectory.dcd", "energy.dat", "state.xml", "final_structure.pdb",
              "tempering.csv", "tempering.json", "solute_indices.json", "sst2_driver.log"):
        assert (out / f).is_file(), f
    t = res["tempering"]
    assert t["solute_atoms"] == 52
    assert t["boundary_exceptions"] == 30
    assert sum(t["rung_visits"]) == 10
    assert t["report_rows"] == 10
    assert len(t["rung_occupancy"]) == 3
    side = json.loads((out / "tempering.json").read_text())
    assert side["solute"]["solute_residue_count"] == 3
    assert side["weights_fixed"] is False


@needs_sst2
def test_run_sst2_node_mode_and_continuation(xml_triple, tmp_path):
    import shutil
    from mdclaw._node import (
        complete_node, create_node, init_progress_v3, read_node,
    )

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
    (tart / "amber_metadata.json").write_text(json.dumps({
        "parameters": {"hmr": False, "water_model": "tip3pfb"},
        "forcefield_provenance": {"protein": "amber14-all.xml", "water": "amber14/tip3pfb.xml"},
    }))
    complete_node(str(jd), topo, {"system_xml": "artifacts/system.xml",
                                   "topology_pdb": "artifacts/topology.pdb",
                                   "state_xml": "artifacts/state.xml"},
                  metadata={"hmr": False})
    eq = _node("eq", parent_node_ids=[topo])
    eart = jd / "nodes" / eq / "artifacts"
    eart.mkdir(parents=True, exist_ok=True)
    shutil.copy(xml_triple / "state.xml", eart / "equilibrated.xml")
    complete_node(str(jd), eq, {"state": "artifacts/equilibrated.xml"}, metadata={"final_step": 0})

    prod1 = _node("prod", parent_node_ids=[eq])
    res1 = run_sst2(job_dir=str(jd), node_id=prod1, **SHORT)
    assert res1["success"], res1
    n1 = read_node(str(jd), prod1)
    assert n1["status"] == "completed"
    assert n1["artifacts"]["tempering_state"] == "artifacts/tempering.json"
    assert n1["metadata"]["sampling_method"] == "sst2"
    assert n1["metadata"]["final_step"] == 100
    leftovers = [p.name for p in (jd / "nodes" / prod1 / "artifacts").iterdir()
                 if p.name.startswith("sst2_sst2") or p.name.startswith("tmp_")]
    assert leftovers == [], leftovers   # SST2's own PDB/CIF copies are removed

    # continue the walker in a second node: sidecar and state come from prod1
    prod2 = _node("prod", continue_from=prod1)
    res2 = run_sst2(job_dir=str(jd), node_id=prod2, **SHORT)
    assert res2["success"], res2
    n2 = read_node(str(jd), prod2)
    assert n2["status"] == "completed"
    assert n2["metadata"]["start_step"] == 100
    assert n2["metadata"]["final_step"] == 200
    side2 = json.loads((jd / "nodes" / prod2 / "artifacts" / "tempering.json").read_text())
    assert side2["restart_json"].endswith(f"{prod1}/artifacts/tempering.json")
    assert sum(side2["e_num"]) == 20

    # a bad ladder seals the node as failed with a stable code
    prod3 = _node("prod", parent_node_ids=[eq])
    res3 = run_sst2(job_dir=str(jd), node_id=prod3, **{**SHORT, "temperatures_kelvin": [330.0, 300.0]})
    assert res3["success"] is False and res3["code"] == "sst2_ladder_invalid"
    assert read_node(str(jd), prod3)["status"] == "failed"
