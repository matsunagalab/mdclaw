"""analyze_tempering: MBAR over the rungs of one or more SST2 walkers.

A ``run_sst2`` production node records, at every exchange attempt, the rung
the walker sat on and the unscaled energy components of the configuration
(``tempering.csv``): the solute-internal terms split by the exponent of
lambda that scales them (``E frac <f>``) and the solute-solvent term scaled
by sqrt(lambda). From those columns the reduced potential of the same
configuration at any rung follows,

    u_m(x) = beta_ref * ( sum_f lambda_m^f E_f(x) + sqrt(lambda_m) E_pw(x) ),

because the terms that lambda does not touch (solvent-solvent, the unscaled
solute part) cancel between rungs. MBAR over all rungs then gives every
recorded configuration a weight in the reference-temperature ensemble, the
rung free energies ``f_k`` (the fixed weights for the next stage), and the
effective sample size at the reference rung.

Within a rung the configurations are Boltzmann-distributed at that rung
whether the tempering weights were still adapting or not (the weights only
decide how often each rung is visited), so the adaptive stage contributes to
the reweighting; the caller can still drop a burn-in with ``discard_ns`` or
keep only fixed-weight nodes with ``fixed_weights_only``.

Node mode: the analyze node's parents are ``run_sst2`` prod nodes. With one
parent the tool walks its continuation chain (``analysis_data_scope``
``production_chain``) or takes that segment only (``segment``); with several
parents every parent is one independent walker and all walkers are pooled
in one MBAR (they must share ladder, reference temperature and solute).
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import ensure_directory, setup_logger
from mdclaw._tool_meta import node_tool
from mdclaw.analyze.inputs import _rel_to_node_root

logger = setup_logger(__name__)

GAS_CONSTANT_KJ_MOL_K = 8.314462618e-3
REPORT_STEP = "Step"
REPORT_TEMP = "Aim Temp (K)"
REPORT_PW = "E solvent-solute (kJ/mole)"
_FRAC_RE = re.compile(r"^E frac ([0-9.]+) \(kJ/mole\)$")
_WEIGHT_RE = re.compile(r"^Weight (\d+) \(kJ/mole\)$")


class TemperingAnalysisError(Exception):
    """Structured failure: ``code`` is the stable agent-facing guardrail code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Report parsing                                                              #
# --------------------------------------------------------------------------- #

def _read_report(report_csv: str) -> dict[str, np.ndarray]:
    """Parse one ``tempering.csv`` into arrays: step, temperature, the
    fractional solute energies (columns ``E frac <f>``), E_pw and the
    on-the-fly weights recorded on each row."""
    path = Path(report_csv)
    if not path.is_file():
        raise TemperingAnalysisError(code="tempering_report_missing", message=f"tempering report not found: {report_csv}")
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise TemperingAnalysisError(code="tempering_report_invalid", message=f"{report_csv} is empty") from None
        rows = [r for r in reader if r and len(r) == len(header)]
    columns = {name: i for i, name in enumerate(header)}
    for needed in (REPORT_STEP, REPORT_TEMP, REPORT_PW):
        if needed not in columns:
            raise TemperingAnalysisError(
                code="tempering_report_invalid",
                message=f"{report_csv} lacks the column {needed!r}; it is not a run_sst2 tempering.csv",
            )
    frac_cols = sorted(
        ((float(m.group(1)), i) for name, i in columns.items() if (m := _FRAC_RE.match(name))),
        key=lambda t: t[0],
    )
    if not frac_cols:
        raise TemperingAnalysisError(code="tempering_report_invalid", message=f"{report_csv} has no 'E frac' columns")
    weight_cols = sorted(
        ((int(m.group(1)), i) for name, i in columns.items() if (m := _WEIGHT_RE.match(name))),
        key=lambda t: t[0],
    )
    if not rows:
        raise TemperingAnalysisError(code="tempering_report_invalid", message=f"{report_csv} has a header but no rows")
    data = np.array(rows, dtype=float)
    return {
        "step": data[:, columns[REPORT_STEP]].astype(np.int64),
        "temperature": data[:, columns[REPORT_TEMP]],
        "fractions": np.array([f for f, _ in frac_cols]),
        "e_frac": data[:, [i for _, i in frac_cols]],
        "e_pw": data[:, columns[REPORT_PW]],
        "weights": data[:, [i for _, i in weight_cols]] if weight_cols else None,
    }


def _read_sidecar(state_json: Optional[str]) -> dict:
    if not state_json:
        return {}
    path = Path(state_json)
    if not path.is_file():
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _rung_index(temperature: np.ndarray, ladder: list[float]) -> np.ndarray:
    ladder_arr = np.asarray(ladder, dtype=float)
    diff = np.abs(temperature[:, None] - ladder_arr[None, :])
    idx = np.argmin(diff, axis=1)
    if np.any(diff[np.arange(len(temperature)), idx] > 0.5):
        bad = temperature[diff[np.arange(len(temperature)), idx] > 0.5][:3]
        raise TemperingAnalysisError(
            code="tempering_walkers_incompatible",
            message=f"report temperatures {bad.tolist()} are not on the ladder {ladder}",
        )
    return idx


def _reduced_potentials(
    e_frac: np.ndarray, fractions: np.ndarray, e_pw: np.ndarray, lambdas: np.ndarray, beta_ref: float
) -> np.ndarray:
    """u_kn (K x N): reduced potential of every configuration at every rung."""
    scale = lambdas[:, None] ** fractions[None, :]           # K x F
    u = scale @ e_frac.T + np.sqrt(lambdas)[:, None] * e_pw[None, :]
    return beta_ref * u


def _round_trips(rungs: np.ndarray, top: int) -> int:
    trips, seen_top = 0, False
    for r in rungs:
        if r == top:
            seen_top = True
        elif r == 0 and seen_top:
            trips += 1
            seen_top = False
    return trips


# --------------------------------------------------------------------------- #
# Walker collection                                                           #
# --------------------------------------------------------------------------- #

