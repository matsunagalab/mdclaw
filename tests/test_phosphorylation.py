"""Tests for phosphorylation support: detection, the `phosphorylate_residues`
tool, and `build_amber_system`'s phosaa autoload.

These tests synthesize tiny PDBs by hand rather than running PDBFixer/tleap,
so the suite stays in the unit/level-1 tier (no scientific deps beyond gemmi
which is already a baseline mdclaw dep).
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from mdclaw._node import (
    complete_node,
    create_node,
    read_node,
)
from mdclaw.amber.build_system import build_amber_system
from mdclaw.chemistry_constants import PHOSPHO_RESNAMES
from mdclaw.research.inspection import detect_ptm_sites
from mdclaw.structure.phosphorylation import (
    _PHOSPHO_TARGETS,
    _apply_phosphorylation_to_pdb,
    _build_source_to_merged_chain_map,
    _parse_sites_str,
    _remap_detected_ptm_chains,
    phosphorylate_residues,
)


# ---------- helpers ----------------------------------------------------------


def _atom_line(serial: int, atom_name: str, resname: str, chain: str,
               resnum: int, x: float, y: float, z: float, element: str) -> str:
    """Return one valid PDB ATOM record (column-positioned)."""
    # Atom name follows the right-justified-with-leading-space rule for
    # 3-letter+ names; for our test atoms (N, CA, OG, HG, etc.) the standard
    # convention is left-justified with a leading space — i.e. " N  ".
    return (
        f"ATOM  {serial:>5} {atom_name:<4} {resname:>3} {chain}{resnum:>4}"
        f"    {x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00 20.00           {element:>2}\n"
    )


def _write_ser_pdb(path: Path) -> None:
    """Tiny SER-A-65 with backbone + CB/OG + hydroxyl HG."""
    path.write_text(
        _atom_line(1, "N",   "SER", "A", 65, 0.0, 0.0, 0.0, "N")
        + _atom_line(2, "CA", "SER", "A", 65, 1.0, 0.0, 0.0, "C")
        + _atom_line(3, "C",  "SER", "A", 65, 2.0, 0.0, 0.0, "C")
        + _atom_line(4, "O",  "SER", "A", 65, 3.0, 0.0, 0.0, "O")
        + _atom_line(5, "CB", "SER", "A", 65, 1.0, 1.0, 0.0, "C")
        + _atom_line(6, "OG", "SER", "A", 65, 1.0, 2.0, 0.0, "O")
        + _atom_line(7, "HG", "SER", "A", 65, 1.0, 2.5, 0.0, "H")
        + "END\n"
    )


def _write_sep_pdb(path: Path) -> None:
    """Tiny SEP-A-65 with phosphate atoms — used to test detection."""
    path.write_text(
        _atom_line(1, "N",   "SEP", "A", 65, 0.0, 0.0, 0.0, "N")
        + _atom_line(2, "CA", "SEP", "A", 65, 1.0, 0.0, 0.0, "C")
        + _atom_line(3, "C",  "SEP", "A", 65, 2.0, 0.0, 0.0, "C")
        + _atom_line(4, "O",  "SEP", "A", 65, 3.0, 0.0, 0.0, "O")
        + _atom_line(5, "CB", "SEP", "A", 65, 1.0, 1.0, 0.0, "C")
        + _atom_line(6, "OG", "SEP", "A", 65, 1.0, 2.0, 0.0, "O")
        + _atom_line(7, "P",  "SEP", "A", 65, 1.0, 3.0, 0.0, "P")
        + _atom_line(8, "O1P","SEP", "A", 65, 1.0, 4.0, 0.0, "O")
        + _atom_line(9, "O2P","SEP", "A", 65, 2.0, 3.0, 0.0, "O")
        + _atom_line(10,"O3P","SEP", "A", 65, 0.0, 3.0, 0.0, "O")
        + "END\n"
    )


def _write_thr_pdb(path: Path) -> None:
    path.write_text(
        _atom_line(1, "N",   "THR", "B", 12, 0.0, 0.0, 0.0, "N")
        + _atom_line(2, "CA", "THR", "B", 12, 1.0, 0.0, 0.0, "C")
        + _atom_line(3, "C",  "THR", "B", 12, 2.0, 0.0, 0.0, "C")
        + _atom_line(4, "O",  "THR", "B", 12, 3.0, 0.0, 0.0, "O")
        + _atom_line(5, "CB", "THR", "B", 12, 1.0, 1.0, 0.0, "C")
        + _atom_line(6, "OG1","THR", "B", 12, 1.0, 2.0, 0.0, "O")
        + _atom_line(7, "HG1","THR", "B", 12, 1.0, 2.5, 0.0, "H")
        + _atom_line(8, "CG2","THR", "B", 12, 0.0, 1.5, 0.0, "C")
        + "END\n"
    )


# ---------- _parse_sites_str -------------------------------------------------


def test_parse_sites_str_basic():
    sites = _parse_sites_str("A:65:SEP,A:178:TPO,B:12:PTR")
    assert sites == [
        {"chain": "A", "resnum": 65, "target": "SEP"},
        {"chain": "A", "resnum": 178, "target": "TPO"},
        {"chain": "B", "resnum": 12, "target": "PTR"},
    ]


def test_parse_sites_str_lowercase_target_is_normalized():
    sites = _parse_sites_str("A:65:sep")
    assert sites[0]["target"] == "SEP"


def test_parse_sites_str_invalid_form():
    with pytest.raises(ValueError, match="expected 'CHAIN:RESNUM:TARGET'"):
        _parse_sites_str("A-65-SEP")


def test_parse_sites_str_invalid_resnum():
    with pytest.raises(ValueError, match="Invalid resnum"):
        _parse_sites_str("A:NOT_A_NUMBER:SEP")


# ---------- _apply_phosphorylation_to_pdb -----------------------------------


def test_apply_phosphorylation_ser_to_sep_renames_and_strips_hg(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_ser_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "A", "resnum": 65, "target": "SEP"}],
    )
    assert res["mismatch"] == []
    assert res["not_found"] == []
    assert res["applied"] == [{"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}]
    text = out_pdb.read_text()
    assert " SEP " in text  # residue renamed
    assert " SER " not in text
    assert " OG " in text  # OG kept
    assert " HG " not in text  # hydroxyl H stripped


def test_apply_phosphorylation_thr_to_tpo_strips_hg1(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_thr_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "B", "resnum": 12, "target": "TPO"}],
    )
    assert res["mismatch"] == []
    assert res["applied"][0]["target"] == "TPO"
    text = out_pdb.read_text()
    assert " TPO " in text
    assert " OG1 " in text
    assert " HG1 " not in text


def test_apply_phosphorylation_mismatch_records_error(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_ser_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "A", "resnum": 65, "target": "PTR"}],
    )
    assert res["applied"] == []
    assert len(res["mismatch"]) == 1
    assert res["mismatch"][0]["expected"] == "TYR"
    assert res["mismatch"][0]["actual"] == "SER"


def test_apply_phosphorylation_site_not_in_pdb_is_recorded(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_ser_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "Z", "resnum": 999, "target": "SEP"}],
    )
    assert res["applied"] == []
    assert res["not_found"] == [{"chain": "Z", "resnum": 999, "target": "SEP"}]


# ---------- detect_ptm_sites ------------------------------------------------


def test_detect_ptm_sites_finds_sep(tmp_path):
    pdb = tmp_path / "ptm.pdb"
    _write_sep_pdb(pdb)
    sites = detect_ptm_sites(str(pdb))
    assert sites == [{"chain": "A", "resnum": 65, "name": "SEP"}]


def test_detect_ptm_sites_empty_for_plain_protein(tmp_path):
    pdb = tmp_path / "ser.pdb"
    _write_ser_pdb(pdb)
    assert detect_ptm_sites(str(pdb)) == []


def test_detect_ptm_sites_missing_file_returns_empty():
    assert detect_ptm_sites("/nonexistent/file.pdb") == []


# ---------- phosphorylate_residues (top-level) -------------------------------


def test_phosphorylate_residues_mutual_exclusivity():
    res = phosphorylate_residues(
        pdb_file="anything.pdb",
        sites_str="A:65:SEP",
        restore_from_detection=True,
    )
    assert res["success"] is False
    assert any("exactly one" in e for e in res["errors"])


def test_phosphorylate_residues_nothing_provided():
    res = phosphorylate_residues(pdb_file="anything.pdb")
    assert res["success"] is False
    assert any("exactly one" in e for e in res["errors"])


def test_phosphorylate_residues_unsupported_target(tmp_path):
    pdb = tmp_path / "ser.pdb"
    _write_ser_pdb(pdb)
    res = phosphorylate_residues(
        pdb_file=str(pdb),
        sites_str="A:65:HEP",   # HEP not supported in v1
    )
    assert res["success"] is False
    assert any("Unsupported target residue" in e for e in res["errors"])


def test_phosphorylate_residues_explicit_sites_str(tmp_path):
    pdb = tmp_path / "ser.pdb"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_ser_pdb(pdb)
    res = phosphorylate_residues(
        pdb_file=str(pdb),
        sites_str="A:65:SEP",
        output_dir=str(out_dir),
    )
    assert res["success"], res.get("errors")
    out_path = Path(res["output_path"])
    assert out_path.is_file()
    assert " SEP " in out_path.read_text()
    assert res["applied_sites"] == [
        {"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}
    ]


def test_phosphorylate_residues_node_mode_restore_from_detection(tmp_path):
    """Happy path: prep_001 records a PTM whose `chain` is the *merged*
    chain id (the post-merge form, as produced by the prepare_complex
    chain remap). phosphorylate_residues looks the residue up at that
    chain id and applies the phosphorylation."""
    job_dir = tmp_path / "job_phospho"

    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_ser_pdb(parent_pdb)
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        metadata={
            "detected_ptm_residues": [
                # chain="A" is the merged chain. original_chain may differ.
                {"chain": "A", "original_chain": "A", "resnum": 65, "name": "SEP"},
            ],
        },
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"], res.get("errors")
    node_data = read_node(str(job_dir), "prep_002")
    assert node_data["status"] == "completed"
    assert node_data["artifacts"]["merged_pdb"] == "artifacts/phosphorylated.pdb"
    assert node_data["artifacts"]["phosphorylated_pdb"] == "artifacts/phosphorylated.pdb"
    ptm_meta = node_data["metadata"]["ptm_residues"]
    assert ptm_meta == [
        {"chain": "A", "resnum": 65, "name": "SEP", "source": "detected"}
    ]


def test_phosphorylate_residues_node_mode_uses_merged_not_original_chain(tmp_path):
    """Regression for a real bug: when the source PDB's chain id (e.g. "B")
    differs from the merged.pdb chain id (e.g. "A"), the consumer must
    look the residue up at the *merged* chain id. If we accidentally used
    `original_chain`, the lookup would fail (or worse, silently match a
    different chain in the merged PDB).
    """
    job_dir = tmp_path / "job_phospho_remapped"

    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    # merged.pdb has SER on chain A — that's where the residue actually is.
    _write_ser_pdb(parent_pdb)
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        metadata={
            "detected_ptm_residues": [
                # `chain` (= merged) is "A"; original was "B".
                {"chain": "A", "original_chain": "B",
                 "resnum": 65, "name": "SEP"},
            ],
        },
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"], res.get("errors")
    assert res["applied_sites"] == [
        {"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}
    ]


# ---------- composite chain map + _remap_detected_ptm_chains ----------------


def _make_protein_entry(chain_id, output_file, success=True):
    """Shape that prepare_complex's `result["proteins"]` entries take."""
    return {"chain_id": chain_id, "output_file": output_file, "success": success}


