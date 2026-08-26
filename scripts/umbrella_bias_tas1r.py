"""2D umbrella bias on the TAS1R2-TAS1R3 CRD-loop / LB2-LB2 coordinate pair.

CV1  LB2-LB2 centre-of-mass distance (nm)
CV2  distance from the TAS1R3 CRD loop centre of mass to the combined centre of
     mass of both LB2 groups (nm); smaller means deeper in the crevice.

Both are mass-weighted over the heavy-atom index groups written by
scripts/cv_selection.py, so the bias and the post-hoc analysis measure the same
coordinate. Parameters (--custom-force-parameters):

    selection_json   path to the cv_selection.py output for this system
    cv1_center_nm    umbrella centre for CV1
    cv2_center_nm    umbrella centre for CV2
    k1, k2           force constants, kJ/mol/nm^2
"""
import json

import torch

# Standard atomic weights, matching the values mdtraj uses in cv_compute.py so
# the biased coordinate and the analysed coordinate are the same number.
_MASS_BY_Z = {6: 12.011, 7: 14.007, 8: 15.999, 16: 32.06}
_GROUP_NAMES = ("lb2_a", "lb2_b", "loop_b", "lb2_ab")

_STATE = {}


def _groups(positions, ctx):
    """Index + normalised mass-weight tensors, built once per device.

    Keyed by device and dtype, not cached outright: the force is validated on
    CPU before it runs on the GPU, and tensors built during that first CPU call
    would otherwise be reused against CUDA positions -- "Expected all tensors to
    be on the same device".
    """
    key = (str(positions.device), str(positions.dtype))
    cached = _STATE.get(key)
    if cached is not None:
        return cached

    selection = json.load(open(ctx.params["selection_json"]))["atom_indices"]
    atomic_numbers = list(ctx.atomic_numbers)
    device, dtype = positions.device, positions.dtype

    groups = {}
    for name in _GROUP_NAMES:
        indices = selection[name]
        masses = [_MASS_BY_Z[atomic_numbers[i]] for i in indices]
        weights = torch.tensor(masses, dtype=dtype, device=device)
        groups[name] = (
            torch.tensor(indices, dtype=torch.long, device=device),
            (weights / weights.sum()).unsqueeze(1),
        )
    _STATE[key] = groups
    return groups


def _com(positions, group):
    indices, weights = group
    return (positions[indices] * weights).sum(0)


def energy(positions, ctx):
    groups = _groups(positions, ctx)

    cv1 = torch.linalg.vector_norm(
        _com(positions, groups["lb2_a"]) - _com(positions, groups["lb2_b"])
    )
    cv2 = torch.linalg.vector_norm(
        _com(positions, groups["loop_b"]) - _com(positions, groups["lb2_ab"])
    )

    bias = (
        0.5 * ctx.params["k1"] * (cv1 - ctx.params["cv1_center_nm"]) ** 2
        + 0.5 * ctx.params["k2"] * (cv2 - ctx.params["cv2_center_nm"]) ** 2
    )
    return bias, {"cv1_lb2_lb2_nm": cv1, "cv2_crd_depth_nm": cv2}
