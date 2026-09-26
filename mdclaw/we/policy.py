"""``we_resample``: the weighted-ensemble policy of a ``rounds`` scheme.

Run on the round's analyze node (parents: the round's completed segments),
it evaluates the progress coordinate over every segment's trajectory, bins
the walkers by the last frame, recycles those inside the target (their
weight restarts from a basis node), splits and merges per bin
(``mdclaw.we.resample``) and writes the next round (``next_round.json``, the
``rounds`` contract) plus the round's ledger (``we_round.json``) and every
frame's coordinates (``we_pcoords.csv``). Merged-away walkers get a
``we_merged`` event, since their sealed node.json cannot be edited.

Policy arguments come from the scheme's ``policy_args`` (``setup_rounds``)
and can be overridden per call::

    {"pcoord": [{"type": "rmsd", "name": "r", "selection": "backbone", "reference_pdb": "..."}],
     "bins": {"edges": [[0.1, 0.2, 0.3, 0.4, 0.6, 0.8]]},
     "walkers_per_bin": 5,
     "target": {"pcoord_ranges": [[0.8, null]]},   # recycle walkers past 0.8 nm
     "recycle": true,
     "basis_node_ids": ["eq_001"],                   # default: the scheme's start nodes
     "extend_bins": true}
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

import numpy as np

from mdclaw._common import ensure_directory, setup_logger
from mdclaw._event import write_event
from mdclaw._tool_meta import node_tool
from mdclaw.analyze.cv import (
    CVError,
    compile_cvs,
    evaluate_cvs,
    evaluate_cvs_on_frames,
    half_box_nm,
    load_topology,
    minimum_image_cvs,
    normalize_cv_specs,
)
from mdclaw.analyze.inputs import _rel_to_node_root
from mdclaw.node.graph import find_ancestor_artifact
from mdclaw.node.io import _read_artifact_from_node, _read_node_json
from mdclaw.rounds.plan import NEXT_ROUND_ARTIFACT, NEXT_ROUND_FILENAME
from mdclaw.rounds.scheme import RoundsError, _validate_start_nodes, check_basis_temperatures, read_scheme
from mdclaw.we.resample import ResampleError, in_target, normalize_edges, normalize_target, resample

logger = setup_logger(__name__)

POLICY_ARG_KEYS = ("pcoord", "bins", "walkers_per_bin", "target", "recycle",
                   "basis_node_ids", "extend_bins")
DEFAULT_WALKERS_PER_BIN = 5
WE_ROUND_FILENAME = "we_round.json"
WE_PCOORDS_FILENAME = "we_pcoords.csv"


class WEError(Exception):
    """Structured failure with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _args_invalid(message: str) -> WEError:
    return WEError(code="we_policy_args_invalid", message=message)


def resolve_policy_args(explicit: dict, defaults: dict, *, start_node_ids: Optional[list[str]]) -> dict:
    """Merge per-call overrides over the scheme's ``policy_args`` and validate."""
    unknown = sorted(set(defaults) - set(POLICY_ARG_KEYS))
    if unknown:
        raise _args_invalid(f"policy_args has unknown keys {unknown}; known: {list(POLICY_ARG_KEYS)}")
    args = {key: (explicit.get(key) if explicit.get(key) is not None else defaults.get(key))
            for key in POLICY_ARG_KEYS}
    if args["pcoord"] is None:
        raise _args_invalid("pcoord is required: a list of CV specs (distance / rmsd / dihedral / q)")
    args["pcoord"] = normalize_cv_specs(args["pcoord"])
    n_dims = len(args["pcoord"])
    bins = args["bins"]
    if not isinstance(bins, dict) or "edges" not in bins:
        raise _args_invalid('bins is required: {"edges": [[...], ...]} with one boundary list per pcoord')
    extend = True if args["extend_bins"] is None else bool(args["extend_bins"])
    args["extend_bins"] = extend
    args["edges"] = normalize_edges(bins["edges"], n_dims, extend=extend)
    if args["walkers_per_bin"] is None:
        args["walkers_per_bin"] = DEFAULT_WALKERS_PER_BIN
    if (isinstance(args["walkers_per_bin"], bool) or not isinstance(args["walkers_per_bin"], int)
            or args["walkers_per_bin"] < 1):
        raise _args_invalid("walkers_per_bin must be an integer >= 1")
    args["target_ranges"] = normalize_target(args["target"], n_dims)
    if args["recycle"] is None:
        args["recycle"] = args["target_ranges"] is not None
    args["recycle"] = bool(args["recycle"])
    if args["recycle"] and args["target_ranges"] is None:
        raise _args_invalid("recycle needs a target (target.pcoord_ranges)")
    basis = args["basis_node_ids"]
    if basis is None:
        basis = list(start_node_ids or [])
    if not isinstance(basis, list) or any(not isinstance(b, str) for b in basis):
        raise _args_invalid("basis_node_ids must be a list of node ids")
    if args["recycle"] and not basis:
        raise _args_invalid("recycling needs basis_node_ids (or a scheme with start.node_ids)")
    args["basis_node_ids"] = basis
    return args