def test_build_composite_map_single_letter_authors():
    """All single-letter source chains, no truncation surprise."""
    chain_file_info = [
        {"chain_id": "A", "author_chain": "A", "file": "/x/protein_1.pdb"},
        {"chain_id": "B", "author_chain": "B", "file": "/x/protein_2.pdb"},
    ]
    proteins = [
        _make_protein_entry("A", "/x/protein_1.amber.pdb"),
        _make_protein_entry("B", "/x/protein_2.amber.pdb"),
    ]
    merge_mapping = {
        "/x/protein_1.amber.pdb": {"A": "A"},
        "/x/protein_2.amber.pdb": {"B": "B"},
    }
    assert _build_source_to_merged_chain_map(
        chain_file_info, proteins, merge_mapping
    ) == {"A": "A", "B": "B"}


def test_build_composite_map_pool_shift_when_no_chain_a():
    """Source has chains B,C only; merge pool starts at A so B->A, C->B."""
    chain_file_info = [
        {"chain_id": "B", "author_chain": "B", "file": "/x/protein_1.pdb"},
        {"chain_id": "C", "author_chain": "C", "file": "/x/protein_2.pdb"},
    ]
    proteins = [
        _make_protein_entry("B", "/x/protein_1.amber.pdb"),
        _make_protein_entry("C", "/x/protein_2.amber.pdb"),
    ]
    merge_mapping = {
        "/x/protein_1.amber.pdb": {"B": "A"},
        "/x/protein_2.amber.pdb": {"C": "B"},
    }
    assert _build_source_to_merged_chain_map(
        chain_file_info, proteins, merge_mapping
    ) == {"B": "A", "C": "B"}


