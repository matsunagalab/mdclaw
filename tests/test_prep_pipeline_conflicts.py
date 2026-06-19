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
    _build_source_to_merged_chain_map,
    _remap_detected_ptm_chains,
    _remap_disulfide_chains,
    _remap_histidine_state_chains,
    _remap_protonation_state_chains,
)
from mdclaw.structure.pdb_utils import (
    _pdb_chain_id_for_index,
    restore_residue_numbering_from_reference,
)


def _composite_bbb_to_a():
    """source author chain 'BBB' (mmCIF multi-letter) -> merged 'A'."""
    return _build_source_to_merged_chain_map(
        chain_file_info=[{"chain_id": "B", "author_chain": "BBB"}],
        proteins=[{"success": True, "chain_id": "B", "output_file": "/x/p1.pdb"}],
        merge_chain_mapping={"/x/p1.pdb": {"B": "A"}},
    )


# --- CASE 1: mmCIF multi-letter chain + PTM, auto-detect path ---------------
def test_case1_multiletter_chain_ptm_autodetect_remaps_correctly():
    composite = _composite_bbb_to_a()
    assert composite == {"BBB": "A"}
    remapped, dropped = _remap_detected_ptm_chains(
        [{"chain": "BBB", "resnum": 65, "name": "SEP"}], composite
    )
    assert dropped == []
    assert remapped == [{"chain": "A", "original_chain": "BBB",
                         "resnum": 65, "name": "SEP"}]


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

# --- CASE 10: chain-id pool exhaustion (>62 chains) reuses ids; the PTM remap
# does not yet disambiguate via topology_chain_index. Fix pending.
@pytest.mark.xfail(reason="Case 10 fix pending: chain-id reuse >62 chains is "
                          "not yet disambiguated in the source->merged map")
def test_case10_chain_pool_exhaustion_disambiguated():
    # index 0 and index 62 both map to 'A' (pool length 62) -> ambiguous reuse.
    assert _pdb_chain_id_for_index(0) != _pdb_chain_id_for_index(62)
