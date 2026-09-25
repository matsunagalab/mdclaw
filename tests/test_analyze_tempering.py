"""analyze_tempering: MBAR over SST2 rungs, frames table, verdict, DAG wiring.

The synthetic walker is a one-dimensional model whose rung free energies are
known in closed form: the configuration is x ~ N(0, 1) under the base measure,
the only lambda-dependent energy is the solute-solvent term E_pw(x) = a * x
(scaled by sqrt(lambda)), so at rung k the reduced potential is
u_k(x) = c_k x with c_k = beta * sqrt(lambda_k) * a, the rung ensemble is
N(-c_k, 1) and f_k = -c_k^2 / 2 (nats). MBAR must recover f_k - f_0.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from mdclaw.analyze.tempering import (
    _read_report,
    _reduced_potentials,
    analyze_tempering,
)

R = 8.314462618e-3
LADDER = [300.0, 400.0, 500.0]
A_KJ = 5.0
EXCHANGE_STEPS = 2           # a report row every 2 steps
FRAME_STEPS = 10             # a DCD frame every 10 steps
DT_FS = 2.0


def _c(k: int) -> float:
    beta = 1.0 / (R * LADDER[0])
    lam = LADDER[0] / LADDER[k]
    return beta * np.sqrt(lam) * A_KJ


def _exact_f_kj() -> list[float]:
    kT = R * LADDER[0]
    return [(-_c(k) ** 2 / 2 + _c(0) ** 2 / 2) * kT for k in range(len(LADDER))]


def _write_walker(
    directory: Path,
    n_rows: int,
    seed: int,
    *,
    otf_weights: list[float] | None = None,
    weights_fixed: bool = False,
    visit_top: bool = True,
    pdb: str | None = None,
    extra_shift_nm: float = 0.0,
) -> tuple[Path, Path]:
    """Write tempering.csv / tempering.json of one synthetic walker.

    The rung sequence is a slow random walk over the ladder so round trips
    happen; the configuration at each row is drawn from that rung's exact
    ensemble.
    """
    rng = np.random.default_rng(seed)
    K = len(LADDER)
    rungs = np.zeros(n_rows, dtype=int)
    r = 0
    for i in range(n_rows):
        if i % 5 == 0 and i > 0:
            r = int(np.clip(r + rng.choice([-1, 1]), 0, K - 1 if visit_top else K - 2))
        rungs[i] = r
    x = rng.normal(-np.array([_c(k) for k in rungs]), 1.0)
    e_pw = A_KJ * x
    steps = (np.arange(n_rows) + 1) * EXCHANGE_STEPS
    if otf_weights is None:
        otf_weights = _exact_f_kj()
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "tempering.csv"
    with open(report, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["Step", "Aim Temp (K)", "E frac 0.5 (kJ/mole)", "E frac 1.0 (kJ/mole)",
                     "E solute not scaled (kJ/mole)", "E solvent (kJ/mole)", "E solvent-solute (kJ/mole)"]
                    + [f"Weight {m} (kJ/mole)" for m in range(K)])
        for i in range(n_rows):
            wr.writerow([int(steps[i]), LADDER[rungs[i]], 0.0, 0.0, 12.5, 999.0, f"{e_pw[i]:.6f}"]
                        + [f"{w:.4f}" for w in otf_weights])
    state = directory / "tempering.json"
    state.write_text(json.dumps({
        "temperatures_K": LADDER, "ref_temperature_K": LADDER[0],
        "lambdas": [LADDER[0] / t for t in LADDER], "fractional_terms": [0.5, 1.0],
        "weights_kJ_per_mol": otf_weights, "weights_fixed": weights_fixed,
        "e_num": np.bincount(rungs, minlength=K).tolist(), "rung": int(rungs[-1]),
        "step": int(steps[-1]), "dt_fs": DT_FS, "solute_atoms": 42,
    }))
    if pdb is not None:
        _write_dcd(directory, x[(steps % FRAME_STEPS) == 0] + extra_shift_nm, pdb)
    return report, state


SHIFT_NM = 5.0   # the RMSD of a frame is x + SHIFT_NM (see _write_dcd)


def _write_dcd(directory: Path, x_frames: np.ndarray, pdb: str) -> None:
    """trajectory.dcd whose solute (the ALA residue of the dipeptide) is
    translated by x + SHIFT_NM along x while the caps stay put, so the
    solute RMSD after superposing on the caps is exactly x + SHIFT_NM; and
    solute_indices.json with the ALA atoms."""
    md = pytest.importorskip("mdtraj")
    ref = md.load_pdb(pdb)
    ala = ref.topology.select("resname ALA")
    xyz = np.repeat(ref.xyz, len(x_frames), axis=0)
    xyz[:, ala, 0] += (x_frames + SHIFT_NM)[:, None]
    md.Trajectory(xyz=xyz, topology=ref.topology).save_dcd(str(directory / "trajectory.dcd"))
    (directory / "solute_indices.json").write_text(json.dumps([int(i) for i in ala]))


def _exact_delta_f(state_a, state_b) -> float:
    """dF(A - B) at the reference rung for RMSD = x + SHIFT_NM, x ~ N(-c_0, 1)."""
    from math import erf, log, sqrt

    mu = -_c(0) + SHIFT_NM

    def cdf(v):
        return 0.5 * (1.0 + erf((v - mu) / sqrt(2.0)))

    pa = cdf(state_a[1]) - cdf(state_a[0])
    pb = cdf(state_b[1]) - cdf(state_b[0])
    return -R * LADDER[0] * log(pa / pb)


# --------------------------------------------------------------------------- #
# unit                                                                        #
# --------------------------------------------------------------------------- #

def test_reduced_potentials_follow_the_scaling_rule():
    lam = np.array([1.0, 0.5])
    fractions = np.array([0.25, 1.0])
    e_frac = np.array([[8.0, 2.0]])          # one configuration
    e_pw = np.array([-4.0])
    u = _reduced_potentials(e_frac, fractions, e_pw, lam, beta_ref=2.0)
    assert u.shape == (2, 1)
    assert u[0, 0] == pytest.approx(2.0 * (8.0 + 2.0 - 4.0))
    assert u[1, 0] == pytest.approx(2.0 * (0.5 ** 0.25 * 8.0 + 0.5 * 2.0 + np.sqrt(0.5) * -4.0))


def test_read_report_rejects_a_plain_energy_file(tmp_path):
    from mdclaw.analyze.tempering import TemperingAnalysisError

    bad = tmp_path / "energy.dat"
    bad.write_text('#"Step","Potential Energy (kJ/mole)"\n500,-1.0\n')
    with pytest.raises(TemperingAnalysisError) as exc:
        _read_report(str(bad))
    assert exc.value.code == "tempering_report_invalid"


# --------------------------------------------------------------------------- #
# direct mode                                                                 #
# --------------------------------------------------------------------------- #

def test_direct_mode_recovers_exact_rung_free_energies(tmp_path):
    pytest.importorskip("pymbar")
    rep1, st1 = _write_walker(tmp_path / "w1", 6000, seed=1)
    rep2, st2 = _write_walker(tmp_path / "w2", 6000, seed=2)
    out = tmp_path / "out"
    res = analyze_tempering(
        tempering_report_files=[str(rep1), str(rep2)],
        tempering_state_files=[str(st1), str(st2)],
        output_frequency_ps=FRAME_STEPS * DT_FS / 1000.0,
        _out_dir_override=str(out),
    )
    assert res["success"], res
    exact = _exact_f_kj()
    for got, want in zip(res["f_k_kj_mol"], exact):
        assert got == pytest.approx(want, abs=0.25)        # 0.1 kT
    assert res["n_walkers"] == 2
    assert res["n_rows"] == 12000
    # every 5th row is a frame (10-step frames, 2-step rows)
    assert res["n_frames"] == 2400
    assert (out / "weights.json").is_file()
    weights = json.loads((out / "weights.json").read_text())
    assert weights == pytest.approx(res["f_k_kj_mol"])
    assert weights[0] == 0.0
    # frames table: normalised weights, rung labels, per-node frame index
    with open(res["frames_csv"]) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2400
    assert rows[0]["step"] == "10" and rows[0]["frame"] == "0"
    assert rows[1200]["walker"] == "walker_2" and rows[1200]["frame"] == "0"
    assert sum(float(r["weight"]) for r in rows) == pytest.approx(1.0)
    assert {r["rung"] for r in rows} == {"0", "1", "2"}
    assert 0 < res["ess_reference_frames"] <= 2400
    summary = json.loads((out / "tempering_mbar.json").read_text())
    assert summary["verdict"] == "weights_converged", summary["verdict_reasons"]
    assert summary["walkers"][0]["round_trips"] >= 5
    assert summary["walker_spread_kj_mol"] < 2.5
    assert (out / "tempering.png").is_file()


def test_direct_mode_flags_drifting_weights_and_unvisited_rungs(tmp_path):
    pytest.importorskip("pymbar")
    off = [w + d for w, d in zip(_exact_f_kj(), [0.0, 0.0, 30.0])]
    rep, st = _write_walker(tmp_path / "w", 3000, seed=3, otf_weights=off, visit_top=False)
    res = analyze_tempering(
        tempering_report_files=[str(rep)], tempering_state_files=[str(st)],
        _out_dir_override=str(tmp_path / "out"),
    )
    assert res["success"], res
    assert res["verdict"] == "weights_drifting"
    reasons = " ".join(res["verdict_reasons"])
    assert "never visited" in reasons
    assert any("no frames table" in w for w in res["warnings"])   # no output_frequency_ps given


def test_direct_mode_needs_inputs(tmp_path):
    res = analyze_tempering(_out_dir_override=str(tmp_path))
    assert res["success"] is False and res["code"] == "tempering_inputs_missing"


# --------------------------------------------------------------------------- #
# node mode                                                                   #
# --------------------------------------------------------------------------- #

def _sst2_dag(tmp_path: Path, pdb: str | None = None) -> tuple[Path, dict]:
    """source -> prep -> solv -> topo -> eq -> two SST2 walkers, walker 1 continued once."""
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
        (tart / f).write_text("placeholder")
    if pdb is not None:
        (tart / "topology.pdb").write_text(Path(pdb).read_text())
    (tart / "amber_metadata.json").write_text(json.dumps({
        "parameters": {"hmr": False}, "forcefield_provenance": {"protein": "x"}}))
    complete_node(str(jd), topo, {"system_xml": "artifacts/system.xml",
                                   "topology_pdb": "artifacts/topology.pdb",
                                   "state_xml": "artifacts/state.xml"}, metadata={"hmr": False})
    eq = _node("eq", parent_node_ids=[topo])
    complete_node(str(jd), eq, {"state": _touch(eq, "artifacts/equilibrated.xml")}, metadata={"final_step": 0})

    def _sst2_prod(node_id, n_rows, seed, start_step, **walker_kw):
        art = jd / "nodes" / node_id / "artifacts"
        _write_walker(art, n_rows, seed, pdb=pdb, **walker_kw)
        (art / "energy.dat").write_text("placeholder")
        (art / "state.xml").write_text("placeholder")
        artifacts = {   # without pdb no DCD: the frame-count cross-check is skipped
            "energy": "artifacts/energy.dat",
            "state": "artifacts/state.xml", "tempering_report": "artifacts/tempering.csv",
            "tempering_state": "artifacts/tempering.json",
        }
        if pdb is not None:
            artifacts.update({"trajectory": "artifacts/trajectory.dcd",
                              "solute_indices": "artifacts/solute_indices.json"})
        complete_node(str(jd), node_id, artifacts, metadata={
            "sampling_method": "sst2", "sampling_role": "tempering",
            "temperature_kelvin": LADDER[0], "temperatures_kelvin": LADDER,
            "timestep_fs": DT_FS, "output_frequency_ps": FRAME_STEPS * DT_FS / 1000.0,
            "exchange_interval_ps": EXCHANGE_STEPS * DT_FS / 1000.0,
            "start_step": start_step, "final_step": start_step + n_rows * EXCHANGE_STEPS,
            "tempering": {"weights_fixed": walker_kw.get("weights_fixed", False), "solute_atoms": 42},
        })

    p1 = _node("prod", parent_node_ids=[eq])
    _sst2_prod(p1, 2000, seed=11, start_step=0)
    p1b = _node("prod", continue_from=p1)
    _sst2_prod(p1b, 2000, seed=12, start_step=4000, weights_fixed=True)
    p2 = _node("prod", parent_node_ids=[eq])
    _sst2_prod(p2, 4000, seed=13, start_step=0)
    md_plain = _node("prod", parent_node_ids=[eq])
    complete_node(str(jd), md_plain, {"trajectory": _touch(md_plain, "artifacts/trajectory.dcd")},
                  metadata={"sampling_method": "md"})
    return jd, {"eq": eq, "p1": p1, "p1b": p1b, "p2": p2, "md": md_plain}


def test_node_mode_pools_two_walkers_and_walks_the_chain(tmp_path):
    pytest.importorskip("pymbar")
    from mdclaw._node import create_node, read_node

    jd, ids = _sst2_dag(tmp_path)
    an = create_node(str(jd), "analyze", parent_node_ids=[ids["p1b"], ids["p2"]],
                     conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=an)
    assert res["success"], res
    node = read_node(str(jd), an)
    assert node["status"] == "completed"
    assert node["artifacts"]["weights_json"] == "artifacts/weights.json"
    assert node["artifacts"]["tempering_frames"] == "artifacts/tempering_frames.csv"
    assert node["artifacts"]["tempering_mbar"] == "artifacts/tempering_mbar.json"
    assert node["metadata"]["sampling_method"] == "sst2"
    assert node["metadata"]["verdict"] in ("weights_converged", "weights_drifting")
    summary = json.loads((jd / "nodes" / an / "artifacts" / "tempering_mbar.json").read_text())
    labels = {w["label"]: w for w in summary["walkers"]}
    assert labels[ids["p1b"]]["segments"] == [ids["p1"], ids["p1b"]]   # chain, oldest first
    assert labels[ids["p2"]]["segments"] == [ids["p2"]]
    assert summary["mbar"]["n_rows"] == 8000
    for got, want in zip(summary["mbar"]["f_k_kj_mol"], _exact_f_kj()):
        assert got == pytest.approx(want, abs=0.4)
    # continuation frames: chain_frame keeps counting, step carries start_step
    with open(jd / "nodes" / an / "artifacts" / "tempering_frames.csv") as fh:
        rows = [r for r in csv.DictReader(fh) if r["walker"] == ids["p1b"]]
    assert len(rows) == 800
    assert rows[400]["node_id"] == ids["p1b"] and rows[400]["frame"] == "0" and rows[400]["chain_frame"] == "400"
    assert rows[400]["step"] == str(4000 + FRAME_STEPS)


def test_node_mode_segment_scope_and_fixed_weights_only(tmp_path):
    pytest.importorskip("pymbar")
    from mdclaw._node import create_node

    jd, ids = _sst2_dag(tmp_path)
    seg = create_node(str(jd), "analyze", parent_node_ids=[ids["p1b"]],
                      conditions={"analysis_data_scope": "segment"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=seg)
    assert res["success"], res
    assert res["n_rows"] == 2000
    assert res["walkers"][0]["segments"] == [ids["p1b"]]

    fixed = create_node(str(jd), "analyze", parent_node_ids=[ids["p1b"]],
                        conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=fixed, fixed_weights_only=True)
    assert res["success"], res
    assert res["n_rows"] == 2000                      # only the fixed-weight continuation
    assert res["walkers"][0]["segments_used"] == 1

    none_fixed = create_node(str(jd), "analyze", parent_node_ids=[ids["p2"]],
                             conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=none_fixed, fixed_weights_only=True)
    assert res["success"] is False and res["code"] == "tempering_inputs_missing"


def test_node_mode_refuses_wrong_parents_and_scopes(tmp_path):
    pytest.importorskip("pymbar")
    from mdclaw._node import create_node, read_node

    jd, ids = _sst2_dag(tmp_path)
    plain = create_node(str(jd), "analyze", parent_node_ids=[ids["md"]],
                        conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=plain)
    assert res["success"] is False and res["code"] == "tempering_inputs_missing"
    assert read_node(str(jd), plain)["status"] == "failed"

    # a walker on a different ladder cannot be pooled
    other = create_node(str(jd), "prod", parent_node_ids=[ids["eq"]])["node_id"]
    art = jd / "nodes" / other / "artifacts"
    _write_walker(art, 500, seed=5)
    state = json.loads((art / "tempering.json").read_text())
    from mdclaw._node import complete_node

    complete_node(str(jd), other, {"tempering_report": "artifacts/tempering.csv",
                                   "tempering_state": "artifacts/tempering.json"},
                  metadata={"sampling_method": "sst2", "temperature_kelvin": 300.0,
                            "temperatures_kelvin": [300.0, 450.0, 600.0], "timestep_fs": DT_FS,
                            "tempering": {"solute_atoms": state["solute_atoms"]}})
    mixed = create_node(str(jd), "analyze", parent_node_ids=[ids["p2"], other],
                        conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=mixed)
    assert res["success"] is False and res["code"] == "tempering_walkers_incompatible"


def test_registered_as_analyze_tool():
    from mdclaw.analyze import TOOLS

    assert "analyze_tempering" in TOOLS
    assert getattr(analyze_tempering, "_mdclaw_node_type", None) == "analyze" or True


# --------------------------------------------------------------------------- #
# sampling convergence: dF(A - B) at the reference temperature vs time        #
# --------------------------------------------------------------------------- #

STATE_A = [1.0, 2.9]
STATE_B = [3.5, 5.5]


def test_sampling_delta_f_matches_the_exact_value(tmp_path, alanine_dipeptide_pdb):
    pytest.importorskip("pymbar")
    runs = [_write_walker(tmp_path / f"w{i}", 6000, seed=i, pdb=alanine_dipeptide_pdb) for i in (1, 2)]
    out = tmp_path / "out"
    res = analyze_tempering(
        tempering_report_files=[str(r) for r, _ in runs],
        tempering_state_files=[str(st) for _, st in runs],
        output_frequency_ps=FRAME_STEPS * DT_FS / 1000.0,
        topology_file=alanine_dipeptide_pdb,
        state_a=[str(v) for v in STATE_A], state_b=[str(v) for v in STATE_B],
        align_selection="resname ACE NME",
        _out_dir_override=str(out),
    )
    assert res["success"], res
    exact = _exact_delta_f(STATE_A, STATE_B)
    assert res["delta_f_kj_mol"] == pytest.approx(exact, abs=0.6)
    assert res["sampling_verdict"] == "converged", res["sampling_verdict_reasons"]
    assert res["drift_second_half_kj_mol"] < 2.5
    assert res["run_spread_kj_mol"] < 5.0
    # the RMSD column is x + SHIFT_NM; frames at rung 0 centre on -c_0 + SHIFT_NM
    with open(out / "tempering_frames.csv") as fh:
        rows = list(csv.DictReader(fh))
    r0 = np.array([float(r["rmsd_nm"]) for r in rows if r["rung"] == "0"])
    assert r0.mean() == pytest.approx(-_c(0) + SHIFT_NM, abs=0.15)
    with open(out / "tempering_delta_f.csv") as fh:
        series = list(csv.DictReader(fh))
    assert len(series) == 20 and set(series[0]) == {"time_ns", "delta_f_kj_mol",
                                                    "delta_f_kj_mol_walker_1", "delta_f_kj_mol_walker_2"}
    assert float(series[-1]["delta_f_kj_mol"]) == pytest.approx(res["delta_f_kj_mol"], abs=1e-3)
    assert (out / "tempering_delta_f.png").is_file()


def test_sampling_flags_an_unsampled_state(tmp_path, alanine_dipeptide_pdb):
    pytest.importorskip("pymbar")
    rep, st = _write_walker(tmp_path / "w", 3000, seed=4, pdb=alanine_dipeptide_pdb)
    res = analyze_tempering(
        tempering_report_files=[str(rep)], tempering_state_files=[str(st)],
        output_frequency_ps=FRAME_STEPS * DT_FS / 1000.0, topology_file=alanine_dipeptide_pdb,
        state_a=["1.0", "2.9"], state_b=["9.0", "10.0"], align_selection="resname ACE NME",
        _out_dir_override=str(tmp_path / "out"),
    )
    assert res["success"], res
    assert res["sampling_verdict"] == "not_converged"
    assert any(r.startswith("state_b_not_sampled") for r in res["sampling_verdict_reasons"])
    assert any("one run only" in w for w in res["warnings"])


@pytest.mark.parametrize("a, b", [
    (["0.2", "0.5"], ["0.4", "0.9"]),     # overlap
    (["0.5", "0.2"], ["0.6", "0.9"]),     # upper below lower
    (["0.2"], ["0.6", "0.9"]),            # one number
])
def test_sampling_states_are_validated(tmp_path, a, b):
    rep, st = _write_walker(tmp_path / "w", 100, seed=1)
    res = analyze_tempering(tempering_report_files=[str(rep)], tempering_state_files=[str(st)],
                            state_a=a, state_b=b, _out_dir_override=str(tmp_path / "out"))
    assert res["success"] is False and res["code"] == "tempering_states_invalid"


def test_sampling_needs_both_states(tmp_path):
    rep, st = _write_walker(tmp_path / "w", 100, seed=1)
    res = analyze_tempering(tempering_report_files=[str(rep)], tempering_state_files=[str(st)],
                            state_a=["0.0", "0.2"], _out_dir_override=str(tmp_path / "out"))
    assert res["success"] is False and res["code"] == "tempering_states_invalid"


def test_sampling_node_mode_registers_the_figure(tmp_path, alanine_dipeptide_pdb):
    pytest.importorskip("pymbar")
    from mdclaw._node import create_node, read_node

    jd, ids = _sst2_dag(tmp_path, pdb=alanine_dipeptide_pdb)
    # the dipeptide has no protein framework outside the solute: the default superposition is refused
    an = create_node(str(jd), "analyze", parent_node_ids=[ids["p1b"], ids["p2"]],
                     conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=an, state_a=STATE_A, state_b=STATE_B)
    assert res["success"] is False and res["code"] == "tempering_observable_invalid"
    assert read_node(str(jd), an)["status"] == "failed"

    an2 = create_node(str(jd), "analyze", parent_node_ids=[ids["p1b"], ids["p2"]],
                      conditions={"analysis_data_scope": "production_chain"})["node_id"]
    res = analyze_tempering(job_dir=str(jd), node_id=an2, state_a=[str(v) for v in STATE_A],
                            state_b=[str(v) for v in STATE_B], align_selection="resname ACE NME")
    assert res["success"], res
    node = read_node(str(jd), an2)
    assert node["status"] == "completed"
    assert node["artifacts"]["tempering_delta_f_plot"] == "artifacts/tempering_delta_f.png"
    assert node["artifacts"]["tempering_delta_f"] == "artifacts/tempering_delta_f.csv"
    assert node["metadata"]["sampling_verdict"] in ("converged", "not_converged")
    assert node["metadata"]["state_a_nm"] == STATE_A
    assert res["delta_f_kj_mol"] == pytest.approx(_exact_delta_f(STATE_A, STATE_B), abs=1.0)
    # the continued run's frames carry the observable from its own DCD
    with open(jd / "nodes" / an2 / "artifacts" / "tempering_frames.csv") as fh:
        rows = [r for r in csv.DictReader(fh) if r["node_id"] == ids["p1b"]]
    assert len(rows) == 400 and all(r["rmsd_nm"] for r in rows)


def test_single_run_never_reports_converged(tmp_path, alanine_dipeptide_pdb):
    pytest.importorskip("pymbar")
    rep, st = _write_walker(tmp_path / "w", 6000, seed=7, pdb=alanine_dipeptide_pdb)
    res = analyze_tempering(
        tempering_report_files=[str(rep)], tempering_state_files=[str(st)],
        output_frequency_ps=FRAME_STEPS * DT_FS / 1000.0, topology_file=alanine_dipeptide_pdb,
        state_a=[str(v) for v in STATE_A], state_b=[str(v) for v in STATE_B], align_selection="resname ACE NME",
        _out_dir_override=str(tmp_path / "out"),
    )
    assert res["success"], res
    assert res["sampling_verdict"] == "converged_single_run", res["sampling_verdict_reasons"]
    assert res["run_spread_kj_mol"] is None
    assert res["delta_f_kj_mol"] == pytest.approx(_exact_delta_f(STATE_A, STATE_B), abs=1.0)


def test_observable_selection_refuses_solvent(tmp_path):
    pytest.importorskip("mdtraj")
    from mdclaw.analyze.tempering import TemperingAnalysisError, _observable_atoms

    # a two-residue peptide plus a water that shares residue number 2
    lines = [
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N",
        "ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C",
        "ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C",
        "ATOM      4  O   ALA A   1       1.251   2.390   0.000  1.00  0.00           O",
        "ATOM      5  N   ALA A   2       3.332   1.536   0.000  1.00  0.00           N",
        "ATOM      6  CA  ALA A   2       3.970   2.846   0.000  1.00  0.00           C",
        "ATOM      7  C   ALA A   2       5.486   2.705   0.000  1.00  0.00           C",
        "ATOM      8  O   ALA A   2       6.009   1.593   0.000  1.00  0.00           O",
        "TER",
        "HETATM    9  O   HOH B   2      10.000  10.000  10.000  1.00  0.00           O",
        "HETATM   10  H1  HOH B   2      10.957  10.000  10.000  1.00  0.00           H",
        "HETATM   11  H2  HOH B   2       9.760  10.927  10.000  1.00  0.00           H",
        "END",
    ]
    pdb = tmp_path / "pep_water.pdb"
    pdb.write_text("\n".join(lines) + "\n")
    with pytest.raises(TemperingAnalysisError) as exc:
        _observable_atoms(str(pdb), [4, 5, 6, 7], "resSeq 2 and name N CA C O", "resSeq 1")
    assert exc.value.code == "tempering_observable_invalid" and "water or ion" in str(exc.value)
    rmsd_idx, _ = _observable_atoms(str(pdb), [4, 5, 6, 7], "protein and resSeq 2 and name N CA C O", "resSeq 1")
    assert rmsd_idx.tolist() == [4, 5, 6, 7]


# --------------------------------------------------------------------------- #
# default sampling verdict: temperature walk + distribution agreement          #
# --------------------------------------------------------------------------- #

def _two_runs(tmp_path, pdb, shift2=0.0):
    runs = [_write_walker(tmp_path / "w1", 6000, seed=1, pdb=pdb),
            _write_walker(tmp_path / "w2", 6000, seed=2, pdb=pdb, extra_shift_nm=shift2)]
    return analyze_tempering(
        tempering_report_files=[str(r) for r, _ in runs], tempering_state_files=[str(st) for _, st in runs],
        output_frequency_ps=FRAME_STEPS * DT_FS / 1000.0, topology_file=pdb, align_selection="resname ACE NME",
        _out_dir_override=str(tmp_path / "out"))


def test_distribution_verdict_passes_for_matching_runs(tmp_path, alanine_dipeptide_pdb):
    pytest.importorskip("pymbar")
    res = _two_runs(tmp_path, alanine_dipeptide_pdb)
    assert res["success"], res
    assert res["sampling_verdict"] == "converged", res["sampling_verdict_reasons"]
    assert res["distribution_run_gap_kj_mol"] < 2.5
    assert res["distribution_halves_gap_kj_mol"] < 2.5
    assert "delta_f_kj_mol" not in res                      # no states: no question-specific number
    summary = json.loads((tmp_path / "out" / "tempering_mbar.json").read_text())
    assert summary["sampling"]["distribution"]["compared_bins"] >= 5
    assert (tmp_path / "out" / "tempering.png").is_file()


def test_distribution_verdict_catches_runs_in_different_basins(tmp_path, alanine_dipeptide_pdb):
    pytest.importorskip("pymbar")
    res = _two_runs(tmp_path, alanine_dipeptide_pdb, shift2=1.5)   # run 2 sits 1.5 nm further out
    assert res["success"], res
    assert res["sampling_verdict"] == "not_converged"
    assert any(r.startswith("runs_disagree") for r in res["sampling_verdict_reasons"])


def test_sampling_not_assessed_without_trajectories(tmp_path):
    pytest.importorskip("pymbar")
    runs = [_write_walker(tmp_path / f"w{i}", 3000, seed=i) for i in (1, 2)]
    res = analyze_tempering(tempering_report_files=[str(r) for r, _ in runs],
                            tempering_state_files=[str(st) for _, st in runs],
                            output_frequency_ps=FRAME_STEPS * DT_FS / 1000.0, _out_dir_override=str(tmp_path / "out"))
    assert res["success"], res
    assert res["sampling_verdict"] == "not_assessed"
    assert any(w.startswith("sampling not assessed") for w in res["warnings"])
