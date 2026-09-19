"""``analyze_fep`` (MBAR over the windows of one leg) and ``estimate_ddg``.

``analyze_fep`` is an ``analyze`` node whose parents are ``fep`` nodes of one
job. It gathers every window's reduced-potential matrix (following the
``fep -> fep`` segment chain recorded in ``fep_windows.json``), discards the
initial part of each window, subsamples to statistically independent frames,
and runs MBAR. The result is the free energy of the wild type -> mutant
transformation *in that environment* (folded protein, or the capped
tripeptide standing in for the unfolded state).

``estimate_ddg`` is a plain helper: it subtracts two such legs,
``ddG = dG(folded) - dG(unfolded)``, and writes a small report.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

from mdclaw._common import create_validation_error, ensure_directory
from mdclaw._tool_meta import node_tool
from mdclaw.fep.protocol import PHASE_BOUNDS, load_protocol

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

def _read_windows_index(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FepAnalysisError(code="fep_windows_missing", message=f"cannot read {path}: {exc}") from exc


def collect_windows(window_index_files: list[str]) -> dict:
    """Merge the ``fep_windows.json`` files of several fep nodes.

    Returns ``{"protocol_file", "temperature_kelvin", "pressure_bar",
    "n_protocol_windows", "windows": {index: [segment, ...]}}``. The same
    index appearing in two parents (independent replicas) simply pools its
    segments.
    """
    merged: dict[int, list[dict]] = {}
    protocol_file = None
    temperature = None
    pressure = None
    n_protocol = None
    sources = []
    for path in window_index_files:
        data = _read_windows_index(path)
        sources.append({"file": path, "node_id": data.get("node_id"), "lambda_indices": data.get("lambda_indices")})
        if protocol_file is None:
            protocol_file = data.get("fep_protocol_file")
            temperature = data.get("temperature_kelvin")
            pressure = data.get("pressure_bar")
            n_protocol = data.get("n_protocol_windows")
        else:
            if data.get("fep_protocol_file") != protocol_file or data.get("n_protocol_windows") != n_protocol:
                raise FepAnalysisError(
                    code="fep_windows_incompatible", message=f"{path} was sampled with a different protocol ({data.get('fep_protocol_file')}) "
                    f"than {protocol_file}; analyze one hybrid topology at a time",
                )
            if abs(float(data.get("temperature_kelvin") or 0) - float(temperature or 0)) > 1e-6:
                raise FepAnalysisError(
                    code="fep_windows_incompatible", message=f"{path} was sampled at a different temperature")
        for key, record in (data.get("windows") or {}).items():
            segments = record.get("segments") or []
            merged.setdefault(int(key), []).extend(segments)
    if not merged:
        raise FepAnalysisError(code="fep_windows_missing", message="no windows found in the fep parents")
    return {
        "protocol_file": protocol_file,
        "temperature_kelvin": float(temperature) if temperature is not None else None,
        "pressure_bar": pressure,
        "n_protocol_windows": int(n_protocol) if n_protocol is not None else None,
        "windows": merged,
        "sources": sources,
    }


def _load_window_samples(segments: list[dict], n_states: int, discard_fraction: float) -> tuple[np.ndarray, dict]:
    """Concatenate one window's segments; drop the first ``discard_fraction``
    of *each* segment (each restart re-equilibrates a little)."""
    blocks = []
    kept = dropped = 0
    for seg in segments:
        path = seg.get("energies_file")
        if not path or not Path(path).is_file():
            raise FepAnalysisError(code="fep_windows_missing", message=f"energies file missing for a segment: {path}")
        with np.load(path) as data:
            u = np.asarray(data["u_kn"], dtype=float)
        if u.shape[0] != n_states:
            raise FepAnalysisError(
                code="fep_windows_incompatible", message=f"{path} has {u.shape[0]} states, protocol has {n_states}")
        n_drop = int(math.floor(u.shape[1] * discard_fraction))
        dropped += n_drop
        u = u[:, n_drop:]
        kept += u.shape[1]
        if u.shape[1]:
            blocks.append(u)
    if not blocks:
        raise FepAnalysisError(code="fep_windows_missing", message="a window has no samples left after discarding")
    return np.concatenate(blocks, axis=1), {"n_raw": kept + dropped, "n_after_discard": kept}


def _subsample(u_k: np.ndarray, own_index: int, timeseries) -> tuple[np.ndarray, dict]:
    """Statistically independent subset using the window's own reduced potential."""
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
        u_k, info = _load_window_samples(windows[k], n_states, discard_fraction)
        if subsample:
            u_k, sub = _subsample(u_k, k, timeseries)
        else:
            sub = {"g": 1.0, "n_used": int(u_k.shape[1])}
        n_k[k] = u_k.shape[1]
        u_blocks.append(u_k)
        per_window.append({"index": k, "lambda": protocol["windows"][k].get("lambda"), **info, **sub})
    u_kn = np.concatenate(u_blocks, axis=1)
    mbar = pymbar.MBAR(u_kn, n_k)
    fe = mbar.compute_free_energy_differences()
    delta_f, d_delta_f = np.asarray(fe["Delta_f"]), np.asarray(fe["dDelta_f"])
    overlap = np.asarray(mbar.compute_overlap()["matrix"])
    kT = _KB_KJ_MOL_K * temperature_kelvin

    neighbour = [float(overlap[k, k + 1]) for k in range(n_states - 1)]
    cumulative = [float(delta_f[0, k] * kT) for k in range(n_states)]
    cumulative_err = [float(d_delta_f[0, k] * kT) for k in range(n_states)]
    lambdas = [w.get("lambda") for w in protocol["windows"]]

    def _phase_dg(lo: float, hi: float) -> Optional[float]:
        idx = [k for k, lam in enumerate(lambdas) if lam is not None and lo - 1e-9 <= lam <= hi + 1e-9]
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
    ``--conditions '{"analysis_data_scope": "production_chain"}'`` whose
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
    result: dict = {"success": False, "tool": "analyze_fep", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)
    parent_ids: list[str] = []
    hybrid_manifest_file = None
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
        protocol = load_protocol(collected["protocol_file"])
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "fep_windows_missing")
        result["errors"].append(str(exc))
        result["code"] = code
        if node_mode:
            from mdclaw._node import begin_node, fail_node

            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=result["errors"], code=code)
        return result

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
        result["errors"].append(str(exc))
        result["code"] = exc.code
        if node_mode:
            from mdclaw._node import fail_node

            fail_node(job_dir, node_id, errors=result["errors"], code=exc.code)
        return result
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["code"] = "fep_analysis_failed"
        if node_mode:
            from mdclaw._node import fail_node

            fail_node(job_dir, node_id, errors=result["errors"], code="fep_analysis_failed")
        return result

    result["warnings"].extend(mbar.pop("warnings"))
    report = {
        "schema_version": 1,
        "mutation": protocol.get("mutation"),
        "protocol_file": collected["protocol_file"],
        "hybrid_manifest_file": hybrid_manifest_file,
        "pressure_bar": collected["pressure_bar"],
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


def estimate_ddg(
    folded: str,
    unfolded: str,
    output_file: Optional[str] = None,
    study_dir: Optional[str] = None,
) -> dict:
    """ddG of folding stability from two ``analyze_fep`` results.

    ``ddG = dG_mut(folded) - dG_mut(unfolded)`` with the sign convention of
    ``ddG_folding = dG_fold(mutant) - dG_fold(wild type)``: positive means the
    mutation destabilises the fold. Errors add in quadrature. No node state is
    touched; the report is written next to the folded result (or to
    ``output_file``) and appended to the study log when ``study_dir`` is given.

    Args:
        folded: ``fep_result.json`` of the folded-protein leg.
        unfolded: ``fep_result.json`` of the capped-tripeptide leg.
        output_file: Where to write ``ddg.json`` (default: beside ``folded``).
        study_dir: Optional study to append a ``record_study_log`` entry to.
    """
    result: dict = {"success": False, "tool": "estimate_ddg", "errors": [], "warnings": []}
    try:
        leg_f = _load_leg(folded, "folded")
        leg_u = _load_leg(unfolded, "unfolded")
    except FepAnalysisError as exc:
        return {**result, **create_validation_error("folded/unfolded", str(exc), code=exc.code)}
    mut_f = (leg_f.get("mutation") or {}).get("label")
    mut_u = (leg_u.get("mutation") or {}).get("label")
    if mut_f and mut_u and mut_f.split(":")[-1] != mut_u.split(":")[-1]:
        result["warnings"].append(f"the two legs carry different mutation labels ({mut_f} vs {mut_u})")
    ddg = leg_f["dG_kj_mol"] - leg_u["dG_kj_mol"]
    err = math.sqrt(leg_f.get("dG_error_kj_mol", 0.0) ** 2 + leg_u.get("dG_error_kj_mol", 0.0) ** 2)
    for label, leg in (("folded", leg_f), ("unfolded", leg_u)):
        for w in leg.get("warnings") or []:
            result["warnings"].append(f"[{label}] {w}")
    report = {
        "schema_version": 1,
        "mutation": mut_f or mut_u,
        "ddG_kj_mol": ddg,
        "ddG_error_kj_mol": err,
        "ddG_kcal_mol": ddg / _KJ_PER_KCAL,
        "ddG_error_kcal_mol": err / _KJ_PER_KCAL,
        "sign_convention": "ddG > 0: mutation destabilises the folded state",
        "legs": {
            "folded": {"file": str(Path(folded).resolve()), "dG_kj_mol": leg_f["dG_kj_mol"],
                       "dG_error_kj_mol": leg_f.get("dG_error_kj_mol"), "n_samples_total": leg_f.get("n_samples_total"),
                       "min_neighbour_overlap": leg_f.get("min_neighbour_overlap")},
            "unfolded": {"file": str(Path(unfolded).resolve()), "dG_kj_mol": leg_u["dG_kj_mol"],
                         "dG_error_kj_mol": leg_u.get("dG_error_kj_mol"), "n_samples_total": leg_u.get("n_samples_total"),
                         "min_neighbour_overlap": leg_u.get("min_neighbour_overlap")},
        },
        "warnings": list(result["warnings"]),
    }
    out = Path(output_file) if output_file else Path(folded).resolve().parent / "ddg.json"
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
                reason="difference of the folded and capped-tripeptide MBAR legs",
                inputs=[str(Path(folded).resolve()), str(Path(unfolded).resolve())],
                outputs=[str(out)],
                metadata={"ddG_kj_mol": ddg, "ddG_error_kj_mol": err},
            )
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"study log not updated: {type(exc).__name__}: {exc}")
    return result


__all__ = ["FepAnalysisError", "analyze_fep", "collect_windows", "estimate_ddg", "run_mbar"]