def test_build_composite_map_multi_letter_mmcif_author_chain():
    """The case the previous remap implementation missed: an mmCIF source
    with a multi-letter author chain (e.g. "BBB") gets truncated to "B" by
    split_molecules' PDB writer, merge_structures sees "B" and reassigns
    to "A". The composite map must restore the link from the *full* source
    author "BBB" through to the merged "A"."""
    chain_file_info = [
        {"chain_id": "B", "author_chain": "BBB", "file": "/x/protein_1.pdb"},
    ]
    proteins = [
        _make_protein_entry("B", "/x/protein_1.amber.pdb"),
    ]
    merge_mapping = {
        # split_molecules wrote "B" (= "BBB"[0]) into the PDB column;
        # merge_structures saw "B" and reassigned to "A".
        "/x/protein_1.amber.pdb": {"B": "A"},
    }
    composite = _build_source_to_merged_chain_map(
        chain_file_info, proteins, merge_mapping
    )
    assert composite == {"BBB": "A"}


def test_build_composite_map_truncation_collision_resolved_by_file_path():
    """The motivating case for the file-path join: two mmCIF author
    chains whose first characters collide under split_molecules' PDB
    truncation. ``"BBB"[0] == "BCC"[0] == "B"``, so both cleaned PDBs
    declare an internal chain ``"B"``. ``merge_structures`` then
    reassigns each to a distinct merged id (``A`` and ``B``) based on
    arrival order. The composite builder must distinguish the two
    sources via the cleaned file path — not via the (colliding)
    truncated chain id — and produce ``{BBB: A, BCC: B}``.
    """
    chain_file_info = [
        # gemmi's label_asym_id for these chains may be different, so
        # chain_id can be "B" / "C" while the author chains are "BBB"
        # / "BCC". Both author chains truncate to "B" in the split PDB.
        {"chain_id": "B", "author_chain": "BBB", "file": "/x/p1.pdb"},
        {"chain_id": "C", "author_chain": "BCC", "file": "/x/p2.pdb"},
    ]
    proteins = [
        _make_protein_entry("B", "/x/p1.amber.pdb"),
        _make_protein_entry("C", "/x/p2.amber.pdb"),
    ]
    merge_mapping = {
        # Both cleaned PDBs internally declare chain "B" (the collision).
        # merge_structures reassigns them to A and B by arrival order.
        # Joining on the cleaned file path keeps the two sources distinct.
        "/x/p1.amber.pdb": {"B": "A"},
        "/x/p2.amber.pdb": {"B": "B"},
    }
    assert _build_source_to_merged_chain_map(
        chain_file_info, proteins, merge_mapping
    ) == {"BBB": "A", "BCC": "B"}


