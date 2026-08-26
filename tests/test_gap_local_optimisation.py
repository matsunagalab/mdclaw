"""Filling a gap must not re-optimise the structure around it.

MODELLER optimises every atom by default, so adding a few loops rewrote the whole
model: on 9UT9, observed heavy atoms moved a median 0.28 A and 15.4 A next to a
gap. For adding missing loops to an experimental structure that is wrong, and for
comparing two deposits that leave *different* residues unresolved it makes the
model error asymmetric between them. Restricting `select_atoms` to the gaps plus a
short anchor is what fixes it.

The comparison has to ignore rigid motion. The repaired model is superposed onto
its template, and that fit is a compromise once the gaps differ -- it left every
atom apparently 0.03 A out while the internal geometry was untouched.
"""
import importlib

import pytest

gm = importlib.import_module("mdclaw.genesis.modeller")

# What the residual has to fall under once rigid motion is removed. The PDB
# writes three decimals, so 0.001 A is the floor any comparison can reach.
MAX_ANGSTROM = 0.01
RMSD_ANGSTROM = 0.003


def _grid(n, spacing=1.5):
    return {("A", i, "", "CA"): (spacing * i, 0.31 * (i % 7), 0.17 * (i % 5))
            for i in range(1, n + 1)}


def _rotate_translate(coords, degrees, shift):
    import math

    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    out = {}
    for key, (x, y, z) in coords.items():
        out[key] = (cos * x - sin * y + shift[0],
                    sin * x + cos * y + shift[1],
                    z + shift[2])
    return out


def test_a_pure_rigid_move_reads_as_no_deviation():
    """The whole point: superposition must not look like re-optimisation."""
    reference = _grid(60)
    candidate = _rotate_translate(reference, 0.0244, (0.016, 0.0, 0.0))
    got = gm.internal_geometry_deviation(reference, candidate, list(reference))
    assert got["max_angstrom"] < 1e-6
    assert got["rotation_degrees"] == pytest.approx(0.0244, abs=1e-3)


def test_pdb_rounding_passes_the_threshold():
    reference = _grid(60)
    candidate = {k: (v[0] + 0.0005, v[1], v[2]) for k, v in reference.items()}
    got = gm.internal_geometry_deviation(reference, candidate, list(reference))
    assert got["max_angstrom"] <= MAX_ANGSTROM
    assert got["rmsd_angstrom"] <= RMSD_ANGSTROM


def test_one_atom_actually_moving_is_caught():
    """A single displaced atom must fail even though the rest is identical."""
    reference = _grid(60)
    candidate = dict(reference)
    key = ("A", 30, "", "CA")
    candidate[key] = (reference[key][0] + 0.5, reference[key][1], reference[key][2])
    got = gm.internal_geometry_deviation(reference, candidate, list(reference))
    assert got["max_angstrom"] > MAX_ANGSTROM


def test_a_whole_structure_re_optimisation_is_caught():
    """What the unrestricted MODELLER run did: a small shift everywhere."""
    reference = _grid(60)
    candidate = {k: (v[0] + 0.28 * ((i % 3) - 1), v[1] + 0.28 * ((i % 5) - 2), v[2])
                 for i, (k, v) in enumerate(reference.items())}
    got = gm.internal_geometry_deviation(reference, candidate, list(reference))
    assert got["rmsd_angstrom"] > RMSD_ANGSTROM


def test_too_few_atoms_raises_rather_than_reporting_zero():
    """Two atoms can always be superposed exactly; that is not a pass."""
    reference = _grid(2)
    with pytest.raises(ValueError, match="at least 3 atoms"):
        gm.internal_geometry_deviation(reference, dict(reference), list(reference))


def test_an_empty_selection_raises():
    with pytest.raises(ValueError):
        gm.internal_geometry_deviation({}, {}, [])
