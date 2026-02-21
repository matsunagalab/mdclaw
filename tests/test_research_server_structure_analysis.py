import textwrap


def test_analyze_structure_details_does_not_treat_hie_as_ligand(tmp_path):
    """HIE/HID/HIP are protein residues (Amber naming), not ligands."""
    from servers.research_server import analyze_structure_details

    pdb = textwrap.dedent(
        """\
        ATOM      1  N   HIE A   1      11.104  13.207  12.000  1.00 20.00           N
        ATOM      2  CA  HIE A   1      12.560  13.100  12.000  1.00 20.00           C
        ATOM      3  C   HIE A   1      13.000  11.650  12.000  1.00 20.00           C
        ATOM      4  O   HIE A   1      12.300  10.700  12.000  1.00 20.00           O
        TER
        END
        """
    )
    p = tmp_path / "hie_only.pdb"
    p.write_text(pdb)

    # Force identify_ligands=True; should still return no ligand_analysis.
    result = analyze_structure_details(
        structure_file=str(p),
        ph=7.4,
        detect_disulfides=False,
        estimate_protonation=False,
        check_missing=False,
        identify_ligands=True,
    )
    assert result["success"] is True
    assert result["ligand_analysis"] == []
    assert result["summary"]["num_ligands"] == 0