def test_build_composite_map_drops_excluded_chain():
    """If select_chains excluded a chain at split time, it never appears
    in proteins[] / merge_mapping, so the composite map omits it. The
    remap then drops the corresponding PTM with a warning."""
    chain_file_info = [
        {"chain_id": "A", "author_chain": "A", "file": "/x/protein_1.pdb"},
    ]
    proteins = [_make_protein_entry("A", "/x/protein_1.amber.pdb")]
    merge_mapping = {"/x/protein_1.amber.pdb": {"A": "A"}}
    composite = _build_source_to_merged_chain_map(
        chain_file_info, proteins, merge_mapping
    )
    assert composite == {"A": "A"}  # B is missing


def test_build_composite_map_skips_failed_protein():
    """A protein chain whose cleaning failed has success=False. The map
    must skip it so we never claim it's available for restore."""
    chain_file_info = [
        {"chain_id": "A", "author_chain": "A", "file": "/x/p1.pdb"},
        {"chain_id": "B", "author_chain": "B", "file": "/x/p2.pdb"},
    ]
    proteins = [
        _make_protein_entry("A", "/x/p1.amber.pdb", success=True),
        _make_protein_entry("B", "/x/p2.amber.pdb", success=False),
    ]
    merge_mapping = {"/x/p1.amber.pdb": {"A": "A"}}  # B never reached merge
    assert _build_source_to_merged_chain_map(
        chain_file_info, proteins, merge_mapping
    ) == {"A": "A"}


