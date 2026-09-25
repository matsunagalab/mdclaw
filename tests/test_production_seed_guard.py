"""run_production refuses a sibling that would repeat a completed segment.

The effective integrator seed of a restarted segment is derived from
``random_seed`` and the restart ancestor's step count alone, so two children
of one ancestor that share a seed are the same trajectory.
"""

from mdclaw._node import create_node, init_progress_v3, read_node
from mdclaw.simulation.production import run_production
from mdclaw.simulation.restart import _sibling_seed_collision
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node


def _job_with_eq(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    init_progress_v3(str(jd))
    src = create_node(str(jd), "source")["node_id"]
    complete_node(str(jd), src, {"structure_file": "artifacts/x.cif"})
    prep = create_node(str(jd), "prep", parent_node_ids=[src])["node_id"]
    complete_node(str(jd), prep, {"merged_pdb": "artifacts/x.pdb"})
    solv = create_node(str(jd), "solv", parent_node_ids=[prep])["node_id"]
    complete_node(str(jd), solv, {"solvated_pdb": "artifacts/x.pdb",
                                  "box_dimensions": "artifacts/x.json"})
    topo = create_node(str(jd), "topo", parent_node_ids=[solv])["node_id"]
    complete_node(str(jd), topo, {"system_xml": "artifacts/system.xml",
                                  "topology_pdb": "artifacts/topology.pdb",
                                  "state_xml": "artifacts/state.xml"},
                  metadata={"hmr": False})
    eq = create_node(str(jd), "eq", parent_node_ids=[topo])["node_id"]
    complete_node(str(jd), eq, {"state": "artifacts/equilibrated.xml"},
                  metadata={"final_step": 0, "final_ensemble": "NVT"})
    return jd, eq


def test_sibling_seed_collision_is_detected_only_for_completed_same_seed_siblings(tmp_path):
    jd, eq = _job_with_eq(tmp_path)
    first = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
    complete_node(str(jd), first, {"trajectory": "artifacts/trajectory.dcd",
                                   "state": "artifacts/state.xml"},
                  metadata={"random_seed": 7, "final_step": 100})
    pending_sibling = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
    second = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]

    hit = _sibling_seed_collision(str(jd), second, eq, 7)
    assert hit == {"sibling_node_id": first, "restart_node_id": eq, "random_seed": 7}
    assert _sibling_seed_collision(str(jd), second, eq, 8) is None
    assert _sibling_seed_collision(str(jd), second, eq, None) is None
    # a pending sibling produced nothing, so it is not a collision partner
    assert _sibling_seed_collision(str(jd), pending_sibling, eq, 7)["sibling_node_id"] == first
    # a continuation of the completed prod restarts from it, not from eq
    child = create_node(str(jd), "prod", continue_from=first)["node_id"]
    assert _sibling_seed_collision(str(jd), child, first, 7) is None


def test_run_production_refuses_the_repeated_seed_and_leaves_the_node_pending(tmp_path):
    jd, eq = _job_with_eq(tmp_path)
    first = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
    complete_node(str(jd), first, {"trajectory": "artifacts/trajectory.dcd",
                                   "state": "artifacts/state.xml"},
                  metadata={"random_seed": 7, "final_step": 100})
    second = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]

    result = run_production(job_dir=str(jd), node_id=second, random_seed=7,
                            platform="Reference", hmr=False)
    assert result["success"] is False
    assert result["code"] == "production_sibling_seed_collision"
    assert first in result["message"]
    assert any("--allow-seed-reuse" in hint for hint in result["hints"])
    # nothing ran: the node is not spent
    assert read_node(str(jd), second)["status"] == "pending"

    # a different seed, or an explicit replay, passes the guard (and then
    # fails on the placeholder topology, which is a different code)
    other = run_production(job_dir=str(jd), node_id=second, random_seed=8,
                           platform="Reference", hmr=False)
    assert other.get("code") != "production_sibling_seed_collision"
    third = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
    replay = run_production(job_dir=str(jd), node_id=third, random_seed=7,
                            platform="Reference", hmr=False, allow_seed_reuse=True)
    assert replay.get("code") != "production_sibling_seed_collision"


def test_biased_siblings_do_not_count_as_collisions(tmp_path):
    jd, eq = _job_with_eq(tmp_path)
    steered = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
    complete_node(str(jd), steered, {"trajectory": "artifacts/trajectory.dcd",
                                     "state": "artifacts/state.xml"},
                  metadata={"random_seed": 7, "final_step": 100, "sampling_role": "steered",
                            "steering": {"mode": "steered"}})
    plain = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
    # the steered sibling integrated a different System: same seed, different trajectory
    assert _sibling_seed_collision(str(jd), plain, eq, 7) is None
    result = run_production(job_dir=str(jd), node_id=plain, random_seed=7,
                            platform="Reference", hmr=False)
    assert result.get("code") != "production_sibling_seed_collision"
