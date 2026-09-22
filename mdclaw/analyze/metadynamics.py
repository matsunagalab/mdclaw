"""analyze_metadynamics: is a well-tempered metadynamics run converged?

One number, one figure.  Two states A and B are given as ranges of the
collective variable (folded / unfolded, helix / extended).  From the
Gaussians every walker deposited, merged in time order, the free-energy
profile at time t is ``F(s, t) = -(gamma / (gamma - 1)) V(s, t)`` and the
free-energy difference between the states is

    dF(t) = -kT ln [ int_A exp(-F/kT) ds / int_B exp(-F/kT) ds ].

The run is ``converged`` when dF(t) moves by less than ``drift_tolerance``
(one kT by default) over the second half of the simulation; otherwise
``not_converged`` with the drift as the number to report.  The reported
value is dF at the end with the second-half drift as its uncertainty.

One diagnostic comes with it: the fraction of its depositions each walker
made inside state A.  Walkers that share one bias should spend comparable
time there; a walker that never enters A, or one that never leaves it, is a
sign of a slow motion orthogonal to the coordinate (the bias along the
coordinate cannot flatten it) and is reported as a warning.

Node mode: the analyze node's parents are ``run_metadynamics`` prod nodes,
one walker each; with ``analysis_data_scope`` ``production_chain`` a
parent's ``continue_from`` chain is pooled, with ``segment`` only that
node.  Direct mode: ``metadynamics_report_files`` (one ``metadynamics.csv``
per walker; the sidecar ``metadynamics.json`` next to it gives the
settings).
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import ensure_directory, setup_logger
from mdclaw._tool_meta import node_tool
from mdclaw.analyze.inputs import _rel_to_node_root

logger = setup_logger(__name__)

GAS_CONSTANT_KJ_MOL_K = 8.314462618e-3


class MetadynamicsAnalysisError(Exception):
    """Structured failure: ``code`` is the stable agent-facing guardrail code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# inputs                                                                      #
# --------------------------------------------------------------------------- #

def _read_report(report_csv: str) -> dict[str, np.ndarray]:
    """``metadynamics.csv``: step, time_ps, <cv>_nm, bias_at_cv_kj_mol, gaussian_height_kj_mol."""
    path = Path(report_csv)
    if not path.is_file():
        raise MetadynamicsAnalysisError(code="metadynamics_report_missing",
                                        message=f"metadynamics report not found: {report_csv}")
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise MetadynamicsAnalysisError(code="metadynamics_report_invalid", message=f"{report_csv} is empty") from None
        rows = [r for r in reader if r and len(r) == len(header)]
    if len(header) < 5 or header[0] != "step" or header[1] != "time_ps" or header[4] != "gaussian_height_kj_mol":
        raise MetadynamicsAnalysisError(
            code="metadynamics_report_invalid",
            message=f"{report_csv} is not a run_metadynamics metadynamics.csv (columns step, time_ps, <cv>_nm, "
            "bias_at_cv_kj_mol, gaussian_height_kj_mol)",
        )
    if not rows:
        raise MetadynamicsAnalysisError(code="metadynamics_report_invalid", message=f"{report_csv} has a header but no rows")
    data = np.array(rows, dtype=float)
    return {"step": data[:, 0].astype(np.int64), "time_ps": data[:, 1], "cv": data[:, 2],
            "height": data[:, 4], "cv_name": header[2][:-3] if header[2].endswith("_nm") else header[2]}


def _read_sidecar(path: Optional[str]) -> dict:
    if not path or not Path(path).is_file():
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _walk_chain(job_dir: str, leaf_prod_id: str, segment_only: bool) -> list[dict]:
    """Chronological ``run_metadynamics`` segments of one walker."""
    from mdclaw.node.io import _read_artifact_from_node, _read_node_json

    reversed_records: list[dict] = []
    current: Optional[str] = leaf_prod_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        data = _read_node_json(job_dir, current)
        if data is None or data.get("node_type") != "prod":
            break
        md = data.get("metadata", {}) or {}
        report = _read_artifact_from_node(job_dir, current, "metadynamics_report")
        if md.get("sampling_method") != "metadynamics" or report is None:
            break
        reversed_records.append({
            "node_id": current,
            "report_file": report,
            "state_file": _read_artifact_from_node(job_dir, current, "metadynamics_state"),
            "timestep_fs": md.get("timestep_fs"),
        })
        if segment_only:
            break
        next_id = md.get("continued_from")
        if not next_id:
            parents = data.get("parent_node_ids", [])
            next_id = parents[0] if parents else None
        current = next_id
    reversed_records.reverse()
    return reversed_records


