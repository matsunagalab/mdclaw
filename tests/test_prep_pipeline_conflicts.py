"""Fast regression tests for the prep-pipeline residue-modification 导线.

These encode the 10 conflict cases found by the multi-agent audit so the
"fix -> re-check -> loop until all pass" cycle is test-driven instead of
re-running LLM agents each round. Each test is a fast, deterministic,
function-level check (no network, no full prepare_complex run).

Cases that are FIXED assert the corrected behavior; cases not yet addressed
are marked xfail with a note so the suite stays green while flagging the gap.

Run with: conda run -n mdclaw pytest tests/test_prep_pipeline_conflicts.py -v
"""

import pytest

from mdclaw.structure.phosphorylation import (
    _apply_phosphorylation_to_pdb,
    _remap_detected_ptm_chains,
    _remap_disulfide_chains,
    _remap_histidine_state_chains,
    _remap_protonation_state_chains,
)
from mdclaw.structure.pdb_utils import (
    _pdb_chain_id_for_index,
    restore_residue_numbering_from_reference,
)


# --- CASE 1: dropped. The mmCIF multi-letter chain remap (BBB->A) and the
# explicit wrong-chain not_found / mismatch guards are covered by
# test_phosphorylation.py. The only residual is a user explicitly naming the
# wrong chain that happens to hold the same resnum AND the expected source
# residue — indistinguishable from a valid request, so not defensible. Not a
# pipeline conflict; intentionally not tracked here.

# --- CASE 2: select_chains drops a PTM chain -> dropped, not silent ---------
def test_case2_excluded_chain_ptm_is_dropped_not_misapplied():
    # chain B excluded -> not in composite map
    remapped, dropped = _remap_detected_ptm_chains(
        [{"chain": "B", "resnum": 12, "name": "SEP"}], {"A": "A"}
    )
    assert remapped == []
    assert dropped == [{"chain": "B", "resnum": 12, "name": "SEP"}]


# --- CASE 3 / 7: phosphorylation refuses a mismatched (mutated) residue -----
def test_case3_phosphorylate_refuses_mismatched_residue(tmp_path):
    # target A:65 is ALA (e.g. after an S65A mutation), not the SER that SEP needs
    pdb = ("ATOM      1  N   ALA A  65       0.000   0.000   0.000  1.00  0.00\n"
           "ATOM      2  CA  ALA A  65       1.000   0.000   0.000  1.00  0.00\n"
           "ATOM      3  CB  ALA A  65       1.000   1.000   0.000  1.00  0.00\nEND\n")
    src = tmp_path / "in.pdb"; out = tmp_path / "out.pdb"
    src.write_text(pdb)
    res = _apply_phosphorylation_to_pdb(
        src, out, [{"chain": "A", "resnum": 65, "target": "SEP"}]
    )
    assert res["applied"] == []          # nothing silently applied
    assert res["mismatch"]               # mismatch recorded instead
    assert res["mismatch"][0]["actual"] == "ALA"


# --- CASE 4: protonation / histidine summary remapped to merged chains ------
def test_case4_protonation_states_remapped_to_merged_chain():
    states = [{"chain": "B", "resnum": 126, "state": "HID"}]
    _remap_protonation_state_chains(states, {"B": "A"})
    assert states[0]["chain"] == "A"
    assert states[0]["original_chain"] == "B"


def test_case4_histidine_states_keys_remapped_to_merged_chain():
    out = _remap_histidine_state_chains({"B:126": "HID", "A:5": "HIE"}, {"B": "A"})
    assert out == {"A:126": "HID", "A:5": "HIE"}


# --- CASE 6: disulfide pair chains remapped before CYX reconcile ------------
def test_case6_disulfide_pair_chains_remapped_to_merged():
    bonds = [{"cys1": {"chain": "B", "resnum": 10},
              "cys2": {"chain": "B", "resnum": 55}}]
    _remap_disulfide_chains(bonds, {"B": "A"})
    assert bonds[0]["cys1"]["chain"] == "A"
    assert bonds[0]["cys2"]["chain"] == "A"
    assert bonds[0]["cys1"]["original_chain"] == "B"


# --- CASE 9: pdb4amber renumbering is undone from the reference -------------
def test_case9_pdb4amber_renumber_restored(tmp_path):
    ref = ("ATOM      1  N   ALA A   1       0.0   0.0   0.0\n"
           "ATOM      2  N   MET B   1       3.0   0.0   0.0\n"
           "ATOM      3  N   LEU B   2       4.0   0.0   0.0\nEND\n")
    tgt = ("ATOM      1  N   ALA A   1       0.0   0.0   0.0\n"
           "ATOM      2  N   MET B 215       3.0   0.0   0.0\n"
           "ATOM      3  N   LEU B 216       4.0   0.0   0.0\nEND\n")
    rf = tmp_path / "ref.pdb"; tf = tmp_path / "tgt.pdb"
    rf.write_text(ref); tf.write_text(tgt)
    assert restore_residue_numbering_from_reference(tf, rf) is not None
    keys = [(l[21], l[22:26].strip()) for l in tf.read_text().splitlines()
            if l.startswith("ATOM  ")]
    assert keys == [("A", "1"), ("B", "1"), ("B", "2")]


