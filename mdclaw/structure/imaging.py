"""Periodic-box imaging helpers for build-time coordinate hygiene.

OpenMM's primary periodic cell is the corner-origin box ``[0, Lx) x [0, Ly) x
[0, Lz)``. Solvation tools (packmol-memgen, ``Modeller.addSolvent``) commonly
place the solute centered near the coordinate origin, so a plain
``enforcePeriodicBox=True`` wrap splits the solute across the periodic boundary
and scatters its fragments into box corners. That is purely a *visualization*
artifact -- the physics is translation-invariant under PBC -- but it makes the
emitted ``topology.pdb`` / ``state.xml`` look broken in PyMOL/VMD.

:func:`center_solute_and_wrap_solvent` reproduces cpptraj ``autoimage``
semantics for orthorhombic boxes: rigidly translate the whole system so the
largest molecule (the solute anchor) sits at the box center, then image every
other molecule as a whole unit to the periodic image nearest that anchor. The
anchor molecule is only translated, never wrapped, so its internal geometry is
untouched, and nothing bound to it is carried a box away.
"""

from __future__ import annotations

from typing import Any, List, Sequence

__all__ = ["center_solute_and_wrap_solvent"]

# A molecule whose centroid is this close to the solute's own extent is taken
# to belong with it — a bound ligand, a coordinated ion, a lipid around a
# membrane protein — and is imaged with the solute rather than into the cell.
_CONTACT_NM = 0.5


def _connected_molecules(topology: Any) -> List[List[int]]:
    """Group atom indices into molecules via bond connectivity (union-find)."""
    n = topology.getNumAtoms()
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a1, a2 in topology.bonds():
        ra, rb = find(a1.index), find(a2.index)
        if ra != rb:
            parent[ra] = rb

    groups: dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def center_solute_and_wrap_solvent(
    topology: Any,
    positions_nm: Any,
    box_lengths_nm: Sequence[float],
) -> Any:
    """Center the solute anchor and whole-molecule-wrap everything else.

    Args:
        topology: OpenMM ``Topology`` (used only for bond connectivity).
        positions_nm: ``(N, 3)`` array-like of positions in nanometers, with
            molecules already contiguous (i.e. *not* per-atom wrapped).
        box_lengths_nm: Orthorhombic box edge lengths ``(Lx, Ly, Lz)`` in nm.

    Returns:
        A ``(N, 3)`` ``numpy.ndarray`` of imaged positions in nanometers, in the
        cell centred on the anchor rather than the corner-origin one. The input
        is returned unchanged (as an array) when the box is degenerate or no
        molecules are found.
    """
    import numpy as np

    pos = np.asarray(positions_nm, dtype=float).copy()
    box = np.asarray(box_lengths_nm, dtype=float)
    if pos.ndim != 2 or pos.shape[1] != 3 or box.shape[0] != 3:
        return pos
    if not np.all(box > 0):
        return pos

    molecules = _connected_molecules(topology)
    if not molecules:
        return pos

    anchor = max(molecules, key=len)
    anchor_idx = np.asarray(anchor, dtype=int)

    # Rigid translation so the anchor centroid lands at the box center. This is
    # translation-invariant under PBC, so energies/forces are unaffected.
    shift = (box / 2.0) - pos[anchor_idx].mean(axis=0)
    pos += shift

    # Image every non-anchor molecule as a whole unit to the periodic image
    # nearest the anchor — not into [0, L).
    #
    # Folding into the primary cell looks equivalent and is not: after the
    # anchor is centred, a bound ligand or ion whose centroid lands a hair
    # outside a face is moved one full box away from its site. Reproduced at
    # 0.2 A -> 9.8 A from the binding site. The same applies to any chain that
    # is a separate connected component (a noncovalent multimer), and to a
    # bilayer under a protein whose centroid is not the membrane midplane —
    # its two leaflets end up at opposite z faces.
    #
    # Nearest-image-to-anchor keeps everything associated with the solute
    # around it, and puts bulk solvent in the cell centred on the anchor
    # instead of the corner-origin one, which is the frame the picture wants
    # anyway. Still a whole-molecule move, so no molecule is ever split.
    #
    # "Nearest the anchor" is measured against the anchor's extent, not its
    # centroid. A long anchor puts its own ends more than half a box from its
    # centre, so a centroid test moves a ligand bound at one end a full box
    # away -- the same defect in a different disguise. The cell is
    # orthorhombic here, so the choice separates per axis and stays O(1) per
    # molecule.
    anchor_lo = pos[anchor_idx].min(axis=0)
    anchor_hi = pos[anchor_idx].max(axis=0)
    anchor_centre = pos[anchor_idx].mean(axis=0)
    others = [np.asarray(mol, dtype=int) for mol in molecules if mol is not anchor]
    if not others:
        return pos

    centroids = np.array([pos[idx].mean(axis=0) for idx in others], dtype=float)

    def gap(points):
        # Per-axis distance from each point to the anchor's extent.
        return (
            np.maximum(anchor_lo - points, 0.0)
            + np.maximum(points - anchor_hi, 0.0)
        )

    # Two candidate images per molecule. The centroid one puts it in the cell
    # centred on the anchor, which is what bulk solvent should fill. The extent
    # one puts it against the anchor's own surface, which is what anything
    # touching the anchor should follow.
    centred = -np.round((centroids - anchor_centre) / box)
    against = centred.copy()
    best_gap = gap(centroids + against * box)
    for step in (-1.0, 1.0):
        trial = centred + step
        trial_gap = gap(centroids + trial * box)
        closer = trial_gap < best_gap - 1e-9
        tied = (np.abs(trial_gap - best_gap) <= 1e-9) & (
            np.abs(trial) < np.abs(against)
        )
        take = closer | tied
        against = np.where(take, trial, against)
        best_gap = np.where(take, trial_gap, best_gap)

    # Follow the anchor only where that means staying in contact with it.
    # Applying it to bulk solvent as well would spread the solvent over the
    # anchor's extent plus a box instead of filling one box, which is a worse
    # picture than the one this function exists to produce.
    touching = np.linalg.norm(best_gap, axis=1) <= _CONTACT_NM
    shift = np.where(touching[:, None], against, centred)

    for row, idx in enumerate(others):
        moved = shift[row]
        if moved.any():
            pos[idx] += moved * box

    return pos
