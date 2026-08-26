"""Run MODELLER for real and check it only moved the gap.

The cheap contract test catches `select_atoms()` being deleted. This one catches
it being wrong: it measures the coordinates that come back. Skipped wherever
MODELLER or its licence is absent, which is most CI.

Fixture: 20 residues, a 4-residue gap at 9-12 rebuilt as a CXXC so the disulfide
machinery is exercised in the same run. Residues 1-6 and 15-20 are outside the
gap plus its two-residue anchor and must come back untouched.
"""
import importlib
import os

import pytest

gm = importlib.import_module("mdclaw.genesis.modeller")

pytestmark = pytest.mark.integration

MAX_ANGSTROM = 0.01          # internal geometry, rigid motion removed
RMSD_ANGSTROM = 0.003
FRAME_RMSD_ANGSTROM = 1.0    # absolute, so a large rigid shift cannot pass


def _has_modeller():
    """MDClaw's own probe: a bare `import modeller` fails on the placeholder
    licence in the installed config even when a key is exported."""
    if not any(k.startswith("KEY_MODELLER") and v for k, v in os.environ.items()):
        return False
    try:
        usability = importlib.import_module(
            "mdclaw.structure.clean_protein")._modeller_repair_usability()
    except Exception:
        return False
    return bool(usability.get("usable"))


pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not _has_modeller(),
                                 reason="MODELLER or its licence is unavailable")]

# Alanine everywhere it matters: no symmetry-equivalent atom names to normalise,
# so a comparison that moves is a comparison that really moved.
SEQUENCE = ["ALA"] * 8 + ["CYS", "ALA", "ALA", "CYS"] + ["ALA"] * 8
BACKBONE = (("N", "N", 0.0, 0.0, 0.0), ("CA", "C", 1.46, 0.0, 0.0),
            ("C", "C", 2.01, 1.42, 0.0), ("O", "O", 1.25, 2.39, 0.0),
            ("CB", "C", 1.99, -0.77, -1.20))
CYS_EXTRA = (("SG", "S", 3.30, -1.30, -1.80),)


def _write(path, keep):
    """A PDB holding only the residues in ``keep`` (1-based numbers)."""
    lines, serial = [], 1
    for index, resname in enumerate(SEQUENCE, start=1):
        if index not in keep:
            continue
        atoms = BACKBONE + (CYS_EXTRA if resname == "CYS" else ())
        for name, element, dx, dy, dz in atoms:
            x, y, z = 3.4 * index + dx, dy + 0.4 * (index % 3), dz
            lines.append(
                "ATOM  " + f"{serial:>5}" + " " + f"{name:<4}" + " " + resname
                + " " + "A" + f"{index:>4}" + " " + "   "
                + f"{x:8.3f}{y:8.3f}{z:8.3f}" + "  1.00  0.00          "
                + f"{element:>2}")
            serial += 1
    path.write_text("\n".join(lines) + "\nTER\nEND\n")
    return path


def _atoms(path):
    out = {}
    for line in path.read_text().splitlines():
        if line.startswith(("ATOM  ", "HETATM")):
            element = (line[76:78].strip() or line[12:16].strip()[:1])
            if element == "H":
                continue
            out[(line[21], int(line[22:26]), line[26].strip(), line[12:16].strip())] = (
                float(line[30:38]), float(line[38:46]), float(line[46:54]))
    return out


@pytest.fixture(scope="module")
def repaired(tmp_path_factory):
    import math

    work = tmp_path_factory.mktemp("smoke")
    observed = [n for n in range(1, 21) if not 9 <= n <= 12]
    template = _write(work / "t.pdb", set(observed))

    one_letter = {"ALA": "A", "CYS": "C"}
    target = "".join(one_letter[r] for r in SEQUENCE)
    row = "".join(one_letter[SEQUENCE[n - 1]] if n in observed else "-"
                  for n in range(1, 21))
    alignment = work / "a.ali"
    alignment.write_text("\n".join([
        ">P1;target",
        "sequence:target:::::target:synthetic:-1.00:-1.00",
        target + "*",
        ">P1;t",
        "structureX:t:FIRST:A:LAST:A:template:synthetic:-1.00:-1.00",
        row + "*", ""]))

    result = gm.modeller_from_alignment(
        template_pdb=str(template), alignment_file=str(alignment),
        template_code="t", target_code="target", num_models=1,
        loop_refinement=True, loop_models=1, loop_max_length=30,
        template_frame=True, random_seed=1,
        # Positions, 0-based: residues 9 and 12 are indices 8 and 11.
        disulfide_patches=[(8, 11)],
        output_dir=str(work / "out"))
    assert result.get("success"), result.get("errors")
    model = (result.get("selected_model") or {}).get("path")
    assert model, result
    from pathlib import Path
    return _atoms(template), _atoms(Path(model)), math


def test_the_gap_was_actually_rebuilt(repaired):
    _, model, _ = repaired
    rebuilt = {key[1] for key in model if 9 <= key[1] <= 12}
    assert rebuilt == {9, 10, 11, 12}


def test_the_declared_disulfide_formed(repaired):
    _, model, math = repaired
    one = model[("A", 9, "", "SG")]
    two = model[("A", 12, "", "SG")]
    assert 1.8 <= math.dist(one, two) <= 2.3


def test_observed_residues_outside_the_selection_did_not_move(repaired):
    """The regression the whole change exists for."""
    template, model, _ = repaired
    keys = [k for k in template if k in model and (k[1] <= 6 or k[1] >= 15)]
    assert len(keys) > 20, "fixture too small to be meaningful"
    got = gm.internal_geometry_deviation(template, model, keys)
    assert got["max_angstrom"] <= MAX_ANGSTROM, got
    assert got["rmsd_angstrom"] <= RMSD_ANGSTROM, got


def test_the_frame_was_kept_too(repaired):
    """Removing rigid motion alone would pass a structure translated 50 A.

    Measured on the whole structure, not just the unselected part: superposing a
    20-residue model is a coarser fit than the 1062-residue complex this was
    built for (0.016 A there), so the residual on a fragment says more about the
    fixture than about the frame. What matters is that the model came back where
    the template is, not tens of angstroms away.
    """
    template, model, math = repaired
    keys = [k for k in template if k in model and k[3] == "CA"]
    assert keys
    squared = sum(math.dist(template[k], model[k]) ** 2 for k in keys) / len(keys)
    assert math.sqrt(squared) <= FRAME_RMSD_ANGSTROM
