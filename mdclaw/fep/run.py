"""``run_fep`` — sample lambda windows of a hybrid topology (``fep`` node).

Each window is an independent equilibrium simulation of the hybrid System
with its five global parameters fixed at the window's values. Every
``sample_interval_ps`` the potential energy is re-evaluated at *all* windows
of the protocol, giving the reduced-potential matrix ``u_kn`` that
``analyze_fep`` feeds to MBAR. Windows run sequentially inside one node; a
job array covers the protocol with several ``fep`` nodes that each take a
subset via ``--lambda-indices`` and share the same ``eq`` parent.

Per-window artifacts (``artifacts/window_XX/``): ``state.xml`` (restart),
``energies.npz`` (``u_kn`` in kT, times, potential, volume), ``window.json``.
The node artifact ``fep_windows.json`` indexes them; it is rewritten after
every window so a node killed half-way still leaves a usable partial index
(``"complete": false``). For ``fep -> fep`` extension the parent node's
segments are chained into the child's index, so analysis sees the full
sample set without copying files. No artifact carries an absolute path: the
index and each ``window.json`` record paths relative to their own directory,
so the job directory can be moved or analysed from another host.

Starting a window from the equilibrated lambda=0 state: the appearing atoms
were non-interacting ghosts during ``eq``, so solvent may sit on top of them.
Each window therefore minimises briefly *at its own lambda* before the
discarded equilibration segment, which removes those overlaps before the
integrator sees them (windows continued from a parent fep node skip this).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import create_validation_error
from mdclaw._tool_meta import node_tool
from mdclaw.fep.hybrid import FEP_PARAMETERS
from mdclaw.fep.protocol import ProtocolError, load_protocol, parse_lambda_indices
from mdclaw.simulation._base import (
    WORKING_DIR,
    _resolve_topology_run_settings,
    resolve_platform_name,
)

logger = logging.getLogger(__name__)

WINDOWS_SCHEMA_VERSION = 2
# kJ/mol per (bar * nm^3): 1e5 Pa * 1e-27 m^3 * N_A / 1000
_BAR_NM3_TO_KJ_MOL = 0.0602214076
_KB_KJ_MOL_K = 0.008314462618
START_MINIMISATION_ITERATIONS = 200


class FepRunError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Index paths (relative to the index file's directory)                          #
# --------------------------------------------------------------------------- #

def _window_dirname(index: int) -> str:
    return f"window_{index:02d}"


def rel_to(path: str | Path, base: Path) -> str:
    return os.path.relpath(Path(path).resolve(), Path(base).resolve())


def resolve_index_path(rel: Optional[str], index_file: str | Path) -> Optional[Path]:
    """Absolute path of an entry recorded in a ``fep_windows.json``.

    Entries are relative to the index file's directory; absolute entries
    (schema 1) pass through unchanged.
    """
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else (Path(index_file).resolve().parent / p).resolve()


def load_windows_index(path: str | Path) -> dict:
    """Read a ``fep_windows.json`` and resolve its paths to absolute ones.

    Returns the payload with ``windows`` as ``{int index: record}`` where
    ``state_file`` / ``energies_file`` / ``trajectory_file`` and each
    segment's ``energies_file`` are absolute. Raises ``FepRunError``
    (``fep_windows_missing``) when the file cannot be read.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FepRunError(code="fep_windows_missing", message=f"cannot read {path}: {exc}") from exc
    for key in ("fep_protocol_file", "system_xml_file", "topology_pdb_file", "extended_from"):
        if data.get(key):
            data[key] = str(resolve_index_path(data[key], path))
    windows: dict[int, dict] = {}
    for k, record in (data.get("windows") or {}).items():
        record = dict(record)
        for key in ("state_file", "energies_file", "trajectory_file", "restarted_from"):
            if record.get(key):
                record[key] = str(resolve_index_path(record[key], path))
        record["segments"] = [
            {**seg, "energies_file": str(resolve_index_path(seg.get("energies_file"), path))}
            for seg in record.get("segments") or []
        ]
        windows[int(k)] = record
    data["windows"] = windows
    data["index_file"] = str(Path(path).resolve())
    return data


