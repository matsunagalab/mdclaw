"""Regression tests for Fix B: split_molecules chain-ID selection contract.

External API: ``select_chains`` is interpreted as ``chain_id`` (label_asym_id)
first, with a safety fallback to ``author_chain`` (auth_asym_id). These tests
use tiny hand-crafted mmCIF fixtures so they run without network access or
scientific-package heavy deps beyond gemmi.

Covers:
- label-only match (user passes label, label present, label != auth)
- author fallback (user passes author, label not matched, author hit)
- missing chain error lists both systems and the label->author map
- summary exposes protein_label_ids and chain_id_map
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mdclaw"))


# ─────────────────────────────────────────────────────────────────────────
# Minimal mmCIF with label_asym_id "A"/"B" and auth_asym_id "AAA"/"BBB"
# (mirrors the 7QVK pattern that broke the 25 chain_not_found cases).
# ─────────────────────────────────────────────────────────────────────────
_CIF_LABEL_NE_AUTH = """\
data_TEST
#
_entry.id TEST
#
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
ATOM 1 N N   . ALA A 1 1 ? 0.000 0.000 0.000 1.00 0.00 ? 1 ALA AAA N 1
ATOM 2 C CA  . ALA A 1 1 ? 1.500 0.000 0.000 1.00 0.00 ? 1 ALA AAA CA 1
ATOM 3 C C   . ALA A 1 1 ? 2.000 1.500 0.000 1.00 0.00 ? 1 ALA AAA C 1
ATOM 4 O O   . ALA A 1 1 ? 1.500 2.500 0.000 1.00 0.00 ? 1 ALA AAA O 1
ATOM 5 C CB  . ALA A 1 1 ? 2.000 -1.000 1.000 1.00 0.00 ? 1 ALA AAA CB 1
ATOM 6 N N   . GLY B 2 1 ? 10.000 0.000 0.000 1.00 0.00 ? 1 GLY BBB N 1
ATOM 7 C CA  . GLY B 2 1 ? 11.500 0.000 0.000 1.00 0.00 ? 1 GLY BBB CA 1
ATOM 8 C C   . GLY B 2 1 ? 12.000 1.500 0.000 1.00 0.00 ? 1 GLY BBB C 1
ATOM 9 O O   . GLY B 2 1 ? 11.500 2.500 0.000 1.00 0.00 ? 1 GLY BBB O 1
#
"""


@pytest.fixture
def cif_label_ne_auth(tmp_path: Path) -> str:
    p = tmp_path / "test_label_ne_auth.cif"
    p.write_text(_CIF_LABEL_NE_AUTH, encoding="utf-8")
    return str(p)


def _inspect(path: str) -> dict:
    from mdclaw.structure_server import _inspect_molecules_impl
    return _inspect_molecules_impl(path)


def test_inspect_molecules_exposes_label_and_author_ids(cif_label_ne_auth):
    """inspect_molecules summary must surface both label IDs and the label->author map."""
    r = _inspect(cif_label_ne_auth)
    assert r["success"], r.get("errors")

    # Per-chain records carry both IDs.
    chains = {c["chain_id"]: c["author_chain"] for c in r["chains"]}
    assert chains == {"A": "AAA", "B": "BBB"}

    s = r["summary"]
    # Label lists are what callers pass to select_chains.
    assert sorted(s["protein_label_ids"]) == ["A", "B"]
    # Author lists remain under the historical names for display.
    assert sorted(s["protein_chain_ids"]) == ["AAA", "BBB"]
    # Explicit mapping for surprising pairings (e.g. swaps).
    assert s["chain_id_map"] == {"A": "AAA", "B": "BBB"}


def test_split_molecules_matches_label_asym_id(cif_label_ne_auth, tmp_path):
    """Primary contract: select_chains=['B'] picks label 'B' even though auth is 'BBB'."""
    from mdclaw.structure_server import split_molecules

    out_dir = tmp_path / "out_label"
    r = split_molecules(
        structure_file=cif_label_ne_auth,
        output_dir=str(out_dir),
        select_chains=["B"],
        include_types=["protein"],
    )
    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    # Only chain B (author=BBB) residues should be in the output.
    content = Path(r["protein_files"][0]).read_text()
    assert "GLY" in content  # chain B contained GLY
    assert "ALA" not in content  # chain A contained ALA


def test_split_molecules_author_fallback_emits_warning(cif_label_ne_auth, tmp_path):
    """Fallback contract: select_chains=['BBB'] (author value) still resolves,
    but emits a warning pointing the caller at the label-first convention."""
    from mdclaw.structure_server import split_molecules

    out_dir = tmp_path / "out_author_fallback"
    r = split_molecules(
        structure_file=cif_label_ne_auth,
        output_dir=str(out_dir),
        select_chains=["BBB"],
        include_types=["protein"],
    )
    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    # Fallback used -> the warning must mention it.
    assert any("author_chain fallback" in w for w in r["warnings"]), r["warnings"]


def test_split_molecules_missing_chain_reports_both_systems(cif_label_ne_auth, tmp_path):
    """Error path: unknown chain reports available labels, authors, and the map."""
    from mdclaw.structure_server import split_molecules

    out_dir = tmp_path / "out_missing"
    r = split_molecules(
        structure_file=cif_label_ne_auth,
        output_dir=str(out_dir),
        select_chains=["Z"],
        include_types=["protein"],
    )
    assert r["success"] is False
    joined = " ".join(r["errors"])
    assert "Chain(s) not found" in joined
    assert "label_asym_id" in joined
    assert "auth_asym_id" in joined
    assert "label -> author mapping" in joined


def test_split_molecules_use_author_chains_kwarg_removed(cif_label_ne_auth):
    """The deprecated `use_author_chains` kwarg is no longer accepted."""
    from mdclaw.structure_server import split_molecules

    with pytest.raises(TypeError):
        split_molecules(
            structure_file=cif_label_ne_auth,
            select_chains=["A"],
            include_types=["protein"],
            use_author_chains=True,  # removed — must raise TypeError
        )


# ─────────────────────────────────────────────────────────────────────────
# PDB-format inputs: gemmi generates subchain_id like 'Axp' / 'Ax1' / 'Axw'
# so the user's natural 1-char ID ('A') always lands on the author-fallback
# path. That is the expected behavior for PDB and must NOT produce a
# fallback warning — the warning is only meaningful for mmCIF where the
# user could have passed the label directly.
# ─────────────────────────────────────────────────────────────────────────
_MINIMAL_PDB = """\
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.500   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.000   1.500   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.500   2.500   0.000  1.00  0.00           O
ATOM      5  CB  ALA A   1       2.000  -1.000   1.000  1.00  0.00           C
ATOM      6  N   GLY A   2       3.000   1.800   0.000  1.00  0.00           N
ATOM      7  CA  GLY A   2       4.000   2.800   0.000  1.00  0.00           C
ATOM      8  C   GLY A   2       5.000   2.500   1.000  1.00  0.00           C
ATOM      9  O   GLY A   2       5.500   1.400   1.000  1.00  0.00           O
ATOM     10  N   ALA B   1      10.000   0.000   0.000  1.00  0.00           N
ATOM     11  CA  ALA B   1      11.500   0.000   0.000  1.00  0.00           C
ATOM     12  C   ALA B   1      12.000   1.500   0.000  1.00  0.00           C
ATOM     13  O   ALA B   1      11.500   2.500   0.000  1.00  0.00           O
ATOM     14  CB  ALA B   1      12.000  -1.000   1.000  1.00  0.00           C
END
"""


@pytest.fixture
def pdb_simple(tmp_path: Path) -> str:
    p = tmp_path / "simple.pdb"
    p.write_text(_MINIMAL_PDB, encoding="utf-8")
    return str(p)


# ─────────────────────────────────────────────────────────────────────────
# include_types author fallback: when the primary label match lands only on
# chains outside include_types, retry via author_chain (including the
# first-character-of-author shortcut for SabDab-style 1-char IDs).
# ─────────────────────────────────────────────────────────────────────────
# 7OAO-pattern minimal CIF: two proteins with authors 'EEE'/'FFF', plus a
# waiter/ligand chain at label F. User passes 'F' expecting the nanobody
# (auth 'FFF', actually at label B); primary label-F match lands on the
# ligand. Rescue must fall through to the protein at label B.
_CIF_TYPE_FALLBACK = """\
data_TEST
#
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
ATOM  1 N N   . ALA A 1 1 ? 0.0 0.0 0.0 1.0 0.0 ? 1 ALA EEE N   1
ATOM  2 C CA  . ALA A 1 1 ? 1.5 0.0 0.0 1.0 0.0 ? 1 ALA EEE CA  1
ATOM  3 C C   . ALA A 1 1 ? 2.0 1.5 0.0 1.0 0.0 ? 1 ALA EEE C   1
ATOM  4 O O   . ALA A 1 1 ? 1.5 2.5 0.0 1.0 0.0 ? 1 ALA EEE O   1
ATOM  5 N N   . GLY B 2 1 ? 10.0 0.0 0.0 1.0 0.0 ? 1 GLY FFF N   1
ATOM  6 C CA  . GLY B 2 1 ? 11.5 0.0 0.0 1.0 0.0 ? 1 GLY FFF CA  1
ATOM  7 C C   . GLY B 2 1 ? 12.0 1.5 0.0 1.0 0.0 ? 1 GLY FFF C   1
ATOM  8 O O   . GLY B 2 1 ? 11.5 2.5 0.0 1.0 0.0 ? 1 GLY FFF O   1
HETATM 9 O O   . HOH F 3 1 ? 20.0 0.0 0.0 1.0 0.0 ? 1 HOH FFF O   1
#
"""


@pytest.fixture
def cif_type_fallback(tmp_path: Path) -> str:
    p = tmp_path / "type_fallback.cif"
    p.write_text(_CIF_TYPE_FALLBACK, encoding="utf-8")
    return str(p)


def test_split_molecules_include_types_author_fallback(cif_type_fallback, tmp_path):
    """User passes 'F'; label F exists but is water; include_types=['protein']
    must rescue via author_chain → auth 'FFF' → label B (the protein)."""
    from mdclaw.structure_server import split_molecules

    out_dir = tmp_path / "out_type_fallback"
    r = split_molecules(
        structure_file=cif_type_fallback,
        output_dir=str(out_dir),
        select_chains=["F"],
        include_types=["protein"],
    )
    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    content = Path(r["protein_files"][0]).read_text()
    assert "GLY" in content   # the auth=FFF protein contained GLY
    assert "HOH" not in content  # water must not have been extracted
    # Rescue path must emit a warning explaining the jump.
    assert any("rescued via author-chain fallback" in w for w in r["warnings"]), r["warnings"]


def test_split_molecules_pdb_input_no_fallback_warning(pdb_simple, tmp_path):
    """PDB input: user passes author chain 'A' (1-char, which is how PDB
    spec identifies chains). gemmi's subchain_id is an artifact like 'Axp'
    that the user wouldn't type. Author-fallback resolves and must stay
    silent — telling the caller to pass 'Axp' would be misleading."""
    from mdclaw.structure_server import split_molecules

    out_dir = tmp_path / "out_pdb"
    r = split_molecules(
        structure_file=pdb_simple,
        output_dir=str(out_dir),
        select_chains=["A"],
        include_types=["protein"],
    )
    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    # No fallback warning for PDB inputs.
    assert not any("author_chain fallback" in w for w in r["warnings"]), r["warnings"]


_CIF_PROTEIN_A_LIGAND_C_AUTH_A = """\
data_TEST
#
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
ATOM   1 N N   . ALA A 1 1   ? 0.0 0.0 0.0 1.0 0.0 ? 1   ALA A N   1
ATOM   2 C CA  . ALA A 1 1   ? 1.5 0.0 0.0 1.0 0.0 ? 1   ALA A CA  1
ATOM   3 C C   . ALA A 1 1   ? 2.0 1.5 0.0 1.0 0.0 ? 1   ALA A C   1
ATOM   4 O O   . ALA A 1 1   ? 1.5 2.5 0.0 1.0 0.0 ? 1   ALA A O   1
HETATM 5 P P1  . AP5 C 2 215 ? 5.0 0.0 0.0 1.0 0.0 ? 215 AP5 A P1  1
HETATM 6 O O1  . AP5 C 2 215 ? 6.5 0.0 0.0 1.0 0.0 ? 215 AP5 A O1  1
#
"""


@pytest.fixture
def cif_protein_a_ligand_c_auth_a(tmp_path: Path) -> str:
    p = tmp_path / "protein_a_ligand_c_auth_a.cif"
    p.write_text(_CIF_PROTEIN_A_LIGAND_C_AUTH_A, encoding="utf-8")
    return str(p)


_CIF_PROTEIN_A_AP5_ACT_AUTH_A = """\
data_TEST
#
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
ATOM   1 N N   . ALA A 1 1   ? 0.0 0.0 0.0 1.0 0.0 ? 1   ALA A N   1
ATOM   2 C CA  . ALA A 1 1   ? 1.5 0.0 0.0 1.0 0.0 ? 1   ALA A CA  1
ATOM   3 C C   . ALA A 1 1   ? 2.0 1.5 0.0 1.0 0.0 ? 1   ALA A C   1
ATOM   4 O O   . ALA A 1 1   ? 1.5 2.5 0.0 1.0 0.0 ? 1   ALA A O   1
ATOM   5 N N   . GLY B 4 1   ? 0.0 5.0 0.0 1.0 0.0 ? 1   GLY B N   1
ATOM   6 C CA  . GLY B 4 1   ? 1.5 5.0 0.0 1.0 0.0 ? 1   GLY B CA  1
ATOM   7 C C   . GLY B 4 1   ? 2.0 6.5 0.0 1.0 0.0 ? 1   GLY B C   1
ATOM   8 O O   . GLY B 4 1   ? 1.5 7.5 0.0 1.0 0.0 ? 1   GLY B O   1
HETATM 9 P P1  . AP5 C 2 215 ? 5.0 0.0 0.0 1.0 0.0 ? 215 AP5 A P1  1
HETATM 10 O O1 . AP5 C 2 215 ? 6.5 0.0 0.0 1.0 0.0 ? 215 AP5 A O1  1
HETATM 11 C C1 . ACT D 3 216 ? 8.0 0.0 0.0 1.0 0.0 ? 216 ACT A C1  1
HETATM 12 O O1 . ACT D 3 216 ? 9.5 0.0 0.0 1.0 0.0 ? 216 ACT A O1  1
#
"""


@pytest.fixture
def cif_protein_a_ap5_act_auth_a(tmp_path: Path) -> str:
    p = tmp_path / "protein_a_ap5_act_auth_a.cif"
    p.write_text(_CIF_PROTEIN_A_AP5_ACT_AUTH_A, encoding="utf-8")
    return str(p)


def test_split_molecules_auto_includes_requested_ligand_chain(
    cif_protein_a_ligand_c_auth_a,
    tmp_path,
):
    """Explicit ligand IDs should not be silently dropped by protein-only chain selection."""
    from mdclaw.structure_server import split_molecules

    out_dir = tmp_path / "out_ligand_auto_add"
    r = split_molecules(
        structure_file=cif_protein_a_ligand_c_auth_a,
        output_dir=str(out_dir),
        select_chains=["A"],
        include_types=["protein", "ligand"],
        include_ligand_ids=["A:AP5:215"],
    )
    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    assert len(r["ligand_files"]) == 1
    assert r["selection_adjustments"][0]["code"] == "ligand_chain_auto_included"
    assert r["selection_adjustments"][0]["added_chain_ids"] == ["C"]


def test_inspect_molecules_reports_associated_ligand_candidates(
    cif_protein_a_ligand_c_auth_a,
):
    """Inspection should surface same-author ligand candidates for agents."""
    from mdclaw.structure_server import _inspect_molecules_impl

    r = _inspect_molecules_impl(cif_protein_a_ligand_c_auth_a)

    assert r["success"], r.get("errors")
    candidates = r["summary"]["associated_ligand_candidates"]
    assert candidates == [
        {
            "author_chain": "A",
            "ligand_chain_id": "C",
            "unique_id": "A:AP5:215",
            "residue_names": ["AP5"],
            "resname": "AP5",
            "resnum": 215,
            "num_atoms": 2,
            "num_residues": 1,
            "associated_chain_ids": ["A"],
            "associated_chain_types": ["protein"],
            "recommended_select_chains_add": ["C"],
            "recommended_include_ligand_ids": ["A:AP5:215"],
        }
    ]
    assert r["summary"]["associated_ligands_by_author_chain"]["A"][0][
        "unique_id"
    ] == "A:AP5:215"


def test_research_inspect_molecules_reports_associated_ligand_candidates(
    cif_protein_a_ligand_c_auth_a,
):
    """The public inspect_molecules CLI path should expose the same hints."""
    from mdclaw.research_server import inspect_molecules

    r = inspect_molecules(cif_protein_a_ligand_c_auth_a)

    assert r["success"], r.get("errors")
    assert r["associated_ligand_candidates"][0]["unique_id"] == "A:AP5:215"
    assert r["associated_ligand_candidates"][0]["ligand_chain_id"] == "C"


def test_split_molecules_blocks_associated_ligand_silent_drop(
    cif_protein_a_ligand_c_auth_a,
    tmp_path,
):
    """Protein-only chain selection cannot silently drop associated ligands."""
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ligand_c_auth_a,
        output_dir=str(tmp_path / "out_ligand_block"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
    )

    assert r["success"] is False
    assert r["code"] == "associated_ligands_require_selection"
    assert r["ligand_selection"]["recommended_include_ligand_ids"] == [
        "A:AP5:215"
    ]
    assert r["ligand_selection"]["recommended_select_chain_additions"] == ["C"]
    assert r["ligand_selection"]["associated_ligand_candidates"][0][
        "unique_id"
    ] == "A:AP5:215"


def test_prepare_complex_surfaces_associated_ligand_selection_block(
    cif_protein_a_ligand_c_auth_a,
    tmp_path,
):
    """prepare_complex should expose the split guardrail without tool-log parsing."""
    from mdclaw.structure_server import prepare_complex

    r = prepare_complex(
        structure_file=cif_protein_a_ligand_c_auth_a,
        output_dir=str(tmp_path / "prep_ligand_block"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
    )

    assert r["success"] is False
    assert r["code"] == "associated_ligands_require_selection"
    assert r["overall_status"] == "failed"
    assert r["ligand_selection"]["recommended_include_ligand_ids"] == [
        "A:AP5:215"
    ]


def test_split_molecules_can_auto_include_associated_ligands(
    cif_protein_a_ligand_c_auth_a,
    tmp_path,
):
    """The explicit convenience flag includes associated ligand candidates."""
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ligand_c_auth_a,
        output_dir=str(tmp_path / "out_ligand_assoc"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
        include_associated_ligands=True,
    )

    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    assert len(r["ligand_files"]) == 1
    assert r["selection_adjustments"][0]["code"] == (
        "associated_ligand_chain_auto_included"
    )
    assert r["selection_adjustments"][0]["added_chain_ids"] == ["C"]
    assert r["ligand_selection"]["selected_ligand_ids"] == ["A:AP5:215"]


def test_split_molecules_includes_associated_ligand_by_resname(
    cif_protein_a_ap5_act_auth_a,
    tmp_path,
):
    """Residue-name selection should add the ligand label chain, not all ligands."""
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ap5_act_auth_a,
        output_dir=str(tmp_path / "out_ligand_resname"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
        include_ligand_resnames=["AP5"],
    )

    assert r["success"], r.get("errors")
    assert len(r["protein_files"]) == 1
    assert len(r["ligand_files"]) == 1
    assert r["selection_adjustments"][0]["code"] == "ligand_resname_chain_auto_included"
    assert r["selection_adjustments"][0]["added_chain_ids"] == ["C"]
    assert r["ligand_selection"]["mode"] == "include_ligand_resnames"
    assert r["ligand_selection"]["scope"] == "selected_associated_ligands"
    assert r["ligand_selection"]["selected_ligand_ids"] == ["A:AP5:215"]
    assert r["ligand_selection"]["excluded_same_author_ligand_ids"] == ["A:ACT:216"]

    ligand_content = Path(r["ligand_files"][0]).read_text()
    assert "AP5" in ligand_content
    assert "ACT" not in ligand_content


def test_split_molecules_resname_scope_rejects_other_chain_match(
    cif_protein_a_ap5_act_auth_a,
    tmp_path,
):
    """A resname match outside the selected polymer scope should not be stolen."""
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ap5_act_auth_a,
        output_dir=str(tmp_path / "out_ligand_resname_wrong_scope"),
        select_chains=["B"],
        include_types=["protein", "ligand"],
        include_ligand_resnames=["AP5"],
    )

    assert r["success"] is False
    assert r["code"] == "requested_ligand_resnames_not_in_selected_scope"
    assert r["ligand_selection"]["selected_ligand_ids"] == []


def test_split_molecules_resname_selector_without_chain_selects_all_matches(
    cif_protein_a_ap5_act_auth_a,
    tmp_path,
):
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ap5_act_auth_a,
        output_dir=str(tmp_path / "out_ligand_resname_global"),
        include_types=["protein", "ligand"],
        include_ligand_resnames=["AP5"],
    )

    assert r["success"], r.get("errors")
    assert len(r["ligand_files"]) == 1
    assert r["ligand_selection"]["scope"] == "all_matching_ligands"
    assert r["ligand_selection"]["selected_ligand_ids"] == ["A:AP5:215"]


def test_prepare_complex_surfaces_resname_ligand_selection(
    cif_protein_a_ap5_act_auth_a,
    tmp_path,
):
    """prepare_complex should preserve the split ligand-selection metadata."""
    from mdclaw.structure_server import prepare_complex

    r = prepare_complex(
        structure_file=cif_protein_a_ap5_act_auth_a,
        output_dir=str(tmp_path / "prep_ligand_resname"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
        process_proteins=False,
        process_ligands=False,
        include_ligand_resnames=["AP5"],
    )

    assert r["success"], r.get("errors")
    assert r["ligand_selection"]["selected_ligand_ids"] == ["A:AP5:215"]
    assert r["split"]["ligand_selection"]["selected_ligand_ids"] == ["A:AP5:215"]
    assert len(r["split"]["ligand_files"]) == 1


def test_split_molecules_rejects_ligand_id_resname_mismatch(
    cif_protein_a_ap5_act_auth_a,
    tmp_path,
):
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ap5_act_auth_a,
        output_dir=str(tmp_path / "out_ligand_id_resname_mismatch"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
        include_ligand_ids=["A:ACT:216"],
        include_ligand_resnames=["AP5"],
    )

    assert r["success"] is False
    assert r["code"] == "ligand_id_resname_mismatch"
    assert r["ligand_selection"]["mismatched_ligand_ids"] == ["A:ACT:216"]


def test_split_molecules_rejects_bare_ligand_residue_name(
    cif_protein_a_ligand_c_auth_a,
    tmp_path,
):
    """A residue name is not a stable ligand instance selector."""
    from mdclaw.structure_server import split_molecules

    r = split_molecules(
        structure_file=cif_protein_a_ligand_c_auth_a,
        output_dir=str(tmp_path / "out_bad_ligand_id"),
        select_chains=["A"],
        include_types=["protein", "ligand"],
        include_ligand_ids=["AP5"],
    )
    assert r["success"] is False
    assert r["code"] == "requested_ligand_ids_not_found"
    assert r["ligand_selection"]["missing_ligand_ids"] == ["AP5"]
    assert r["ligand_selection"]["available_ligand_ids"] == ["A:AP5:215"]
    assert "A:AP5:215" in r["hints"][-1]