def _walk_sst2_chain(job_dir: str, leaf_prod_id: str, segment_only: bool) -> list[dict]:
    """Chronological ``run_sst2`` segments of one walker: node id, report,
    sidecar, trajectory and the cadence metadata needed to place frames."""
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
        report = _read_artifact_from_node(job_dir, current, "tempering_report")
        if md.get("sampling_method") != "sst2" or report is None:
            break  # a plain-MD ancestor (or an eq node) ends the walker
        reversed_records.append({
            "node_id": current,
            "report_file": report,
            "state_file": _read_artifact_from_node(job_dir, current, "tempering_state"),
            "trajectory_file": _read_artifact_from_node(job_dir, current, "trajectory"),
            "timestep_fs": md.get("timestep_fs"),
            "output_frequency_ps": md.get("output_frequency_ps"),
            "start_step": int(md.get("start_step") or 0),
            "temperatures_kelvin": md.get("temperatures_kelvin"),
            "reference_temperature_kelvin": md.get("temperature_kelvin"),
            "weights_fixed": ((md.get("tempering") or {}).get("weights_fixed")),
            "solute_atoms": ((md.get("tempering") or {}).get("solute_atoms")),
            "solute_indices_file": _read_artifact_from_node(job_dir, current, "solute_indices"),
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
    if scope == "comparison":
        raise TemperingAnalysisError(
            code="tempering_scope_unsupported",
            message="analyze_tempering pools walkers itself; create the node with analysis_data_scope "
            "'production_chain' (or 'segment') and the run_sst2 prod nodes as parents.",
        )
    parents = node.get("parent_node_ids") or []
    walkers: list[dict] = []
    for pid in parents:
        pdata = _read_node_json(job_dir, pid) or {}
        if pdata.get("node_type") != "prod":
            raise TemperingAnalysisError(
                code="tempering_inputs_missing",
                message=f"parent {pid} is a {pdata.get('node_type')!r} node; analyze_tempering takes run_sst2 prod parents",
            )
        segments = _walk_sst2_chain(job_dir, pid, segment_only=(scope == "segment"))
        if not segments:
            raise TemperingAnalysisError(
                code="tempering_inputs_missing",
                message=f"parent {pid} carries no tempering_report artifact; it is not a completed run_sst2 node",
            )
        from mdclaw.node.graph import find_ancestor_artifact

        walkers.append({"label": pid, "leaf_prod_id": pid, "segments": segments,
                        "topology_file": find_ancestor_artifact(job_dir, pid, "topo", "topology_pdb")})
    if not walkers:
        raise TemperingAnalysisError(code="tempering_inputs_missing", message="the analyze node has no prod parent")
    return walkers, {"analysis_data_scope": scope, "parent_node_ids": parents}


def _collect_walkers_direct(
    report_files: list[str], state_files: Optional[list[str]], output_frequency_ps: Optional[float],
    trajectory_files: Optional[list[str]] = None, topology_file: Optional[str] = None,
) -> list[dict]:
    if trajectory_files and len(trajectory_files) != len(report_files):
        raise TemperingAnalysisError(
            code="tempering_inputs_missing",
            message="trajectory_files must list one DCD per tempering report (same order)",
        )
    if state_files and len(state_files) != len(report_files):
        raise TemperingAnalysisError(
            code="tempering_inputs_missing",
            message="tempering_state_files must list one sidecar per tempering report (same order)",
        )
    walkers = []
    for i, report in enumerate(report_files):
        state = state_files[i] if state_files else str(Path(report).with_name("tempering.json"))
        side = _read_sidecar(state)
        traj = trajectory_files[i] if trajectory_files else str(Path(report).with_name("trajectory.dcd"))
        solute_file = Path(report).with_name("solute_indices.json")
        walkers.append({
            "label": f"walker_{i + 1}",
            "leaf_prod_id": None,
            "topology_file": topology_file,
            "segments": [{
                "node_id": Path(report).parent.parent.name or f"walker_{i + 1}",
                "report_file": report,
                "state_file": state if Path(state).is_file() else None,
                "trajectory_file": traj if Path(traj).is_file() else None,
                "solute_indices_file": str(solute_file) if solute_file.is_file() else None,
                "timestep_fs": side.get("dt_fs"),
                "output_frequency_ps": output_frequency_ps,
                "start_step": 0,
                "temperatures_kelvin": side.get("temperatures_K"),
                "reference_temperature_kelvin": side.get("ref_temperature_K"),
                "weights_fixed": side.get("weights_fixed"),
                "solute_atoms": side.get("solute_atoms"),
            }],
        })
    return walkers


def _dcd_frame_count(path: Optional[str]) -> Optional[int]:
    """Frame count from the DCD header (CHARMM/NAMD layout: an 84-byte block
    that starts with ``CORD`` and the number of frames). Read directly so
    no plugin writes to stdout, which is the CLI's JSON channel."""
    if not path or not Path(path).is_file():
        return None
    try:
        import struct

        with open(path, "rb") as fh:
            head = fh.read(12)
        if len(head) < 12:
            return None
        for order in ("<", ">"):
            block, magic, nframes = struct.unpack(order + "i4si", head)
            if block == 84 and magic == b"CORD":
                return int(nframes)
        return None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Sampling convergence: dF(A - B) at the reference temperature vs time         #
# --------------------------------------------------------------------------- #

def _parse_state(values, name: str) -> tuple[float, float]:
    try:
        lo, hi = (float(v) for v in values)
    except (TypeError, ValueError):
        raise TemperingAnalysisError(
            code="tempering_states_invalid",
            message=f"{name} must be two numbers, the lower and upper edge of the state in nm "
            f"(e.g. --{name.replace('_', '-')} 0.0 0.15)",
        ) from None
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        raise TemperingAnalysisError(code="tempering_states_invalid",
                                     message=f"{name}: the upper edge must exceed the lower edge, got {lo}, {hi}")
    return lo, hi


def _observable_atoms(topology_file: str, solute: list[int], rmsd_selection: Optional[str],
                      align_selection: Optional[str]) -> tuple[np.ndarray, np.ndarray]:
    """Atom indices (in the full system) for the RMSD and for the superposition.

    Defaults: the solute backbone (N, CA, C, O of the solute atoms) against
    the CA atoms of every protein residue that holds no solute atom (the
    framework the loop sits on), preferring the solute's own chains."""
    import mdtraj as md

    top = md.load_topology(topology_file)

    def _select(expr: str, what: str) -> np.ndarray:
        try:
            idx = np.asarray(top.select(expr), dtype=np.int64)
        except Exception as exc:  # noqa: BLE001
            raise TemperingAnalysisError(code="tempering_observable_invalid",
                                         message=f"{what} {expr!r} is not a valid mdtraj selection: {exc}") from None
        if idx.size == 0:
            raise TemperingAnalysisError(code="tempering_observable_invalid",
                                         message=f"{what} {expr!r} matched no atoms in {topology_file}")
        return idx

    solute_set = set(int(i) for i in solute)
    if rmsd_selection:
        rmsd_idx = _select(rmsd_selection, "rmsd_selection")
    else:
        if not solute_set:
            raise TemperingAnalysisError(code="tempering_observable_invalid",
                                         message="solute_indices.json is missing; pass --rmsd-selection")
        rmsd_idx = np.array(sorted(a.index for a in top.atoms if a.index in solute_set
                                   and a.name in ("N", "CA", "C", "O")), dtype=np.int64)
        if rmsd_idx.size == 0:
            rmsd_idx = np.array(sorted(a.index for a in top.atoms if a.index in solute_set
                                       and a.element is not None and a.element.symbol != "H"), dtype=np.int64)
    if align_selection:
        align_idx = _select(align_selection, "align_selection")
    else:
        solute_res = {a.residue.index for a in top.atoms if a.index in solute_set}
        solute_chains = {a.residue.chain.index for a in top.atoms if a.index in solute_set}
        framework = [a for a in top.atoms if a.name == "CA" and a.residue.is_protein
                     and a.residue.index not in solute_res]
        same_chain = [a.index for a in framework if a.residue.chain.index in solute_chains]
        chosen = same_chain if len(same_chain) >= 3 else [a.index for a in framework]
        if len(chosen) < 3:
            raise TemperingAnalysisError(
                code="tempering_observable_invalid",
                message="no protein framework outside the solute to superpose on (fewer than 3 CA atoms); "
                "pass --align-selection",
            )
        align_idx = np.array(sorted(chosen), dtype=np.int64)
    overlap = set(align_idx.tolist()) & set(rmsd_idx.tolist())
    if overlap and not align_selection:
        align_idx = np.array(sorted(set(align_idx.tolist()) - overlap), dtype=np.int64)
    return rmsd_idx, align_idx


def _rmsd_series(trajectory_file: str, topology_file: str, reference_file: str,
                 rmsd_idx: np.ndarray, align_idx: np.ndarray) -> np.ndarray:
    """RMSD (nm) of ``rmsd_idx`` to the reference after superposing every
    frame on ``align_idx`` (no refit on the RMSD atoms)."""
    import mdtraj as md

    needed = np.unique(np.concatenate([rmsd_idx, align_idx]))
    pos = {int(a): i for i, a in enumerate(needed)}
    r_loc = np.array([pos[int(a)] for a in rmsd_idx])
    a_loc = np.array([pos[int(a)] for a in align_idx])
    ref = md.load(reference_file)
    n_top = md.load_topology(topology_file).n_atoms
    if ref.n_atoms != n_top:
        raise TemperingAnalysisError(
            code="tempering_observable_invalid",
            message=f"reference {reference_file} has {ref.n_atoms} atoms but the trajectory topology has {n_top}; "
            "use a structure written from this system (the topo node's topology.pdb, or a frame of the trajectory)",
        )
    ref = ref.atom_slice(needed)
    out: list[np.ndarray] = []
    for chunk in md.iterload(trajectory_file, top=topology_file, atom_indices=needed, chunk=500):
        chunk.superpose(ref, atom_indices=a_loc, ref_atom_indices=a_loc)
        d = chunk.xyz[:, r_loc, :] - ref.xyz[0, r_loc, :]
        out.append(np.sqrt((d ** 2).sum(axis=2).mean(axis=1)))
    return np.concatenate(out) if out else np.zeros(0)


def _reference_log_weights(u_kn: np.ndarray, k_n: np.ndarray, K: int, pymbar, initial_f_k=None):
    """MBAR on the given rows; log-weights of every row in the reference
    (rung 0) ensemble and the rung free energies (in kT)."""
    N_k = np.bincount(k_n, minlength=K)
    kwargs = {"initial_f_k": initial_f_k} if initial_f_k is not None else {"initialize": "BAR"}
    m = pymbar.MBAR(u_kn, N_k, **kwargs)
    f = np.asarray(m.f_k, dtype=float)
    a = np.log(np.maximum(N_k, 1e-300))[:, None] + f[:, None] - u_kn
    amax = a.max(axis=0)
    log_den = amax + np.log(np.exp(a - amax).sum(axis=0))
    return -u_kn[0] - log_den, f


def _delta_f(values: np.ndarray, log_w: np.ndarray, state_a, state_b, kT: float) -> float:
    in_a = (values >= state_a[0]) & (values <= state_a[1])
    in_b = (values >= state_b[0]) & (values <= state_b[1])
    if not in_a.any() or not in_b.any():
        return float("nan")
    lw_max = log_w.max()
    wa = np.exp(log_w[in_a] - lw_max).sum()
    wb = np.exp(log_w[in_b] - lw_max).sum()
    if wa <= 0 or wb <= 0:
        return float("nan")
    return float(-kT * np.log(wa / wb))


def _state_ess(values: np.ndarray, log_w: np.ndarray, lo_hi) -> float:
    m = (values >= lo_hi[0]) & (values <= lo_hi[1])
    if not m.any():
        return 0.0
    w = np.exp(log_w[m] - log_w[m].max())
    return float(w.sum() ** 2 / (w ** 2).sum())


def _sampling_convergence(*, u_kn, k_n, row_elapsed, row_run, frame_rows_idx, frame_values, K, kT,
                          n_runs, state_a, state_b, n_time_points, pymbar) -> dict:
    """dF(A - B) at the reference temperature from the rows recorded up to
    time t (each run's own simulation time), for the pooled runs and for
    every run alone."""
    ok = np.isfinite(frame_values)
    f_idx, f_val = frame_rows_idx[ok], frame_values[ok]
    t_end = float(row_elapsed.max())
    times = np.linspace(t_end / n_time_points, t_end, n_time_points)
    pooled = np.full(n_time_points, np.nan)
    per_run = np.full((n_runs, n_time_points), np.nan)
    f_prev = None
    f_prev_run: list = [None] * n_runs
    final_logw = None
    for j, t in enumerate(times):
        rows = row_elapsed <= t + 1e-9
        frames = rows[f_idx]
        if frames.sum() >= 2:
            sub = np.nonzero(rows)[0]
            pos = -np.ones(len(rows), dtype=np.int64)
            pos[sub] = np.arange(len(sub))
            try:
                log_w, f_prev = _reference_log_weights(u_kn[:, sub], k_n[sub], K, pymbar, f_prev)
                pooled[j] = _delta_f(f_val[frames], log_w[pos[f_idx[frames]]], state_a, state_b, kT)
                if j == n_time_points - 1:
                    final_logw = log_w[pos[f_idx]]
            except Exception:  # noqa: BLE001
                f_prev = None
        if n_runs >= 2:
            for r in range(n_runs):
                rows_r = rows & (row_run == r)
                frames_r = rows_r[f_idx]
                if frames_r.sum() < 2:
                    continue
                sub = np.nonzero(rows_r)[0]
                pos = -np.ones(len(rows), dtype=np.int64)
                pos[sub] = np.arange(len(sub))
                try:
                    log_w, f_prev_run[r] = _reference_log_weights(u_kn[:, sub], k_n[sub], K, pymbar, f_prev_run[r])
                    per_run[r, j] = _delta_f(f_val[frames_r], log_w[pos[f_idx[frames_r]]], state_a, state_b, kT)
                except Exception:  # noqa: BLE001
                    f_prev_run[r] = None
    return {"times_ns": times, "pooled": pooled, "per_run": per_run,
            "final_log_w": final_logw, "frame_values": f_val, "frame_run": row_run[f_idx]}


_RUN_COLORS = ["#1F77B4", "#D95F02", "#7570B3", "#1B9E77", "#E7298A", "#66A61E"]


def _sampling_block(walkers, walker_summaries, frame_rows, *, u_kn, k_n, row_elapsed, row_run, K, kT, states,
                    rmsd_selection, align_selection, reference_pdb, drift_tolerance_kj_mol, n_time_points,
                    out_dir: Path, output_name: str, pymbar, warnings: list[str]) -> tuple[dict, np.ndarray]:
    """Observable per frame, dF(A - B)(t) pooled and per run, verdict, CSV, figure."""
    state_a, state_b = states
    if not frame_rows:
        raise TemperingAnalysisError(code="tempering_observable_invalid",
                                     message="no frames table (output interval unknown), so no observable per frame")
    if row_elapsed is None:
        raise TemperingAnalysisError(code="tempering_observable_invalid",
                                     message="timestep_fs is not recorded, so dF cannot be followed in time")
    topology = walkers[0].get("topology_file")
    if not topology or not Path(topology).is_file():
        raise TemperingAnalysisError(
            code="tempering_observable_invalid",
            message="no topology for the trajectories (node mode: the topo ancestor's topology_pdb; direct mode: --topology-file)",
        )
    solute: list[int] = []
    for fr in frame_rows:
        if fr.get("solute_indices_file") and Path(fr["solute_indices_file"]).is_file():
            with open(fr["solute_indices_file"]) as fh:
                solute = [int(i) for i in json.load(fh)]
            break
    rmsd_idx, align_idx = _observable_atoms(topology, solute, rmsd_selection, align_selection)
    reference = reference_pdb or topology

    # observable of every DCD frame, one pass per segment
    series: dict[str, np.ndarray] = {}   # keyed by trajectory path: direct-mode node ids need not be unique
    for fr in frame_rows:
        traj = fr.get("trajectory_file")
        if not traj or not Path(traj).is_file():
            raise TemperingAnalysisError(code="tempering_observable_invalid",
                                         message=f"{fr['node_id']}: trajectory.dcd is missing, so the observable cannot be computed")
        if traj not in series:
            series[traj] = _rmsd_series(traj, topology, reference, rmsd_idx, align_idx)
    values = np.full(len(frame_rows), np.nan)
    short = set()
    for i, fr in enumerate(frame_rows):
        arr = series[fr["trajectory_file"]]
        if fr["frame"] < len(arr):
            values[i] = arr[fr["frame"]]
        else:
            short.add(fr["node_id"])
    if short:
        warnings.append(f"{sorted(short)}: fewer DCD frames than report rows at the output interval; those frames are left out")

    frame_idx = np.array([fr["pooled_row"] for fr in frame_rows], dtype=np.int64)
    n_runs = len(walker_summaries)
    conv = _sampling_convergence(u_kn=u_kn, k_n=k_n, row_elapsed=row_elapsed, row_run=row_run,
                                 frame_rows_idx=frame_idx, frame_values=values, K=K, kT=kT, n_runs=n_runs,
                                 state_a=state_a, state_b=state_b, n_time_points=n_time_points, pymbar=pymbar)
    times, pooled, per_run = conv["times_ns"], conv["pooled"], conv["per_run"]
    dF = float(pooled[-1])
    second = times >= times[-1] / 2.0
    tail = pooled[second]
    reasons: list[str] = []
    drift = float(np.nanmax(tail) - np.nanmin(tail)) if np.isfinite(tail).any() else float("nan")
    if not np.isfinite(tail).all():
        reasons.append("delta_f_undefined: a state had no reweighted population over part of the second half")
    elif drift > drift_tolerance_kj_mol:
        reasons.append(f"delta_f_drifting: dF moved {drift:.1f} kJ/mol over the second half (> {drift_tolerance_kj_mol})")
    log_w = conv["final_log_w"]
    ess_a = ess_b = 0.0
    if log_w is not None:
        ess_a = _state_ess(conv["frame_values"], log_w, state_a)
        ess_b = _state_ess(conv["frame_values"], log_w, state_b)
    for name, e in (("state_a", ess_a), ("state_b", ess_b)):
        if e < 10.0:
            reasons.append(f"{name}_not_sampled: {e:.1f} effective frames at the reference temperature (< 10)")
    run_final = per_run[:, -1] if n_runs >= 2 else np.array([])
    run_spread = None
    if n_runs >= 2:
        if not np.isfinite(run_final).all():
            reasons.append("runs_disagree: at least one run never populated both states")
        else:
            run_spread = float(run_final.max() - run_final.min())
            if run_spread > 2.0 * drift_tolerance_kj_mol:
                reasons.append(f"runs_disagree: the independent runs give dF values {run_spread:.1f} kJ/mol apart "
                               f"(> {2.0 * drift_tolerance_kj_mol})")
    else:
        warnings.append("one run only: no run-to-run check; start a second run_sst2 with another --random-seed")
    # A run trapped in one basin is self-consistent in time (1KXV CDR-H3: seed 2 alone flat at -11 kJ/mol
    # for 30 ns while seed 1 ended at +6), so one run can pass the time checks and still be wrong.
    if reasons:
        verdict = "not_converged"
    elif n_runs < 2:
        verdict = "converged_single_run"
    else:
        verdict = "converged"

    csv_path = out_dir / f"{output_name}_delta_f.csv"
    labels = [ws["label"] for ws in walker_summaries]
    with open(csv_path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["time_ns", "delta_f_kj_mol"] + ([f"delta_f_kj_mol_{lab}" for lab in labels] if n_runs >= 2 else []))
        for j, t in enumerate(times):
            row = [f"{t:.4f}", "" if not np.isfinite(pooled[j]) else f"{pooled[j]:.4f}"]
            if n_runs >= 2:
                row += ["" if not np.isfinite(v) else f"{v:.4f}" for v in per_run[:, j]]
            wr.writerow(row)

    plot = _plot_delta_f(out_dir / f"{output_name}_delta_f.png", times, pooled, per_run, labels, conv, state_a,
                         state_b, kT, dF, drift, verdict, walker_summaries, u_kn, k_n, row_run, frame_idx, values,
                         K, pymbar, warnings)
    for ws, v in zip(walker_summaries, run_final if n_runs >= 2 else [None] * n_runs):
        ws["delta_f_kj_mol_this_run"] = None if v is None or not np.isfinite(v) else float(v)
    return {
        "observable": "rmsd_nm",
        "rmsd_atoms": int(rmsd_idx.size),
        "align_atoms": int(align_idx.size),
        "rmsd_selection": rmsd_selection or "solute backbone (N, CA, C, O)",
        "align_selection": align_selection or "CA of protein residues outside the solute",
        "reference": str(reference),
        "state_a_nm": list(state_a),
        "state_b_nm": list(state_b),
        "delta_f_kj_mol": dF,
        "drift_second_half_kj_mol": drift,
        "run_spread_kj_mol": run_spread,
        "drift_tolerance_kj_mol": drift_tolerance_kj_mol,
        "effective_frames_state_a": ess_a,
        "effective_frames_state_b": ess_b,
        "sampling_verdict": verdict,
        "sampling_verdict_reasons": reasons,
        "time_ns": float(times[-1]),
        "delta_f_csv": str(csv_path),
        "plot": plot,
    }, values


def _plot_delta_f(path, times, pooled, per_run, labels, conv, state_a, state_b, kT, dF, drift, verdict,
                  walker_summaries, u_kn, k_n, row_run, frame_idx, values, K, pymbar, warnings) -> Optional[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"plot skipped: {exc}")
        return None
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 3.9), gridspec_kw={"width_ratios": [1.25, 1]})
    ax.axvspan(times[-1] / 2.0, times[-1], color="#2CA02C" if verdict == "converged" else "#D62728",
               alpha=0.08, lw=0)
    n_runs = per_run.shape[0]
    if n_runs >= 2:
        for r in range(n_runs):
            ax.plot(times, per_run[r], "-", lw=1.2, color=_RUN_COLORS[r % len(_RUN_COLORS)],
                    label=f"{labels[r]} alone")
    ax.plot(times, pooled, "-", lw=2.2, color="black", label="all runs pooled")
    if np.isfinite(dF):
        ax.axhline(dF, color="gray", lw=0.7, ls="--")
    # scale to the later 80 % of the run: the first points rest on a few ns and would flatten the rest
    late = times >= 0.2 * times[-1]
    shown = np.concatenate([pooled[late], per_run[:, late].ravel()]) if n_runs >= 2 else pooled[late]
    shown = shown[np.isfinite(shown)]
    if shown.size:
        pad = max(2.0, 0.15 * float(shown.max() - shown.min()))
        ax.set_ylim(float(shown.min()) - pad, float(shown.max()) + pad)
    ax.set_xlabel("simulation time per run / ns")
    ax.set_ylabel("dF(A - B) at T_ref / kJ mol$^{-1}$")
    ax.set_title(f"dF = {dF:.1f} kJ/mol, second-half drift {drift:.1f} ({verdict})", fontsize=9)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, frameon=False)

    # reference-temperature profile along the observable at the end, pooled and per run
    ok = np.isfinite(values)
    lo = min(float(np.nanmin(values)), state_a[0], state_b[0])
    hi = max(float(np.nanmax(values)), state_a[1], state_b[1])
    edges = np.linspace(lo, hi, 41)
    centres = 0.5 * (edges[1:] + edges[:-1])

    def _profile(v, lw):
        w = np.exp(lw - lw.max())
        H, _ = np.histogram(v, bins=edges, weights=w)
        G = np.full(len(H), np.nan)
        m = H > 0
        G[m] = -kT * np.log(H[m] / H[m].max())
        return G

    for (a, b), c, name in ((state_a, "#1F77B4", "A"), (state_b, "#D95F02", "B")):
        ax2.axvspan(a, b, color=c, alpha=0.10, lw=0)
        ax2.text(0.5 * (a + b), 0.97, name, transform=ax2.get_xaxis_transform(), ha="center", va="top",
                 fontsize=9, color="#333333")
    if n_runs >= 2:
        for r in range(n_runs):
            sub = np.nonzero(row_run == r)[0]
            pos = -np.ones(len(row_run), dtype=np.int64)
            pos[sub] = np.arange(len(sub))
            fr_r = ok & (row_run[frame_idx] == r)
            try:
                lw_r, _ = _reference_log_weights(u_kn[:, sub], k_n[sub], K, pymbar)
                ax2.plot(centres, _profile(values[fr_r], lw_r[pos[frame_idx[fr_r]]]), "-", lw=1.2,
                         color=_RUN_COLORS[r % len(_RUN_COLORS)], label=f"{labels[r]} alone")
            except Exception:  # noqa: BLE001
                pass
    if conv["final_log_w"] is not None:
        ax2.plot(centres, _profile(conv["frame_values"], conv["final_log_w"]), "-", lw=2.2, color="black",
                 label="all runs pooled")
    ax2.set_xlabel("RMSD to reference / nm")
    ax2.set_ylabel("F at T_ref / kJ mol$^{-1}$")
    ax2.set_title("profile at the end of the run", fontsize=9)
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


