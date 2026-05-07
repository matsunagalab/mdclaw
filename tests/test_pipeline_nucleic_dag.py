"""Level 3: standard nucleic-acid node-DAG topology integration tests."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import fetch_pdb_node, require_tleap

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skip(
        reason=(
            'PR3 of openmmforcefields-unification: build_amber_system now '
            'emits system.xml + topology.pdb + state.xml instead of '
            'parm7/rst7. Pipeline tests will be re-enabled after PR5 '
            'migrates run_equilibration / run_production to the new triple.'
        )
    ),
]


@pytest.mark.parametrize(
    ("pdb_id", "expected_subtype", "expected_library"),
    [
        ("5MVQ", "dna", "leaprc.DNA.OL15"),
        ("4RBQ", "rna", "leaprc.RNA.OL3"),
    ],
)
def test_standard_nucleic_pdb_builds_real_topology(
    tmp_path, pdb_id, expected_subtype, expected_library
):
    from mdclaw._node import create_node, read_node
    from mdclaw.amber_server import build_amber_system
    from mdclaw.research_server import inspect_molecules
    from mdclaw.structure_server import prepare_complex
    from tests.pipeline_helpers import node_artifact

    require_tleap()
    job_dir = tmp_path / f"job_{pdb_id.lower()}_nucleic"

    fetch_id = fetch_pdb_node(job_dir, pdb_id)
    inspected = inspect_molecules(str(node_artifact(job_dir, fetch_id, "structure_file")))
    assert inspected["success"], inspected.get("errors")
    assert expected_subtype in set(inspected["summary"]["nucleic_subtypes"].values())

    prep = create_node(str(job_dir), "prep", parent_node_ids=[fetch_id])
    assert prep["success"], prep
    prepared = prepare_complex(
        job_dir=str(job_dir),
        node_id=prep["node_id"],
        include_types=["nucleic"],
        process_proteins=False,
        process_ligands=False,
    )
    assert prepared["success"], prepared.get("errors")
    assert prepared["preparation_summary"]["has_nucleic"] is True
    assert prepared["residue_mapping"]

    topo = create_node(str(job_dir), "topo", parent_node_ids=[prep["node_id"]])
    assert topo["success"], topo
    built = build_amber_system(
        job_dir=str(job_dir),
        node_id=topo["node_id"],
        forcefield="ff14SB",
        water_model="tip3p",
    )
    assert built["success"], built.get("errors")
    topo_node = read_node(str(job_dir), topo["node_id"])
    assert topo_node["artifacts"]["parm7"]
    assert topo_node["artifacts"]["rst7"]
    assert topo_node["metadata"]["nucleic_libraries"] == [expected_library]
