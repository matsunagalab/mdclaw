"""Full BPTI control: the six-residue SG fixture is insufficient for MD."""

import pytest
from tests.pipeline_helpers import fetch_pdb_node, require_topology_builder_stack

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_bpti_disulfides_survive_explicit_short_md(tmp_path):
    from mdclaw.study.workflow import bootstrap_md_workflow
    from mdclaw._node import create_node, read_node
    from mdclaw.structure.prepare_complex import prepare_complex
    from mdclaw.solvation import solvate_structure
    from mdclaw.amber.build_system import build_amber_system
    from mdclaw.simulation.minimize import run_minimization
    from mdclaw.simulation.equilibrate import run_equilibration

    require_topology_builder_stack()
    boot = bootstrap_md_workflow(str(tmp_path / "bpti"), "BPTI disulfide handoff regression")
    assert boot["success"], boot
    job = boot["job_dir"]
    parent = fetch_pdb_node(job, "1BPI")
    for kind, tool, params in [
        (
            "prep",
            prepare_complex,
            dict(
                select_chains=["A"],
                include_types=["protein"],
                process_ligands=False,
                cap_termini=False,
            ),
        ),
        ("solv", solvate_structure, dict(dist=10.0, salt=True, saltcon=0.0, water_model="opc")),
        (
            "topo",
            build_amber_system,
            dict(forcefield="ff19SB", water_model="opc", hmr=False, pablo_auto_download=False),
        ),
        ("min", run_minimization, dict(max_iterations=500, hmr=False)),
        (
            "eq",
            run_equilibration,
            dict(nvt_time_ns=0.01, npt_time_ns=0.02, hmr=False, timestep_fs=2.0, random_seed=1234),
        ),
    ]:
        node = create_node(job, kind, parent_node_ids=[parent])
        assert node["success"], node
        parent = node["node_id"]
        r = tool(job_dir=job, node_id=parent, **params)
        assert r["success"], r
        if kind == "topo":
            assert r["topology_validation"]["disulfides"]["expected_count"] == 3
            assert abs(r["system_net_charge_e"]) < 1e-3
        assert read_node(job, parent)["status"] == "completed"
