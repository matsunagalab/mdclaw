"""Glycoprotein/glycan support tests."""
from __future__ import annotations

import textwrap
from pathlib import Path


_GLYCOPROTEIN_PDB = textwrap.dedent("""\
ATOM      1  N   ASN A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ASN A   1       1.400   0.000   0.000  1.00  0.00           C
ATOM      3  C   ASN A   1       2.000   1.300   0.000  1.00  0.00           C
ATOM      4  O   ASN A   1       1.500   2.300   0.000  1.00  0.00           O
ATOM      5  CB  ASN A   1       1.900  -0.800   1.200  1.00  0.00           C
ATOM      6  CG  ASN A   1       3.300  -0.800   1.200  1.00  0.00           C
ATOM      7  OD1 ASN A   1       3.900  -1.800   1.200  1.00  0.00           O
ATOM      8  ND2 ASN A   1       3.900   0.300   1.200  1.00  0.00           N
TER
HETATM    9  C1  NAG B   2       5.200   0.300   1.200  1.00  0.00           C
HETATM   10  O1  NAG B   2       5.800   1.300   1.200  1.00  0.00           O
HETATM   11  C2  NAG B   2       5.700  -1.000   1.200  1.00  0.00           C
HETATM   12  O5  NAG B   2       4.000   0.300   1.200  1.00  0.00           O
TER
END
""")


def _write_6ya2_like_mmcif(tmp_path: Path) -> Path:
    rows = [
        ("1", "N", "ND2", "ASN", "A", "1", "116", "ASN", "A", "ND2", "0", "0", "0"),
        ("2", "C", "C1", "NAG", "B", ".", "303", "NAG", "A", "C1", "1", "0", "0"),
        ("3", "N", "ND2", "ASN", "A", "2", "210", "ASN", "A", "ND2", "2", "0", "0"),
        ("4", "C", "C1", "NAG", "D", ".", "1", "NAG", "D", "C1", "3", "0", "0"),
        ("5", "N", "ND2", "ASN", "C", "1", "116", "ASN", "B", "ND2", "4", "0", "0"),
        ("6", "C", "C1", "NAG", "E", ".", "301", "NAG", "B", "C1", "5", "0", "0"),
        ("7", "N", "ND2", "ASN", "C", "2", "210", "ASN", "B", "ND2", "6", "0", "0"),
        ("8", "C", "C1", "NAG", "F", ".", "1", "NAG", "E", "C1", "7", "0", "0"),
        ("9", "N", "ND2", "ASN", "G", "1", "116", "ASN", "C", "ND2", "8", "0", "0"),
        ("10", "C", "C1", "NAG", "H", ".", "302", "NAG", "C", "C1", "9", "0", "0"),
        ("11", "N", "ND2", "ASN", "G", "2", "210", "ASN", "C", "ND2", "10", "0", "0"),
        ("12", "C", "C1", "NAG", "I", ".", "301", "NAG", "C", "C1", "11", "0", "0"),
    ]
    atom_rows = "\n".join(
        (
            f"{'ATOM' if res == 'ASN' else 'HETATM'} {atom_id} {element} {atom} . "
            f"{res} {label_asym} 1 {label_seq} ? {x} {y} {z} 1.00 0.00 ? "
            f"{auth_seq} {auth_res} {auth_asym} {auth_atom} 1"
        )
        for atom_id, element, atom, res, label_asym, label_seq, auth_seq,
        auth_res, auth_asym, auth_atom, x, y, z in rows
    )
    conn_rows = "\n".join([
        "covale1 covale A ASN 1 ND2 A ASN 116 B NAG . C1 A NAG 303 1.43",
        "covale2 covale A ASN 2 ND2 A ASN 210 D NAG . C1 D NAG 1 1.43",
        "covale3 covale C ASN 1 ND2 B ASN 116 E NAG . C1 B NAG 301 1.43",
        "covale4 covale C ASN 2 ND2 B ASN 210 F NAG . C1 E NAG 1 1.43",
        "covale5 covale G ASN 1 ND2 C ASN 116 H NAG . C1 C NAG 302 1.43",
        "covale6 covale G ASN 2 ND2 C ASN 210 I NAG . C1 C NAG 301 1.43",
        # Glycan-glycan covalent records should be ignored by the protein-glycan extractor.
        "covale7 covale D NAG . O4 D NAG 1 J NAG . C1 D NAG 2 1.43",
    ])
    cif = textwrap.dedent(f"""\
    data_6YA2_like
    _entry.id 6YA2_like
    loop_
    _atom_site.group_PDB
    _atom_site.id
    _atom_site.type_symbol
    _atom_site.label_atom_id
    _atom_site.label_alt_id
    _atom_site.label_comp_id
    _atom_site.label_asym_id
    _atom_site.label_entity_id
    _atom_site.label_seq_id
    _atom_site.pdbx_PDB_ins_code
    _atom_site.Cartn_x
    _atom_site.Cartn_y
    _atom_site.Cartn_z
    _atom_site.occupancy
    _atom_site.B_iso_or_equiv
    _atom_site.pdbx_formal_charge
    _atom_site.auth_seq_id
    _atom_site.auth_comp_id
    _atom_site.auth_asym_id
    _atom_site.auth_atom_id
    _atom_site.pdbx_PDB_model_num
    {atom_rows}
    loop_
    _struct_conn.id
    _struct_conn.conn_type_id
    _struct_conn.ptnr1_label_asym_id
    _struct_conn.ptnr1_label_comp_id
    _struct_conn.ptnr1_label_seq_id
    _struct_conn.ptnr1_label_atom_id
    _struct_conn.ptnr1_auth_asym_id
    _struct_conn.ptnr1_auth_comp_id
    _struct_conn.ptnr1_auth_seq_id
    _struct_conn.ptnr2_label_asym_id
    _struct_conn.ptnr2_label_comp_id
    _struct_conn.ptnr2_label_seq_id
    _struct_conn.ptnr2_label_atom_id
    _struct_conn.ptnr2_auth_asym_id
    _struct_conn.ptnr2_auth_comp_id
    _struct_conn.ptnr2_auth_seq_id
    _struct_conn.pdbx_dist_value
    {conn_rows}
    """)
    path = tmp_path / "6ya2_like.cif"
    path.write_text(cif, encoding="utf-8")
    return path


