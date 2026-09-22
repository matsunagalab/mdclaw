"""analyze_metadynamics: dF(t) between two states from deposited Gaussians, verdict, DAG wiring."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from mdclaw.analyze.metadynamics import (
    MetadynamicsAnalysisError,
    _read_report,
    analyze_metadynamics,
    delta_f_vs_time,
)

KT = 8.314462618e-3 * 300.0
MANIFEST = {"sampling_method": "metadynamics", "distance_cv": {"name": "d", "selection_group1": "index 0", "selection_group2": "index 1"},
            "cv_min_nm": 0.0, "cv_max_nm": 2.0, "bias_width_nm": 0.05, "grid_points": 201, "grid_min_nm": -0.2, "grid_max_nm": 2.2,
            "temperature_kelvin": 300.0, "bias_factor": 5.0, "bias_height_kj_mol": 1.0, "deposition_interval_ps": 1.0}


def _write_walker(directory: Path, centres: np.ndarray, heights: np.ndarray, *, manifest=MANIFEST) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "metadynamics.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["step", "time_ps", "d_nm", "bias_at_cv_kj_mol", "gaussian_height_kj_mol"])
        for i, (c, h) in enumerate(zip(centres, heights)):
            wr.writerow([250 * (i + 1), f"{float(i + 1):.1f}", f"{c:.6f}", "0", f"{h:.6f}"])
    (directory / "metadynamics.json").write_text(json.dumps({"manifest": manifest, "grid_nm": []}))
    return directory / "metadynamics.csv"


def test_delta_f_matches_the_analytic_two_plateau_case():
    """Gaussians laid densely and evenly over A raise V there by a constant; with V = 0 in B,
    dF = -gamma/(gamma-1) * V_A - kT ln(width_A / width_B) exactly (up to grid quadrature)."""
    grid = np.linspace(0.0, 2.0, 2001)
    sigma = 0.05
    centres = np.tile(np.linspace(0.2, 0.8, 61), 20)           # 20 sweeps over A = [0.2, 0.8]
    heights = np.full(len(centres), 0.1)
    t = np.arange(1, len(centres) + 1, dtype=float)
    time_ns, dF = delta_f_vs_time([(t, centres, heights)], grid=grid, sigma=sigma, gamma=5.0, kT=KT,
                                  state_a=(0.3, 0.7), state_b=(1.2, 1.6), n_time_points=10)
    # V on the plateau: sum over the 61-point comb, 20 sweeps: h * 20 * sum_j exp(-(x - x_j)^2 / 2 sigma^2) ~ h*20*sqrt(2 pi) sigma / dx
    dx = 0.6 / 60
    V_A = 0.1 * 20 * np.sqrt(2 * np.pi) * sigma / dx
    expected = -(5.0 / 4.0) * V_A - KT * np.log(0.4 / 0.4)
    assert dF[-1] == pytest.approx(expected, abs=0.3)
    # deposition pattern is stationary -> dF grows linearly with time, so the curve is monotone
    assert np.all(np.diff(dF) < 0)


def test_read_report_rejects_other_csv(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(MetadynamicsAnalysisError) as exc:
        _read_report(str(bad))
    assert exc.value.code == "metadynamics_report_invalid"


def test_direct_mode_verdicts(tmp_path):
    rng = np.random.default_rng(1)
    # converged: the same random pattern over both states from the start, heights decaying (well-tempered)
    n = 4000
    centres = np.where(rng.random(n) < 0.5, rng.uniform(0.2, 0.8, n), rng.uniform(1.2, 1.8, n))
    heights = 1.0 * np.exp(-np.arange(n) / 800.0)
    r1 = _write_walker(tmp_path / "w1" / "artifacts", centres, heights)
    r2 = _write_walker(tmp_path / "w2" / "artifacts", centres[::-1], heights)
    res = analyze_metadynamics(state_a=["0.2", "0.8"], state_b=["1.2", "1.8"], metadynamics_report_files=[str(r1), str(r2)],
                               _out_dir_override=str(tmp_path / "out"))
    assert res["success"], res
    assert res["verdict"] == "converged" and res["n_walkers"] == 2
    assert Path(res["delta_f_csv"]).is_file() and Path(res["summary_json"]).is_file()
    assert abs(res["walkers"][0]["fraction_in_state_a"] - 0.5) < 0.05
    # drifting: all of B's Gaussians come in the second half
    centres2 = np.concatenate([rng.uniform(0.2, 0.8, n // 2), rng.uniform(1.2, 1.8, n // 2)])
    r3 = _write_walker(tmp_path / "w3" / "artifacts", centres2, np.full(n, 0.5))
    res2 = analyze_metadynamics(state_a=["0.2", "0.8"], state_b=["1.2", "1.8"], metadynamics_report_files=[str(r3)],
                                _out_dir_override=str(tmp_path / "out2"))
    assert res2["success"] and res2["verdict"] == "not_converged" and "profile_drifting" in res2["verdict_reasons"]
    assert any(w.startswith("gaussian_height_not_decayed") for w in res2["warnings"])
    # unequal residence between walkers is warned
    r4 = _write_walker(tmp_path / "w4" / "artifacts", rng.uniform(1.2, 1.8, n), heights)
    res3 = analyze_metadynamics(state_a=["0.2", "0.8"], state_b=["1.2", "1.8"], metadynamics_report_files=[str(r1), str(r4)],
                                _out_dir_override=str(tmp_path / "out3"))
    assert res3["success"] and any(w.startswith("walkers_unequal_residence") for w in res3["warnings"])
    # bad states
    for a, b in ((["0.8", "0.2"], ["1.2", "1.8"]), (["0.2", "1.3"], ["1.2", "1.8"]), (["0.2", "0.8"], ["1.5", "3.0"])):
        r = analyze_metadynamics(state_a=a, state_b=b, metadynamics_report_files=[str(r1)], _out_dir_override=str(tmp_path / "o"))
        assert r["success"] is False and r["code"] == "metadynamics_states_invalid"
    # incompatible walkers
    r5 = _write_walker(tmp_path / "w5" / "artifacts", centres, heights, manifest={**MANIFEST, "bias_factor": 9.0})
    r = analyze_metadynamics(state_a=["0.2", "0.8"], state_b=["1.2", "1.8"], metadynamics_report_files=[str(r1), str(r5)],
                             _out_dir_override=str(tmp_path / "o2"))
    assert r["success"] is False and r["code"] == "metadynamics_walkers_incompatible"


def _triple(d: Path) -> Path:
    import openmm
    from openmm import unit
    from openmm.app import PDBFile, Topology, element

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
    d.mkdir(parents=True, exist_ok=True)
    (d / "system.xml").write_text(openmm.XmlSerializer.serialize(system))
    positions = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]] * unit.nanometer
    with open(d / "topology.pdb", "w") as fh:
        PDBFile.writeFile(top, positions, fh, keepIds=True)
    ctx = openmm.Context(system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
    ctx.setPositions(positions)
    ctx.setVelocitiesToTemperature(300 * unit.kelvin, 3)
    (d / "state.xml").write_text(openmm.XmlSerializer.serialize(ctx.getState(getPositions=True, getVelocities=True)))
    return d


def test_node_mode_from_real_walkers(tmp_path):
    """Two toy run_metadynamics walkers sharing a bias, then the analyze node over them."""
    from mdclaw._node import create_node, read_node
    from mdclaw.simulation.metadynamics import run_metadynamics
    from tests.test_metadynamics import SHORT, _job

    triple = _triple(tmp_path / "triple")
    jd, eq, _node = _job(triple, tmp_path)
    shared = tmp_path / "shared"
    w1 = _node("prod", parent_node_ids=[eq])
    assert run_metadynamics(job_dir=str(jd), node_id=w1, bias_dir=str(shared), **{**SHORT, "random_seed": 1})["success"]
    w2 = _node("prod", parent_node_ids=[eq])
    assert run_metadynamics(job_dir=str(jd), node_id=w2, bias_dir=str(shared), **{**SHORT, "random_seed": 2})["success"]
    an = create_node(str(jd), "analyze", parent_node_ids=[w1, w2], conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_metadynamics(job_dir=str(jd), node_id=an, state_a=["0.7", "0.95"], state_b=["1.05", "1.3"])
    assert res["success"], res
    n = read_node(str(jd), an)
    assert n["status"] == "completed" and n["metadata"]["analysis"] == "metadynamics_delta_f"
    assert n["artifacts"]["metadynamics_delta_f"] == "artifacts/metadynamics_delta_f.csv"
    assert res["verdict"] in ("converged", "not_converged") and res["n_walkers"] == 2
    # the segment scope analyzes the leaf block only and gives the same wiring
    seg = create_node(str(jd), "analyze", parent_node_ids=[w1], conditions={"analysis_data_scope": "segment"})["node_id"]
    r = analyze_metadynamics(job_dir=str(jd), node_id=seg, state_a=["0.7", "0.95"], state_b=["1.05", "1.3"])
    assert r["success"] and r["n_walkers"] == 1 and read_node(str(jd), seg)["metadata"]["verdict"] == r["verdict"]
    # bad states seal the node as failed with a stable code
    bad = create_node(str(jd), "analyze", parent_node_ids=[w1], conditions={"analysis_data_scope": "segment"})["node_id"]
    r = analyze_metadynamics(job_dir=str(jd), node_id=bad, state_a=["0.95", "0.7"], state_b=["1.05", "1.3"])
    assert r["success"] is False and r["code"] == "metadynamics_states_invalid"
    assert read_node(str(jd), bad)["status"] == "failed"