def _collect_walkers(job_dir: str, node: dict) -> list[dict]:
    parents = node.get("parent_node_ids") or []
    if not parents:
        raise WEError(code="we_inputs_missing", message="the policy node has no parent segments")
    walkers: list[dict] = []
    for index, pid in enumerate(parents):
        pnode = _read_node_json(job_dir, pid) or {}
        if pnode.get("node_type") != "prod" or pnode.get("status") != "completed":
            raise WEError(code="we_inputs_missing",
                          message=f"parent {pid} is not a completed prod segment "
                                  f"({pnode.get('node_type')}, {pnode.get('status')})")
        trajectory = _read_artifact_from_node(job_dir, pid, "trajectory")
        if not trajectory or not Path(trajectory).is_file():
            raise WEError(code="we_inputs_missing", message=f"parent {pid} has no trajectory artifact")
        pmeta = pnode.get("metadata") or {}
        scheme_meta = pmeta.get("scheme") or {}
        weight = scheme_meta.get("weight")
        if weight is None:
            raise WEError(code="we_weights_invalid",
                          message=f"segment {pid} carries no weight (metadata.scheme.weight); the scheme "
                                  "needs initial_weights ('uniform' is the default for weighted policies)")
        walkers.append({
            "id": pid,
            "replica": scheme_meta.get("replica", index + 1),
            "weight": float(weight),
            "trajectory": trajectory,
            "temperature_kelvin": _recorded_temperature(pmeta),
        })
    return walkers


def _recorded_temperature(metadata: dict) -> Optional[float]:
    """The temperature a segment ran at (its integrator signature, else
    ``metadata.temperature_kelvin``), or None."""
    signature = metadata.get("integrator_signature")
    candidates = (signature.get("temperature_kelvin") if isinstance(signature, dict) else None,
                  metadata.get("temperature_kelvin"))
    for value in candidates:
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _half_box_guard(args: dict, compiled: list, trajectory: str) -> None:
    names = minimum_image_cvs(compiled)
    if not names:
        return
    half = half_box_nm(trajectory)
    if half is None:
        raise WEError(code="cv_box_missing",
                      message=f"pcoord {names[0]!r} is a distance between molecules but the trajectory has no box")
    _check_half_box(args, names, half)


def _check_half_box(args: dict, names: list[str], half: float) -> None:
    """Refuse bin edges / targets an intermolecular (minimum-image) distance
    cannot represent: anything beyond half the shortest box vector."""
    for dim, spec in enumerate(args["pcoord"]):
        if spec["name"] not in names:
            continue
        finite_edges = [e for e in args["edges"][dim] if np.isfinite(e)]
        top_edge = max(finite_edges) if finite_edges else None
        bounds = (args["target_ranges"] or [None] * len(args["pcoord"]))[dim]
        limits = [v for v in (top_edge, (bounds[0] if bounds else None), (bounds[1] if bounds else None))
                  if v is not None and np.isfinite(v)]
        if limits and max(limits) > half:
            raise WEError(
                code="we_target_exceeds_half_box",
                message=(f"pcoord {spec['name']!r} is a minimum-image distance defined up to half the box "
                         f"({half:.2f} nm) but the bins / target reach {max(limits):.2f} nm"),
            )