# --------------------------------------------------------------------------- #
# The tool                                                                    #
# --------------------------------------------------------------------------- #

@node_tool(node_type="analyze")
def analyze_tempering(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    tempering_report_files: Optional[list[str]] = None,
    tempering_state_files: Optional[list[str]] = None,
    output_frequency_ps: Optional[float] = None,
    discard_ns: float = 0.0,
    fixed_weights_only: bool = False,
    row_stride: int = 1,
    weights_tolerance_kj_mol: float = 2.5,
    min_round_trips: int = 5,
    state_a: Optional[list[str]] = None,
    state_b: Optional[list[str]] = None,
    rmsd_selection: Optional[str] = None,
    align_selection: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    drift_tolerance_kj_mol: float = 2.5,
    n_time_points: int = 20,
    trajectory_files: Optional[list[str]] = None,
    topology_file: Optional[str] = None,
    output_name: str = "tempering",
    _out_dir_override: Optional[str] = None,
) -> dict:
    """MBAR reweighting of SST2 (solute tempering) walkers to the reference rung.

    Reads the ``tempering.csv`` reports of one or more ``run_sst2`` walkers,
    builds the reduced potential of every recorded configuration at every
    rung, and runs MBAR over all rungs. Outputs, under the analyze node:

    - ``{output_name}_frames.csv``: one row per trajectory frame with
      ``walker``, ``node_id``, ``frame`` (index in that node's DCD),
      ``chain_frame`` (index in the walker's concatenated chain), ``step``,
      ``time_ns``, ``rung``, ``temperature_K``, ``log_weight`` and
      ``weight`` (normalised over all frames of all walkers; the reference-
      temperature ensemble). This is the per-frame lambda label and weight
      a surrogate dataset needs.
    - ``{output_name}_mbar.json``: rung free energies ``f_k`` (kJ/mol, with
      MBAR errors), the on-the-fly weights of every walker for comparison,
      per-walker rung statistics, ESS, and the ``verdict``.
    - ``weights.json``: the MBAR ``f_k`` as a plain list, ready for
      ``run_sst2 --weights-file`` in the fixed-weight stage.
    - ``{output_name}.png``: rung timelines and MBAR-vs-on-the-fly weights.

    ``verdict`` is ``weights_converged`` when every rung was visited, the
    MBAR ``f_k`` agree with every walker's final on-the-fly weights (and
    with each other across walkers) within ``weights_tolerance_kj_mol``, and
    every walker made at least ``min_round_trips`` bottom-top-bottom trips;
    otherwise ``weights_drifting`` with the reasons listed.

    Node mode: parents are ``run_sst2`` prod nodes (one walker per parent;
    a parent's continuation chain is pooled unless the node's
    ``analysis_data_scope`` is ``segment``). Direct mode: pass
    ``tempering_report_files`` (one per walker) and, for the frames table,
    ``output_frequency_ps``.

    Sampling convergence (``state_a`` and ``state_b`` given): the
    observable is the RMSD of the solute backbone to a reference after
    superposing every frame on the protein framework outside the solute
    (both overridable). For two states given as RMSD ranges in nm, the
    free-energy difference at the reference temperature, dF(A - B), is
    followed as a function of simulation time by MBAR on the rows recorded
    up to that time, for all runs pooled and for every run alone.
    ``sampling_verdict`` is ``converged`` when the pooled dF moved by less
    than ``drift_tolerance_kj_mol`` over the second half, the independent
    runs end within twice that of each other, and both states hold at
    least 10 effective frames at the reference temperature; otherwise
    ``not_converged`` with ``sampling_verdict_reasons``. With a single run
    the time checks can pass for a run trapped in one basin, so a passing
    single run reports ``converged_single_run``, never ``converged``. Outputs
    ``{output_name}_delta_f.csv``, ``{output_name}_delta_f.png`` (dF(t) and
    the end profile along the RMSD) and an ``rmsd_nm`` column in the frames
    table. SST2 runs one trajectory per ``run_sst2`` node; "walker" here
    means one independent run (one seed), not a coupled walker.

    Args:
        state_a: Lower and upper RMSD edge of state A in nm
            (CLI: ``--state-a 0.0 0.15``).
        state_b: Lower and upper RMSD edge of state B in nm; disjoint from A.
        rmsd_selection: mdtraj selection for the RMSD atoms (default: the
            solute backbone N, CA, C, O).
        align_selection: mdtraj selection to superpose on (default: CA of
            protein residues outside the solute, same chains first).
        reference_pdb: Reference structure with the system's atom count
            (default: the topo node's topology.pdb, the start structure).
        drift_tolerance_kj_mol: second-half drift allowed for the sampling
            verdict (default 2.5, one kT at 300 K).
        n_time_points: points on the dF(t) curve (default 20).
        trajectory_files: direct mode, one DCD per report (default: the
            trajectory.dcd next to each report).
        topology_file: direct mode, the topology of the DCDs.
        discard_ns: burn-in dropped from the start of every walker.
        fixed_weights_only: use only rows from nodes that ran with fixed
            weights (``weights_fixed``); adaptive-stage nodes are skipped.
        row_stride: keep every n-th report row for MBAR (rows are recorded
            every exchange interval and are correlated; the point estimate
            does not need them all).
        weights_tolerance_kj_mol: agreement threshold for the verdict.
        min_round_trips: round trips per walker required for the verdict.
    """
    result: dict[str, Any] = {
        "success": False,
        "n_walkers": 0,
        "frames_csv": None,
        "mbar_json": None,
        "weights_json": None,
        "plot": None,
        "verdict": None,
        "errors": [],
        "warnings": [],
    }
    node_mode = bool(job_dir and node_id)
    if row_stride < 1:
        row_stride = 1

    # ---- resolve inputs ---------------------------------------------------
    try:
        states = None
        if state_a is not None or state_b is not None:
            if state_a is None or state_b is None:
                raise TemperingAnalysisError(code="tempering_states_invalid",
                                             message="pass both --state-a and --state-b (RMSD ranges in nm)")
            sa, sb = _parse_state(state_a, "state_a"), _parse_state(state_b, "state_b")
            if sa[1] > sb[0] and sb[1] > sa[0]:
                raise TemperingAnalysisError(code="tempering_states_invalid",
                                             message="state_a and state_b overlap; choose disjoint RMSD ranges")
            states = (sa, sb)
        if node_mode:
            walkers, scope_info = _collect_walkers_node_mode(job_dir, node_id)
        else:
            if not tempering_report_files:
                raise TemperingAnalysisError(
                    code="tempering_inputs_missing",
                    message="pass --job-dir/--node-id (parents: run_sst2 prod nodes) or --tempering-report-files",
                )
            walkers = _collect_walkers_direct(tempering_report_files, tempering_state_files, output_frequency_ps,
                                              trajectory_files, topology_file)
            scope_info = {"analysis_data_scope": None, "parent_node_ids": []}
    except TemperingAnalysisError as exc:
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
        out_dir = ensure_directory(Path(os.getcwd()) / "tempering_output")

    try:
        summary = _analyze_walkers(
            walkers,
            out_dir=out_dir,
            output_name=output_name,
            discard_ns=discard_ns,
            fixed_weights_only=fixed_weights_only,
            row_stride=row_stride,
            weights_tolerance_kj_mol=weights_tolerance_kj_mol,
            min_round_trips=min_round_trips,
            warnings=result["warnings"],
            states=states,
            rmsd_selection=rmsd_selection,
            align_selection=align_selection,
            reference_pdb=reference_pdb,
            drift_tolerance_kj_mol=drift_tolerance_kj_mol,
            n_time_points=max(4, int(n_time_points)),
        )
        summary["analysis_data_scope"] = scope_info["analysis_data_scope"]
        summary["parent_node_ids"] = scope_info["parent_node_ids"]
        with open(out_dir / f"{output_name}_mbar.json", "w") as fh:
            json.dump(summary, fh, indent=2)
        result.update({
            "success": True,
            "n_walkers": len(walkers),
            "frames_csv": str(out_dir / f"{output_name}_frames.csv") if summary["frames"]["n_frames"] else None,
            "mbar_json": str(out_dir / f"{output_name}_mbar.json"),
            "weights_json": str(out_dir / "weights.json"),
            "plot": summary.get("plot"),
            "verdict": summary["verdict"],
            "verdict_reasons": summary["verdict_reasons"],
            "f_k_kj_mol": summary["mbar"]["f_k_kj_mol"],
            "ess_reference": summary["mbar"]["ess_reference_rows"],
            "ess_reference_frames": summary["frames"].get("ess_reference"),
            "n_rows": summary["mbar"]["n_rows"],
            "n_frames": summary["frames"]["n_frames"],
            "walkers": summary["walkers"],
        })
        smp = summary.get("sampling")
        if smp:
            result.update({
                "sampling_verdict": smp["sampling_verdict"],
                "sampling_verdict_reasons": smp["sampling_verdict_reasons"],
                "delta_f_kj_mol": smp["delta_f_kj_mol"],
                "drift_second_half_kj_mol": smp["drift_second_half_kj_mol"],
                "run_spread_kj_mol": smp["run_spread_kj_mol"],
                "delta_f_csv": smp["delta_f_csv"],
                "delta_f_plot": smp["plot"],
            })
    except TemperingAnalysisError as exc:
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["code"] = "unhandled_exception"

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            artifacts = {
                "tempering_mbar": _rel_to_node_root(result["mbar_json"], out_dir),
                "weights_json": _rel_to_node_root(result["weights_json"], out_dir),
            }
            if result["frames_csv"]:
                artifacts["tempering_frames"] = _rel_to_node_root(result["frames_csv"], out_dir)
            if result["plot"]:
                artifacts["tempering_plot"] = _rel_to_node_root(result["plot"], out_dir)
            if result.get("delta_f_csv"):
                artifacts["tempering_delta_f"] = _rel_to_node_root(result["delta_f_csv"], out_dir)
            if result.get("delta_f_plot"):
                artifacts["tempering_delta_f_plot"] = _rel_to_node_root(result["delta_f_plot"], out_dir)
            metadata = {
                "sampling_method": "sst2",
                "analysis": "tempering_mbar",
                "n_walkers": result["n_walkers"],
                "n_rows": result["n_rows"],
                "n_frames": result["n_frames"],
                "verdict": result["verdict"],
                "verdict_reasons": result["verdict_reasons"],
                "f_k_kj_mol": result["f_k_kj_mol"],
                "ess_reference": result["ess_reference"],
                "ess_reference_frames": result["ess_reference_frames"],
                "discard_ns": discard_ns,
                "fixed_weights_only": fixed_weights_only,
                "row_stride": row_stride,
            }
            if result.get("sampling_verdict"):
                metadata.update({k: result[k] for k in ("sampling_verdict", "sampling_verdict_reasons", "delta_f_kj_mol",
                                                        "drift_second_half_kj_mol", "run_spread_kj_mol")})
                metadata["state_a_nm"] = list(states[0])
                metadata["state_b_nm"] = list(states[1])
            complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata,
                          warnings=result["warnings"] or None)
        else:
            fail_node(job_dir, node_id, errors=result["errors"], code=result.get("code"))
    return result