def _write_glycoprotein(tmp_path: Path) -> str:
    pdb = tmp_path / "glycoprotein.pdb"
    pdb.write_text(_GLYCOPROTEIN_PDB, encoding="utf-8")
    return str(pdb)


def test_glycan_linkages_are_extracted_from_mmcif_struct_conn(tmp_path):
    from mdclaw.structure_server import _parse_glycan_link_records

    links = _parse_glycan_link_records(_write_6ya2_like_mmcif(tmp_path))

    assert len(links) == 6
    assert {link["source"] for link in links} == {"mmcif_struct_conn"}
    assert links[0]["protein"] == {
        "atom": "ND2",
        "resname": "ASN",
        "chain": "A",
        "resnum": 116,
        "icode": "",
        "source": "mmcif_struct_conn",
        "connection_id": "covale1",
        "reported_distance": 1.43,
    }
    assert links[0]["glycan"]["resname"] == "NAG"
    assert {link["connection_id"] for link in links} == {
        "covale1",
        "covale2",
        "covale3",
        "covale4",
        "covale5",
        "covale6",
    }


def test_glycan_linkages_keep_pdb_link_fallback(tmp_path):
    from mdclaw.structure_server import _parse_pdb_glycan_link_records

    pdb = tmp_path / "link.pdb"
    pdb.write_text(
        "LINK         ND2 ASN A   1                 C1  NAG B   2     1555   1555  1.43\n"
        + _GLYCOPROTEIN_PDB,
        encoding="utf-8",
    )

    links = _parse_pdb_glycan_link_records(pdb)

    assert len(links) == 1
    assert links[0]["source"] == "pdb_link"
    assert links[0]["protein"]["chain"] == "A"
    assert links[0]["glycan"]["chain"] == "B"