# --- CASE 5 & 8: PDBFixer-internal behavior (numbering preservation across
# missing-residue modeling; ACE/NME cap H completion). Verified OK by the audit
# and exercised by the slow integration pipeline tests; not unit-testable here
# without invoking PDBFixer, so intentionally left to those suites.

# --- CASE 10: chain-id pool exhaustion (>62 chains). PDB's single-character
# chain field cannot give >62 unique ids, so two chains can share one merged
# id. The REAL fix: site-keyed PTM edits resolve the chain by topology_chain
# index (== chain block position in merged.pdb), not by the ambiguous id, so the
# correct chain is targeted. A site naming a reused id WITHOUT a topology index
# is reported as `ambiguous` (fail-clear), never silently applied to the first.
def test_case10_pool_is_62_and_reuses_beyond():
    from mdclaw.structure.merge import PDB_CHAIN_ID_POOL
    assert len(PDB_CHAIN_ID_POOL) == 62
    assert _pdb_chain_id_for_index(62) == _pdb_chain_id_for_index(0) == "A"
    assert len({_pdb_chain_id_for_index(i) for i in range(62)}) == 62


def _ser(serial, chain, resnum, atom, res="SER"):
    return (f"ATOM  {serial:>5} {atom:<4} {res:<3} {chain}{resnum:>4}"
            f"       0.000   0.000   0.000  1.00  0.00\n")


def _ser_block(start_serial, chain, resnum):
    # a minimal SER residue (N/CA/CB/OG) so phospho geometry/rename can run
    return "".join(
        _ser(start_serial + i, chain, resnum, a)
        for i, a in enumerate(("N", "CA", "CB", "OG"))
    )


def _resname_at(text, block_index, resnum):
    """resName of the residue at (block_index, resnum), block = chain-id run."""
    blk, prev = -1, None
    for ln in text.splitlines():
        if not ln.startswith(("ATOM  ", "HETATM")):
            continue
        c = ln[21:22]
        if c != prev:
            blk += 1
            prev = c
        if blk == block_index and ln[22:26].strip() == str(resnum):
            return ln[17:20].strip()
    return None


def test_case10_topology_index_targets_correct_reused_chain(tmp_path):
    # Two chains share id 'A' (block 0 and block 2), as when the 62-id pool is
    # reused. A PTM on the SECOND 'A' must hit block 2, not block 0.
    pdb = (_ser_block(1, "A", 65)       # block 0, chain 'A'
           + _ser_block(5, "B", 10)     # block 1, chain 'B'
           + _ser_block(9, "A", 65)     # block 2, chain 'A' (reused id)
           + "END\n")
    src = tmp_path / "in.pdb"; out = tmp_path / "out.pdb"
    src.write_text(pdb)
    res = _apply_phosphorylation_to_pdb(
        src, out,
        [{"chain": "A", "resnum": 65, "target": "SEP", "topology_chain_index": 2}],
    )
    assert res["ambiguous"] == []
    assert [a["resnum"] for a in res["applied"]] == [65]
    text = out.read_text()
    assert _resname_at(text, 0, 65) == "SER"   # first 'A' untouched
    assert _resname_at(text, 2, 65) == "SEP"   # the targeted 'A' phosphorylated


def test_case10_reused_chain_without_index_is_ambiguous(tmp_path):
    # Same duplicate-'A' pdb, but the site does NOT carry a topology index ->
    # must be reported ambiguous (fail-clear), never applied to the first 'A'.
    pdb = (_ser_block(1, "A", 65) + _ser_block(5, "B", 10)
           + _ser_block(9, "A", 65) + "END\n")
    src = tmp_path / "in.pdb"; out = tmp_path / "out.pdb"
    src.write_text(pdb)
    res = _apply_phosphorylation_to_pdb(
        src, out, [{"chain": "A", "resnum": 65, "target": "SEP"}],
    )
    assert res["applied"] == []
    assert res["ambiguous"] and res["ambiguous"][0]["block_indices"] == [0, 2]


def test_case10_remap_attaches_topology_chain_index():
    # The detection remap carries topology_chain_index from the index map so the
    # restore path can disambiguate downstream.
    remapped, dropped = _remap_detected_ptm_chains(
        [{"chain": "X", "resnum": 65, "name": "SEP"}],
        {"X": "A"}, {"X": 62},
    )
    assert dropped == []
    assert remapped[0]["chain"] == "A"
    assert remapped[0]["topology_chain_index"] == 62


def test_case10_build_topology_index_map_joins_on_file():
    from mdclaw.structure.phosphorylation import (
        _build_source_to_topology_index_map,
    )
    idx = _build_source_to_topology_index_map(
        chain_file_info=[{"chain_id": "B", "author_chain": "BBB"}],
        proteins=[{"success": True, "chain_id": "B", "output_file": "/x/p1.pdb"}],
        chain_mapping_entries=[
            {"source_file": "/x/p1.pdb", "topology_chain_index": 62,
             "md_chain_id": "A"},
        ],
    )
    assert idx == {"BBB": 62}