# --------------------------------------------------------------------------- #
# One window                                                                    #
# --------------------------------------------------------------------------- #

def _evaluate_all_windows(context, protocol_windows: list[dict], current: dict[str, float]) -> np.ndarray:
    """Potential energy (kJ/mol) of the current configuration at every window."""
    from openmm import unit

    energies = np.empty(len(protocol_windows), dtype=float)
    for k, window in enumerate(protocol_windows):
        for name, value in window["parameters"].items():
            context.setParameter(name, value)
        energies[k] = context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(
            unit.kilojoule_per_mole)
    for name, value in current.items():
        context.setParameter(name, value)
    return energies


def sample_window(
    *,
    window: dict,
    protocol_windows: list[dict],
    xml_inputs,
    system_factory,
    out_dir: Path,
    restart_state: Optional[Path],
    minimise_start: bool,
    temperature_kelvin: float,
    pressure_bar: Optional[float],
    timestep_fs: float,
    equilibration_time_ns: float,
    sampling_time_ns: float,
    sample_interval_ps: float,
    trajectory_interval_ps: float,
    platform_name: Optional[str],
    platform_properties: dict,
    random_seed: Optional[int],
) -> dict:
    """Run one window and write its artifacts; returns the window record
    (paths absolute — the caller relativises them for the index).

    A NaN during dynamics is retried once per halving of the timestep down
    to 1 fs (:func:`mdclaw.simulation.nan_retry.run_with_halved_timestep`).
    """
    import openmm
    from openmm import unit
    from openmm.app import DCDReporter

    from mdclaw._common import new_simulation
    from mdclaw.simulation.nan_retry import run_with_halved_timestep
    from mdclaw.simulation.restart import _load_state_into_simulation, _save_state_atomic

    out_dir.mkdir(parents=True, exist_ok=True)
    is_periodic = bool(xml_inputs.is_periodic)
    ensemble = "NPT" if (pressure_bar is not None and pressure_bar > 0 and is_periodic) else "NVT"
    params = dict(window["parameters"])
    kT = _KB_KJ_MOL_K * temperature_kelvin
    pv_factor = (pressure_bar * _BAR_NM3_TO_KJ_MOL) if ensemble == "NPT" else 0.0
    n_samples = int(round(sampling_time_ns * 1000.0 / sample_interval_ps))
    if n_samples < 1:
        raise FepRunError(code="invalid_parameter_value",
                          message="sampling_time_ns / sample_interval_ps gives no samples")
    outcome: dict[str, Any] = {}

    def _run(ts_fs: float) -> None:
        system = system_factory()
        if ensemble == "NPT":
            system.addForce(openmm.MonteCarloBarostat(pressure_bar * unit.bar, temperature_kelvin * unit.kelvin, 25))
        integrator = openmm.LangevinMiddleIntegrator(
            temperature_kelvin * unit.kelvin, 1.0 / unit.picosecond, ts_fs * unit.femtoseconds)
        if random_seed is not None:
            integrator.setRandomNumberSeed(int(random_seed) + int(window["index"]) + 1)
        kwargs: dict[str, Any] = {}
        if platform_name:
            kwargs["platform"] = openmm.Platform.getPlatformByName(platform_name)
            if platform_properties:
                kwargs["platformProperties"] = dict(platform_properties)
        simulation = new_simulation(xml_inputs.topology, system, integrator, **kwargs)
        platform_used = simulation.context.getPlatform().getName()

        if restart_state is not None:
            _load_state_into_simulation(
                simulation, restart_state, is_periodic=is_periodic,
                temperature_kelvin=temperature_kelvin, random_seed=random_seed)
        else:
            simulation.context.setPositions(xml_inputs.positions)
            if is_periodic and xml_inputs.box_vectors is not None:
                simulation.context.setPeriodicBoxVectors(*xml_inputs.box_vectors)
            simulation.context.setVelocitiesToTemperature(temperature_kelvin * unit.kelvin)
        for name, value in params.items():
            simulation.context.setParameter(name, value)

        start_min = None
        if minimise_start:
            e0 = simulation.context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            openmm.LocalEnergyMinimizer.minimize(simulation.context, maxIterations=START_MINIMISATION_ITERATIONS)
            e1 = simulation.context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            simulation.context.setVelocitiesToTemperature(temperature_kelvin * unit.kelvin)
            start_min = {"iterations": START_MINIMISATION_ITERATIONS, "before_kj_mol": float(e0), "after_kj_mol": float(e1)}

        steps_per_ps = 1000.0 / ts_fs
        eq_steps = int(round(equilibration_time_ns * 1000.0 * steps_per_ps))
        interval_steps = max(1, int(round(sample_interval_ps * steps_per_ps)))
        t0 = time.time()
        if eq_steps > 0:
            simulation.step(eq_steps)
        if trajectory_interval_ps and trajectory_interval_ps > 0:
            traj_steps = max(1, int(round(trajectory_interval_ps * steps_per_ps)))
            simulation.reporters.append(DCDReporter(str(out_dir / "trajectory.dcd"), traj_steps))

        u_kn = np.empty((len(protocol_windows), n_samples), dtype=float)
        potential = np.empty(n_samples, dtype=float)
        volume = np.empty(n_samples, dtype=float)
        times_ps = np.empty(n_samples, dtype=float)
        for n in range(n_samples):
            simulation.step(interval_steps)
            energies = _evaluate_all_windows(simulation.context, protocol_windows, params)
            if not np.all(np.isfinite(energies)):
                raise RuntimeError(f"non-finite energy (NaN) at window {window['index']} sample {n}")
            box = simulation.context.getState().getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)
            vol = float(abs(np.linalg.det(box))) if is_periodic else 0.0
            u_kn[:, n] = (energies + pv_factor * vol) / kT
            potential[n] = energies[window["index"]]
            volume[n] = vol
            times_ps[n] = (eq_steps + (n + 1) * interval_steps) / steps_per_ps
        simulation.reporters.clear()  # DCDReporter closes its file on garbage collection
        wall = time.time() - t0

        state_file = out_dir / "state.xml"
        _save_state_atomic(simulation, state_file)
        energies_file = out_dir / "energies.npz"
        np.savez_compressed(energies_file, u_kn=u_kn, time_ps=times_ps, potential_kj_mol=potential,
                            volume_nm3=volume, lambda_index=int(window["index"]))
        total_steps = eq_steps + n_samples * interval_steps
        outcome.update({
            "index": int(window["index"]),
            "lambda": window.get("lambda"),
            "parameters": params,
            "ensemble": ensemble,
            "platform": platform_used,
            "restarted_from": str(restart_state) if restart_state else None,
            "start_minimisation": start_min,
            "timestep_fs": float(ts_fs),
            "equilibration_time_ns": float(equilibration_time_ns),
            "sampling_time_ns": float(n_samples * sample_interval_ps / 1000.0),
            "sample_interval_ps": float(sample_interval_ps),
            "n_samples": int(n_samples),
            "steps": int(total_steps),
            "wall_time_s": round(wall, 1),
            "ns_per_day": round(total_steps / steps_per_ps / 1000.0 / wall * 86400.0, 2) if wall > 0 else None,
            "mean_potential_kj_mol": float(potential.mean()),
            "mean_volume_nm3": float(volume.mean()) if is_periodic else None,
            "state_file": str(state_file),
            "energies_file": str(energies_file),
            "trajectory_file": str(out_dir / "trajectory.dcd") if trajectory_interval_ps else None,
        })
        del simulation, integrator, system

    retry = run_with_halved_timestep(f"fep window {window['index']}", timestep_fs, _run, log=logger)
    outcome["nan_retry"] = {"retried": retry["retried"], "attempts": retry["attempts"]} if retry["retried"] else None
    # window.json follows the same convention as the index: paths relative to
    # the file's own directory. The returned record keeps absolute paths for
    # the caller, which relativises them against the index.
    on_disk = {**outcome, **{k: rel_to(outcome[k], out_dir)
                             for k in ("state_file", "energies_file", "trajectory_file", "restarted_from")
                             if outcome.get(k)}}
    (out_dir / "window.json").write_text(json.dumps(on_disk, indent=2))
    return outcome