def test_inspect_molecules_classifies_glycan_not_ligand(tmp_path):
    from mdclaw.structure_server import _inspect_molecules_impl

    result = _inspect_molecules_impl(_write_glycoprotein(tmp_path))

    assert result["success"], result.get("errors")
    summary = result["summary"]
    assert summary["num_glycan_chains"] == 1
    assert summary["glycan_chain_ids"] == ["B"]
    assert not summary["ligand_chain_ids"]


def test_split_molecules_emits_glycan_files(tmp_path):
    from mdclaw.structure_server import split_molecules

    result = split_molecules(
        structure_file=_write_glycoprotein(tmp_path),
        output_dir=str(tmp_path / "out"),
        include_types=["protein", "glycan"],
    )

    assert result["success"], result.get("errors")
    assert len(result["protein_files"]) == 1
    assert len(result["glycan_files"]) == 1
    assert not result["ligand_files"]
    assert {i["chain_type"] for i in result["chain_file_info"]} == {"protein", "glycan"}


def test_prepare_complex_passes_glycans_through_without_ligand_chemistry(tmp_path):
    from mdclaw.structure_server import prepare_complex

    result = prepare_complex(
        structure_file=_write_glycoprotein(tmp_path),
        output_dir=str(tmp_path / "prep"),
        include_types=["glycan"],
        process_proteins=False,
        process_ligands=False,
    )

    assert result["success"], result.get("errors")
    assert len(result["glycans"]) == 1
    assert result["glycans"][0]["residue_names"] == ["NAG"]
    assert result["preparation_summary"]["has_glycan"] is True
    assert "ligand_chemistry" not in result


