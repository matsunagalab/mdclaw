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
    init_progress_v3,
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

ZN_MG_PROTEIN_PDB = textwrap.dedent("""\
ATOM      1  N   ALA A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A   1       2.450   1.000   1.000  1.00 10.00           C
TER
HETATM    3 ZN    ZN A 101       5.000   5.000   5.000  1.00 15.00          ZN
HETATM    4 MG    MG A 102       7.000   7.000   7.000  1.00 15.00          MG
END
""")


@pytest.fixture
def prep_node_with_merged_pdb(tmp_path):
    """Job with a completed prep node containing a merged.pdb with a Zn ion."""
    jd = tmp_path / "job_metal"
    jd.mkdir()
    init_progress_v3(str(jd), "job_metal")

    # source node (root)
    source = create_node(str(jd), "source")
    assert source["success"]
    source_id = source["node_id"]
    (jd / "nodes" / source_id / "artifacts").mkdir(parents=True, exist_ok=True)
    struct = jd / "nodes" / source_id / "artifacts" / "zn_protein.pdb"
    struct.write_text(ZN_PROTEIN_PDB)
    complete_node(
        str(jd),
        source_id,
        artifacts={"structure_file": "artifacts/zn_protein.pdb"},
        metadata={"source_type": "local"},
    )

    # prep node (child) with a merged_pdb artifact
    prep = create_node(str(jd), "prep", parent_node_ids=[source_id])
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

    return str(jd), source_id, prep_id


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

    def test_non_prep_node_rejected(
        self, prep_node_with_merged_pdb, monkeypatch
    ):
        job_dir, source_id, _prep_id = prep_node_with_merged_pdb
        _stub_metalpdb2mol2(monkeypatch)
        from mdclaw import metal_server as ms

        result = ms.parameterize_metal_ion(
            job_dir=job_dir, node_id=source_id, water_model="opc"
        )
        assert result["success"] is False
        assert any("expected 'prep'" in e for e in result["errors"])

    def test_missing_merged_pdb_fails_clearly(self, tmp_path, monkeypatch):
        jd = tmp_path / "job_bare"
        jd.mkdir()
        init_progress_v3(str(jd), "job_bare")
        source = create_node(str(jd), "source")
        prep = create_node(
            str(jd), "prep", parent_node_ids=[source["node_id"]]
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

    def test_single_charge_override_rejected_for_multiple_metals(self, tmp_path, monkeypatch):
        pdb_file = tmp_path / "zn_mg.pdb"
        pdb_file.write_text(ZN_MG_PROTEIN_PDB)
        _stub_metalpdb2mol2(monkeypatch)
        from mdclaw import metal_server as ms

        result = ms.parameterize_metal_ion(
            pdb_file=str(pdb_file),
            output_dir=str(tmp_path / "out"),
            metal_charge=2,
        )

        assert result["success"] is False
        assert result["code"] == "single_metal_charge_for_multiple_metals"

    def test_charge_metadata_uses_override_consistently(self, tmp_path, monkeypatch):
        pdb_file = tmp_path / "zn.pdb"
        pdb_file.write_text(ZN_PROTEIN_PDB)
        _stub_metalpdb2mol2(monkeypatch)
        from mdclaw import metal_server as ms

        result = ms.parameterize_metal_ion(
            pdb_file=str(pdb_file),
            output_dir=str(tmp_path / "out"),
            metal_charge=3,
        )

        assert result["success"] is True, result.get("errors")
        assert result["metal_params"][0]["charge"] == 3
        assert result["metal_params"][0]["atom_type"] == "Zn3+"
        assert result["metals_parameterized"][0]["charge"] == 3
        assert result["metals_parameterized"][0]["atom_type"] == "Zn3+"


# ----------------------------------------------------------------------------
# Bug 6: metal params without an OpenMM XML port must fail-fast
# ----------------------------------------------------------------------------


def test_build_amber_system_blocks_metal_without_openmm_xml(tmp_path):
    """metal_params bundles (frcmod + mol2) come from MCPB / metalpdb2mol2
    and are AMBER-native. The openmmforcefields path cannot consume them
    directly; until a parmed bridge ships, build_amber_system must fail-fast
    with ``metal_openmm_xml_required`` rather than emit a warning that
    masks the eventual ``No template found`` crash inside SystemGenerator."""
    from mdclaw.amber_server import build_amber_system

    pdb = tmp_path / "with_metal.pdb"
    pdb.write_text(
        "ATOM      1  N   ALA A   1       1.000   1.000   1.000  1.00 10.00           N\n"
        "ATOM      2  CA  ALA A   1       2.450   1.000   1.000  1.00 10.00           C\n"
        "ATOM      3  C   ALA A   1       3.000   2.400   1.000  1.00 10.00           C\n"
        "ATOM      4  O   ALA A   1       2.300   3.400   1.000  1.00 10.00           O\n"
        "ATOM      5  CB  ALA A   1       3.000   0.200   2.200  1.00 10.00           C\n"
        "TER\n"
        "HETATM    6 ZN    ZN B   1       4.000   2.000   1.500  1.00 10.00          ZN\n"
        "END\n",
        encoding="utf-8",
    )
    mol2 = tmp_path / "ZN.mol2"
    # Atom type ``Zn2+`` matches ``validate_metal_params``'s regex for Amber
    # ion atom types — without that the bare ``ZN`` validator would block
    # the build before we reached the openmmforcefields-bridge guard.
    mol2.write_text(
        "@<TRIPOS>MOLECULE\nZN\n  1 0 0 0 0\nSMALL\nUSER_CHARGES\n"
        "@<TRIPOS>ATOM\n  1 ZN  0.0 0.0 0.0 Zn2+  1 ZN  2.0\n",
        encoding="utf-8",
    )
    frcmod = tmp_path / "ZN.frcmod"
    frcmod.write_text("MASS\nZn2+  65.380\n\n", encoding="utf-8")

    result = build_amber_system(
        pdb_file=str(pdb),
        metal_params=[
            {"residue_name": "ZN", "mol2": str(mol2), "frcmods": [str(frcmod)]}
        ],
        output_dir=str(tmp_path / "topo"),
    )

    assert result["success"] is False
    assert result.get("code") == "metal_openmm_xml_required"
    assert any("Metal parameters" in e for e in result["errors"])
