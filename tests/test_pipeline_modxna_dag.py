"""Level 3: optional real modXNA branch integration test using PDB 6JV5."""
from __future__ import annotations

import os

import pytest

from tests.pipeline_helpers import fetch_pdb_node, node_artifact, require_tleap

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_modified_nucleic_branch_resolves_into_topology(tmp_path):
    from mdclaw._node import create_node, read_node
    from mdclaw.amber_server import build_amber_system
    from mdclaw.research_server import inspect_molecules
    from mdclaw.structure_server import prepare_complex, prepare_modified_nucleic

    modxna_dir = os.environ.get("MDCLAW_MODXNA_DIR")
    if not modxna_dir:
        pytest.skip("MDCLAW_MODXNA_DIR is not set")
    if not (os.path.exists(os.path.join(modxna_dir, "modxna.sh"))):
        pytest.skip("MDCLAW_MODXNA_DIR does not contain modxna.sh")
    require_tleap()

    job_dir = tmp_path / "job_6jv5_modxna"

    fetch_id = fetch_pdb_node(job_dir, "6JV5")
    inspected = inspect_molecules(str(node_artifact(job_dir, fetch_id, "structure_file")))
    assert inspected["success"], inspected.get("errors")
    assert inspected["summary"]["modified_nucleic_residues"] == [
        {"chain": "A", "resname": "5CM"}
    ]

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
    target = next(
        entry for entry in prepared["residue_mapping"] if entry["source_resname"] == "5CM"
    )

    branch = create_node(str(job_dir), "prep", parent_node_ids=[prep["node_id"]])
    assert branch["success"], branch
    modified = prepare_modified_nucleic(
        modifications=[
            {
                "chain": target["source_chain"],
                "resnum": target["source_resnum"],
                "source_resname": "5CM",
                "backbone": "DPO",
                "sugar": "DC2",
                "base": "M5C",
            }
        ],
        modxna_dir=modxna_dir,
        job_dir=str(job_dir),
        node_id=branch["node_id"],
    )
    assert modified["success"], modified.get("errors")
    assert modified["modxna_params"]

    branch_node = read_node(str(job_dir), branch["node_id"])
    assert branch_node["artifacts"]["merged_pdb"] == "artifacts/modified_nucleic.pdb"
    assert branch_node["artifacts"]["modxna_params"] == "artifacts/modxna_params.json"
    assert branch_node["metadata"]["has_modified_nucleic"] is True

    topo = create_node(str(job_dir), "topo", parent_node_ids=[branch["node_id"]])
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
    assert topo_node["metadata"]["modxna_params"]
