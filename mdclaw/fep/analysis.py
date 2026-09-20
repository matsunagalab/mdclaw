"""``analyze_fep`` (MBAR over the windows of one leg) and ``estimate_ddg``.

``analyze_fep`` is an ``analyze`` node whose parents are ``fep`` nodes of one
job. It gathers every window's reduced-potential matrix (following the
``fep -> fep`` segment chain recorded in ``fep_windows.json``), discards the
initial part of each window, subsamples to statistically independent frames,
and runs MBAR. The result is the free energy of the wild type -> mutant
transformation *in that environment* (folded protein, or the capped
tripeptide standing in for the unfolded state).

``estimate_ddg`` is the ``comparison`` analyze node over the two legs'
``analyze_fep`` nodes: it checks that both legs sampled the same mutation
under the same protocol / force field / ensemble, subtracts them,
``ddG = dG(folded) - dG(unfolded)``, and records the result on the node.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Optional

import numpy as np

from mdclaw._common import ensure_directory
from mdclaw._tool_meta import node_tool
from mdclaw.fep.protocol import PHASE_BOUNDS, ProtocolError, load_protocol, protocols_equivalent

logger = logging.getLogger(__name__)

_KB_KJ_MOL_K = 0.008314462618
_KJ_PER_KCAL = 4.184
MIN_NEIGHBOUR_OVERLAP = 0.03


class FepAnalysisError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Window collection                                                             #
# --------------------------------------------------------------------------- #

def collect_windows(window_index_files: list[str]) -> dict:
    """Merge the ``fep_windows.json`` files of several fep nodes.

    Returns ``{"protocol", "protocol_file", "temperature_kelvin",
    "pressure_bar", "ensemble", "n_protocol_windows", "windows": {index:
    [segment, ...]}, "sources"}``. The same index appearing in two parents
    (independent replicas) pools its segments. Paths are resolved relative to
    each index file (:func:`mdclaw.fep.run.load_windows_index`); protocols are
    compared by content, ensembles (temperature, pressure) must match because
    the reduced potential carries ``pV/kT``.
    """
    from mdclaw.fep.run import FepRunError, load_windows_index

    merged: dict[int, list[dict]] = {}
    reference: Optional[dict] = None
    protocol: Optional[dict] = None
    sources = []
    for path in window_index_files:
        try:
            data = load_windows_index(path)
        except FepRunError as exc:
            raise FepAnalysisError(code=exc.code, message=str(exc)) from exc
        sources.append({"file": str(Path(path).resolve()), "node_id": data.get("node_id"),
                        "lambda_indices": data.get("lambda_indices"), "complete": data.get("complete", True)})
        if not data.get("complete", True):
            logger.warning("%s is a partial index (run_fep did not finish); using its finished windows", path)
        try:
            this_protocol = load_protocol(data["fep_protocol_file"])
        except (KeyError, ProtocolError) as exc:
            raise FepAnalysisError(code="fep_windows_missing", message=f"{path}: protocol unreadable: {exc}") from exc
        if reference is None:
            reference, protocol = data, this_protocol
        else:
            if not protocols_equivalent(protocol, this_protocol):
                raise FepAnalysisError(
                    code="fep_windows_incompatible",
                    message=f"{path} was sampled with a different lambda protocol than {reference['index_file']}; "
                    "analyze one hybrid topology at a time")
            if abs(float(data.get("temperature_kelvin") or 0) - float(reference.get("temperature_kelvin") or 0)) > 1e-6:
                raise FepAnalysisError(code="fep_windows_incompatible", message=f"{path} was sampled at a different temperature")
            if (data.get("pressure_bar") or None) != (reference.get("pressure_bar") or None):
                raise FepAnalysisError(
                    code="fep_windows_incompatible",
                    message=f"{path} was sampled at pressure_bar={data.get('pressure_bar')} ({data.get('ensemble')}) but "
                    f"{reference['index_file']} at {reference.get('pressure_bar')}; the reduced potential includes pV/kT, "
                    "so NPT and NVT windows cannot be pooled")
        for key, record in data["windows"].items():
            merged.setdefault(int(key), []).extend(record.get("segments") or [])
    if not merged or reference is None:
        raise FepAnalysisError(code="fep_windows_missing", message="no windows found in the fep parents")
    # A fep -> fep child's index already chains its parent's segments. When
    # the analyze node is parented to both (against the advice in
    # skills/md-fep/windows.md) the same energies.npz would enter MBAR twice
    # and shrink the error estimate; keep the first occurrence of each file.
    warnings: list[str] = []
    n_dup = 0
    for k, segments in merged.items():
        seen: set[str] = set()
        unique = []
        for seg in segments:
            key = os.path.realpath(seg.get("energies_file") or "")
            if key in seen:
                n_dup += 1
                continue
            seen.add(key)
            unique.append(seg)
        merged[k] = unique
    if n_dup:
        warnings.append(
            f"{n_dup} segment(s) were listed by more than one parent index (a fep node and its extension child); "
            "each was counted once. Parent the analyze node to the leaf of each fep chain only.")
    temperature = reference.get("temperature_kelvin")
    return {
        "warnings": warnings,
        "protocol": protocol,
        "protocol_file": reference.get("fep_protocol_file"),
        "temperature_kelvin": float(temperature) if temperature is not None else None,
        "pressure_bar": reference.get("pressure_bar"),
        "ensemble": reference.get("ensemble"),
        "n_protocol_windows": len(protocol["windows"]),
        "windows": merged,
        "sources": sources,
    }


def _load_segment(seg: dict, n_states: int, discard_fraction: float) -> np.ndarray:
    """One segment's ``u_kn`` with the first ``discard_fraction`` dropped
    (each restart re-equilibrates a little)."""
    path = seg.get("energies_file")
    if not path or not Path(path).is_file():
        raise FepAnalysisError(code="fep_windows_missing", message=f"energies file missing for a segment: {path}")
    with np.load(path) as data:
        u = np.asarray(data["u_kn"], dtype=float)
    if u.shape[0] != n_states:
        raise FepAnalysisError(
            code="fep_windows_incompatible", message=f"{path} has {u.shape[0]} states, protocol has {n_states}")
    return u[:, int(math.floor(u.shape[1] * discard_fraction)):]


def _subsample(u_k: np.ndarray, own_index: int, timeseries) -> tuple[np.ndarray, dict]:
    """Statistically independent subset of one *contiguous* time series,
    using the window's own reduced potential."""
    series = u_k[own_index]
    if series.size < 10:
        return u_k, {"g": 1.0, "n_used": int(series.size)}
    try:
        t0, g, _neff = timeseries.detect_equilibration(series)
        indices = timeseries.subsample_correlated_data(series[t0:], g=g)
        indices = [int(t0) + int(i) for i in indices]
    except Exception:  # noqa: BLE001 - fall back to the full set
        return u_k, {"g": 1.0, "n_used": int(series.size), "subsampling": "failed"}
    if len(indices) < 5:
        return u_k, {"g": float(g), "n_used": int(series.size), "subsampling": "too_few_kept"}
    return u_k[:, indices], {"g": float(g), "t0": int(t0), "n_used": len(indices)}


