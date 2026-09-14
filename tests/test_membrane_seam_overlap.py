"""The extended membrane cell never cuts through the patch's own water.

Reproduces 006_membrane_6a94 cli_sif r3 (campaign v2): the patch water is
lopsided (down to -48 A, up to +33 A about the midplane), the solute needed
only 3.4 A of extra room below, so the interval's floor (-43.9 A) landed
inside the patch water and the periodic image of the top copies overlapped it:
502 atom pairs under 0.6 A and a built state of 1.7e6 kJ/mol per atom.
"""

import math

from mdclaw.solvation.patch_membrane import (
    PATCH_WATER_TILE_MIN_SEPARATION_ANGSTROM,
    _is_heavy_patch_atom,
    _parse_pdb_atoms,
    extend_water_slabs,
)


def _lopsided_patch(path, *, spacing=3.1, side=5):
    """Water only: below the bilayer from -47 to -26 A, above it from 26 to 32 A."""
    lines, serial, resseq = [], 1, 1
    levels = [-47.0 + spacing * k for k in range(int((-26.0 + 47.0) / spacing) + 1)]
    levels += [26.0 + spacing * k for k in range(int((32.0 - 26.0) / spacing) + 1)]
    for z in levels:
        for ix in range(side):
            for iy in range(side):
                x, y = ix * spacing, iy * spacing
                for name, dx, dy, element in (("O", 0.0, 0.0, "O"), ("H1", 0.96, 0.0, "H"), ("H2", -0.24, 0.93, "H")):
                    lines.append(f"HETATM{serial:5d} {name:<4s} HOH A{resseq:4d}    "
                                 f"{x + dx:8.3f}{y + dy:8.3f}{z:8.3f}  1.00  0.00           {element}")
                    serial += 1
                resseq += 1
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _placed_from(path):
    _lines, atoms = _parse_pdb_atoms(path)
    placed = [(a, a.line, a.x, a.y, a.z) for a in atoms]
    keys = [a.residue_key for a in atoms]
    return placed, keys


def _periodic_heavy_clashes(placed, box_c, cutoff):
    heavy = [(i[2], i[3], i[4]) for i in placed if _is_heavy_patch_atom(i[0])]
    count = 0
    for a in heavy:
        for b in heavy:
            dz = abs(a[2] - b[2]) - box_c
            if abs(dz) < cutoff and math.dist((a[0], a[1], 0.0), (b[0], b[1], 0.0)) < cutoff:
                count += 1
    return count


def test_the_cell_grows_to_the_patch_water_instead_of_cutting_it(tmp_path):
    placed, keys = _placed_from(_lopsided_patch(tmp_path / "patch.pdb"))
    interval = {"membrane_center_z": 0.0, "dist_wat": 17.5, "leaflet": 23.0, "patch_box_c": 81.0,
                "low": -43.9, "high": 57.2, "box_c": 101.1,
                "extend_below": 0.0, "extend_above": 16.7, "extended": True}
    out, out_keys, report = extend_water_slabs(
        placed, keys, membrane_center_z=0.0, leaflet=23.0, patch_box_c=81.0, interval=interval)
    assert len(out) == len(out_keys)
    assert interval["low"] <= min(i[4] for i in out) and interval["high"] >= max(i[4] for i in out)
    assert interval["widened_to_material"]["low_after"] < -43.9
    assert interval["box_c"] == round(interval["high"] - interval["low"], 3)
    assert report["extended"] and report["added_molecules"] > 0
    # nothing overlaps its periodic image across the new seam
    assert _periodic_heavy_clashes(out, interval["box_c"], PATCH_WATER_TILE_MIN_SEPARATION_ANGSTROM) == 0


def test_a_cell_that_already_holds_the_material_is_not_widened(tmp_path):
    placed, keys = _placed_from(_lopsided_patch(tmp_path / "patch.pdb"))
    interval = {"membrane_center_z": 0.0, "dist_wat": 17.5, "leaflet": 23.0, "patch_box_c": 81.0,
                "low": -55.3, "high": 45.1, "box_c": 100.4,
                "extend_below": 14.8, "extend_above": 4.6, "extended": True}
    extend_water_slabs(placed, keys, membrane_center_z=0.0, leaflet=23.0, patch_box_c=81.0, interval=interval)
    assert "widened_to_material" not in interval and interval["low"] == -55.3


def test_an_unextended_patch_keeps_its_own_box(tmp_path):
    placed, keys = _placed_from(_lopsided_patch(tmp_path / "patch.pdb"))
    interval = {"membrane_center_z": 0.0, "dist_wat": 17.5, "leaflet": 23.0, "patch_box_c": 81.0,
                "low": -40.5, "high": 40.5, "box_c": 81.0, "extend_below": 0.0, "extend_above": 0.0,
                "extended": False}
    out, _keys, report = extend_water_slabs(
        placed, keys, membrane_center_z=0.0, leaflet=23.0, patch_box_c=81.0, interval=interval)
    assert interval["box_c"] == 81.0 and len(out) == len(placed) and not report["extended"]
