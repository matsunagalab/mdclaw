#!/usr/bin/env python
"""Compute the two TAS1R2-TAS1R3 collective variables from a trajectory.

CV1  LB2-LB2 centre-of-mass distance (nm)
CV2  TAS1R3 CRD-loop insertion depth: distance from the loop centre of mass to
     the combined centre of mass of both LB2 groups (nm). Smaller is deeper.

Both are mass-weighted over the heavy-atom index groups written by
cv_selection.py, so the biasing force and the analysis measure the same thing.

Only the ~3200 atoms the CVs need are read, in chunks: a 20 ns trajectory of a
350k-atom system is several GB, and loading one whole would cost more memory
than the machine has to spare.
"""
import argparse
import json

import mdtraj as md
import numpy as np

GROUPS = ("lb2_a", "lb2_b", "loop_b", "lb2_ab")


def com(xyz, weights):
    return np.einsum("fai,a->fi", xyz, weights) / weights.sum()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trajectory", required=True, nargs="+")
    ap.add_argument("--topology-pdb", required=True)
    ap.add_argument("--selection-json", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--time-per-frame-ps", type=float, default=10.0)
    ap.add_argument("--chunk", type=int, default=100)
    args = ap.parse_args()

    selection = json.load(open(args.selection_json))["atom_indices"]
    top = md.load_topology(args.topology_pdb)
    masses = np.array([a.element.mass for a in top.atoms])

    # Read only the atoms the CVs use, and renumber the groups into that subset.
    subset = sorted({i for name in GROUPS for i in selection[name]})
    position = {atom: k for k, atom in enumerate(subset)}
    local = {name: np.array([position[i] for i in selection[name]], dtype=int)
             for name in GROUPS}
    weights = {name: masses[np.asarray(selection[name], dtype=int)]
               for name in GROUPS}

    cv1_parts, cv2_parts = [], []
    for path in args.trajectory:
        for chunk in md.iterload(path, top=top, chunk=args.chunk,
                                 stride=args.stride, atom_indices=subset):
            xyz = chunk.xyz
            a = com(xyz[:, local["lb2_a"], :], weights["lb2_a"])
            b = com(xyz[:, local["lb2_b"], :], weights["lb2_b"])
            loop = com(xyz[:, local["loop_b"], :], weights["loop_b"])
            both = com(xyz[:, local["lb2_ab"], :], weights["lb2_ab"])
            cv1_parts.append(np.linalg.norm(a - b, axis=1))
            cv2_parts.append(np.linalg.norm(loop - both, axis=1))

    cv1 = np.concatenate(cv1_parts)
    cv2 = np.concatenate(cv2_parts)
    time_ps = np.arange(len(cv1)) * args.time_per_frame_ps * args.stride

    with open(args.out_csv, "w") as fh:
        fh.write("time_ps,cv1_lb2_lb2_nm,cv2_crd_depth_nm\n")
        for t, x, y in zip(time_ps, cv1, cv2):
            fh.write(f"{t:.3f},{x:.6f},{y:.6f}\n")
    print(json.dumps({
        "frames": int(len(cv1)),
        "atoms_read": len(subset),
        "cv1_nm": {"mean": float(cv1.mean()), "std": float(cv1.std(ddof=1)),
                   "min": float(cv1.min()), "max": float(cv1.max())},
        "cv2_nm": {"mean": float(cv2.mean()), "std": float(cv2.std(ddof=1)),
                   "min": float(cv2.min()), "max": float(cv2.max())},
    }))


if __name__ == "__main__":
    main()