def _analyze_walkers(
    walkers: list[dict],
    *,
    out_dir: Path,
    output_name: str,
    discard_ns: float,
    fixed_weights_only: bool,
    row_stride: int,
    weights_tolerance_kj_mol: float,
    min_round_trips: int,
    warnings: list[str],
    states: Optional[tuple] = None,
    rmsd_selection: Optional[str] = None,
    align_selection: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    drift_tolerance_kj_mol: float = 2.5,
    n_time_points: int = 20,
) -> dict:
    try:
        import pymbar
    except ImportError as exc:  # pragma: no cover - environment
        raise TemperingAnalysisError(code="pymbar_not_installed", message=f"pymbar is required for analyze_tempering: {exc}")

    ladder: Optional[list[float]] = None
    ref_temp: Optional[float] = None
    fractions: Optional[np.ndarray] = None
    solute_atoms: Optional[int] = None

    # per-row arrays (pooled) and per-frame rows
    u_blocks: list[np.ndarray] = []      # each K x n
    rung_blocks: list[np.ndarray] = []
    frame_rows: list[dict] = []          # bookkeeping for frames (index into pooled rows)
    walker_summaries: list[dict] = []
    n_rows_total = 0
    elapsed_blocks: list[np.ndarray] = []   # simulation time of each row since the run's first used segment
    run_blocks: list[np.ndarray] = []
    have_time = True

    for w_idx, walker in enumerate(walkers):
        w_rungs: list[np.ndarray] = []
        w_times: list[np.ndarray] = []
        otf_weights: Optional[list[float]] = None
        walker_t0: Optional[float] = None
        walker_origin: Optional[float] = None
        chain_frame = 0
        rows_before_walker = n_rows_total
        segments_used = 0
        for seg in walker["segments"]:
            side = _read_sidecar(seg.get("state_file"))
            seg_ladder = seg.get("temperatures_kelvin") or side.get("temperatures_K")
            seg_ref = seg.get("reference_temperature_kelvin") or side.get("ref_temperature_K")
            if seg_ladder is None or seg_ref is None:
                raise TemperingAnalysisError(
                    code="tempering_inputs_missing",
                    message=f"{seg['node_id']}: ladder / reference temperature not recorded (metadata or tempering.json)",
                )
            seg_ladder = [float(t) for t in seg_ladder]
            seg_ref = float(seg_ref)
            seg_solute = seg.get("solute_atoms") or side.get("solute_atoms")
            if ladder is None:
                ladder, ref_temp, solute_atoms = seg_ladder, seg_ref, seg_solute
            elif seg_ladder != ladder or abs(seg_ref - ref_temp) > 1e-6 or (
                solute_atoms and seg_solute and int(seg_solute) != int(solute_atoms)
            ):
                raise TemperingAnalysisError(
                    code="tempering_walkers_incompatible",
                    message=f"{seg['node_id']}: ladder {seg_ladder} / T_ref {seg_ref} / solute {seg_solute} atoms differ "
                    f"from the first segment ({ladder}, {ref_temp}, {solute_atoms}); pool only walkers of one condition",
                )
            weights_fixed = seg.get("weights_fixed")
            if weights_fixed is None:
                weights_fixed = side.get("weights_fixed")
            if fixed_weights_only and not weights_fixed:
                continue

            rep = _read_report(seg["report_file"])
            if fractions is None:
                fractions = rep["fractions"]
            elif not np.allclose(fractions, rep["fractions"]):
                raise TemperingAnalysisError(
                    code="tempering_walkers_incompatible",
                    message=f"{seg['node_id']}: the fractional-term set {rep['fractions'].tolist()} differs from {fractions.tolist()}",
                )
            timestep_fs = seg.get("timestep_fs") or side.get("dt_fs")
            steps = rep["step"]
            start_step = int(seg.get("start_step") or 0)
            time_ns = ((start_step + steps) * float(timestep_fs) * 1e-6) if timestep_fs else None
            rungs = _rung_index(rep["temperature"], ladder)

            keep = np.ones(len(steps), dtype=bool)
            if discard_ns > 0:
                if time_ns is None:
                    raise TemperingAnalysisError(
                        code="tempering_inputs_missing",
                        message=f"{seg['node_id']}: timestep_fs unknown, cannot apply discard_ns",
                    )
                if walker_t0 is None:
                    walker_t0 = float(np.min(time_ns))
                keep &= time_ns >= walker_t0 + discard_ns   # burn-in counted from the walker's first row
            # frames: rows whose step is a multiple of the DCD output interval
            frame_mask = np.zeros(len(steps), dtype=bool)
            out_ps = seg.get("output_frequency_ps")
            if out_ps and timestep_fs:
                interval = int(round(float(out_ps) * 1000.0 / float(timestep_fs)))
                if interval > 0:
                    frame_mask = (steps % interval) == 0
                    n_dcd = _dcd_frame_count(seg.get("trajectory_file"))
                    if n_dcd is not None and n_dcd != int(frame_mask.sum()):
                        warnings.append(
                            f"{seg['node_id']}: {n_dcd} DCD frames but {int(frame_mask.sum())} report rows at the "
                            f"output interval; frame indices in the frames table may be shifted"
                        )
            # rows used by MBAR: kept rows, strided, plus every frame row
            row_mask = keep & (((np.arange(len(steps)) % row_stride) == 0) | frame_mask)
            sel = np.nonzero(row_mask)[0]
            lambdas = ref_temp / np.asarray(ladder, dtype=float)
            beta_ref = 1.0 / (GAS_CONSTANT_KJ_MOL_K * ref_temp)
            u = _reduced_potentials(rep["e_frac"][sel], fractions, rep["e_pw"][sel], lambdas, beta_ref)
            u_blocks.append(u)
            rung_blocks.append(rungs[sel])
            run_blocks.append(np.full(len(sel), w_idx, dtype=np.int64))
            if time_ns is not None:
                if walker_origin is None:
                    walker_origin = start_step * float(timestep_fs) * 1e-6
                elapsed_blocks.append(time_ns[sel] - walker_origin)
            else:
                have_time = False
                elapsed_blocks.append(np.full(len(sel), np.nan))
            # frames bookkeeping (frame index within node counts every frame row, discarded or not)
            frame_positions = np.nonzero(frame_mask)[0]
            pos_in_sel = {int(p): i for i, p in enumerate(sel)}
            for local_frame, p in enumerate(frame_positions):
                if int(p) in pos_in_sel:
                    frame_rows.append({
                        "walker": walker["label"],
                        "node_id": seg["node_id"],
                        "frame": local_frame,
                        "chain_frame": chain_frame + local_frame,
                        "step": int(start_step + steps[p]),
                        "time_ns": float(time_ns[p]) if time_ns is not None else None,
                        "rung": int(rungs[p]),
                        "temperature_K": float(ladder[int(rungs[p])]),
                        "pooled_row": n_rows_total + pos_in_sel[int(p)],
                        "run": w_idx,
                        "trajectory_file": seg.get("trajectory_file"),
                        "solute_indices_file": seg.get("solute_indices_file"),
                    })
            chain_frame += len(frame_positions)
            n_rows_total += len(sel)
            segments_used += 1
            w_rungs.append(rungs)
            if time_ns is not None:
                w_times.append(time_ns)
            if side.get("weights_kJ_per_mol"):
                otf_weights = [float(x) for x in side["weights_kJ_per_mol"]]
            elif rep["weights"] is not None:
                otf_weights = [float(x) for x in rep["weights"][-1]]

        if segments_used == 0:
            raise TemperingAnalysisError(
                code="tempering_inputs_missing",
                message=f"walker {walker['label']}: no usable segment"
                + (" (fixed_weights_only: none ran with fixed weights)" if fixed_weights_only else ""),
            )
        all_rungs = np.concatenate(w_rungs)
        K = len(ladder)
        occupancy = np.bincount(all_rungs, minlength=K).tolist()
        changes = int(np.sum(all_rungs[1:] != all_rungs[:-1]))
        walker_summaries.append({
            "label": walker["label"],
            "leaf_prod_id": walker.get("leaf_prod_id"),
            "segments": [s["node_id"] for s in walker["segments"]],
            "segments_used": segments_used,
            "rows": int(n_rows_total - rows_before_walker),
            "report_rows": int(len(all_rungs)),
            "time_ns": float(np.max(np.concatenate(w_times)) - np.min(np.concatenate(w_times))) if w_times else None,
            "rung_occupancy": occupancy,
            "rung_occupancy_fraction": [c / len(all_rungs) for c in occupancy],
            "rung_changes": changes,
            "rung_change_fraction": changes / max(1, len(all_rungs) - 1),
            "round_trips": _round_trips(all_rungs, K - 1),
            "on_the_fly_weights_kj_mol": otf_weights,
            "_rungs": all_rungs,
            "_times": np.concatenate(w_times) if w_times else None,
        })

    assert ladder is not None and ref_temp is not None
    K = len(ladder)
    kT = GAS_CONSTANT_KJ_MOL_K * ref_temp
    u_kn = np.concatenate(u_blocks, axis=1)
    k_n = np.concatenate(rung_blocks)
    N_k = np.bincount(k_n, minlength=K)
    unvisited = [int(k) for k in range(K) if N_k[k] == 0]
    if unvisited:
        # MBAR cannot place a rung nobody sampled; drop it from the estimate
        warnings.append(f"rungs {unvisited} were never visited; their f_k are undefined")
    mbar = pymbar.MBAR(u_kn, N_k, initialize="BAR")
    f_k = np.asarray(mbar.f_k, dtype=float)
    f_k = f_k - f_k[0]
    try:
        df = mbar.compute_free_energy_differences()
        f_err = np.asarray(df["dDelta_f"][0], dtype=float)
    except Exception:  # noqa: BLE001
        f_err = np.full(K, np.nan)
    W = mbar.weights()[:, 0]
    ess_rows = float(1.0 / np.sum(W ** 2))
    log_w_rows = np.log(np.clip(W, 1e-300, None))

    # ---- per-walker MBAR (seed-to-seed spread) ----------------------------
    per_walker_f: list[Optional[list[float]]] = []
    if len(walkers) >= 2:
        offset = 0
        for ws in walker_summaries:
            sl = slice(offset, offset + ws["rows"])
            offset += ws["rows"]
            Nk_w = np.bincount(k_n[sl], minlength=K)
            if np.any(Nk_w == 0):
                per_walker_f.append(None)
                continue
            try:
                m_w = pymbar.MBAR(u_kn[:, sl], Nk_w, initialize="BAR")
                fw = np.asarray(m_w.f_k, dtype=float)
                per_walker_f.append(((fw - fw[0]) * kT).tolist())
            except Exception:  # noqa: BLE001
                per_walker_f.append(None)
    else:
        per_walker_f = [None] * len(walkers)

    # ---- verdict ------------------------------------------------------------
    f_k_kj = (f_k * kT).tolist()
    reasons: list[str] = []
    if unvisited:
        reasons.append(f"rungs {unvisited} never visited: insert rungs or lengthen the run")
    max_otf_dev = 0.0
    for ws in walker_summaries:
        otf = ws["on_the_fly_weights_kj_mol"]
        if otf and len(otf) == K:
            dev = max(abs((otf[k] - otf[0]) - f_k_kj[k]) for k in range(K) if k not in unvisited)
            ws["max_dev_from_mbar_kj_mol"] = dev
            max_otf_dev = max(max_otf_dev, dev)
            if dev > weights_tolerance_kj_mol:
                reasons.append(
                    f"walker {ws['label']}: on-the-fly weights differ from MBAR f_k by up to {dev:.1f} kJ/mol"
                )
        if ws["round_trips"] < min_round_trips:
            reasons.append(f"walker {ws['label']}: {ws['round_trips']} round trips (< {min_round_trips})")
        if ws["rung_change_fraction"] < 0.1:
            reasons.append(
                f"walker {ws['label']}: rung change fraction {ws['rung_change_fraction']:.2f} < 0.1; the ladder is too sparse"
            )
    walker_spread = None
    valid = [f for f in per_walker_f if f]
    if len(valid) >= 2:
        arr = np.array(valid)
        walker_spread = float(np.max(np.max(arr, axis=0) - np.min(arr, axis=0)))
        if walker_spread > weights_tolerance_kj_mol:
            reasons.append(f"walkers disagree on f_k by up to {walker_spread:.1f} kJ/mol")
    verdict = "weights_converged" if not reasons else "weights_drifting"

    # ---- sampling convergence: dF(A - B) at T_ref vs time ------------------------
    frame_values: Optional[np.ndarray] = None
    sampling: Optional[dict] = None
    if states is not None:
        sampling, frame_values = _sampling_block(
            walkers, walker_summaries, frame_rows, u_kn=u_kn, k_n=k_n,
            row_elapsed=np.concatenate(elapsed_blocks) if have_time else None,
            row_run=np.concatenate(run_blocks), K=K, kT=kT, states=states,
            rmsd_selection=rmsd_selection, align_selection=align_selection, reference_pdb=reference_pdb,
            drift_tolerance_kj_mol=drift_tolerance_kj_mol, n_time_points=n_time_points,
            out_dir=out_dir, output_name=output_name, pymbar=pymbar, warnings=warnings,
        )

    # ---- outputs --------------------------------------------------------------
    with open(out_dir / "weights.json", "w") as fh:
        json.dump([float(x) for x in f_k_kj], fh)

    frames_info: dict[str, Any] = {"n_frames": 0}
    if frame_rows:
        rows_idx = np.array([fr["pooled_row"] for fr in frame_rows], dtype=np.int64)
        lw = log_w_rows[rows_idx]
        wf = np.exp(lw - np.max(lw))
        wf /= wf.sum()
        frames_info = {
            "n_frames": len(frame_rows),
            "ess_reference": float(1.0 / np.sum(wf ** 2)),
            "csv": str(out_dir / f"{output_name}_frames.csv"),
        }
        with open(out_dir / f"{output_name}_frames.csv", "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["walker", "node_id", "frame", "chain_frame", "step", "time_ns", "rung",
                         "temperature_K", "log_weight", "weight"]
                        + (["rmsd_nm"] if frame_values is not None else []))
            for i, (fr, logw, w) in enumerate(zip(frame_rows, lw, wf)):
                extra = []
                if frame_values is not None:
                    extra = ["" if not np.isfinite(frame_values[i]) else f"{frame_values[i]:.5f}"]
                wr.writerow([fr["walker"], fr["node_id"], fr["frame"], fr["chain_frame"], fr["step"],
                             "" if fr["time_ns"] is None else f"{fr['time_ns']:.6f}", fr["rung"],
                             f"{fr['temperature_K']:.3f}", f"{float(logw):.6f}", f"{float(w):.6e}"] + extra)
    else:
        warnings.append("no frames table: output_frequency_ps / timestep_fs unknown (direct mode: pass --output-frequency-ps)")

    for ws, fw in zip(walker_summaries, per_walker_f):
        ws["mbar_f_k_kj_mol_this_walker"] = fw
    plot = _plot(out_dir / f"{output_name}.png", walker_summaries, ladder, f_k_kj, f_err * kT, warnings)
    for ws in walker_summaries:
        ws.pop("_rungs", None)
        ws.pop("_times", None)

    return {
        "temperatures_kelvin": ladder,
        "reference_temperature_kelvin": ref_temp,
        "lambdas": [ref_temp / t for t in ladder],
        "fractional_terms": fractions.tolist() if fractions is not None else None,
        "solute_atoms": solute_atoms,
        "mbar": {
            "n_rows": int(u_kn.shape[1]),
            "N_k": N_k.tolist(),
            "f_k_kj_mol": f_k_kj,
            "f_k_err_kj_mol": (f_err * kT).tolist(),
            "f_k_kT": f_k.tolist(),
            "ess_reference_rows": ess_rows,
            "row_stride": row_stride,
            "discard_ns": discard_ns,
            "fixed_weights_only": fixed_weights_only,
            "reduced_potential": "u_m = beta_ref * (sum_f lambda_m^f E_f + sqrt(lambda_m) E_pw); E_ww and the unscaled solute part cancel",
        },
        "walkers": walker_summaries,
        "walker_spread_kj_mol": walker_spread,
        "max_on_the_fly_deviation_kj_mol": max_otf_dev,
        "weights_tolerance_kj_mol": weights_tolerance_kj_mol,
        "min_round_trips": min_round_trips,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "frames": frames_info,
        "sampling": sampling,
        "weights_json": str(out_dir / "weights.json"),
        "plot": plot,
        "next": (
            "fixed-weight stage: create_node --continue-from <leaf prod> and run_sst2 --weights-file "
            f"{out_dir / 'weights.json'}"
            if verdict == "weights_converged"
            else "extend the adaptive stage (continue_from) or fix the ladder, then analyze again"
        ),
    }


