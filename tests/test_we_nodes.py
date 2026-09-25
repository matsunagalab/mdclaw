"""A weighted ensemble on the DAG: rounds + we_resample + analyze_we.

Two carbons on a harmonic bond in a periodic box; the bond length is the
progress coordinate, stretched bonds are the target and are recycled to
the equilibrated state.
"""

import csv
import json

import pytest

from mdclaw._node import create_node, read_node
from mdclaw.rounds.driver import run_rounds
from mdclaw.rounds.scheme import segment_node_id, setup_rounds
from mdclaw.we.analysis import analyze_we
from mdclaw.we.policy import we_resample
from tests.test_rounds import STAGE_ARGS, _job_with_eq, periodic_triple  # noqa: F401  (fixture re-export)

PCOORD = [{"type": "distance", "name": "bond", "selection_group1": "name C1", "selection_group2": "name C2"}]
POLICY_ARGS = {
    "pcoord": PCOORD,
    "bins": {"edges": [[0.9, 1.0, 1.1]]},
    "walkers_per_bin": 2,
    "target": {"pcoord_ranges": [[1.25, None]]},
}


def _we_scheme(eq, **overrides):
    spec = {"scheme_id": "we1", "policy": "we_resample", "policy_args": POLICY_ARGS,
            "stage_tool": "run_production", "stage_args": STAGE_ARGS,
            "start": {"node_ids": [eq], "n_replicas": 4}, "seed": 5}
    spec.update(overrides)
    return spec


