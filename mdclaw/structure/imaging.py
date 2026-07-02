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
other molecule as a whole unit into the primary cell. The anchor molecule is
only translated, never wrapped, so its internal geometry is untouched.
"""

from __future__ import annotations

from typing import Any, List, Sequence

__all__ = ["center_solute_and_wrap_solvent"]


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
        A ``(N, 3)`` ``numpy.ndarray`` of imaged positions in nanometers. The
        input is returned unchanged (as an array) when the box is degenerate or
        no molecules are found.
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

    # Image every non-anchor molecule as a whole unit into [0, L). Molecules
    # already near the (now centered) anchor keep floor()==0 and do not move,
    # so bound ligands/ions stay put; only bulk solvent gets imaged.
    for mol in molecules:
        if mol is anchor:
            continue
        idx = np.asarray(mol, dtype=int)
        centroid = pos[idx].mean(axis=0)
        pos[idx] -= np.floor(centroid / box) * box

    return pos
