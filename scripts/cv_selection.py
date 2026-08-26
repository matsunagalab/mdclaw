#!/usr/bin/env python
"""Resolve the TAS1R2-TAS1R3 CV atom groups against a built topology.

The topology PDB that build_amber_system emits cannot carry author residue
numbers: PDB residue numbers wrap at 9999 and the solvated chains reuse chain
IDs, so ``resSeq 184 to 330`` selects water. What does survive is order -- the
solute comes first, in the order prep merged it -- so the author numbering is
recovered by walking the prep merged.pdb and the topology residue list
together, checking the residue names agree at every step.

Writes a JSON file of 0-based atom indices for:
  lb2_a   TAS1R2 LB2   (auth A 184-330, 445-489)
  lb2_b   TAS1R3 LB2   (auth B 183-248, 253-330, 438-489)
  loop_b  TAS1R3 CRD loop (auth B 510-519)
  lb2_ab  lb2_a + lb2_b, the crevice reference group for the depth CV
"""
import argparse
import json

import mdtraj as md

LB2_A = ((184, 330), (445, 489))
# 249-252 is modelled in 9UTC and disordered in 9UT9; dropped from both so the
# apo and holo CV groups are the same atoms.
LB2_B = ((183, 248), (253, 330), (438, 489))
LOOP_B = ((510, 519),)


def _merged_residues(merged_pdb):
    """Ordered (chain, auth_resnum, resname) for each solute residue."""
    out, seen = [], None
    for line in open(merged_pdb):
        if not line.startswith(("ATOM", "HETATM")):
            continue
        key = (line[21], line[22:27])
        if key != seen:
            seen = key
            out.append((line[21], int(line[22:26]), line[17:20].strip()))
    return out


def _in(resnum, spans):
    return any(lo <= resnum <= hi for lo, hi in spans)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged-pdb", required=True)
    ap.add_argument("--topology-pdb", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    merged = _merged_residues(args.merged_pdb)
    top = md.load_topology(args.topology_pdb)
    residues = list(top.residues)

    groups = {"lb2_a": [], "lb2_b": [], "loop_b": []}
    mismatches = []
    for position, (chain, resnum, resname) in enumerate(merged):
        residue = residues[position]
        # Amber protonation variants are renamed on the way into topology;
        # compare only what both sides agree to spell the same way.
        if residue.name[:2] != resname[:2]:
            mismatches.append((position, chain, resnum, resname, residue.name))
        if chain == "A" and _in(resnum, LB2_A):
            groups["lb2_a"].append(position)
        elif chain == "B" and _in(resnum, LB2_B):
            groups["lb2_b"].append(position)
        if chain == "B" and _in(resnum, LOOP_B):
            groups["loop_b"].append(position)

    if mismatches:
        raise SystemExit(
            f"residue order does not line up between merged and topology; "
            f"first 5 of {len(mismatches)}: {mismatches[:5]}"
        )

    out = {"n_topology_atoms": top.n_atoms, "residue_counts": {}, "atom_indices": {}}
    for name, positions in groups.items():
        atoms = [
            a.index
            for p in positions
            for a in residues[p].atoms
            if a.element is not None and a.element.symbol != "H"
        ]
        out["residue_counts"][name] = len(positions)
        out["atom_indices"][name] = atoms
    out["atom_indices"]["lb2_ab"] = sorted(
        out["atom_indices"]["lb2_a"] + out["atom_indices"]["lb2_b"]
    )
    out["residue_counts"]["lb2_ab"] = (
        out["residue_counts"]["lb2_a"] + out["residue_counts"]["lb2_b"]
    )
    out["atom_counts"] = {k: len(v) for k, v in out["atom_indices"].items()}
    json.dump(out, open(args.out, "w"))
    print(json.dumps({k: out[k] for k in ("n_topology_atoms", "residue_counts", "atom_counts")}))


if __name__ == "__main__":
    main()