def test_build_amber_system_loads_glycam_and_bonds_linkage(monkeypatch, tmp_path):
    """Glycoprotein build path: cpptraj prepareforleap is still invoked
    upstream of the topology load, and the SystemGenerator XML bundle picks
    up the GLYCAM_06j-1 conversion XML."""
    from unittest.mock import patch
    from mdclaw import amber_server

    class FakeCpptraj:
        def is_available(self):
            return True

        def run(self, args, cwd=None, timeout=None):
            cwd_path = Path(cwd)
            input_path = Path(args[1])
            script = input_path.read_text(encoding="utf-8")
            assert "prepareforleap crdset MDClawCrd" in script
            assert "skiperrors" in script
            assert "nohisdetect nodisulfides" in script
            assert "keepaltloc highestocc" in script
            cpptraj_pdb = cwd_path / "system.prepareforleap.pdb"
            cpptraj_pdb_text = cpptraj_pdb.read_text(encoding="utf-8")
            assert "LINK" in cpptraj_pdb_text
            assert "ND2 ASN A   1" in cpptraj_pdb_text
            assert "CONECT    8    9" in cpptraj_pdb_text
            assert "CONECT    9    8" in cpptraj_pdb_text
            prepared_pdb = cwd_path / "system.glycam.pdb"
            generated_leap = cwd_path / "system.glycam.leap.in"
            prepared_pdb.write_text(_GLYCOPROTEIN_PDB.replace("ASN A   1", "NLN A   1"), encoding="utf-8")
            generated_leap.write_text(
                f"mol = loadpdb {prepared_pdb}\n"
                "bond mol.1.ND2 mol.2.C1\n",
                encoding="utf-8",
            )
            return type("ProcResult", (), {"stdout": "prepareforleap ok", "stderr": ""})()

    monkeypatch.setattr(amber_server, "cpptraj_wrapper", FakeCpptraj())

    captured: dict = {}

    def _fake_om_build(**kwargs):
        from mdclaw import forcefield_catalog as _fc
        from mdclaw.amber_server import _resolve_glycan_name_from_library
        bundle = _fc.resolve_xml_bundle(
            protein=_fc.normalize_protein(kwargs["forcefield"]) or kwargs["forcefield"],
            water=_fc.normalize_water(kwargs["water_model"]) if kwargs["water_model"] else None,
            glycan=_resolve_glycan_name_from_library(kwargs["glycan_library"]),
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

    glycan_linkages = [{
        "source": "pdb_link",
        "protein": {
            "chain": "A",
            "merged_chain": "A",
            "resnum": 1,
            "merged_resnum": 1,
            "resname": "ASN",
            "atom": "ND2",
            "icode": "",
        },
        "glycan": {
            "chain": "B",
            "merged_chain": "B",
            "resnum": 2,
            "merged_resnum": 2,
            "resname": "NAG",
            "atom": "C1",
            "icode": "",
        },
    }]

    with patch(
        "mdclaw.amber_server._run_openmmforcefields_build",
        side_effect=_fake_om_build,
    ):
        result = amber_server.build_amber_system(
            pdb_file=_write_glycoprotein(tmp_path),
            output_dir=str(tmp_path / "topo"),
            glycan_linkages=glycan_linkages,
        )

    assert result["success"], result.get("errors")
    assert result["parameters"]["glycan_library"] == "leaprc.GLYCAM_06j-1"
    assert result["glycan_linkage_plan"][0]["status"] == "handled_by_prepareforleap"
    assert result["glycam_prepareforleap"]["prepared_pdb"].endswith("system.glycam.pdb")
    assert "amber/GLYCAM_06j-1.xml" in captured.get("bundle", []), (
        "GLYCAM XML must be resolved into the SystemGenerator bundle"
    )


def test_packmol_solute_identity_restore_keeps_water_renumbering(tmp_path):
    from mdclaw.solvation_server import _restore_packmol_solute_identity

    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "solvated.pdb"
    input_pdb.write_text(_GLYCOPROTEIN_PDB, encoding="utf-8")
    output_pdb.write_text(
        _GLYCOPROTEIN_PDB
        .replace("ASN A   1", "ASN A   8")
        .replace("NAG B   2", "NAG A  42")
        .replace("END\n", "HETATM   13  O   HOH X   1       9.000   9.000   9.000  1.00  0.00           O\nEND\n"),
        encoding="utf-8",
    )

    report = _restore_packmol_solute_identity(input_pdb, output_pdb)
    restored = output_pdb.read_text(encoding="utf-8")

    assert report["solute_identity_restored"] is True
    assert report["solute_identity_restored_atom_count"] == 12
    assert "ASN A   1" in restored
    assert "NAG B   2" in restored
    assert "HOH X   1" in restored


def test_glycan_linkage_mapping_failure_stops_before_cpptraj(monkeypatch, tmp_path):
    from mdclaw import amber_server

    class FakeCpptraj:
        def is_available(self):
            return True

        def run(self, args, cwd=None, timeout=None):  # pragma: no cover - must not be called
            raise AssertionError("cpptraj should not run when glycan LINK mapping fails")

    monkeypatch.setattr(amber_server, "cpptraj_wrapper", FakeCpptraj())
    bad_linkages = [{
        "source": "pdb_link",
        "protein": {
            "chain": "A",
            "merged_chain": "A",
            "resnum": 999,
            "merged_resnum": 999,
            "resname": "ASN",
            "atom": "ND2",
            "icode": "",
        },
        "glycan": {
            "chain": "B",
            "merged_chain": "B",
            "resnum": 2,
            "merged_resnum": 2,
            "resname": "NAG",
            "atom": "C1",
            "icode": "",
        },
    }]

    result = amber_server._prepare_glycam_pdb_with_cpptraj(
        pdb_path=Path(_write_glycoprotein(tmp_path)),
        out_dir=tmp_path,
        output_name="system",
        glycan_linkages=bad_linkages,
    )

    assert result["success"] is False
    assert result["code"] == "glycan_linkage_mapping_failed"
    assert result["link_injection"]["missing_link_count"] == 1
    assert "Could not resolve glycan LINK endpoint atoms" in result["errors"][0]