def test_remap_detected_ptm_chains_basic():
    detected = [
        {"chain": "A", "resnum": 65, "name": "SEP"},
        {"chain": "B", "resnum": 178, "name": "TPO"},
    ]
    composite = {"A": "A", "B": "B"}
    remapped, dropped = _remap_detected_ptm_chains(detected, composite)
    assert dropped == []
    assert remapped == [
        {"chain": "A", "original_chain": "A", "resnum": 65, "name": "SEP"},
        {"chain": "B", "original_chain": "B", "resnum": 178, "name": "TPO"},
    ]


def test_remap_detected_ptm_chains_with_pool_shift():
    detected = [
        {"chain": "B", "resnum": 65, "name": "SEP"},
        {"chain": "C", "resnum": 178, "name": "TPO"},
    ]
    composite = {"B": "A", "C": "B"}
    remapped, dropped = _remap_detected_ptm_chains(detected, composite)
    assert dropped == []
    assert {(r["chain"], r["original_chain"]) for r in remapped} == {
        ("A", "B"), ("B", "C"),
    }


def test_remap_detected_ptm_chains_with_multi_letter_author():
    """End-to-end of the bug the user flagged: detected_ptm_residues lists
    a multi-letter source chain "BBB" (as gemmi reports it on an mmCIF),
    composite map (from _build_source_to_merged_chain_map) translates that
    to merged "A", and the remapped record stores merged="A" with the
    original "BBB" preserved for provenance."""
    detected = [{"chain": "BBB", "resnum": 65, "name": "SEP"}]
    composite = {"BBB": "A"}
    remapped, dropped = _remap_detected_ptm_chains(detected, composite)
    assert dropped == []
    assert remapped == [
        {"chain": "A", "original_chain": "BBB", "resnum": 65, "name": "SEP"}
    ]


def test_remap_detected_ptm_chains_drops_chain_not_in_map():
    detected = [
        {"chain": "A", "resnum": 65, "name": "SEP"},
        {"chain": "BBB", "resnum": 178, "name": "TPO"},
    ]
    composite = {"A": "A"}  # BBB missing (e.g. excluded by select_chains)
    remapped, dropped = _remap_detected_ptm_chains(detected, composite)
    assert remapped == [
        {"chain": "A", "original_chain": "A", "resnum": 65, "name": "SEP"},
    ]
    assert dropped == [{"chain": "BBB", "resnum": 178, "name": "TPO"}]


def test_remap_detected_ptm_chains_empty_inputs():
    assert _remap_detected_ptm_chains([], {"A": "A"}) == ([], [])
    assert _remap_detected_ptm_chains(
        [{"chain": "A", "resnum": 65, "name": "SEP"}], {}
    ) == ([], [{"chain": "A", "resnum": 65, "name": "SEP"}])


