"""Standard DNA/RNA support tests."""
from __future__ import annotations

from pathlib import Path


_DNA_RNA_PDB = """\
ATOM      1  P    DA A   1       0.000   0.000   0.000  1.00  0.00           P
ATOM      2  O5'  DA A   1       1.000   0.000   0.000  1.00  0.00           O
ATOM      3  C5'  DA A   1       1.500   1.000   0.000  1.00  0.00           C
ATOM      4  C4'  DA A   1       2.500   1.000   0.000  1.00  0.00           C
ATOM      5  C3'  DA A   1       3.000   2.000   0.000  1.00  0.00           C
ATOM      6  O3'  DA A   1       4.000   2.000   0.000  1.00  0.00           O
ATOM      7  P    DC A   2       5.000   2.000   0.000  1.00  0.00           P
ATOM      8  O5'  DC A   2       6.000   2.000   0.000  1.00  0.00           O
ATOM      9  C5'  DC A   2       6.500   3.000   0.000  1.00  0.00           C
ATOM     10  C4'  DC A   2       7.500   3.000   0.000  1.00  0.00           C
ATOM     11  C3'  DC A   2       8.000   4.000   0.000  1.00  0.00           C
ATOM     12  O3'  DC A   2       9.000   4.000   0.000  1.00  0.00           O
ATOM     13  P     A B   1       0.000  10.000   0.000  1.00  0.00           P
ATOM     14  O5'   A B   1       1.000  10.000   0.000  1.00  0.00           O
ATOM     15  C5'   A B   1       1.500  11.000   0.000  1.00  0.00           C
ATOM     16  C4'   A B   1       2.500  11.000   0.000  1.00  0.00           C
ATOM     17  C3'   A B   1       3.000  12.000   0.000  1.00  0.00           C
ATOM     18  O3'   A B   1       4.000  12.000   0.000  1.00  0.00           O
ATOM     19  P     U B   2       5.000  12.000   0.000  1.00  0.00           P
ATOM     20  O5'   U B   2       6.000  12.000   0.000  1.00  0.00           O
ATOM     21  C5'   U B   2       6.500  13.000   0.000  1.00  0.00           C
ATOM     22  C4'   U B   2       7.500  13.000   0.000  1.00  0.00           C
ATOM     23  C3'   U B   2       8.000  14.000   0.000  1.00  0.00           C
ATOM     24  O3'   U B   2       9.000  14.000   0.000  1.00  0.00           O
END
"""

_STANDARD_NUCLEIC_REBUILD_PDB = """\
ATOM      1  O5'  DC A   1     -19.071   4.593   5.171  1.00 35.30           O
ATOM      2  C5'  DC A   1     -19.480   5.704   5.922  1.00 33.81           C
ATOM      3  C4'  DC A   1     -18.585   6.910   5.665  1.00 34.46           C
ATOM      4  O4'  DC A   1     -17.346   6.758   6.394  1.00 33.94           O
ATOM      5  C3'  DC A   1     -18.161   7.117   4.222  1.00 33.34           C
ATOM      6  O3'  DC A   1     -19.138   7.834   3.512  1.00 30.79           O
ATOM      7  C2'  DC A   1     -16.870   7.899   4.375  1.00 32.88           C
ATOM      8  C1'  DC A   1     -16.265   7.277   5.623  1.00 32.31           C
ATOM      9  N1   DC A   1     -15.327   6.176   5.327  1.00 29.52           N
ATOM     10  C2   DC A   1     -14.122   6.463   4.683  1.00 27.09           C
ATOM     11  O2   DC A   1     -13.889   7.623   4.356  1.00 28.83           O
ATOM     12  N3   DC A   1     -13.266   5.463   4.420  1.00 28.28           N
ATOM     13  C4   DC A   1     -13.567   4.223   4.782  1.00 30.37           C
ATOM     14  N4   DC A   1     -12.677   3.267   4.505  1.00 29.19           N
ATOM     15  C5   DC A   1     -14.787   3.912   5.454  1.00 31.39           C
ATOM     16  C6   DC A   1     -15.628   4.909   5.695  1.00 29.01           C
ATOM     17  O5'   A B   1      10.751   3.325 -41.079  1.00 29.78           O
ATOM     18  C5'   A B   1       9.400   2.902 -41.189  1.00 28.67           C
ATOM     19  C4'   A B   1       9.308   1.418 -41.432  1.00 26.37           C
ATOM     20  O4'   A B   1       9.991   1.081 -42.670  1.00 22.63           O
ATOM     21  C3'   A B   1       9.979   0.529 -40.397  1.00 27.52           C
ATOM     22  O3'   A B   1       9.209   0.333 -39.226  1.00 31.63           O
ATOM     23  C2'   A B   1      10.231  -0.745 -41.184  1.00 23.82           C
ATOM     24  O2'   A B   1       9.032  -1.490 -41.347  1.00 23.53           O
ATOM     25  C1'   A B   1      10.634  -0.170 -42.537  1.00 21.54           C
ATOM     26  N9    A B   1      12.089   0.041 -42.619  1.00 19.19           N
ATOM     27  C8    A B   1      12.784   1.224 -42.588  1.00 19.31           C
ATOM     28  N7    A B   1      14.084   1.073 -42.683  1.00 18.78           N
ATOM     29  C5    A B   1      14.254  -0.303 -42.776  1.00 17.46           C
ATOM     30  C6    A B   1      15.393  -1.117 -42.901  1.00 16.65           C
ATOM     31  N6    A B   1      16.644  -0.651 -42.954  1.00 17.63           N
ATOM     32  N1    A B   1      15.202  -2.453 -42.977  1.00 15.99           N
ATOM     33  C2    A B   1      13.952  -2.930 -42.926  1.00 15.57           C
ATOM     34  N3    A B   1      12.804  -2.268 -42.809  1.00 15.87           N
ATOM     35  C4    A B   1      13.031  -0.947 -42.740  1.00 17.48           C
END
"""


