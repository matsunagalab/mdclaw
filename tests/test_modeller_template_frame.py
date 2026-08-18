"""Tests for restoring a MODELLER model to its template's frame and numbering.

MODELLER emits a model in its own coordinate frame numbered from 1. When the
model is a *repair* of the template (loop refinement filling missing residues)
that silently misplaces anything carried over from the original structure, so
``_restore_template_frame`` fits the model back and renumbers it. These tests
build the PDB and PIR inputs directly, so MODELLER itself is never invoked.
"""

import numpy as np
import pytest

from mdclaw.genesis.modeller import (
    _map_model_to_template,
    _parse_pir_alignment,
    _restore_template_frame,
)

# Template: residues 10,11,12 then 16,17 — a 3-residue numbering gap at 13-15.
TEMPLATE_RESIDUES = [(10, "ALA"), (11, "GLY"), (12, "SER"), (16, "VAL"), (17, "LEU")]
# Model: the full 10..17 stretch, as MODELLER writes it — numbered 1..8.
MODEL_RESIDUES = [
    (1, "ALA"), (2, "GLY"), (3, "SER"), (4, "THR"),
    (5, "THR"), (6, "THR"), (7, "VAL"), (8, "LEU"),
]
TARGET_ALN = "AGSTTTVL"
TEMPLATE_ALN = "AGS---VL"


# One CA per template residue, on a diagonal.
TEMPLATE_XYZ = [np.array([i * 3.0, i * 1.0, 0.0]) for i in range(len(TEMPLATE_RESIDUES))]
# The model repeats the template geometry for the residues it shares with it,
# and puts the three gap-filled residues in between. A rigid fit can therefore
# put the shared residues back exactly on the template.
MODEL_XYZ = [
    TEMPLATE_XYZ[0], TEMPLATE_XYZ[1], TEMPLATE_XYZ[2],
    np.array([10.0, 3.0, 2.0]), np.array([11.0, 4.0, 3.0]), np.array([10.5, 3.5, 1.0]),
    TEMPLATE_XYZ[3], TEMPLATE_XYZ[4],
]


def _write_pdb(path, residues, coords, chain, offset):
    """One CA per residue at ``coords``, shifted by ``offset``."""
    lines = []
    for i, ((num, name), xyz) in enumerate(zip(residues, coords)):
        x, y, z = xyz + offset
        lines.append(
            f"ATOM  {i + 1:>5}  CA  {name} {chain}{num:>4}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C"
        )
    path.write_text("\n".join(lines) + "\nTER\nEND\n")


def _write_pir(path, target_code, template_code):
    path.write_text(
        f">P1;{target_code}\n"
        f"sequence:{target_code}:::::::0.00: 0.00\n"
        f"{TARGET_ALN}*\n"
        f">P1;{template_code}\n"
        f"structureX:{template_code}:::::::0.00: 0.00\n"
        f"{TEMPLATE_ALN}*\n"
    )


@pytest.fixture
def case(tmp_path):
    template = tmp_path / "tmpl.pdb"
    model = tmp_path / "model.pdb"
    aln = tmp_path / "aln.ali"
    _write_pdb(template, TEMPLATE_RESIDUES, TEMPLATE_XYZ, "A", np.zeros(3))
    # The model carries the same geometry, displaced far from the template frame.
    _write_pdb(model, MODEL_RESIDUES, MODEL_XYZ, " ", np.array([100.0, -50.0, 25.0]))
    _write_pir(aln, "tgt", "tpl")
    return template, model, aln


def test_parse_pir_alignment_returns_both_entries(case):
    _, _, aln = case
    parsed = _parse_pir_alignment(aln)
    assert parsed == {"tgt": TARGET_ALN, "tpl": TEMPLATE_ALN}


def test_gap_residues_have_no_template_counterpart(case):
    template, model, aln = case
    pairs, order, warnings = _map_model_to_template(
        aln, "tgt", "tpl", model, template
    )
    assert warnings == []
    assert len(order) == len(MODEL_RESIDUES)
    # The three modeled-only residues (target positions 4-6) are unpaired.
    assert len(pairs) == len(TEMPLATE_RESIDUES)
    assert pairs[(" ", 1, " ")] == ("A", 10, " ")
    assert pairs[(" ", 8, " ")] == ("A", 17, " ")
    assert (" ", 4, " ") not in pairs


def test_reports_drift_without_touching_the_file(case):
    template, model, aln = case
    before = model.read_text()
    info = _restore_template_frame(
        model, template, aln, "tgt", "tpl", apply_transform=False
    )
    assert info["applied"] is False
    assert info["paired_ca_atoms"] == len(TEMPLATE_RESIDUES)
    # Displaced by (100, -50, 25) -> |d| = sqrt(100^2 + 50^2 + 25^2).
    assert info["ca_rmsd_in_place"] == pytest.approx(114.564, abs=1e-2)
    assert info["ca_rmsd_after_fit"] == pytest.approx(0.0, abs=1e-3)
    assert model.read_text() == before


def test_applying_the_fit_restores_frame_and_numbering(case):
    template, model, aln = case
    info = _restore_template_frame(
        model, template, aln, "tgt", "tpl", apply_transform=True
    )
    assert info["applied"] is True
    assert info["residues_renumbered"] == len(MODEL_RESIDUES)

    coords, numbers, chains = {}, [], set()
    for line in model.read_text().splitlines():
        if line.startswith("ATOM"):
            numbers.append(int(line[22:26]))
            chains.add(line[21])
            coords[int(line[22:26])] = np.array(
                [float(line[30:38]), float(line[38:46]), float(line[46:54])]
            )
    # Template numbering for matched residues, and the gap filled as 13,14,15.
    assert numbers == [10, 11, 12, 13, 14, 15, 16, 17]
    assert chains == {"A"}

    # Matched residues now sit on their template positions.
    for (num, _name), expected in zip(TEMPLATE_RESIDUES, TEMPLATE_XYZ):
        assert coords[num] == pytest.approx(expected, abs=1e-3)


def test_unusable_alignment_is_reported_not_raised(tmp_path, case):
    template, model, _ = case
    bad = tmp_path / "bad.ali"
    bad.write_text(">P1;other\nsequence:other:::::::0.00: 0.00\nAGS*\n")
    info = _restore_template_frame(
        model, template, bad, "tgt", "tpl", apply_transform=True
    )
    assert info["applied"] is False
    assert any("does not contain both" in w for w in info["warnings"])