def test_weighted_ensemble_rounds_and_analysis(tmp_path, periodic_triple):  # noqa: F811
    jd, eq = _job_with_eq(tmp_path, periodic_triple)
    setup = setup_rounds(str(jd), _we_scheme(eq))
    assert setup["success"] is True, setup
    assert setup["scheme"]["initial_weights"] == "uniform"

    result = run_rounds(str(jd), "we1", max_rounds=3)
    assert result["success"] is True, result
    assert result["rounds_completed"] == 3 and result["failures"] == []

    # round 1: four walkers of weight 1/4
    for w in range(1, 5):
        node = read_node(str(jd), segment_node_id("we1", 1, w))
        assert node["status"] == "completed"
        assert node["metadata"]["scheme"]["weight"] == pytest.approx(0.25)

    total_events = 0
    for r in (1, 2, 3):
        policy = read_node(str(jd), f"analyze_we1_r{r:04d}")
        assert policy["status"] == "completed", policy.get("metadata")
        assert policy["metadata"]["analysis"] == "we_resample"
        assert set(policy["artifacts"]) >= {"next_round", "we_round", "we_pcoords"}
        ledger = json.loads((jd / "nodes" / policy["node_id"] / "artifacts" / "we_round.json").read_text())
        assert ledger["round"] == r and ledger["pcoord_names"] == ["bond"]
        assert ledger["weight_sum_out"] == pytest.approx(1.0) and abs(ledger["weight_residual"]) < 1e-9
        assert ledger["n_in"] == len(policy["parent_node_ids"])
        assert all(w["fate"] in {"continue", "split", "merged", "recycled"} for w in ledger["walkers"])
        for bin_entry in ledger["bins"]:
            assert bin_entry["n_out"] <= 2
        total_events += ledger["flux"]["events"]
        with (jd / "nodes" / policy["node_id"] / "artifacts" / "we_pcoords.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 10 * ledger["n_in"]           # 10 frames per segment
        assert {row["node_id"] for row in rows} == set(policy["parent_node_ids"])
        # the next round's segments carry the planned weights and lineage
        plan = json.loads((jd / "nodes" / policy["node_id"] / "artifacts" / "next_round.json").read_text())
        weights = 0.0
        for child in plan["children"]:
            segment = read_node(str(jd), segment_node_id("we1", r + 1, child["replica"]))
            meta = segment["metadata"]["scheme"]
            assert meta["weight"] == pytest.approx(child["weight"])
            weights += meta["weight"]
            if child.get("parent_node_id"):
                assert segment["metadata"]["continued_from"] == child["parent_node_id"]
            else:
                assert segment["parent_node_ids"] == [eq]
                assert meta["extra"]["recycled_from"] in policy["parent_node_ids"]
            assert segment["dependency_node_ids"] == [policy["node_id"]]
        assert weights == pytest.approx(1.0)
    second = read_node(str(jd), "analyze_we1_r0002")
    assert second["dependency_node_ids"] == ["analyze_we1_r0001"]

    # the terminal analysis over the last policy node; next names analyze_we first
    analysis = create_node(str(jd), "analyze", parent_node_ids=["analyze_we1_r0003"],
                           conditions={"analysis_data_scope": "production_chain"})
    assert analysis["success"], analysis
    from mdclaw._cli import _discover_tools
    from mdclaw._envelope import next_step

    step = next_step(str(jd), analysis["node_id"], _discover_tools())
    assert step["action"] == "run" and step["stage_tools"][0] == "analyze_we"
    outcome = analyze_we(job_dir=str(jd), node_id=analysis["node_id"])
    assert outcome["success"] is True, outcome
    scheme_out = outcome["schemes"]["we1"]
    assert scheme_out["n_rounds"] == 3 and scheme_out["recycle"] is True
    assert scheme_out["time_ns"] == pytest.approx(0.006)
    assert scheme_out["kinetics"]["mode"] == "steady_state_flux"
    assert outcome["verdict"] in {"flux_steady", "rate_not_converged", "flux_transient", "flux_undersampled",
                                  "no_target_events"}
    if total_events == 0:
        assert outcome["verdict"] == "no_target_events"
        assert "we_convergence" not in outcome["artifacts"] and outcome["convergence"] == {}
    else:
        # the convergence history: the rate reported had the run stopped after each round
        conv = scheme_out["kinetics"]["convergence"]
        assert conv is not None and "history" not in conv and conv["tolerance_kt"] == 1.0
        assert outcome["convergence"]["we1"]["tolerance_kt"] == 1.0
        assert set(outcome["artifacts"]) >= {"we_convergence"}
        with (jd / "nodes" / analysis["node_id"] / "artifacts" / "we_convergence.csv").open() as fh:
            history = list(csv.DictReader(fh))
        assert history and history[-1]["scheme_id"] == "we1" and int(history[-1]["round"]) == 3
        full = json.loads((jd / "nodes" / analysis["node_id"] / "artifacts" / "we_kinetics.json").read_text())
        assert full["schemes"]["we1"]["kinetics"]["convergence"]["history"][-1]["round"] == 3
        if "we_convergence_plot" in outcome["artifacts"]:
            assert (jd / "nodes" / analysis["node_id"] / outcome["artifacts"]["we_convergence_plot"]).is_file()
    node = read_node(str(jd), analysis["node_id"])
    assert node["status"] == "completed" and node["metadata"]["analysis"] == "we_kinetics"
    artifacts_dir = jd / "nodes" / analysis["node_id"] / "artifacts"
    assert (artifacts_dir / "we_kinetics.json").is_file()
    with (artifacts_dir / "we_iterations.csv").open() as fh:
        iterations = list(csv.DictReader(fh))
    assert [int(row["round"]) for row in iterations] == [1, 2, 3]
    with (artifacts_dir / "we_frames.csv").open() as fh:
        frames = list(csv.DictReader(fh))
    assert len(frames) == 10 * sum(int(row["n_walkers"]) for row in iterations)
    assert all(float(row["weight"]) > 0 for row in frames)
    with (artifacts_dir / "we_bins.csv").open() as fh:
        bins = list(csv.DictReader(fh))
    assert bins and all(float(row["mean_weight"]) > 0 for row in bins)
    if "box_volume_nm3" in scheme_out["kinetics"]:
        assert scheme_out["kinetics"]["box_volume_nm3"] == pytest.approx(27.0, rel=1e-3)


def test_setup_validates_the_policy_and_we_resample_runs_by_hand(tmp_path, periodic_triple):  # noqa: F811
    jd, eq = _job_with_eq(tmp_path, periodic_triple)
    bad = setup_rounds(str(jd), _we_scheme(eq, policy_args={"bins": {"edges": [[1.0]]}}))
    assert bad["success"] is False and bad["code"] == "we_policy_args_invalid", bad
    typo = setup_rounds(str(jd), _we_scheme(eq, policy_args={
        **POLICY_ARGS,
        "pcoord": [{"type": "distance", "name": "bond", "selection_group1": "name C1",
                    "selection_group2": "name XX"}]}))
    assert typo["success"] is False and typo["code"] == "cv_selection_invalid", typo
    inside = setup_rounds(str(jd), _we_scheme(eq, policy_args={**POLICY_ARGS, "target": {"pcoord_ranges": [[0.5, None]]}}))
    assert inside["success"] is False and inside["code"] == "we_start_in_target", inside
    assert "sampling_schemes" not in json.loads((jd / "progress.json").read_text())["params"]

    # a valid scheme records the start structure's pcoord (from the eq state XML here)
    ok = setup_rounds(str(jd), _we_scheme(eq))
    assert ok["success"] is True, ok
    assert ok["scheme"]["start_pcoords"] == {eq: [pytest.approx(1.0, abs=0.05)]}
    assert run_rounds(str(jd), "we1", max_rounds=1)["success"]

    # inspect_rounds carries the policy's ledger and waits on running nodes
    from mdclaw._node import update_node_status
    from mdclaw.rounds.scheme import inspect_rounds

    seen = inspect_rounds(str(jd), "we1")
    assert seen["rounds"][0]["policy_summary"]["n_out"] >= 1
    assert seen["busy"] is False and "run_rounds" in seen["next_action"]
    update_node_status(str(jd), segment_node_id("we1", 2, 1), "running")
    waiting = inspect_rounds(str(jd), "we1")
    assert waiting["busy"] is True and waiting["next_action"].startswith("wait")
    assert waiting["next"]["action"] == "wait" and waiting["next"]["node_id"] in waiting["next"]["wait_command"]
    assert segment_node_id("we1", 2, 1) in waiting["rounds"][1]["running"]

    # the policy can be run by hand on a plain analyze node with explicit arguments
    parents = [segment_node_id("we1", 1, w) for w in range(1, 5)]
    node = create_node(str(jd), "analyze", parent_node_ids=parents,
                       conditions={"analysis_data_scope": "segment"})
    fixed = we_resample(job_dir=str(jd), node_id=node["node_id"], pcoord=PCOORD, bins={"edges": [[1.0]]},
                        walkers_per_bin=2, target={"pcoord_ranges": [[1.25, None]]}, basis_node_ids=[eq])
    assert fixed["success"] is True, fixed
    assert fixed["scheme_id"] is None and fixed["n_out"] >= 1
    assert read_node(str(jd), node["node_id"])["status"] == "completed"


def test_intermolecular_target_beyond_half_box_is_refused_at_setup(tmp_path, periodic_triple):  # noqa: F811
    jd, eq = _job_with_eq(tmp_path, periodic_triple)
    # break the bond in the topology copy so the two carbons are two molecules
    topo = read_node(str(jd), eq)["parent_node_ids"][0]
    pdb = jd / "nodes" / topo / "artifacts" / "topology.pdb"
    pdb.write_text("\n".join(line for line in pdb.read_text().splitlines() if not line.startswith("CONECT")) + "\n")
    args = {**POLICY_ARGS, "target": {"pcoord_ranges": [[1.6, None]]}}
    result = setup_rounds(str(jd), _we_scheme(eq, policy_args=args))
    assert result["success"] is False and result["code"] == "we_target_exceeds_half_box", result


def test_analyze_we_needs_policy_parents(tmp_path, periodic_triple):  # noqa: F811
    jd, eq = _job_with_eq(tmp_path, periodic_triple)
    setup_rounds(str(jd), _we_scheme(eq))
    assert run_rounds(str(jd), "we1", max_rounds=1)["success"]
    wrong = create_node(str(jd), "analyze", parent_node_ids=[segment_node_id("we1", 1, 1)],
                        conditions={"analysis_data_scope": "segment"})
    result = analyze_we(job_dir=str(jd), node_id=wrong["node_id"])
    assert result["success"] is False and result["code"] == "we_inputs_missing"
    assert read_node(str(jd), wrong["node_id"])["status"] == "pending"