def _write_pdb(tmp_path: Path, text: str = _DNA_RNA_PDB) -> str:
    path = tmp_path / "dna_rna.pdb"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_inspect_molecules_classifies_standard_dna_rna(tmp_path):
    from mdclaw.structure_server import _inspect_molecules_impl

    result = _inspect_molecules_impl(_write_pdb(tmp_path))

    assert result["success"], result.get("errors")
    summary = result["summary"]
    assert summary["num_nucleic_chains"] == 2
    assert set(summary["nucleic_chain_ids"]) == {"A", "B"}
    assert set(summary["nucleic_subtypes"].values()) == {"dna", "rna"}
    assert not summary["ligand_chain_ids"]
    assert summary["modified_nucleic_support_status"] == "not_detected"


def test_inspect_molecules_reports_modified_nucleic_as_unsupported(tmp_path):
    from mdclaw.structure_server import _inspect_molecules_impl

    modified = _DNA_RNA_PDB.replace(" DA A   1", " 5M A   1")

    result = _inspect_molecules_impl(_write_pdb(tmp_path, modified))

    assert result["success"], result.get("errors")
    summary = result["summary"]
    assert summary["modified_nucleic_support_status"] == "unsupported"
    assert summary["modified_nucleic_support"]["code"] == (
        "unsupported_modified_nucleic_residue"
    )
    assert summary["modified_nucleic_support"]["next_action"] == (
        "report_unsupported_and_stop_before_topology"
    )
    unsupported = summary["unsupported_modified_nucleic_residues"]
    assert len(unsupported) == 1
    assert unsupported[0]["chain"] == "A"
    assert unsupported[0]["author_chain"] == "A"
    assert unsupported[0]["resnum"] == 1
    assert unsupported[0]["resname"] == "5M"
    assert unsupported[0]["source_resname"] == "5M"
    assert unsupported[0]["coordinate_frame"] == "source"
    assert any("Modified DNA/RNA" in warning for warning in result["warnings"])


def test_cli_inspect_molecules_reports_modified_nucleic_as_unsupported(tmp_path):
    from mdclaw.research_server import inspect_molecules

    modified = _DNA_RNA_PDB.replace(" DA A   1", " 5M A   1")

    result = inspect_molecules(structure_file=_write_pdb(tmp_path, modified))

    assert result["success"], result.get("errors")
    summary = result["summary"]
    assert summary["modified_nucleic_support_status"] == "unsupported"
    assert summary["modified_nucleic_support"]["code"] == (
        "unsupported_modified_nucleic_residue"
    )
    assert summary["modified_nucleic_support"]["supported_for_md_ready_topology"] is False
    assert any("Modified DNA/RNA" in warning for warning in result["warnings"])


def test_split_molecules_emits_nucleic_files(tmp_path):
    from mdclaw.structure_server import split_molecules

    result = split_molecules(
        structure_file=_write_pdb(tmp_path),
        output_dir=str(tmp_path / "out"),
        include_types=["nucleic"],
    )

    assert result["success"], result.get("errors")
    assert len(result["nucleic_files"]) == 2
    assert not result["ligand_files"]
    assert {i["chain_type"] for i in result["chain_file_info"]} == {"nucleic"}


def test_prepare_complex_rebuilds_standard_nucleic_hydrogens(tmp_path):
    from mdclaw.structure_server import prepare_complex

    result = prepare_complex(
        structure_file=_write_pdb(tmp_path, _STANDARD_NUCLEIC_REBUILD_PDB),
        output_dir=str(tmp_path / "prep"),
    )

    assert result["success"], result.get("errors")
    assert len(result["nucleics"]) == 2
    assert all(n["success"] for n in result["nucleics"])
    dna = next(n for n in result["nucleics"] if n["nucleic_subtype"] == "dna")
    rna = next(n for n in result["nucleics"] if n["nucleic_subtype"] == "rna")
    assert dna["hydrogen_rebuild_method"] == "openmm_modeller"
    assert dna["nucleic_forcefield_xml"] == "amber/DNA.OL15.xml"
    assert dna["hydrogens_added"] > 0
    assert rna["hydrogen_rebuild_method"] == "openmm_modeller"
    assert rna["nucleic_forcefield_xml"] == "amber/RNA.OL3.xml"
    assert rna["hydrogens_added"] > 0
    assert Path(dna["output_file"]).read_text().count(" H") > 0
    assert result["merged_pdb"]
    assert result["preparation_summary"]["has_nucleic"] is True
    assert set(result["preparation_summary"]["nucleic_subtypes"]) == {"dna", "rna"}
    assert result["preparation_summary"]["nucleic_hydrogens_added"] > 0