def _collect_walkers_node_mode(job_dir: str, node_id: str) -> tuple[list[dict], dict]:
    from mdclaw.node.io import _read_node_json

    node = _read_node_json(job_dir, node_id) or {}
    conditions = node.get("conditions") or {}
    scope = conditions.get("analysis_data_scope") or "production_chain"
    if scope not in ("production_chain", "segment"):
        raise MetadynamicsAnalysisError(
            code="metadynamics_scope_unsupported",
            message="analyze_metadynamics pools walkers itself; create the node with analysis_data_scope "
            "'production_chain' (or 'segment') and the run_metadynamics prod nodes as parents.",
        )
    parents = node.get("parent_node_ids") or []
    walkers: list[dict] = []
    for pid in parents:
        pdata = _read_node_json(job_dir, pid) or {}
        if pdata.get("node_type") != "prod":
            raise MetadynamicsAnalysisError(
                code="metadynamics_inputs_missing",
                message=f"parent {pid} is a {pdata.get('node_type')!r} node; analyze_metadynamics takes "
                "run_metadynamics prod parents",
            )
        segments = _walk_chain(job_dir, pid, segment_only=(scope == "segment"))
        if not segments:
            raise MetadynamicsAnalysisError(
                code="metadynamics_inputs_missing",
                message=f"parent {pid} carries no metadynamics_report artifact; it is not a completed run_metadynamics node",
            )
        walkers.append({"label": pid, "segments": segments})
    if not walkers:
        raise MetadynamicsAnalysisError(code="metadynamics_inputs_missing", message="the analyze node has no prod parent")
    return walkers, {"analysis_data_scope": scope, "parent_node_ids": parents}


def _collect_walkers_direct(report_files: list[str], state_files: Optional[list[str]]) -> list[dict]:
    if state_files and len(state_files) != len(report_files):
        raise MetadynamicsAnalysisError(
            code="metadynamics_inputs_missing",
            message="metadynamics_state_files must list one sidecar per report (same order)",
        )
    walkers = []
    for i, report in enumerate(report_files):
        state = state_files[i] if state_files else str(Path(report).with_name("metadynamics.json"))
        walkers.append({"label": f"walker_{i + 1}", "segments": [{
            "node_id": Path(report).parent.parent.name or f"walker_{i + 1}",
            "report_file": report, "state_file": state if Path(state).is_file() else None, "timestep_fs": None,
        }]})
    return walkers


def _parse_state(values, name: str) -> tuple[float, float]:
    try:
        lo, hi = (float(v) for v in values)
    except (TypeError, ValueError):
        raise MetadynamicsAnalysisError(
            code="metadynamics_states_invalid",
            message=f"{name} must be two numbers, the lower and upper edge of the state in nm (e.g. --{name.replace('_', '-')} 0.45 0.8)",
        ) from None
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        raise MetadynamicsAnalysisError(code="metadynamics_states_invalid",
                                        message=f"{name}: the upper edge must exceed the lower edge, got {lo}, {hi}")
    return lo, hi


# --------------------------------------------------------------------------- #
# the computation                                                             #
# --------------------------------------------------------------------------- #

def delta_f_vs_time(
    depositions: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
    *,
    grid: np.ndarray,
    sigma: float,
    gamma: float,
    kT: float,
    state_a: tuple[float, float],
    state_b: tuple[float, float],
    n_time_points: int,
) -> tuple[np.ndarray, np.ndarray]:
    """dF(t) = F_A - F_B from the Gaussians of every walker merged in time.

    ``depositions``: per walker ``(time_ps, cv, height)``.  Returns
    ``(time_ns, dF)`` on ``n_time_points`` equally spaced times up to the
    longest walker time."""
    in_a = (grid >= state_a[0]) & (grid <= state_a[1])
    in_b = (grid >= state_b[0]) & (grid <= state_b[1])
    t_end = max(float(t[-1]) for t, _, _ in depositions)
    times = np.linspace(t_end / n_time_points, t_end, n_time_points)
    V = np.zeros_like(grid)
    pointers = [0] * len(depositions)
    factor = gamma / (gamma - 1.0)
    out = np.empty(n_time_points)
    for k, t in enumerate(times):
        for w, (tw, cw, hw) in enumerate(depositions):
            j = pointers[w]
            end = int(np.searchsorted(tw, t, side="right"))
            if end > j:
                # vectorised sum of the Gaussians deposited in (previous t, t]
                V += (hw[j:end, None] * np.exp(-0.5 * ((grid[None, :] - cw[j:end, None]) / sigma) ** 2)).sum(axis=0)
                pointers[w] = end
        F = -factor * V
        F = F - F.min()
        za = np.trapezoid(np.exp(-F[in_a] / kT), grid[in_a])
        zb = np.trapezoid(np.exp(-F[in_b] / kT), grid[in_b])
        out[k] = -kT * np.log(za / zb)
    return times / 1000.0, out