def test_phosphorylate_residues_node_mode_restore_with_multi_letter_original_chain(tmp_path):
    """End-to-end consumer-side check for the multi-letter mmCIF case:

    - source mmCIF had a PTM at chain `BBB`, resnum 65 (SEP)
    - prepare_complex's chain remap stored that as merged chain `A`,
      original_chain `BBB`
    - merged.pdb has a SER at chain A, resnum 65
    - phosphorylate_residues --restore-from-detection picks up the merged
      chain id "A" (NOT the source "BBB") and produces SEP at chain A
    """
    job_dir = tmp_path / "job_phospho_mmcif"

    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_ser_pdb(parent_pdb)  # SER at chain A, resnum 65
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        metadata={
            "detected_ptm_residues": [
                # The multi-letter mmCIF author chain — this is what
                # prepare_complex now writes after the fix.
                {"chain": "A", "original_chain": "BBB",
                 "resnum": 65, "name": "SEP"},
            ],
        },
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"], res.get("errors")
    assert res["applied_sites"] == [
        {"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}
    ]
    node_data = read_node(str(job_dir), "prep_002")
    assert node_data["status"] == "completed"
    # The phosphorylated PDB should have SEP at chain A, not "BBB" or "B".
    out_pdb = job_dir / "nodes" / "prep_002" / "artifacts" / "phosphorylated.pdb"
    text = out_pdb.read_text()
    assert " SEP A" in text
    assert " SEP B" not in text


# ---------- not_found is fatal by default ----------------------------------


def test_phosphorylate_residues_not_found_is_fatal_by_default(tmp_path):
    """A typo in --sites-str should fail the run, not silently apply a
    subset. Two sites requested, only one exists in the PDB."""
    pdb = tmp_path / "ser.pdb"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_ser_pdb(pdb)
    res = phosphorylate_residues(
        pdb_file=str(pdb),
        sites_str="A:65:SEP,A:999:SEP",  # 999 is a typo
        output_dir=str(out_dir),
    )
    assert res["success"] is False
    joined = " ".join(res["errors"])
    assert "not located" in joined
    assert "999" in joined
    # Output file must not be left behind.
    leftover = list(out_dir.glob("*phosphorylated.pdb"))
    assert leftover == []


def test_phosphorylate_residues_allow_partial_proceeds(tmp_path):
    """allow_partial=True turns not_found into a warning and applies the
    subset that does exist."""
    pdb = tmp_path / "ser.pdb"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_ser_pdb(pdb)
    res = phosphorylate_residues(
        pdb_file=str(pdb),
        sites_str="A:65:SEP,A:999:SEP",
        allow_partial=True,
        output_dir=str(out_dir),
    )
    assert res["success"], res.get("errors")
    assert res["applied_sites"] == [
        {"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}
    ]
    # Warning must mention allow_partial so the user knows why we proceeded.
    assert any("allow_partial" in w for w in res["warnings"])


def test_phosphorylate_residues_node_mode_remap_drift_fails_fast(tmp_path):
    """If detected_ptm_residues somehow points at a chain that does not
    exist in the merged.pdb (e.g. corruption, or a missed remap path),
    the run must fail rather than silently complete the node."""
    job_dir = tmp_path / "job_phospho_drift"

    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_ser_pdb(parent_pdb)  # only chain A
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        metadata={
            "detected_ptm_residues": [
                {"chain": "Z", "original_chain": "Z",
                 "resnum": 65, "name": "SEP"},  # chain Z does not exist
            ],
        },
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"] is False
    assert any("not located" in e for e in res["errors"])
    node_data = read_node(str(job_dir), "prep_002")
    assert node_data["status"] == "failed"


def test_phosphorylate_residues_node_mode_restore_from_empty_detection_fails(tmp_path):
    job_dir = tmp_path / "job_no_ptm"
    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_ser_pdb(parent_pdb)
    # No detected_ptm_residues metadata
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"] is False
    assert any("No detected_ptm_residues" in e for e in res["errors"])


# ---------- build_amber_system phosaa autoload -------------------------------


def _capture_om_bundle(tmp_path):
    """Helper: returns (captured_dict, side_effect_callable) for patching
    `mdclaw.amber_server._run_openmmforcefields_build`. The fake records the
    XML bundle resolved by the catalog so the caller can inspect it.

    PR3 retired the tleap script + tleap process, so the previous
    `_capture_leap_script` helper that scanned `.leap.in` files is no longer
    meaningful — phosaa autoload now manifests as `amber/phosaa19SB.xml`
    appearing in the SystemGenerator forcefields bundle.
    """
    captured: dict = {}

    def _fake_om_build(**kwargs):
        from mdclaw import forcefield_catalog as _fc
        from mdclaw.amber.openmm_build import (
            _resolve_dna_name_from_libraries,
            _resolve_glycan_name_from_library,
            _resolve_phosaa_name_from_library,
            _resolve_rna_name_from_libraries,
        )
        bundle = _fc.resolve_xml_bundle(
            protein=_fc.normalize_protein(kwargs["forcefield"]) or kwargs["forcefield"],
            water=_fc.normalize_water(kwargs["water_model"]) if kwargs["water_model"] else None,
            phosaa=_resolve_phosaa_name_from_library(kwargs["phosaa_library"]),
            dna=_resolve_dna_name_from_libraries(kwargs["nucleic_libraries"]),
            rna=_resolve_rna_name_from_libraries(kwargs["nucleic_libraries"]),
            glycan=_resolve_glycan_name_from_library(kwargs["glycan_library"]),
            lipid="lipid21" if kwargs["is_membrane"] else None,
        )
        captured["bundle"] = bundle
        captured["phosaa_library"] = kwargs["phosaa_library"]
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

    return captured, _fake_om_build


def test_build_amber_system_phosaa_autoload_in_xml_bundle(tmp_path):
    """When the input PDB carries SEP, the SystemGenerator XML bundle must
    include `amber/phosaa19SB.xml` after `amber/protein.ff19SB.xml`, and the
    topo node metadata must record the chosen library + residue list."""
    pdb = tmp_path / "with_sep.pdb"
    out_dir = tmp_path / "topo"
    out_dir.mkdir()
    _write_sep_pdb(pdb)
    captured, fake = _capture_om_bundle(tmp_path)

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        side_effect=fake,
    ):
        result = build_amber_system(
            pdb_file=str(pdb),
            output_dir=str(out_dir),
            box_dimensions={"box_a": 10.0, "box_b": 10.0, "box_c": 10.0},
            forcefield="ff19SB",
            water_model="opc",
        )

    bundle = captured.get("bundle", [])
    assert "amber/protein.ff19SB.xml" in bundle
    assert "amber/phosaa19SB.xml" in bundle
    # Order: phosaa must come AFTER the protein XML (Amber25 manual ch.14.4.1).
    assert bundle.index("amber/protein.ff19SB.xml") < bundle.index("amber/phosaa19SB.xml")
    # The parameters block still records the legacy leaprc string for
    # downstream evidence-server lookups.
    assert result["parameters"].get("phosaa_library") == "leaprc.phosaa19SB"
    assert {s["name"] for s in result["parameters"].get("ptm_residues", [])} == {"SEP"}


def test_build_amber_system_no_phosaa_when_no_ptm(tmp_path):
    pdb = tmp_path / "ser.pdb"
    out_dir = tmp_path / "topo"
    out_dir.mkdir()
    _write_ser_pdb(pdb)
    captured, fake = _capture_om_bundle(tmp_path)

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        side_effect=fake,
    ):
        build_amber_system(
            pdb_file=str(pdb),
            output_dir=str(out_dir),
            box_dimensions={"box_a": 10.0, "box_b": 10.0, "box_c": 10.0},
            forcefield="ff19SB",
            water_model="opc",
        )

    bundle = captured.get("bundle", [])
    assert not any("phosaa" in xml for xml in bundle)


# ---------- sanity ----------------------------------------------------------


def test_phosaa_targets_constant_matches_phospho_resnames():
    """The two sources of truth should agree."""
    assert set(_PHOSPHO_TARGETS) == PHOSPHO_RESNAMES