def _window_samples(segments: list[dict], own_index: int, n_states: int, discard_fraction: float,
                    subsample: bool, timeseries) -> tuple[np.ndarray, dict]:
    """Load, discard, subsample each segment *separately*, then pool.

    Segments are separate trajectories (an extension continues from the
    parent's last frame, replicas are unrelated), so equilibration detection
    and the statistical inefficiency are computed per segment; concatenating
    first would let ``detect_equilibration`` discard a whole replica.
    """
    blocks, per_segment = [], []
    n_raw = 0
    for seg in segments:
        u = _load_segment(seg, n_states, discard_fraction)
        n_after_discard = int(u.shape[1])
        n_raw += n_after_discard
        if n_after_discard == 0:
            continue
        if subsample:
            u, info = _subsample(u, own_index, timeseries)
        else:
            info = {"g": 1.0, "n_used": n_after_discard}
        per_segment.append({"node_id": seg.get("node_id"), "n_after_discard": n_after_discard, **info})
        blocks.append(u)
    if not blocks:
        raise FepAnalysisError(code="fep_windows_missing", message="a window has no samples left after discarding")
    u_k = np.concatenate(blocks, axis=1)
    g_max = max(float(x.get("g", 1.0)) for x in per_segment)
    return u_k, {"n_raw": n_raw, "n_segments": len(blocks), "n_used": int(u_k.shape[1]), "g": g_max,
                 "segments": per_segment}


# --------------------------------------------------------------------------- #
# MBAR                                                                          #
# --------------------------------------------------------------------------- #