def _node_structure_frame(job_dir: str, node_id: str, topology):
    """One mdtraj frame of a node's final structure on the scheme's topology:
    its ``final_structure`` PDB, else the positions and box of its ``state``
    XML (``we_start_structure_missing`` when it has neither)."""
    import mdtraj as md

    pdb = _read_artifact_from_node(job_dir, node_id, "final_structure")
    if pdb and Path(pdb).is_file():
        loaded = md.load(str(pdb))
        if loaded.n_atoms == topology.n_atoms:
            return md.Trajectory(loaded.xyz, topology, unitcell_lengths=loaded.unitcell_lengths,
                                 unitcell_angles=loaded.unitcell_angles)
    state = _read_artifact_from_node(job_dir, node_id, "state")
    if state and Path(state).is_file() and Path(state).suffix == ".xml":
        from openmm import XmlSerializer
        from openmm.unit import nanometer

        saved = XmlSerializer.deserialize(Path(state).read_text())
        xyz = np.asarray(saved.getPositions(asNumpy=True).value_in_unit(nanometer), dtype=np.float32)
        if xyz.shape[0] != topology.n_atoms:
            raise WEError(code="we_start_structure_missing",
                          message=f"start node {node_id}: its state has {xyz.shape[0]} particles, the topology "
                                  f"{topology.n_atoms} atoms")
        lengths = angles = None
        try:
            box = np.asarray(saved.getPeriodicBoxVectors(asNumpy=True).value_in_unit(nanometer), dtype=float)
            a, b, c, alpha, beta, gamma = md.utils.box_vectors_to_lengths_and_angles(box[0], box[1], box[2])
            lengths = np.asarray([[a, b, c]], dtype=np.float32)
            angles = np.asarray([[alpha, beta, gamma]], dtype=np.float32)
        except Exception:  # noqa: BLE001 - a non-periodic state has no box
            pass
        return md.Trajectory(xyz[None, :, :], topology, unitcell_lengths=lengths, unitcell_angles=angles)
    raise WEError(code="we_start_structure_missing",
                  message=f"start node {node_id} has neither a final_structure nor a state XML artifact")


def validate_scheme_policy(job_dir: str, scheme: dict) -> dict:
    """Setup-time check of a ``we_resample`` scheme (called by ``setup_rounds``).

    The policy arguments must resolve, the CVs must compile on the scheme's
    topology, every start structure must lie outside the target when
    recycling (``we_start_in_target``: a walker that starts "arrived" would
    be recycled on its first segment), and the bins / target of an
    intermolecular distance must stay below half the box. Returns the start
    structures' pcoord values, recorded on the scheme.
    """
    start_ids = list(scheme["start"]["node_ids"])
    args = resolve_policy_args({}, dict(scheme.get("policy_args") or {}), start_node_ids=start_ids)
    topology_file = find_ancestor_artifact(job_dir, start_ids[0], "topo", "topology_pdb")
    if not topology_file:
        raise WEError(code="we_inputs_missing",
                      message=f"start node {start_ids[0]} has no topo ancestor with topology_pdb")
    topology = load_topology(topology_file)
    compiled = compile_cvs(args["pcoord"], topology)
    names = [spec["name"] for spec in args["pcoord"]]
    minimum_image = minimum_image_cvs(compiled)
    start_pcoords: dict[str, list[float]] = {}
    for node_id in start_ids:
        frame = _node_structure_frame(job_dir, node_id, topology)
        values = [float(v) for v in evaluate_cvs_on_frames(frame, compiled)[0]]
        start_pcoords[node_id] = values
        if args["recycle"] and in_target(np.asarray(values), args["target_ranges"]):
            raise WEError(
                code="we_start_in_target",
                message=(f"start node {node_id} already lies inside the target: pcoord "
                         f"{dict(zip(names, values))} vs target.pcoord_ranges {args['target']['pcoord_ranges']}"),
            )
        if minimum_image and frame.unitcell_lengths is not None:
            _check_half_box(args, minimum_image, float(0.5 * np.min(frame.unitcell_lengths[0])))
    return {"pcoord_names": names, "start_pcoords": start_pcoords, "recycle": args["recycle"],
            "pcoord_minimum_image": minimum_image}


def _error_result(exc: Exception, code: str, job_dir: str, node_id: str, *, pending: bool) -> dict:
    result = {"success": False, "code": code, "message": str(exc), "errors": [str(exc)],
              "warnings": [], "job_dir": job_dir, "node_id": node_id}
    if pending:
        from mdclaw._node import fail_node_from_result

        return fail_node_from_result(job_dir, node_id, result, default_error=str(exc))
    from mdclaw._node import fail_node

    fail_node(job_dir, node_id, errors=[str(exc)], code=code)
    return result


