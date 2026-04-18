import textwrap
from pathlib import Path


def test_analyze_structure_details_does_not_treat_hie_as_ligand(tmp_path):
    """HIE/HID/HIP are protein residues (Amber naming), not ligands."""
    from mdclaw.research_server import analyze_structure_details

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


def test_parse_ssbond_records_returns_pair(ssbond_mini_pdb):
    """SSBOND record is read back with recomputed SG-SG distance and source tag."""
    from mdclaw.research_server import _parse_ssbond_records

    pairs = _parse_ssbond_records(Path(ssbond_mini_pdb))
    assert len(pairs) == 1
    p = pairs[0]
    assert p["source"] == "pdb_ssbond"
    assert p["confidence"] == "high"
    assert p["recommendation"] == "form_bond"
    assert {p["cys1"]["chain"], p["cys2"]["chain"]} == {"A"}
    assert {p["cys1"]["resnum"], p["cys2"]["resnum"]} == {10, 20}
    # Recomputed from SG coords (0,-1.5,0) and (2.04,-1.5,0) → 2.04 Å.
    assert p["distance_angstrom"] == 2.04


def test_parse_ssbond_records_empty_when_no_ssbond(small_pdb):
    """Structures without SSBOND/disulf connections yield an empty list."""
    from mdclaw.research_server import _parse_ssbond_records

    assert _parse_ssbond_records(Path(small_pdb)) == []


def test_detect_disulfide_candidates_marks_source(ssbond_mini_pdb):
    """Distance-based detection tags entries with source='distance'."""
    from mdclaw.research_server import _detect_disulfide_candidates

    pairs = _detect_disulfide_candidates(Path(ssbond_mini_pdb))
    assert len(pairs) == 1
    assert pairs[0]["source"] == "distance"


def test_merge_disulfide_pairs_dedup_and_merge_source():
    """Same pair in both sources merges to a single entry with combined source."""
    from mdclaw.structure_server import _merge_disulfide_pairs

    ssbond = [{
        "cys1": {"chain": "A", "resnum": 10, "resname": "CYS"},
        "cys2": {"chain": "A", "resnum": 20, "resname": "CYS"},
        "distance_angstrom": None,
        "confidence": "high",
        "recommendation": "form_bond",
        "source": "pdb_ssbond",
    }]
    distance = [{
        "cys1": {"chain": "A", "resnum": 10, "resname": "CYS"},
        "cys2": {"chain": "A", "resnum": 20, "resname": "CYS"},
        "distance_angstrom": 2.04,
        "confidence": "high",
        "recommendation": "form_bond",
        "source": "distance",
    }]
    merged = _merge_disulfide_pairs(ssbond, distance)
    assert len(merged) == 1
    assert merged[0]["source"] == "pdb_ssbond+distance"
    # Measured distance from the distance-based entry wins.
    assert merged[0]["distance_angstrom"] == 2.04


def test_merge_disulfide_pairs_select_chains_filter():
    """Pairs spanning chains outside select_chains are dropped."""
    from mdclaw.structure_server import _merge_disulfide_pairs

    ssbond = [
        {
            "cys1": {"chain": "A", "resnum": 22, "resname": "CYS"},
            "cys2": {"chain": "A", "resnum": 95, "resname": "CYS"},
            "distance_angstrom": 2.03,
            "confidence": "high",
            "recommendation": "form_bond",
            "source": "pdb_ssbond",
        },
        {
            "cys1": {"chain": "B", "resnum": 22, "resname": "CYS"},
            "cys2": {"chain": "B", "resnum": 95, "resname": "CYS"},
            "distance_angstrom": 2.04,
            "confidence": "high",
            "recommendation": "form_bond",
            "source": "pdb_ssbond",
        },
        {
            "cys1": {"chain": "A", "resnum": 50, "resname": "CYS"},
            "cys2": {"chain": "B", "resnum": 60, "resname": "CYS"},
            "distance_angstrom": 2.05,
            "confidence": "high",
            "recommendation": "form_bond",
            "source": "pdb_ssbond",
        },
    ]
    merged = _merge_disulfide_pairs(ssbond, [], select_chains=["B"])
    # Only the B22-B95 pair survives; inter-chain and A-only pairs are dropped.
    assert len(merged) == 1
    assert merged[0]["cys1"]["chain"] == "B"
    assert merged[0]["cys2"]["chain"] == "B"


def test_merge_disulfide_pairs_distance_only_entry():
    """Distance-only entries (no SSBOND) pass through unchanged."""
    from mdclaw.structure_server import _merge_disulfide_pairs

    distance = [{
        "cys1": {"chain": "A", "resnum": 10, "resname": "CYS"},
        "cys2": {"chain": "A", "resnum": 20, "resname": "CYS"},
        "distance_angstrom": 2.4,
        "confidence": "medium",
        "recommendation": "review",
        "source": "distance",
    }]
    merged = _merge_disulfide_pairs([], distance)
    assert len(merged) == 1
    assert merged[0]["source"] == "distance"
    assert merged[0]["confidence"] == "medium"