def _analyze(walkers: list[dict], *, out_dir: Path, output_name: str, state_a, state_b,
             drift_tolerance_kj_mol: float, n_time_points: int, warnings: list[str]) -> dict:
    manifest: Optional[dict] = None
    grid_min = grid_max = None
    depositions: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    walker_rows: list[dict] = []
    cv_name = None
    for walker in walkers:
        times: list[np.ndarray] = []
        cvs: list[np.ndarray] = []
        heights: list[np.ndarray] = []
        for seg in walker["segments"]:
            side = _read_sidecar(seg.get("state_file"))
            m = side.get("manifest")
            if not m:
                raise MetadynamicsAnalysisError(
                    code="metadynamics_inputs_missing",
                    message=f"{seg['node_id']}: metadynamics.json with the settings (manifest) is missing next to the report",
                )
            if manifest is None:
                manifest = m
                grid_min, grid_max = m["cv_min_nm"], m["cv_max_nm"]
            elif json.dumps(m, sort_keys=True) != json.dumps(manifest, sort_keys=True):
                raise MetadynamicsAnalysisError(
                    code="metadynamics_walkers_incompatible",
                    message=f"{seg['node_id']} ran with other metadynamics settings than the first walker; "
                    "analyze one condition per node",
                )
            rep = _read_report(seg["report_file"])
            cv_name = cv_name or rep["cv_name"]
            times.append(rep["time_ps"])
            cvs.append(rep["cv"])
            heights.append(rep["height"])
        t = np.concatenate(times)
        order = np.argsort(t, kind="stable")
        t, c, h = t[order], np.concatenate(cvs)[order], np.concatenate(heights)[order]
        depositions.append((t, c, h))
        in_a = (c >= state_a[0]) & (c <= state_a[1])
        in_b = (c >= state_b[0]) & (c <= state_b[1])
        walker_rows.append({
            "walker": walker["label"],
            "segments": [s["node_id"] for s in walker["segments"]],
            "time_ns": float(t[-1] / 1000.0),
            "depositions": int(len(t)),
            "fraction_in_state_a": float(in_a.mean()),
            "fraction_in_state_b": float(in_b.mean()),
            "cv_min_nm": float(c.min()),
            "cv_max_nm": float(c.max()),
            "final_gaussian_height_kj_mol": float(h[-1]),
            "initial_gaussian_height_kj_mol": float(h[0]),
        })
    assert manifest is not None
    kT = GAS_CONSTANT_KJ_MOL_K * float(manifest["temperature_kelvin"])
    sigma = float(manifest["bias_width_nm"])
    gamma = float(manifest["bias_factor"])
    for name, (lo, hi) in (("state_a", state_a), ("state_b", state_b)):
        if lo < grid_min - 1e-9 or hi > grid_max + 1e-9:
            raise MetadynamicsAnalysisError(
                code="metadynamics_states_invalid",
                message=f"{name} [{lo}, {hi}] lies outside the biased range [{grid_min}, {grid_max}] nm of these walkers",
            )
    if state_a[1] > state_b[0] and state_b[1] > state_a[0]:
        raise MetadynamicsAnalysisError(code="metadynamics_states_invalid",
                                        message="state_a and state_b overlap; choose disjoint ranges")
    grid = np.linspace(grid_min, grid_max, max(400, int(10 * (grid_max - grid_min) / sigma)))
    time_ns, dF = delta_f_vs_time(depositions, grid=grid, sigma=sigma, gamma=gamma, kT=kT,
                                  state_a=state_a, state_b=state_b, n_time_points=n_time_points)
    second_half = time_ns >= time_ns[-1] / 2.0
    drift = float(dF[second_half].max() - dF[second_half].min())
    reasons: list[str] = []
    all_c = np.concatenate([c for _, c, _ in depositions])
    if not ((all_c >= state_a[0]) & (all_c <= state_a[1])).any():
        reasons.append("state_a_unvisited")
    if not ((all_c >= state_b[0]) & (all_c <= state_b[1])).any():
        reasons.append("state_b_unvisited")
    if drift >= drift_tolerance_kj_mol:
        reasons.append("profile_drifting")
    verdict = "converged" if not reasons else "not_converged"
    fa = [w["fraction_in_state_a"] for w in walker_rows]
    if len(fa) > 1 and (max(fa) - min(fa)) > 0.5:
        warnings.append(
            "walkers_unequal_residence: the walkers share one bias but spent very different fractions of their time in "
            f"state A ({', '.join(f'{f:.2f}' for f in fa)}); a slow motion orthogonal to the coordinate keeps them in "
            "different states, which the bias along the coordinate cannot flatten (consider a second CV or solute tempering)."
        )
    h_ratio = max(w["final_gaussian_height_kj_mol"] / max(w["initial_gaussian_height_kj_mol"], 1e-12) for w in walker_rows)
    if h_ratio > 0.1:
        warnings.append(f"gaussian_height_not_decayed: the Gaussian height is still {h_ratio:.0%} of its start; the "
                        "well-tempered bias has not saturated yet.")
    # outputs
    csv_path = out_dir / f"{output_name}_delta_f.csv"
    with open(csv_path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["time_ns", "delta_f_kj_mol"])
        for t, f in zip(time_ns, dF):
            wr.writerow([f"{t:.4f}", f"{f:.4f}"])
    plot_path = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(5.5, 3.6))
        ax.plot(time_ns, dF, "k-")
        ax.axvspan(time_ns[-1] / 2.0, time_ns[-1], color="tab:red" if verdict == "not_converged" else "tab:green", alpha=0.08)
        ax.axhline(dF[-1], color="gray", lw=0.6, ls="--")
        ax.set_xlabel("simulation time per walker / ns")
        ax.set_ylabel("dF(A - B) / kJ mol$^{-1}$")
        ax.set_title(f"{cv_name}: A [{state_a[0]}, {state_a[1]}] vs B [{state_b[0]}, {state_b[1]}] nm\n"
                     f"dF = {dF[-1]:.1f} kJ/mol, drift over the second half {drift:.1f} ({verdict})", fontsize=9)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        plot_path = out_dir / f"{output_name}_delta_f.png"
        fig.savefig(plot_path, dpi=150)
        plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"plot skipped: {type(exc).__name__}: {exc}")
    summary = {
        "analysis": "metadynamics_delta_f",
        "cv_name": cv_name,
        "state_a_nm": [state_a[0], state_a[1]],
        "state_b_nm": [state_b[0], state_b[1]],
        "delta_f_kj_mol": float(dF[-1]),
        "drift_second_half_kj_mol": drift,
        "drift_tolerance_kj_mol": float(drift_tolerance_kj_mol),
        "kT_kj_mol": kT,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "time_ns": float(time_ns[-1]),
        "n_walkers": len(walker_rows),
        "walkers": walker_rows,
        "manifest": manifest,
        "delta_f_csv": str(csv_path),
        "plot": str(plot_path) if plot_path else None,
    }
    return summary