def test_deuterium_detection_does_not_treat_deoxy_atom_names_as_isotopes():
    from mdclaw.structure_server import _is_deuterium_atom_record

    true_deuterium = (
        "ATOM      1  D1  ARG A   1       0.000   0.000   0.000  "
        "1.00  0.00           D"
    )
    deoxy_d5_blank_element = (
        "ATOM      2  D5'  DG A   1       1.000   0.000   0.000  "
        "1.00  0.00            "
    )
    deoxy_d3_blank_element = (
        "ATOM      3  D3'  DG A   1       2.000   0.000   0.000  "
        "1.00  0.00            "
    )

    assert _is_deuterium_atom_record(true_deuterium) is True
    assert _is_deuterium_atom_record(deoxy_d5_blank_element) is False
    assert _is_deuterium_atom_record(deoxy_d3_blank_element) is False


def test_standard_nucleic_hydrogen_rebuild_failure_has_stable_code(tmp_path):
    from mdclaw.structure_server import _prepare_standard_nucleic

    incomplete = """\
ATOM      1  P    DA A   1       0.000   0.000   0.000  1.00  0.00           P
ATOM      2  O5'  DA A   1       1.000   0.000   0.000  1.00  0.00           O
END
"""
    result = _prepare_standard_nucleic(
        _write_pdb(tmp_path, incomplete),
        nucleic_subtype="dna",
        ph=7.4,
    )

    assert result["success"] is False
    assert result["code"] == "nucleic_hydrogen_rebuild_failed"


def test_build_amber_system_loads_standard_nucleic_leaprc(monkeypatch, tmp_path):
    """Standard DNA + RNA presence resolves DNA.OL15 + RNA.OL3 into the
    SystemGenerator XML bundle (PR3: tleap script inspection retired)."""
    from unittest.mock import patch
    from mdclaw import amber_server

    captured: dict = {}

    def _fake_om_build(**kwargs):
        from mdclaw import forcefield_catalog as _fc
        from mdclaw.amber_server import (
            _resolve_dna_name_from_libraries,
            _resolve_rna_name_from_libraries,
        )
        bundle = _fc.resolve_xml_bundle(
            protein=_fc.normalize_protein(kwargs["forcefield"]) or kwargs["forcefield"],
            water=_fc.normalize_water(kwargs["water_model"]) if kwargs["water_model"] else None,
            dna=_resolve_dna_name_from_libraries(kwargs["nucleic_libraries"]),
            rna=_resolve_rna_name_from_libraries(kwargs["nucleic_libraries"]),
        )
        captured["bundle"] = bundle
        kwargs["system_xml_file"].write_text("<System/>")
        kwargs["topology_pdb_file"].write_text("REMARK fake\nEND\n")
        kwargs["state_xml_file"].write_text("<State/>")
        return {
            "success": True,
            "errors": [],
            "warnings": [],
            "system_xml": str(kwargs["system_xml_file"]),
            "topology_pdb": str(kwargs["topology_pdb_file"]),
            "state_xml": str(kwargs["state_xml_file"]),
            "num_atoms": 1,
            "num_residues": 1,
            "forcefield_provenance": {
                "kind": "amber_via_openmmforcefields",
                "openmm_xml": list(bundle),
            },
        }

    with patch(
        "mdclaw.amber_server._run_openmmforcefields_build",
        side_effect=_fake_om_build,
    ):
        result = amber_server.build_amber_system(
            pdb_file=_write_pdb(tmp_path),
            output_dir=str(tmp_path / "topo"),
        )

    assert result["success"], result.get("errors")
    assert result["parameters"]["nucleic_libraries"] == [
        "leaprc.DNA.OL15",
        "leaprc.RNA.OL3",
    ]
    bundle = captured.get("bundle", [])
    assert "amber/DNA.OL15.xml" in bundle
    assert "amber/RNA.OL3.xml" in bundle


def test_build_amber_system_blocks_modified_nucleic_like_residue(tmp_path):
    # The unsupported-modified-nucleic guardrail fires before the
    # openmmforcefields availability check, so this test does not need
    # to mock the build stack.
    from mdclaw import amber_server

    modified = _DNA_RNA_PDB.replace(" DA A   1", " 5M A   1")

    result = amber_server.build_amber_system(
        pdb_file=_write_pdb(tmp_path, modified),
        output_dir=str(tmp_path / "topo"),
    )

    assert result["success"] is False
    assert result["code"] == "unsupported_modified_nucleic_residue"
