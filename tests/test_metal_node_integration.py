"""Tests for metal_server.parameterize_metal_ion node integration.

We mock the `metalpdb2mol2` subprocess (an AmberTools binary) so these
tests run under the Level-1 suite without requiring the conda env. The
goal is to verify wiring between prep node + metal_params artifact +
build_amber_system's DAG auto-resolution — the actual pyMSMT behavior
is exercised in the end-to-end integration test.
"""

import textwrap

import pytest

from mdclaw._node import (
    complete_node,
    create_node,
    find_ancestor_artifact,
    init_progress_v3,
    read_node,
    resolve_node_inputs,
)

ZN_PROTEIN_PDB = textwrap.dedent("""\
ATOM      1  N   ALA A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A   1       2.450   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A   1       3.000   2.400   1.000  1.00 10.00           C
ATOM      4  O   ALA A   1       2.300   3.400   1.000  1.00 10.00           O
ATOM      5  CB  ALA A   1       3.000   0.200   2.200  1.00 10.00           C
TER
HETATM    6 ZN    ZN A 101       5.000   5.000   5.000  1.00 15.00          ZN
END
""")


@pytest.fixture
def prep_node_with_merged_pdb(tmp_path):
    """Job with a completed prep node containing a merged.pdb with a Zn ion."""
    jd = tmp_path / "job_metal"
    jd.mkdir()
    init_progress_v3(str(jd), "job_metal")

    # fetch node (root)
    fetch = create_node(str(jd), "fetch")
    assert fetch["success"]
    fetch_id = fetch["node_id"]
    (jd / "nodes" / fetch_id / "artifacts").mkdir(parents=True, exist_ok=True)
    struct = jd / "nodes" / fetch_id / "artifacts" / "zn_protein.pdb"
    struct.write_text(ZN_PROTEIN_PDB)
    complete_node(
        str(jd),
        fetch_id,
        artifacts={"structure_file": "artifacts/zn_protein.pdb"},
        metadata={"source_type": "local"},
    )

    # prep node (child) with a merged_pdb artifact
    prep = create_node(str(jd), "prep", parent_node_ids=[fetch_id])
    assert prep["success"]
    prep_id = prep["node_id"]
    merge_dir = jd / "nodes" / prep_id / "artifacts" / "merge"
    merge_dir.mkdir(parents=True, exist_ok=True)
    merged = merge_dir / "merged.pdb"
    merged.write_text(ZN_PROTEIN_PDB)
    complete_node(
        str(jd),
        prep_id,
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
    )

    return str(jd), fetch_id, prep_id


def _stub_metalpdb2mol2(monkeypatch):
    """Patch _run_metalpdb2mol2 to write a minimal mol2 file without calling AmberTools."""
    from mdclaw import metal_server

    def fake_run(pdb_file, mol2_file, charge, timeout=60):
        from pathlib import Path as _P

        _P(mol2_file).write_text(
            "@<TRIPOS>MOLECULE\nZN\n    1     0     1     0     0\nSMALL\nNO_CHARGES\n"
            "@<TRIPOS>ATOM\n"
            f"      1 ZN         5.0000    5.0000    5.0000 Zn         1 ZN        {float(charge):.4f}\n"
            "@<TRIPOS>SUBSTRUCTURE\n     1 ZN          1 ****              0 ****  ****    0 ROOT\n"
        )
        return {"success": True, "mol2_file": mol2_file}

    monkeypatch.setattr(metal_server, "_run_metalpdb2mol2", fake_run)
    return metal_server


class TestParameterizeMetalIonNodeIntegration:

    def test_node_mode_registers_metal_params(
        self, prep_node_with_merged_pdb, monkeypatch
    ):
        job_dir, _fetch_id, prep_id = prep_node_with_merged_pdb
        metal_server = _stub_metalpdb2mol2(monkeypatch)

        result = metal_server.parameterize_metal_ion(
            job_dir=job_dir,
            node_id=prep_id,
            water_model="opc",
        )

        assert result["success"], result.get("errors")
        assert result["metal_params"], "metal_params list should be populated"
        zn_entry = result["metal_params"][0]
        assert zn_entry["residue_name"] == "ZN"
        assert zn_entry["charge"] == 2
        assert zn_entry["mol2"].endswith(".mol2")

        # Verify the artifact was registered on the prep node
        prep_node = read_node(job_dir, prep_id)
        assert "metal_params" in prep_node["artifacts"]
        assert isinstance(prep_node["artifacts"]["metal_params"], list)
        # merged_pdb must still be there — we only extended
        assert prep_node["artifacts"]["merged_pdb"] == "artifacts/merge/merged.pdb"
        # Status must not have been mutated
        assert prep_node["status"] == "completed"

    def test_topo_resolve_picks_up_metal_params(
        self, prep_node_with_merged_pdb, monkeypatch
    ):
        """build_amber_system DAG resolution should find metal_params via find_ancestor_artifact."""
        job_dir, _fetch_id, prep_id = prep_node_with_merged_pdb
        metal_server = _stub_metalpdb2mol2(monkeypatch)

        metal_server.parameterize_metal_ion(
            job_dir=job_dir, node_id=prep_id, water_model="opc"
        )

        # Create a topo child of prep
        topo = create_node(job_dir, "topo", parent_node_ids=[prep_id])
        assert topo["success"]
        topo_id = topo["node_id"]

        # Direct ancestor lookup
        mp = find_ancestor_artifact(job_dir, topo_id, "prep", "metal_params")
        assert mp is not None
        assert isinstance(mp, list)
        assert mp[0]["residue_name"] == "ZN"

        # And through resolve_node_inputs (what build_amber_system uses)
        inputs = resolve_node_inputs(job_dir, topo_id, "topo")
        assert "metal_params" in inputs
        assert inputs["metal_params"][0]["residue_name"] == "ZN"

    def test_non_prep_node_rejected(
        self, prep_node_with_merged_pdb, monkeypatch
    ):
        job_dir, fetch_id, _prep_id = prep_node_with_merged_pdb
        _stub_metalpdb2mol2(monkeypatch)
        from mdclaw import metal_server as ms

        result = ms.parameterize_metal_ion(
            job_dir=job_dir, node_id=fetch_id, water_model="opc"
        )
        assert result["success"] is False
        assert any("expected 'prep'" in e for e in result["errors"])

    def test_missing_merged_pdb_fails_clearly(self, tmp_path, monkeypatch):
        jd = tmp_path / "job_bare"
        jd.mkdir()
        init_progress_v3(str(jd), "job_bare")
        fetch = create_node(str(jd), "fetch")
        prep = create_node(
            str(jd), "prep", parent_node_ids=[fetch["node_id"]]
        )
        # prep exists but has NO merged_pdb artifact

        _stub_metalpdb2mol2(monkeypatch)
        from mdclaw import metal_server as ms

        result = ms.parameterize_metal_ion(
            job_dir=str(jd), node_id=prep["node_id"], water_model="opc"
        )
        assert result["success"] is False
        assert any("no merged_pdb" in e for e in result["errors"])

    def test_non_node_mode_requires_explicit_inputs(self, monkeypatch):
        _stub_metalpdb2mol2(monkeypatch)
        from mdclaw import metal_server as ms

        # Neither node flags nor explicit paths
        result = ms.parameterize_metal_ion(water_model="opc")
        assert result["success"] is False
        assert any("pdb_file is required" in e for e in result["errors"])