# --------------------------------------------------------------------------- #
# the tool                                                                    #
# --------------------------------------------------------------------------- #

@node_tool(node_type="analyze")
def analyze_metadynamics(
    state_a: Optional[list[str]] = None,
    state_b: Optional[list[str]] = None,
    drift_tolerance_kj_mol: float = 2.5,
    n_time_points: int = 50,
    output_name: str = "metadynamics",
    metadynamics_report_files: Optional[list[str]] = None,
    metadynamics_state_files: Optional[list[str]] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    _out_dir_override: Optional[str] = None,
) -> dict:
    """Convergence of well-tempered metadynamics: dF between two states over time.

    Pools the Gaussians of every parent walker in time order, reconstructs
    the free-energy profile at successive times and reports the free-energy
    difference between ``state_a`` and ``state_b`` (ranges of the
    collective variable in nm) as a function of simulation time.  The
    verdict is ``converged`` when dF moved by less than
    ``drift_tolerance_kj_mol`` (default 2.5, one kT at 300 K) over the second
    half of the run; the number to report is ``delta_f_kj_mol`` with
    ``drift_second_half_kj_mol`` as its uncertainty.

    Outputs under the analyze node: ``{output_name}_delta_f.csv`` (time,
    dF), ``{output_name}_delta_f.png`` and ``{output_name}.json`` (verdict,
    reasons, per-walker residence in A and B, settings).

    Args:
        state_a: Lower and upper edge of state A in nm (CLI:
            ``--state-a 0.45 0.8``).
        state_b: Lower and upper edge of state B in nm; disjoint from A.
        drift_tolerance_kj_mol: Converged when the second-half drift of dF is
            below this (default 2.5).
        n_time_points: Points on the dF(t) curve (default 50).
        output_name: Output file prefix.
        metadynamics_report_files: Direct mode: one ``metadynamics.csv`` per
            walker (the sidecar next to it supplies the settings).
        metadynamics_state_files: Direct mode: the sidecars, when not next
            to the reports.
        job_dir: Study job directory (node mode; parents are
            ``run_metadynamics`` prod nodes).
        node_id: Analyze node id.
    """
    result: dict[str, Any] = {"success": False, "verdict": None, "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)
    try:
        a = _parse_state(state_a, "state_a")
        b = _parse_state(state_b, "state_b")
        if node_mode:
            walkers, scope_info = _collect_walkers_node_mode(job_dir, node_id)
        else:
            if not metadynamics_report_files:
                raise MetadynamicsAnalysisError(
                    code="metadynamics_inputs_missing",
                    message="pass --job-dir/--node-id (parents: run_metadynamics prod nodes) or --metadynamics-report-files",
                )
            walkers = _collect_walkers_direct(metadynamics_report_files, metadynamics_state_files)
            scope_info = {"analysis_data_scope": None, "parent_node_ids": []}
    except MetadynamicsAnalysisError as exc:
        result["errors"].append(str(exc))
        result["code"] = exc.code
        if node_mode:
            from mdclaw._node import fail_node

            fail_node(job_dir, node_id, errors=result["errors"], code=exc.code)
        return result

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
        from mdclaw._node import begin_node

        out_dir = ensure_directory(Path(job_dir) / "nodes" / node_id / "artifacts")
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "metadynamics_output")

    try:
        summary = _analyze(walkers, out_dir=out_dir, output_name=output_name, state_a=a, state_b=b,
                           drift_tolerance_kj_mol=drift_tolerance_kj_mol, n_time_points=max(4, int(n_time_points)),
                           warnings=result["warnings"])
        summary["analysis_data_scope"] = scope_info["analysis_data_scope"]
        summary["parent_node_ids"] = scope_info["parent_node_ids"]
        summary_path = out_dir / f"{output_name}.json"
        summary_path.write_text(json.dumps(summary, indent=2))
        result.update({
            "success": True,
            "verdict": summary["verdict"],
            "verdict_reasons": summary["verdict_reasons"],
            "delta_f_kj_mol": summary["delta_f_kj_mol"],
            "drift_second_half_kj_mol": summary["drift_second_half_kj_mol"],
            "time_ns": summary["time_ns"],
            "n_walkers": summary["n_walkers"],
            "walkers": summary["walkers"],
            "delta_f_csv": summary["delta_f_csv"],
            "plot": summary["plot"],
            "summary_json": str(summary_path),
            "next_action": (
                "report delta_f_kj_mol with drift_second_half_kj_mol as its uncertainty"
                if summary["verdict"] == "converged"
                else "extend every walker with continue_from and analyze again; if walkers_unequal_residence is "
                     "warned, the coordinate hides a slow motion (add a second CV or combine with solute tempering)"
            ),
        })
    except MetadynamicsAnalysisError as exc:
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["code"] = "unhandled_exception"

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            artifacts = {
                "metadynamics_summary": _rel_to_node_root(result["summary_json"], out_dir),
                "metadynamics_delta_f": _rel_to_node_root(result["delta_f_csv"], out_dir),
            }
            if result["plot"]:
                artifacts["metadynamics_plot"] = _rel_to_node_root(result["plot"], out_dir)
            metadata = {
                "sampling_method": "metadynamics",
                "analysis": "metadynamics_delta_f",
                "verdict": result["verdict"],
                "verdict_reasons": result["verdict_reasons"],
                "delta_f_kj_mol": result["delta_f_kj_mol"],
                "drift_second_half_kj_mol": result["drift_second_half_kj_mol"],
                "drift_tolerance_kj_mol": drift_tolerance_kj_mol,
                "state_a_nm": list(a),
                "state_b_nm": list(b),
                "time_ns": result["time_ns"],
                "n_walkers": result["n_walkers"],
            }
            complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata,
                          warnings=result["warnings"] or None)
        else:
            fail_node(job_dir, node_id, errors=result["errors"], code=result.get("code"))
    return result