def run_mbar(
    windows: dict[int, list[dict]],
    protocol: dict,
    temperature_kelvin: float,
    *,
    discard_fraction: float = 0.1,
    subsample: bool = True,
) -> dict:
    try:
        import pymbar
        from pymbar import timeseries
    except ImportError as exc:  # pragma: no cover - environment
        raise FepAnalysisError(code="pymbar_not_installed", message=f"pymbar is required for analyze_fep: {exc}") from exc

    n_states = len(protocol["windows"])
    missing = [k for k in range(n_states) if k not in windows]
    if missing:
        raise FepAnalysisError(
            code="fep_windows_incomplete", message=f"protocol has {n_states} windows but indices {missing} were not sampled; "
            f"add fep nodes for them (same eq parent) and parent this analyze node to all of them",
        )
    u_blocks = []
    n_k = np.zeros(n_states, dtype=int)
    per_window = []
    for k in range(n_states):
        u_k, info = _window_samples(windows[k], k, n_states, discard_fraction, subsample, timeseries)
        n_k[k] = u_k.shape[1]
        u_blocks.append(u_k)
        per_window.append({"index": k, "lambda": protocol["windows"][k]["lambda"], **info})
    u_kn = np.concatenate(u_blocks, axis=1)
    mbar = pymbar.MBAR(u_kn, n_k)
    fe = mbar.compute_free_energy_differences()
    delta_f, d_delta_f = np.asarray(fe["Delta_f"]), np.asarray(fe["dDelta_f"])
    overlap = np.asarray(mbar.compute_overlap()["matrix"])
    kT = _KB_KJ_MOL_K * temperature_kelvin

    neighbour = [float(overlap[k, k + 1]) for k in range(n_states - 1)]
    cumulative = [float(delta_f[0, k] * kT) for k in range(n_states)]
    cumulative_err = [float(d_delta_f[0, k] * kT) for k in range(n_states)]
    lambdas = [float(w["lambda"]) for w in protocol["windows"]]

    def _phase_dg(lo: float, hi: float) -> Optional[float]:
        idx = [k for k, lam in enumerate(lambdas) if lo - 1e-9 <= lam <= hi + 1e-9]
        if len(idx) < 2:
            return None
        return float((delta_f[idx[0], idx[-1]]) * kT)

    p1, p2 = protocol.get("phase_bounds", PHASE_BOUNDS)
    phases = {
        "decharge_old_kj_mol": _phase_dg(0.0, p1),
        "sterics_swap_kj_mol": _phase_dg(p1, p2),
        "recharge_new_kj_mol": _phase_dg(p2, 1.0),
    }
    warnings = []
    low = [(k, o) for k, o in enumerate(neighbour) if o < MIN_NEIGHBOUR_OVERLAP]
    if low:
        warnings.append(
            "Low phase-space overlap between neighbouring windows "
            + ", ".join(f"{k}-{k + 1} ({o:.3f})" for k, o in low)
            + f" (< {MIN_NEIGHBOUR_OVERLAP}); add windows there (--lambda-schedule) or sample longer."
        )
    thin = [k for k in range(n_states) if n_k[k] < 20]
    if thin:
        warnings.append(f"Windows {thin} have fewer than 20 independent samples; the error estimate is unreliable.")
    dg = float(delta_f[0, -1] * kT)
    ddg = float(d_delta_f[0, -1] * kT)
    return {
        "estimator": f"MBAR (pymbar {getattr(pymbar, '__version__', '?')})",
        "temperature_kelvin": float(temperature_kelvin),
        "kT_kj_mol": kT,
        "n_states": n_states,
        "n_samples_per_state": [int(x) for x in n_k],
        "n_samples_total": int(n_k.sum()),
        "dG_kj_mol": dg,
        "dG_error_kj_mol": ddg,
        "dG_kcal_mol": dg / _KJ_PER_KCAL,
        "dG_error_kcal_mol": ddg / _KJ_PER_KCAL,
        "cumulative_dG_kj_mol": cumulative,
        "cumulative_dG_error_kj_mol": cumulative_err,
        "phases": phases,
        "neighbour_overlap": neighbour,
        "min_neighbour_overlap": min(neighbour) if neighbour else None,
        "overlap_matrix": overlap.tolist(),
        "per_window": per_window,
        "discard_fraction": discard_fraction,
        "subsampled": bool(subsample),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Tools                                                                         #
# --------------------------------------------------------------------------- #

@node_tool(node_type="analyze")
def analyze_fep(
    fep_windows_files: Optional[list[str]] = None,
    discard_fraction: float = 0.1,
    subsample: bool = True,
    output_name: str = "fep_result",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """MBAR free energy of one alchemical leg from its ``fep`` windows.

    Node mode: an ``analyze`` node created with
    ``--conditions '{"analysis_data_scope": "alchemical"}'`` whose
    parents are the ``fep`` nodes covering every window of the protocol
    (one node, or the members of a job array). Extension chains
    (``fep -> fep``) are followed automatically through each window's
    segment list. Direct mode: pass ``--fep-windows-files``.

    Args:
        fep_windows_files: ``fep_windows.json`` paths (direct mode).
        discard_fraction: Fraction of each segment dropped as equilibration
            before subsampling (default 0.1).
        subsample: Subsample each window to statistically independent frames
            with ``pymbar.timeseries`` (default on).
        output_name / output_dir / job_dir / node_id: standard knobs.

    Returns:
        ``dG_kj_mol`` / ``dG_error_kj_mol`` (and kcal/mol), per-phase
        contributions, neighbour overlaps, per-window sample counts and the
        ``fep_result`` JSON path. Failure codes: ``fep_windows_missing``,
        ``fep_windows_incomplete``, ``fep_windows_incompatible``,
        ``pymbar_not_installed``.
    """
    from mdclaw._node import fail_tool

    result: dict = {"success": False, "tool": "analyze_fep", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)
    parent_ids: list[str] = []
    hybrid_manifest_file = None
    if not isinstance(discard_fraction, (int, float)) or not 0.0 <= discard_fraction < 1.0:
        return fail_tool(result, code="invalid_parameter_value", message=f"discard_fraction must be in [0, 1), got {discard_fraction!r}",
                         job_dir=job_dir, node_id=node_id)
    try:
        if node_mode:
            from mdclaw._node import resolve_node_inputs

            inputs = resolve_node_inputs(job_dir, node_id, "analyze")
            if "input_resolution_error" in inputs:
                raise FepAnalysisError(code="fep_windows_missing", message=inputs["input_resolution_error"])
            records = inputs.get("fep_window_records") or []
            if not records:
                raise FepAnalysisError(
                    code="fep_windows_missing", message="the analyze node has no fep parents; parent it to the completed run_fep nodes",
                )
            fep_windows_files = [r["fep_windows_file"] for r in records]
            parent_ids = [r["fep_node_id"] for r in records]
            hybrid_manifest_file = inputs.get("hybrid_manifest_file")
        elif not fep_windows_files:
            raise FepAnalysisError(
                code="fep_windows_missing", message="pass --job-dir/--node-id (fep parents) or --fep-windows-files")
        collected = collect_windows(fep_windows_files)
        protocol = collected["protocol"]
        result["warnings"].extend(collected["warnings"])
    except FepAnalysisError as exc:
        # Nothing ran: the node stays pending so the parents can be fixed.
        return fail_tool(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)

    if node_mode:
        from mdclaw._node import begin_node

        out_dir = ensure_directory(Path(job_dir) / "nodes" / node_id / "artifacts")
        begin_node(job_dir, node_id)
    else:
        from mdclaw._common import create_unique_subdir

        out_dir = create_unique_subdir(Path(output_dir) if output_dir else Path("outputs").resolve(), "fep_analysis")

    try:
        mbar = run_mbar(
            collected["windows"], protocol, collected["temperature_kelvin"] or 300.0,
            discard_fraction=discard_fraction, subsample=subsample,
        )
    except FepAnalysisError as exc:
        return fail_tool(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)
    except Exception as exc:  # noqa: BLE001
        return fail_tool(result, code="fep_analysis_failed", message=f"{type(exc).__name__}: {exc}", job_dir=job_dir, node_id=node_id)

    result["warnings"].extend(mbar.pop("warnings"))
    report = {
        "schema_version": 1,
        "mutation": protocol.get("mutation"),
        "protocol_file": collected["protocol_file"],
        "hybrid_manifest_file": hybrid_manifest_file,
        "pressure_bar": collected["pressure_bar"],
        "ensemble": collected["ensemble"],
        "fep_parent_node_ids": parent_ids,
        "fep_windows_files": fep_windows_files,
        "sources": collected["sources"],
        "job_dir": str(Path(job_dir).resolve()) if job_dir else None,
        "node_id": node_id,
        **mbar,
        "warnings": list(result["warnings"]),
    }
    report_file = out_dir / f"{output_name}.json"
    report_file.write_text(json.dumps(report, indent=2))
    result.update({
        "success": True,
        "fep_result": str(report_file),
        "mutation": protocol.get("mutation", {}).get("label"),
        "dG_kj_mol": mbar["dG_kj_mol"],
        "dG_error_kj_mol": mbar["dG_error_kj_mol"],
        "dG_kcal_mol": mbar["dG_kcal_mol"],
        "dG_error_kcal_mol": mbar["dG_error_kcal_mol"],
        "phases": mbar["phases"],
        "n_states": mbar["n_states"],
        "n_samples_per_state": mbar["n_samples_per_state"],
        "min_neighbour_overlap": mbar["min_neighbour_overlap"],
        "output_dir": str(out_dir),
    })
    if node_mode:
        from mdclaw._node import complete_node

        complete_node(
            job_dir, node_id,
            artifacts={"fep_result": f"artifacts/{output_name}.json"},
            metadata={
                "analysis": "fep_mbar",
                "mutation": result["mutation"],
                "dG_kj_mol": mbar["dG_kj_mol"],
                "dG_error_kj_mol": mbar["dG_error_kj_mol"],
                "dG_kcal_mol": mbar["dG_kcal_mol"],
                "dG_error_kcal_mol": mbar["dG_error_kcal_mol"],
                "n_states": mbar["n_states"],
                "n_samples_total": mbar["n_samples_total"],
                "min_neighbour_overlap": mbar["min_neighbour_overlap"],
                "discard_fraction": discard_fraction,
                "subsampled": bool(subsample),
                "fep_parent_node_ids": parent_ids,
            },
            warnings=result["warnings"] or None,
        )
    return result


# --------------------------------------------------------------------------- #
# ddG = dG(folded) - dG(unfolded): the comparison analyze node                  #
# --------------------------------------------------------------------------- #

LEG_ROLES = ("folded", "unfolded")
FEP_LEG_ANALYSIS = "fep_mbar"
DDG_ANALYSIS = "fep_ddg"


def _load_leg(path: str, label: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FepAnalysisError(code="file_not_found", message=f"{label}: {path} not found")
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise FepAnalysisError(code="fep_result_invalid", message=f"{label}: {path} is not JSON: {exc}") from exc
    if "dG_kj_mol" not in data:
        raise FepAnalysisError(code="fep_result_invalid", message=f"{label}: {path} is not an analyze_fep result")
    return data


def leg_role_of(job_dir: str, node_id: str) -> Optional[str]:
    """``leg_role`` recorded on the nearest prep ancestor of *node_id* (the
    unfolded model's prep node writes ``"unfolded"``; a plain ``prepare_complex``
    prep writes nothing, which means the folded protein)."""
    from mdclaw._node import get_ancestors, read_node

    for anc in get_ancestors(job_dir, node_id):
        try:
            node = read_node(job_dir, anc)
        except Exception:  # noqa: BLE001 - unreadable ancestors are not leg markers
            continue
        if node.get("node_type") == "prep":
            role = (node.get("metadata") or {}).get("leg_role")
            if role:
                return str(role)
    return None


def _assign_leg_roles(job_dir: str, node: dict, parents: list[str]) -> dict[str, str]:
    """``{"folded": node_id, "unfolded": node_id}`` from the parents' prep
    ancestry, falling back to ``analysis_subjects`` order."""
    roles = {pid: leg_role_of(job_dir, pid) for pid in parents}
    unfolded = [pid for pid, role in roles.items() if role == "unfolded"]
    folded = [pid for pid, role in roles.items() if role in (None, "folded")]
    if len(unfolded) == 1 and len(folded) == 1:
        return {"folded": folded[0], "unfolded": unfolded[0]}
    subjects = [s.get("label") for s in ((node.get("conditions") or {}).get("analysis_subjects") or [])
                if isinstance(s, dict)]
    if len(subjects) == len(parents) == 2 and set(subjects) == set(LEG_ROLES):
        return {label: pid for label, pid in zip(subjects, parents)}
    raise FepAnalysisError(
        code="fep_leg_role_ambiguous",
        message=f"cannot tell which parent is the folded and which the unfolded leg (prep leg_role markers: {roles}); "
        "derive the unfolded leg with extract_tripeptide (its prep node carries leg_role), or create the node with "
        "--conditions '{\"analysis_data_scope\": \"comparison\", \"analysis_subjects\": [{\"label\": \"folded\"}, "
        "{\"label\": \"unfolded\"}]}' listing --parent-node-ids in that order")


def _leg_settings(leg: dict) -> dict:
    """What must agree between the two legs, read from an analyze_fep result
    and the files it points at (missing pieces are reported, not assumed)."""
    out: dict = {"mutation": ((leg.get("mutation") or {}).get("label") or "").split(":")[-1] or None,
                 "temperature_kelvin": leg.get("temperature_kelvin"), "pressure_bar": leg.get("pressure_bar"),
                 "n_states": leg.get("n_states"), "protocol": None, "unverified": []}
    protocol_file = leg.get("protocol_file")
    if protocol_file and Path(protocol_file).is_file():
        try:
            out["protocol"] = load_protocol(protocol_file)
        except ProtocolError:
            out["unverified"].append("protocol")
    else:
        out["unverified"].append("protocol")
    manifest_file = leg.get("hybrid_manifest_file")
    if manifest_file and Path(manifest_file).is_file():
        try:
            manifest = json.loads(Path(manifest_file).read_text())
        except (OSError, json.JSONDecodeError):
            manifest = None
        if manifest:
            for key in ("forcefield", "water_model", "hmr", "softcore_alpha"):
                out[key] = manifest.get(key)
        else:
            out["unverified"].append("hybrid_manifest")
    else:
        out["unverified"].append("hybrid_manifest")
    return out


def check_legs_compatible(leg_f: dict, leg_u: dict) -> tuple[list[str], list[str]]:
    """``(mismatches, warnings)``: the two legs must transform the same
    mutation with the same lambda protocol, force field, water model, HMR and
    ensemble, otherwise ddG mixes two different thermodynamic cycles."""
    sf, su = _leg_settings(leg_f), _leg_settings(leg_u)
    mismatches: list[str] = []
    warnings: list[str] = []
    for key in ("mutation", "temperature_kelvin", "pressure_bar", "n_states", "forcefield", "water_model", "hmr",
                "softcore_alpha"):
        a, b = sf.get(key), su.get(key)
        if a is None or b is None:
            continue
        same = math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9) if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool) else a == b
        if not same:
            mismatches.append(f"{key}: folded={a!r}, unfolded={b!r}")
    if sf["protocol"] is not None and su["protocol"] is not None and not protocols_equivalent(sf["protocol"], su["protocol"]):
        mismatches.append("lambda protocol differs (window lambdas / global parameters)")
    unverified = sorted(set(sf["unverified"]) | set(su["unverified"]))
    if unverified:
        warnings.append(f"could not verify {', '.join(unverified)} agreement between the legs (files missing or unreadable)")
    return mismatches, warnings


def _study_dir_from_job(job_dir: str) -> Optional[str]:
    from mdclaw.node.progress import _load_progress_v3

    try:
        progress = _load_progress_v3(Path(job_dir) / "progress.json") or {}
    except Exception:  # noqa: BLE001
        return None
    value = (progress.get("params") or {}).get("study_dir")
    return str(value) if value else None


@node_tool(node_type="analyze")
def estimate_ddg(
    folded: Optional[str] = None,
    unfolded: Optional[str] = None,
    output_file: Optional[str] = None,
    output_name: str = "ddg",
    study_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """ddG of folding stability from the two legs' ``analyze_fep`` results.

    ``ddG = dG_mut(folded) - dG_mut(unfolded)`` with the sign convention of
    ``ddG_folding = dG_fold(mutant) - dG_fold(wild type)``: positive means the
    mutation destabilises the fold. Errors add in quadrature.

    Node mode: an ``analyze`` node with
    ``--conditions '{"analysis_data_scope": "comparison"}'`` whose two parents
    are the legs' ``analyze_fep`` nodes (any order). The unfolded leg is the
    one whose prep ancestor was written by ``extract_tripeptide``
    (``leg_role = "unfolded"``); the other is the folded protein. Before
    subtracting, the legs are checked for the same mutation, lambda protocol,
    force field, water model, HMR, temperature and pressure
    (``fep_legs_incompatible`` otherwise). Direct mode: pass ``--folded`` /
    ``--unfolded`` result files.

    Args:
        folded / unfolded: ``fep_result.json`` of each leg (direct mode).
        output_file: Where to write the report in direct mode (default:
            ``<study_dir>/evidence/ddg_<mutation>.json`` when ``study_dir`` is
            given, else ``outputs/ddg_<mutation>.json``; node mode always
            writes ``artifacts/<output_name>.json``).
        output_name: Artifact stem in node mode (default ``ddg``).
        study_dir: Study whose log receives a decision entry (node mode reads
            it from the job's progress params when omitted).
        job_dir / node_id: node mode.

    Returns:
        ``ddG_kj_mol`` / ``ddG_error_kj_mol`` (and kcal/mol), both legs'
        dG, the leg node ids, warnings carried from the legs, ``ddg_file``.
        Codes: ``fep_ddg_scope_invalid``, ``fep_ddg_parents_invalid``,
        ``fep_leg_role_ambiguous``, ``fep_legs_incompatible``,
        ``fep_result_invalid``, ``file_not_found``.
    """
    from mdclaw._node import fail_tool

    result: dict = {"success": False, "tool": "estimate_ddg", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)
    leg_nodes: dict[str, Optional[str]] = {"folded": None, "unfolded": None}

    def _fail(code: str, message: str) -> dict:
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id)

    try:
        if node_mode:
            from mdclaw._node import read_node, resolve_node_inputs

            inputs = resolve_node_inputs(job_dir, node_id, "analyze")
            if "input_resolution_error" in inputs:
                raise FepAnalysisError(code="fep_ddg_parents_invalid", message=inputs["input_resolution_error"])
            node = read_node(job_dir, node_id)
            scope = (node.get("conditions") or {}).get("analysis_data_scope")
            if scope != "comparison":
                raise FepAnalysisError(
                    code="fep_ddg_scope_invalid",
                    message=f"estimate_ddg consumes two legs; create the analyze node with --conditions "
                    f"'{{\"analysis_data_scope\": \"comparison\"}}' (got {scope!r})")
            parents = list(node.get("parent_node_ids") or [])
            legs: dict[str, str] = {}
            for pid in parents:
                pnode = read_node(job_dir, pid)
                analysis = (pnode.get("metadata") or {}).get("analysis")
                artifact = (pnode.get("artifacts") or {}).get("fep_result")
                if pnode.get("node_type") != "analyze" or analysis != FEP_LEG_ANALYSIS or not artifact:
                    raise FepAnalysisError(
                        code="fep_ddg_parents_invalid",
                        message=f"parent {pid} is not a completed analyze_fep node (node_type={pnode.get('node_type')!r}, "
                        f"analysis={analysis!r}); parent this node to the two legs' analyze_fep nodes")
                legs[pid] = str((Path(job_dir) / "nodes" / pid / artifact).resolve())
            if len(parents) != 2:
                raise FepAnalysisError(
                    code="fep_ddg_parents_invalid",
                    message=f"estimate_ddg takes exactly two analyze_fep parents (folded and unfolded), got {len(parents)}")
            roles = _assign_leg_roles(job_dir, node, parents)
            leg_nodes = dict(roles)
            folded, unfolded = legs[roles["folded"]], legs[roles["unfolded"]]
            if study_dir is None:
                study_dir = _study_dir_from_job(job_dir)
        elif not (folded and unfolded):
            raise FepAnalysisError(code="fep_result_invalid",
                                   message="pass --folded and --unfolded fep_result.json files, or --job-dir/--node-id on a "
                                   "comparison analyze node over the two analyze_fep nodes")
        leg_f = _load_leg(folded, "folded")
        leg_u = _load_leg(unfolded, "unfolded")
        mismatches, compat_warnings = check_legs_compatible(leg_f, leg_u)
        if mismatches:
            raise FepAnalysisError(
                code="fep_legs_incompatible",
                message="the folded and unfolded legs were not sampled with the same settings: " + "; ".join(mismatches)
                + ". Rebuild the unfolded leg's hybrid topology with the folded leg's --mutation / --forcefield / "
                "--water-model / --n-windows and the same run_fep ensemble.")
    except FepAnalysisError as exc:
        # Nothing ran: the node stays pending so the parents / arguments can be fixed.
        return _fail(exc.code, str(exc))
    result["warnings"].extend(compat_warnings)

    if node_mode:
        from mdclaw._node import begin_node

        out_dir = ensure_directory(Path(job_dir) / "nodes" / node_id / "artifacts")
        begin_node(job_dir, node_id)

    mut_f = (leg_f.get("mutation") or {}).get("label")
    mut_u = (leg_u.get("mutation") or {}).get("label")
    if mut_f and mut_u and mut_f != mut_u:
        result["warnings"].append(f"the two legs carry different mutation labels ({mut_f} vs {mut_u}); same residue, different chain id")
    ddg = leg_f["dG_kj_mol"] - leg_u["dG_kj_mol"]
    err = math.sqrt(leg_f.get("dG_error_kj_mol", 0.0) ** 2 + leg_u.get("dG_error_kj_mol", 0.0) ** 2)
    for label, leg in (("folded", leg_f), ("unfolded", leg_u)):
        for w in leg.get("warnings") or []:
            result["warnings"].append(f"[{label}] {w}")

    def _leg_block(path: str, leg: dict, node: Optional[str]) -> dict:
        return {"file": str(Path(path).resolve()), "node_id": node, "dG_kj_mol": leg["dG_kj_mol"],
                "dG_error_kj_mol": leg.get("dG_error_kj_mol"), "n_samples_total": leg.get("n_samples_total"),
                "min_neighbour_overlap": leg.get("min_neighbour_overlap"), "n_states": leg.get("n_states")}

    report = {
        "schema_version": 2,
        "analysis": DDG_ANALYSIS,
        "mutation": mut_f or mut_u,
        "ddG_kj_mol": ddg,
        "ddG_error_kj_mol": err,
        "ddG_kcal_mol": ddg / _KJ_PER_KCAL,
        "ddG_error_kcal_mol": err / _KJ_PER_KCAL,
        "sign_convention": "ddG > 0: mutation destabilises the folded state",
        "legs": {"folded": _leg_block(folded, leg_f, leg_nodes["folded"]),
                 "unfolded": _leg_block(unfolded, leg_u, leg_nodes["unfolded"])},
        "job_dir": str(Path(job_dir).resolve()) if job_dir else None,
        "node_id": node_id,
        "warnings": list(result["warnings"]),
    }
    tag = (report["mutation"] or "ddg").replace(":", "_")
    if node_mode:
        out = out_dir / f"{output_name}.json"
    elif output_file:
        out = Path(output_file)
    elif study_dir:
        out = Path(study_dir) / "evidence" / f"ddg_{tag}.json"
    else:
        out = Path("outputs").resolve() / f"ddg_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    result.update({"success": True, "ddg_file": str(out), **{k: v for k, v in report.items() if k != "warnings"}})
    if study_dir:
        try:
            from mdclaw.study import record_study_log

            record_study_log(
                study_dir=study_dir,
                record_type="decision",
                phase="analysis",
                decision=(f"estimate_ddg {report['mutation']}: ddG = {ddg / _KJ_PER_KCAL:+.2f} +/- "
                          f"{err / _KJ_PER_KCAL:.2f} kcal/mol ({ddg:+.2f} +/- {err:.2f} kJ/mol)"),
                reason="difference of the folded and capped-peptide MBAR legs",
                inputs=[str(Path(folded).resolve()), str(Path(unfolded).resolve())],
                outputs=[str(out)],
                metadata={"ddG_kj_mol": ddg, "ddG_error_kj_mol": err, "job_dir": report["job_dir"], "node_id": node_id},
            )
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"study log not updated: {type(exc).__name__}: {exc}")
    if node_mode:
        from mdclaw._node import complete_node

        complete_node(
            job_dir, node_id,
            artifacts={"ddg": f"artifacts/{output_name}.json"},
            metadata={
                "analysis": DDG_ANALYSIS,
                "mutation": report["mutation"],
                "ddG_kj_mol": ddg,
                "ddG_error_kj_mol": err,
                "ddG_kcal_mol": ddg / _KJ_PER_KCAL,
                "ddG_error_kcal_mol": err / _KJ_PER_KCAL,
                "legs": {role: {"node_id": leg_nodes[role], "dG_kj_mol": leg["dG_kj_mol"],
                                "dG_error_kj_mol": leg.get("dG_error_kj_mol")}
                         for role, leg in (("folded", leg_f), ("unfolded", leg_u))},
            },
            warnings=result["warnings"] or None,
        )
    return result


__all__ = [
    "DDG_ANALYSIS", "FEP_LEG_ANALYSIS", "FepAnalysisError", "LEG_ROLES", "analyze_fep", "check_legs_compatible",
    "collect_windows", "estimate_ddg", "leg_role_of", "run_mbar",
]