def _plot(path: Path, walkers: list[dict], ladder: list[float], f_k_kj: list[float],
          f_err_kj: np.ndarray, warnings: list[str]) -> Optional[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"plot skipped: {exc}")
        return None
    K = len(ladder)
    n = len(walkers)
    fig = plt.figure(figsize=(11, 1.3 * n + 2.6))
    gs = fig.add_gridspec(n, 2, width_ratios=[2.4, 1], hspace=0.15, wspace=0.25)
    has_time = any(ws["_times"] is not None for ws in walkers)
    ax0 = None
    for i, ws in enumerate(walkers):
        ax = fig.add_subplot(gs[i, 0], sharex=ax0)
        ax0 = ax0 or ax
        r = ws["_rungs"]
        t = ws["_times"] if ws["_times"] is not None else np.arange(len(r))
        ax.plot(t, r, lw=0.3, color="#1F77B4", drawstyle="steps-post")
        ax.set_yticks(range(K))
        ax.set_yticklabels([f"{T:.0f}" for T in ladder], fontsize=7)
        ax.set_ylim(-0.5, K - 0.5)
        ax.text(0.01, 0.95, f"{ws['label']}: {ws['round_trips']} round trips", transform=ax.transAxes,
                fontsize=8, va="top")
        if i < n - 1:
            ax.tick_params(labelbottom=False)
    if ax0 is not None:
        fig.axes[-1].set_xlabel("time (ns)" if has_time else "report row")
        fig.axes[0].set_title("rung (K) visited by each walker", fontsize=9)
    ax = fig.add_subplot(gs[:, 1])
    x = np.arange(K)
    ax.axhspan(-2.5, 2.5, color="#E0E0E0", zorder=0)
    ax.errorbar(x, np.zeros(K), yerr=np.nan_to_num(f_err_kj), fmt="o-", color="black", label="MBAR f_k (pooled)", zorder=3)
    colors = ["#1F77B4", "#D95F02", "#7570B3", "#1B9E77", "#E7298A", "#66A61E"]
    for i, ws in enumerate(walkers):
        c = colors[i % len(colors)]
        otf = ws["on_the_fly_weights_kj_mol"]
        if otf and len(otf) == K:
            ax.plot(x, [(v - otf[0]) - f for v, f in zip(otf, f_k_kj)], "s--", ms=4, lw=0.9, color=c,
                    label=f"{ws['label']} on-the-fly")
        own = ws.get("mbar_f_k_kj_mol_this_walker")
        if own:
            ax.plot(x, [v - f for v, f in zip(own, f_k_kj)], "^:", ms=4, lw=0.9, color=c, alpha=0.7,
                    label=f"{ws['label']} MBAR alone")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{T:.0f}" for T in ladder], fontsize=8)
    ax.set_xlabel("rung temperature (K)")
    ax.set_ylabel("deviation from pooled MBAR f_k (kJ/mol)")
    ax.set_title("weights: on-the-fly and per-walker vs pooled", fontsize=9)
    ax.legend(fontsize=6)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return str(path)