# --------------------------------------------------------------------------- #
# Tool                                                                          #
# --------------------------------------------------------------------------- #

def _write_index(index_file: Path, payload: dict, windows: dict[int, dict], *, complete: bool) -> None:
    base = index_file.parent
    rel_windows = {}
    for i, record in sorted(windows.items()):
        rec = dict(record)
        for key in ("state_file", "energies_file", "trajectory_file", "restarted_from"):
            if rec.get(key):
                rec[key] = rel_to(rec[key], base)
        rec["segments"] = [{**seg, "energies_file": rel_to(seg["energies_file"], base)} for seg in rec["segments"]]
        rel_windows[str(i)] = rec
    index_file.write_text(json.dumps({**payload, "complete": complete, "windows": rel_windows}, indent=2))


@node_tool(node_type="fep")
def run_fep(
    lambda_indices: Optional[str] = None,
    sampling_time_ns: float = 1.0,
    equilibration_time_ns: float = 0.1,
    sample_interval_ps: float = 1.0,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = None,
    timestep_fs: Optional[float] = None,
    hmr: Optional[bool] = None,
    trajectory_interval_ps: float = 0.0,
    platform: str = "auto",
    device_index: Optional[str] = None,
    random_seed: Optional[int] = None,
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    fep_protocol_file: Optional[str] = None,
    restart_from: Optional[str] = None,
    restart_windows_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Sample one or more lambda windows of a hybrid topology.

    Node mode (``fep`` node under an ``eq`` or ``fep`` parent) resolves the
    hybrid XML triple, ``fep_protocol.json`` and the equilibrated restart
    state from the DAG. With a ``fep`` parent the same windows continue from
    that node's per-window states and the new samples are chained to the
    parent's (extension); pass ``--lambda-indices`` for a subset of *those*
    windows. To recover the windows of a failed / killed fep node, create a
    new fep node under the same ``eq`` parent and pass its partial index as
    ``--restart-windows-file``: windows it contains are continued, the
    others start from the eq state. Parent windows that are not re-sampled are
    carried into the new index unchanged, so the leaf always covers every
    window sampled so far.

    Args:
        lambda_indices: Windows to run: ``"all"`` (default; or the parent
            fep node's set when extending), ``"0-6"``, ``"0,3,7"``.
        sampling_time_ns: Production sampling per window (after
            ``equilibration_time_ns`` of discarded relaxation at that lambda;
            use ``--equilibration-time-ns 0`` when extending).
        sample_interval_ps: Interval between reduced-potential evaluations.
            Every sample costs one energy evaluation per protocol window
            (21 by default), so 1 ps at 4 fs is ~8 % overhead; halving the
            interval doubles that.
        temperature_kelvin / pressure_bar: Ensemble. ``pressure_bar`` omitted
            follows the eq node (NPT pressure, or NVT); ``0`` forces NVT.
        timestep_fs / hmr: Inherited from the topology (4 fs with HMR).
        trajectory_interval_ps: ``> 0`` writes ``trajectory.dcd`` per window
            (off by default; ddG needs only energies).
        platform / device_index / random_seed: as ``run_production``.
        system_xml_file / topology_pdb_file / state_xml_file /
            fep_protocol_file / restart_from: explicit inputs outside node
            mode.
        restart_windows_file: ``fep_windows.json`` (complete or partial) whose
            windows are continued instead of starting from ``restart_from``.

    Returns:
        Dict with ``fep_windows`` (index file), per-window ``windows``
        records, ``lambda_indices`` (sampled here), ``carried_over_windows``
        (copied from the parent index), timing, and ``code`` on failure
        (``fep_hybrid_topology_required``, ``fep_lambda_index_invalid``,
        ``fep_protocol_invalid``, ``fep_windows_missing``,
        ``invalid_parameter_value``, ``fep_sampling_failed``). A sampling
        failure also reports ``fep_windows`` (the partial index, always on
        disk), ``indexed_windows`` (every window that index lists) and
        ``sampled_windows`` (the subset this node finished).
    """
    from mdclaw._node import fail_tool

    result: dict = {"success": False, "tool": "run_fep", "errors": [], "warnings": [], "windows": []}
    node_mode = bool(job_dir and node_id)

    def _fail(code: str, message: str, **extra) -> dict:
        extra = {k: v for k, v in extra.items() if k not in ("success", "code", "message", "errors", "warnings")}
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id, extra=extra or None)

    # --- argument checks that must not spend the node ---------------------
    for name, value, lo in (("sampling_time_ns", sampling_time_ns, 0.0), ("sample_interval_ps", sample_interval_ps, 0.0),
                            ("equilibration_time_ns", equilibration_time_ns, -1e-12), ("temperature_kelvin", temperature_kelvin, 0.0)):
        if not isinstance(value, (int, float)) or value <= lo:
            return _fail(code="invalid_parameter_value", message=f"{name} must be > {max(lo, 0.0):g}, got {value!r}")
    if int(round(sampling_time_ns * 1000.0 / sample_interval_ps)) < 1:
        return _fail(code="invalid_parameter_value", message=
                     f"sampling_time_ns={sampling_time_ns} at sample_interval_ps={sample_interval_ps} gives no samples")
    try:
        platform_name, platform_properties = resolve_platform_name(platform, device_index)
    except ValueError as exc:
        return _fail(code="invalid_parameter_value", message=str(exc))

    # --- DAG resolution ---------------------------------------------------
    eq_final_ensemble = eq_pressure_bar = topology_hmr = None
    if node_mode:
        from mdclaw._node import begin_node, resolve_node_inputs, validate_node_execution_context

        inputs = resolve_node_inputs(job_dir, node_id, "fep")
        if "input_resolution_error" in inputs:
            # Nothing ran: the node stays pending (same as analyze_fep) so the
            # DAG can be fixed and the same node run again.
            err = inputs["input_resolution_error"]
            code = inputs.get("input_resolution_code") or "input_resolution_blocked"
            return _fail(
                code=code, message=err,
                **create_validation_error(
                    "job_dir/node_id", err,
                    expected="fep node under an eq (or one fep) node whose topo was built by build_hybrid_system",
                    actual=f"job_dir={job_dir}, node_id={node_id}",
                    context_extra={"input_resolution_errors": inputs.get("input_resolution_errors", [])},
                    code=code,
                ),
            )
        system_xml_file = system_xml_file or inputs.get("system_xml_file")
        topology_pdb_file = topology_pdb_file or inputs.get("topology_pdb_file")
        state_xml_file = state_xml_file or inputs.get("state_xml_file")
        fep_protocol_file = fep_protocol_file or inputs.get("fep_protocol_file")
        restart_from = restart_from or inputs.get("restart_from")
        restart_windows_file = restart_windows_file or inputs.get("fep_parent_windows_file")
        eq_final_ensemble = inputs.get("eq_final_ensemble")
        eq_pressure_bar = inputs.get("eq_pressure_bar")
        topology_hmr = inputs.get("topology_hmr")
    hmr, _implicit, timestep_fs = _resolve_topology_run_settings(
        hmr=hmr, implicit_solvent=None, topology_hmr=topology_hmr, timestep_fs=timestep_fs)
    if not (system_xml_file and topology_pdb_file and fep_protocol_file):
        return _fail(code="fep_hybrid_topology_required", message=
                     "the hybrid XML triple and fep_protocol.json are required "
                     "(node mode under build_hybrid_system, or explicit paths)")

    # --- protocol, parent windows, window selection -----------------------
    parent_windows: dict[int, dict] = {}
    parent_index_meta: dict = {}
    try:
        protocol = load_protocol(fep_protocol_file)
        protocol_windows = protocol["windows"]
        if restart_windows_file:
            parent_index_meta = load_windows_index(restart_windows_file)
            parent_windows = parent_index_meta["windows"]
            if parent_index_meta.get("n_protocol_windows") not in (None, len(protocol_windows)):
                raise FepRunError(code="fep_windows_incompatible",
                                  message=f"{restart_windows_file} was sampled with {parent_index_meta.get('n_protocol_windows')} "
                                  f"protocol windows, this topology has {len(protocol_windows)}")
        extending = bool(node_mode and inputs.get("fep_parent_windows_file"))
        if lambda_indices in (None, "", "all") and extending:
            # fep -> fep extension: the parent's set, unless the user narrows it.
            indices = sorted(parent_windows)
        else:
            indices = parse_lambda_indices(lambda_indices, len(protocol_windows))
        # Only windows that are continued need a restart state; carried-over
        # windows need nothing beyond their energies (checked by analyze_fep).
        for i in indices:
            rec = parent_windows.get(i)
            if rec is not None and not (rec.get("state_file") and Path(rec["state_file"]).is_file()):
                raise FepRunError(code="fep_windows_missing",
                                  message=f"window {i} of {restart_windows_file} has no restart state.xml; "
                                  "it cannot be continued (drop it from --lambda-indices to carry it over as is)")
        if extending:
            outside = [i for i in indices if i not in parent_windows]
            if outside:
                raise FepRunError(code="fep_lambda_index_invalid",
                                  message=f"windows {outside} are not in the parent fep node "
                                  f"({sorted(parent_windows)}); a fep -> fep child extends its parent's windows only. "
                                  "Sample new windows in a sibling node under the eq parent.")
    except (ProtocolError, FepRunError) as exc:
        return _fail(exc.code, str(exc))

    # --- hybrid System ----------------------------------------------------
    from mdclaw.simulation.xml_contract import _deserialize_xml_system, _load_xml_topology_inputs

    try:
        xml_text = Path(system_xml_file).read_text()
        missing = [p for p in FEP_PARAMETERS if f'name="{p}"' not in xml_text]
        if missing:
            raise FepRunError(code="fep_hybrid_topology_required",
                              message=f"system.xml is not a hybrid topology (missing global parameters {missing})")
        del xml_text
        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=system_xml_file, topology_pdb_file=topology_pdb_file, state_xml_file=state_xml_file)
    except FepRunError as exc:
        return _fail(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(code="fep_hybrid_topology_required", message=f"could not load the hybrid XML triple: {type(exc).__name__}: {exc}")

    # --- ensemble -------------------------------------------------------
    if pressure_bar is None:
        if eq_final_ensemble == "NPT":
            pressure_bar = float(eq_pressure_bar) if eq_pressure_bar else 1.0
        elif eq_final_ensemble == "NVT":
            pressure_bar = None
        elif parent_index_meta.get("pressure_bar") is not None or parent_index_meta.get("ensemble"):
            pressure_bar = parent_index_meta.get("pressure_bar")
        else:
            pressure_bar = 1.0
            result["warnings"].append("eq ensemble unknown; sampling NPT at 1 bar (pass --pressure-bar 0 for NVT).")
    if pressure_bar is not None and pressure_bar <= 0:
        pressure_bar = None
    if pressure_bar is not None and not xml_inputs.is_periodic:
        pressure_bar = None  # vacuum: no box to couple a barostat to
    if parent_index_meta and (parent_index_meta.get("pressure_bar") or None) != pressure_bar:
        return _fail(code="fep_windows_incompatible",
                     message=f"the parent windows were sampled at pressure_bar={parent_index_meta.get('pressure_bar')} "
                     f"({parent_index_meta.get('ensemble')}), this node would use {pressure_bar}; "
                     "MBAR cannot pool different ensembles — match --pressure-bar")
    parent_t = parent_index_meta.get("temperature_kelvin")
    if parent_t is not None and abs(float(parent_t) - float(temperature_kelvin)) > 1e-6:
        return _fail(code="fep_windows_incompatible",
                     message=f"the parent windows were sampled at {parent_t} K, this node would use {temperature_kelvin} K; "
                     "one index chains one temperature — match --temperature-kelvin")
    ensemble = "NPT" if pressure_bar else "NVT"

    # --- node context -----------------------------------------------------
    if node_mode:
        from mdclaw._node import fail_node_from_result

        ctx = validate_node_execution_context(
            job_dir, node_id, "fep",
            actual_conditions={
                # Echo the user's own spelling ("0-6") so a declared condition
                # written the same way matches; the resolved list is recorded
                # in metadata.
                "lambda_indices": (str(lambda_indices) if lambda_indices not in (None, "", "all")
                                   else ",".join(str(i) for i in indices)),
                "sampling_time_ns": sampling_time_ns,
                "equilibration_time_ns": equilibration_time_ns,
                "sample_interval_ps": sample_interval_ps,
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "timestep_fs": timestep_fs,
                "hmr": hmr,
                "platform": platform,
                "random_seed": random_seed,
            },
        )
        if not ctx["success"]:
            return fail_node_from_result(
                job_dir, node_id, {"success": False, "error_type": "ValidationError", **ctx},
                default_error="run_fep node execution context invalid")
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        from mdclaw._common import create_unique_subdir

        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "fep").resolve()
    result["output_dir"] = str(out_dir)

    eq_restart = Path(restart_from) if restart_from else None
    if eq_restart is not None and not eq_restart.is_file():
        return _fail(code="file_not_found", message=f"restart state not found: {eq_restart}")
    if eq_restart is None and not parent_windows:
        result["warnings"].append(
            "No equilibrated restart state: windows start from the topology state.xml with fresh velocities.")
    fresh = [i for i in indices if i not in parent_windows]
    if fresh and equilibration_time_ns < 0.05:
        result["warnings"].append(
            f"windows {fresh} start from the eq state and are minimised at their lambda first, which removes the "
            f"thermal energy of the whole box; equilibration_time_ns={equilibration_time_ns} leaves little time to "
            "re-heat before sampling. Use >= 0.1 ns for windows that do not continue a fep parent.")

    # --- sample ---------------------------------------------------------
    index_file = out_dir / "fep_windows.json"
    index_payload = {
        "schema_version": WINDOWS_SCHEMA_VERSION,
        "node_id": node_id,
        "fep_protocol_file": rel_to(fep_protocol_file, out_dir),
        "system_xml_file": rel_to(xml_inputs.system_xml_path, out_dir),
        "topology_pdb_file": rel_to(xml_inputs.topology_pdb_path, out_dir),
        "n_protocol_windows": len(protocol_windows),
        "lambda_indices": indices,
        "temperature_kelvin": float(temperature_kelvin),
        "pressure_bar": pressure_bar,
        "ensemble": ensemble,
        "timestep_fs": float(timestep_fs),
        "hmr": bool(hmr),
        "extended_from": rel_to(parent_index_meta["index_file"], out_dir) if parent_index_meta else None,
        # Parent windows this node does not re-sample are copied into its index
        # unchanged, so the leaf of a chain always lists every window sampled
        # so far (a partial recovery or a narrowed extension stays analysable
        # from the leaf alone).
        "carried_over_windows": sorted(i for i in parent_windows if i not in indices),
    }
    windows_index: dict[int, dict] = {i: dict(rec) for i, rec in parent_windows.items() if i not in indices}
    # Written before the first window and after every window: whatever is
    # in the index (carried-over or freshly sampled) is on disk when a window
    # fails, so the failure result never points at a file that does not exist.
    _write_index(index_file, index_payload, windows_index, complete=False)
    t_start = time.time()
    for i in indices:
        window = protocol_windows[i]
        parent = parent_windows.get(i)
        restart_state = Path(parent["state_file"]) if parent else eq_restart
        logger.info("run_fep: window %d/%d (lambda=%s)%s", i, len(protocol_windows), window.get("lambda"),
                    " continuing" if parent else "")
        try:
            record = sample_window(
                window=window, protocol_windows=protocol_windows, xml_inputs=xml_inputs,
                system_factory=lambda: _deserialize_xml_system(xml_inputs), out_dir=out_dir / _window_dirname(i),
                restart_state=restart_state, minimise_start=parent is None,
                temperature_kelvin=temperature_kelvin, pressure_bar=pressure_bar, timestep_fs=timestep_fs,
                equilibration_time_ns=equilibration_time_ns, sampling_time_ns=sampling_time_ns,
                sample_interval_ps=sample_interval_ps, trajectory_interval_ps=trajectory_interval_ps,
                platform_name=platform_name, platform_properties=platform_properties, random_seed=random_seed,
            )
        except Exception as exc:  # noqa: BLE001
            code = exc.code if isinstance(exc, FepRunError) else "fep_sampling_failed"
            # indexed_windows = everything the partial index lists (carried-over
            # plus sampled here); sampled_windows = the ones this node ran.
            return _fail(code, f"window {i} failed: {type(exc).__name__}: {exc}",
                         indexed_windows=sorted(windows_index),
                         sampled_windows=[w["index"] for w in result["windows"]],
                         fep_windows=str(index_file))
        segments = list(parent.get("segments", [])) if parent else []
        segments.append({
            "node_id": node_id, "energies_file": record["energies_file"],
            "n_samples": record["n_samples"], "sampling_time_ns": record["sampling_time_ns"],
            "equilibration_time_ns": record["equilibration_time_ns"],
        })
        windows_index[i] = {**record, "segments": segments,
                            "total_sampling_time_ns": round(sum(s["sampling_time_ns"] for s in segments), 6)}
        result["windows"].append(record)
        result["platform"] = record["platform"]
        # Partial index after every window: a node killed by the scheduler
        # still leaves its finished windows recoverable (--restart-windows-file).
        _write_index(index_file, index_payload, windows_index, complete=False)

    _write_index(index_file, index_payload, windows_index, complete=True)
    retried = [w["index"] for w in result["windows"] if w.get("nan_retry")]
    if retried:
        result["warnings"].append(f"windows {retried} hit a NaN and were rerun at a halved timestep; "
                                  "their samples are valid but slower")
    result.update({
        "success": True,
        "fep_windows": str(index_file),
        "lambda_indices": indices,
        "n_protocol_windows": len(protocol_windows),
        "ensemble": ensemble,
        "pressure_bar": pressure_bar,
        "temperature_kelvin": float(temperature_kelvin),
        "timestep_fs": float(timestep_fs),
        "hmr": bool(hmr),
        "extended_from_fep": bool(parent_windows),
        "carried_over_windows": index_payload["carried_over_windows"],
        "wall_time_s": round(time.time() - t_start, 1),
    })
    if node_mode:
        from mdclaw._node import complete_node

        complete_node(
            job_dir, node_id,
            artifacts={"fep_windows": "artifacts/fep_windows.json"},
            metadata={
                "tool": "run_fep",
                "lambda_indices": indices,
                "n_protocol_windows": len(protocol_windows),
                "sampling_time_ns": float(sampling_time_ns),
                "equilibration_time_ns": float(equilibration_time_ns),
                "sample_interval_ps": float(sample_interval_ps),
                "temperature_kelvin": float(temperature_kelvin),
                "pressure_bar": pressure_bar,
                "ensemble": ensemble,
                "timestep_fs": float(timestep_fs),
                "hmr": bool(hmr),
                "platform": result.get("platform"),
                "extended_from_fep": bool(parent_windows),
                "carried_over_windows": index_payload["carried_over_windows"],
                "ns_per_day": [w.get("ns_per_day") for w in result["windows"]],
            },
            warnings=result["warnings"],
        )
    return result


__all__ = ["FepRunError", "load_windows_index", "rel_to", "resolve_index_path", "run_fep", "sample_window"]
