from pathlib import Path

from openmm.app import PDBFile

from mdclaw.structure_server import (
    _enrich_chain_identity_map,
    _index_prepared_component_sources,
    merge_structures,
)


def _write_tiny_pdb(path: Path, chain_id: str = "A") -> None:
    path.write_text(
        f"ATOM      1  N   GLY {chain_id}   1       0.000   0.000   0.000  "
        "1.00  0.00           N  \n"
        f"ATOM      2  CA  GLY {chain_id}   1       1.000   0.000   0.000  "
        "1.00  0.00           C  \n"
        f"TER       3      GLY {chain_id}   1\n"
        "END\n"
    )


def test_merge_structures_reuses_pdb_chain_ids_after_pool_exhaustion(tmp_path):
    pdb_files = []
    for index in range(65):
        pdb_file = tmp_path / f"chain_{index:03d}.pdb"
        _write_tiny_pdb(pdb_file)
        pdb_files.append(str(pdb_file))

    result = merge_structures(
        pdb_files=pdb_files,
        output_dir=str(tmp_path / "out"),
        output_name="many_chains",
    )

    assert result["success"] is True
    assert result["statistics"]["total_chains"] == 65
    assert result["statistics"]["unique_pdb_chain_ids"] == 62
    assert result["statistics"]["pdb_chain_id_reuse_count"] == 3
    assert Path(result["chain_identity_map_file"]).exists()

    components = result["chain_identity_map"]["components"]
    assert len(components) == 65
    assert len({c["component_id"] for c in components}) == 65
    assert [c["topology_chain_index"] for c in components] == list(range(65))
    assert components[0]["md_chain_id"] == "A"
    assert components[62]["md_chain_id"] == "A"
    assert components[0]["component_id"] != components[62]["component_id"]

    topology = PDBFile(result["output_file"]).topology
    assert topology.getNumChains() == 65


def test_chain_identity_map_can_be_enriched_with_source_label_and_author(tmp_path):
    prepared = tmp_path / "protein_1.amber.pdb"
    _write_tiny_pdb(prepared, chain_id="B")

    source_index = _index_prepared_component_sources(
        chain_info_map={
            "L1": {
                "chain_id": "L1",
                "author_chain": "BBB",
                "chain_type": "protein",
                "unique_id": None,
                "file": str(tmp_path / "protein_1.pdb"),
            }
        },
        proteins=[
            {
                "success": True,
                "chain_id": "L1",
                "input_file": str(tmp_path / "protein_1.pdb"),
                "output_file": str(prepared),
            }
        ],
        nucleics=[],
        glycans=[],
        ligands=[],
        ion_files=[],
    )
    chain_identity_map = {
        "schema_version": "mdclaw.chain_identity_map.v1",
        "components": [
            {
                "component_id": "component_000001",
                "source_file": str(prepared),
                "source_chain_id": "B",
                "topology_chain_index": 0,
                "md_chain_id": "A",
            }
        ],
    }

    enriched = _enrich_chain_identity_map(chain_identity_map, source_index)

    component = enriched["components"][0]
    assert component["source_label_asym_id"] == "L1"
    assert component["source_auth_asym_id"] == "BBB"
    assert component["source_chain_type"] == "protein"
    assert component["prepared_fragment_role"] == "protein"
