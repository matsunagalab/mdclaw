"""Solvation must not be allowed to change what element an atom is.

packmol-memgen's ``MembraneParams.pdb_reindex`` right-aligns any three-character
atom name and then writes ``atomname[0]`` into the element column, so a ligand's
``CL2`` comes out of solvation as carbon, ``BR1`` as boron and ``ZN`` as an
unknown ``Z``. The corruption is deterministic, not occasional.

Measured on campaign task ``041_ligand_4erf``: the two chlorines of a C25 Cl2
ligand were written as carbon, and the topology build then failed with
"No template found for residue 92 (0R3)", which reads like a force-field
problem. Rebuilding the identical DAG with only those two element fields
corrected -- 2 lines of 71134 -- took the node from ``failed`` to ``completed``.
An audit of 102 solvated files also found ZN written as Z and CA as C; bare ions
are repaired downstream by the ion sanitiser, but a metal or halogen inside a
ligand is not, and topology validation does not check element preservation.
"""

from __future__ import annotations

from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix


def _atom(serial, name, resname, chain, resseq, element, x=0.0):
    """One PDB record, with the atom name placed as the caller writes it."""
    return (
        f"HETATM{serial:5d} {name:<4} {resname:>3} {chain:1}{resseq:4d}"
        f"    {x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {element:>2}"
    )


def _memgen_style(serial, name, resname, chain, resseq, x=0.0):
    """What packmol-memgen writes: name right-aligned, element = name[0]."""
    return (
        f"HETATM{serial:5d} {name:>4} {resname:>3} {chain:1}{resseq:4d}"
        f"    {x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {name[0]:>2}"
    )


def _write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def test_a_ligand_halogen_survives_solvation(tmp_path):
    # The real shape of 041_ligand_4erf: a ligand carrying CL1 and CL2.
    source = _write(tmp_path, "solute.pdb", [
        _atom(1, "C1", "LIG", "B", 201, "C", x=1.0),
        _atom(2, "CL1", "LIG", "B", 201, "CL", x=2.0),
        _atom(3, "CL2", "LIG", "B", 201, "CL", x=3.0),
    ])
    solvated = "\n".join([
        _memgen_style(1, "C1", "LIG", "B", 201, x=1.0),
        _memgen_style(2, "CL1", "LIG", "B", 201, x=2.0),
        _memgen_style(3, "CL2", "LIG", "B", 201, x=3.0),
        _atom(4, "O", "WAT", "W", 1, "O", x=9.0),
    ])
    assert " C" in solvated.splitlines()[1][76:78], "fixture must reproduce the bug"

    restored = restore_solute_identity_by_prefix(solvated, source,
                                                 restore_numbering=False)
    assert restored is not None
    elements = [line[76:78].strip() for line in restored.splitlines()
                if line.startswith(("ATOM", "HETATM"))]
    assert elements == ["C", "CL", "CL", "O"]


def test_the_appended_solvent_keeps_whatever_the_writer_gave_it(tmp_path):
    # The overlay is a prefix over the solute; a water the source never had
    # must not be touched, including its element.
    source = _write(tmp_path, "solute.pdb", [_atom(1, "CL1", "LIG", "B", 1, "CL")])
    solvated = "\n".join([
        _memgen_style(1, "CL1", "LIG", "B", 1),
        _atom(2, "NA", "NA", "W", 1, "NA", x=9.0),
    ])
    restored = restore_solute_identity_by_prefix(solvated, source,
                                                 restore_numbering=False)
    assert restored is not None
    lines = [line for line in restored.splitlines()
             if line.startswith(("ATOM", "HETATM"))]
    assert lines[0][76:78].strip() == "CL"     # solute repaired
    assert lines[1][76:78].strip() == "NA"     # solvent untouched


def test_an_element_the_writer_got_right_is_left_alone(tmp_path):
    # Ordinary one-letter elements are unaffected, so this cannot churn files.
    source = _write(tmp_path, "solute.pdb", [
        _atom(1, "CA", "ALA", "A", 1, "C"),
        _atom(2, "N", "ALA", "A", 1, "N"),
    ])
    solvated = "\n".join([
        _atom(1, "CA", "ALA", "A", 1, "C"),
        _atom(2, "N", "ALA", "A", 1, "N"),
    ])
    restored = restore_solute_identity_by_prefix(solvated, source,
                                                 restore_numbering=False)
    # Nothing to change: identical bytes back, and in particular the protein
    # alpha carbon is not "repaired" into calcium.
    assert restored is not None
    assert [line[76:78].strip() for line in restored.splitlines()
            if line.startswith(("ATOM", "HETATM"))] == ["C", "N"]


def test_a_blank_source_element_does_not_erase_a_good_one(tmp_path):
    source = _write(tmp_path, "solute.pdb", [_atom(1, "CL1", "LIG", "B", 1, "")])
    solvated = _atom(1, "CL1", "LIG", "B", 1, "CL")
    restored = restore_solute_identity_by_prefix(solvated, source,
                                                 restore_numbering=False)
    assert restored is not None
    assert restored.splitlines()[0][76:78].strip() == "CL"


def test_the_overlay_still_refuses_when_the_residues_are_out_of_step(tmp_path):
    # The element restore rides on the existing per-residue name check; it must
    # not weaken it into repairing atoms that do not correspond.
    source = _write(tmp_path, "solute.pdb", [
        _atom(1, "CL1", "LIG", "B", 1, "CL"),
        _atom(2, "CL2", "LIG", "B", 1, "CL"),
    ])
    solvated = "\n".join([_memgen_style(1, "CL1", "LIG", "B", 1)])
    assert restore_solute_identity_by_prefix(
        solvated, source, restore_numbering=False) is None