@node_tool(node_type="analyze")
def we_resample(
    job_dir: str,
    node_id: str,
    pcoord: Optional[list[dict]] = None,
    bins: Optional[dict] = None,
    walkers_per_bin: Optional[int] = None,
    target: Optional[dict] = None,
    recycle: Optional[bool] = None,
    basis_node_ids: Optional[list[str]] = None,
    extend_bins: Optional[bool] = None,
    chunk: int = 1000,
) -> dict:
    """Weighted-ensemble resampling of one round (policy of a ``rounds`` scheme).

    Parents are the round's completed segments. The progress coordinate is
    evaluated over each segment's trajectory (``pcoord`` CV specs; the last
    frame decides the bin), walkers inside ``target`` are recycled to a basis
    node with their weight when ``recycle`` is on (their weight is the
    round's flux), and every occupied bin is brought to ``walkers_per_bin``
    walkers by merging the lightest pair (survivor drawn by weight) and
    splitting the heaviest evenly. Arguments default to the scheme's
    ``policy_args``. A basis node at another temperature than the scheme's
    segments (``segment_temperature_kelvin``) is refused
    (``rounds_start_temperature_mismatch``, the node stays pending).

    Writes ``next_round.json`` (consumed by ``run_rounds``), ``we_round.json``
    (walkers with weight, pcoord, bin and fate; bin ledger; flux) and
    ``we_pcoords.csv`` (every frame of every segment).
    """
    node = _read_node_json(job_dir, node_id) or {}
    scope = (node.get("conditions") or {}).get("analysis_data_scope")
    if scope == "comparison":
        return _error_result(WEError(code="we_scope_unsupported",
                                     message="we_resample takes the round's segments as parents, not a comparison"),
                             "we_scope_unsupported", job_dir, node_id, pending=True)
    scheme_meta = (node.get("metadata") or {}).get("scheme") or {}
    scheme_id = scheme_meta.get("scheme_id")
    round_index = scheme_meta.get("round")
    defaults: dict = {}
    start_node_ids: Optional[list[str]] = None
    seed_base = 0
    warnings: list[str] = []
    scheme: Optional[dict] = None
    try:
        if scheme_id:
            scheme = read_scheme(job_dir, scheme_id)
            defaults = dict(scheme.get("policy_args") or {})
            start_node_ids = list(scheme["start"]["node_ids"])
            seed_base = int(scheme.get("seed") or 0)
        explicit = {"pcoord": pcoord, "bins": bins, "walkers_per_bin": walkers_per_bin, "target": target,
                    "recycle": recycle, "basis_node_ids": basis_node_ids, "extend_bins": extend_bins}
        args = resolve_policy_args(explicit, defaults, start_node_ids=start_node_ids)
        if args["recycle"]:
            _validate_start_nodes(job_dir, args["basis_node_ids"])
            if scheme is not None:
                # A recycled walker restarts from its basis node: a basis at
                # another temperature than the scheme's would put part of the
                # ensemble's weight in another ensemble (the node stays pending).
                check_basis_temperatures(job_dir, scheme, args["basis_node_ids"])
        walkers = _collect_walkers(job_dir, node)
        topology_file = find_ancestor_artifact(job_dir, node_id, "topo", "topology_pdb")
        if not topology_file:
            raise WEError(code="we_inputs_missing", message="no topo ancestor with topology_pdb")
    except (WEError, CVError, ResampleError, RoundsError) as exc:
        return _error_result(exc, exc.code, job_dir, node_id, pending=True)

    from mdclaw._node import begin_node, complete_node

    out_dir = ensure_directory(Path(job_dir) / "nodes" / node_id / "artifacts")
    begin_node(job_dir, node_id)
    try:
        topology = load_topology(topology_file)
        compiled = compile_cvs(args["pcoord"], topology)
        names = [spec["name"] for spec in args["pcoord"]]
        _half_box_guard(args, compiled, walkers[0]["trajectory"])
        pcoords_csv = out_dir / WE_PCOORDS_FILENAME
        with pcoords_csv.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["replica", "node_id", "frame", *names])
            for walker in walkers:
                values = evaluate_cvs(walker["trajectory"], topology, compiled, chunk=chunk)
                walker["pcoord"] = [float(v) for v in values[-1]]
                walker["n_frames"] = int(values.shape[0])
                for frame, row in enumerate(values):
                    writer.writerow([walker["replica"], walker["id"], frame,
                                     *[f"{float(v):.6f}" for v in row]])
        seed = (seed_base * 7919 + int(round_index or 0)) % 2_147_483_647
        step = resample(
            [{"id": w["id"], "replica": w["replica"], "weight": w["weight"], "pcoord": w["pcoord"]}
             for w in walkers],
            walkers_per_bin=args["walkers_per_bin"], edges=args["edges"],
            target=args["target_ranges"], recycle=args["recycle"],
            basis_node_ids=args["basis_node_ids"], seed=seed,
        )
        next_round = {"scheme_id": scheme_id, "round": round_index, "policy": "we_resample",
                      "stop": False, "children": step["children"]}
        (out_dir / NEXT_ROUND_FILENAME).write_text(json.dumps(next_round, indent=2))
        n_frames = {w["id"]: w["n_frames"] for w in walkers}
        for record in step["walkers"]:
            record["n_frames"] = n_frames.get(record["id"])
        ledger = {
            "scheme_id": scheme_id,
            "round": round_index,
            "policy": "we_resample",
            "pcoord_names": names,
            # Distance CVs between molecules: the coordinate of a binding /
            # unbinding transition, where a rate per molar makes sense.
            "pcoord_minimum_image": minimum_image_cvs(compiled),
            "policy_args": {
                "pcoord": args["pcoord"],
                "bins": {"edges": [[None if not np.isfinite(e) else float(e) for e in arr]
                                   for arr in args["edges"]]},
                "walkers_per_bin": args["walkers_per_bin"],
                "target": args["target"],
                "recycle": args["recycle"],
                "basis_node_ids": args["basis_node_ids"],
                "extend_bins": args["extend_bins"],
            },
            "seed": seed,
            # What the round's segments ran at: analyze_we takes kT for
            # -kT ln P from here without reading every segment's node.json.
            "segment_temperatures_kelvin": sorted({w["temperature_kelvin"] for w in walkers
                                                   if w["temperature_kelvin"] is not None}),
            **{k: step[k] for k in ("walkers", "bins", "bin_shape", "n_in", "n_out", "target_weight",
                                    "flux", "weight_sum_in", "weight_sum_out", "weight_residual",
                                    "weight_min", "weight_max")},
        }
        (out_dir / WE_ROUND_FILENAME).write_text(json.dumps(ledger, indent=2))
        for record in step["walkers"]:
            if record["fate"] == "merged":
                write_event(job_dir, record["id"], "we_merged",
                            details={"into": record["merged_into"], "round": round_index,
                                     "weight": record["weight"], "policy_node_id": node_id})
            elif record["fate"] == "recycled":
                write_event(job_dir, record["id"], "we_recycled",
                            details={"round": round_index, "weight": record["weight"],
                                     "pcoord": record["pcoord"], "policy_node_id": node_id})
    except (WEError, CVError, ResampleError, RoundsError) as exc:
        return _error_result(exc, exc.code, job_dir, node_id, pending=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("we_resample failed: %s", exc)
        return _error_result(exc, "unhandled_exception", job_dir, node_id, pending=False)

    artifacts = {
        NEXT_ROUND_ARTIFACT: _rel_to_node_root(str(out_dir / NEXT_ROUND_FILENAME), out_dir),
        "we_round": _rel_to_node_root(str(out_dir / WE_ROUND_FILENAME), out_dir),
        "we_pcoords": _rel_to_node_root(str(pcoords_csv), out_dir),
    }
    metadata = {
        "analysis": "we_resample",
        "scheme_id": scheme_id,
        "round": round_index,
        "pcoord_names": names,
        "n_in": step["n_in"],
        "n_out": step["n_out"],
        "flux_weight": step["flux"]["weight_recycled"],
        "flux_events": step["flux"]["events"],
        "target_weight": step["target_weight"],
        "weight_residual": step["weight_residual"],
        "weight_min": step["weight_min"],
        "weight_max": step["weight_max"],
        "recycle": args["recycle"],
        "walkers_per_bin": args["walkers_per_bin"],
    }
    complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata, warnings=warnings or None)
    fates = {}
    for record in step["walkers"]:
        fates[record["fate"]] = fates.get(record["fate"], 0) + 1
    return {
        "success": True,
        "code": "ok",
        "job_dir": job_dir,
        "node_id": node_id,
        "scheme_id": scheme_id,
        "round": round_index,
        "n_in": step["n_in"],
        "n_out": step["n_out"],
        "fates": fates,
        "flux": step["flux"],
        "target_weight": step["target_weight"],
        "bins_occupied": len(step["bins"]),
        "weight_min": step["weight_min"],
        "weight_max": step["weight_max"],
        "weight_residual": step["weight_residual"],
        "artifacts": artifacts,
        "warnings": warnings,
    }
