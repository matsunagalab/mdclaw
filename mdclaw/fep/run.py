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
The node artifact ``fep_windows.json`` indexes them and, for ``fep -> fep``
extension, chains the parent node's segments so analysis sees the full
sample set without copying files.
"""

from __future__ import annotations

import json
import logging
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
    _fail_node_if_running,
    _resolve_topology_run_settings,
)

logger = logging.getLogger(__name__)

WINDOWS_SCHEMA_VERSION = 1
# kJ/mol per (bar * nm^3): 1e5 Pa * 1e-27 m^3 * N_A / 1000
_BAR_NM3_TO_KJ_MOL = 0.0602214076
_KB_KJ_MOL_K = 0.008314462618


def _window_dirname(index: int) -> str:
    return f"window_{index:02d}"


def _load_parent_windows(path: Optional[str]) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {int(k): v for k, v in (data.get("windows") or {}).items()}


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
    temperature_kelvin: float,
    pressure_bar: Optional[float],
    timestep_fs: float,
    equilibration_time_ns: float,
    sampling_time_ns: float,
    sample_interval_ps: float,
    trajectory_interval_ps: float,
    platform_name: Optional[str],
    device_index: Optional[str],
    random_seed: Optional[int],
) -> dict:
    """Run one window and write its artifacts; returns the window record."""
    import openmm
    from openmm import unit
    from openmm.app import DCDReporter

    from mdclaw._common import new_simulation
    from mdclaw.simulation.restart import _load_state_into_simulation, _save_state_atomic

    out_dir.mkdir(parents=True, exist_ok=True)
    system = system_factory()
    is_periodic = bool(xml_inputs.is_periodic)
    ensemble = "NVT"
    if pressure_bar is not None and pressure_bar > 0 and is_periodic:
        system.addForce(openmm.MonteCarloBarostat(pressure_bar * unit.bar, temperature_kelvin * unit.kelvin, 25))
        ensemble = "NPT"
    integrator = openmm.LangevinMiddleIntegrator(
        temperature_kelvin * unit.kelvin, 1.0 / unit.picosecond, timestep_fs * unit.femtoseconds)
    if random_seed is not None:
        integrator.setRandomNumberSeed(int(random_seed) + int(window["index"]) + 1)
    kwargs: dict[str, Any] = {}
    if platform_name:
        kwargs["platform"] = openmm.Platform.getPlatformByName(platform_name)
        if device_index and platform_name in ("CUDA", "OpenCL"):
            kwargs["platformProperties"] = {"DeviceIndex": str(device_index)}
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
    params = dict(window["parameters"])
    for name, value in params.items():
        simulation.context.setParameter(name, value)

    steps_per_ps = 1000.0 / timestep_fs
    eq_steps = int(round(equilibration_time_ns * 1000.0 * steps_per_ps))
    interval_steps = max(1, int(round(sample_interval_ps * steps_per_ps)))
    n_samples = int(round(sampling_time_ns * 1000.0 / sample_interval_ps))
    if n_samples < 1:
        raise ValueError("sampling_time_ns / sample_interval_ps gives no samples")

    t0 = time.time()
    if eq_steps > 0:
        simulation.step(eq_steps)
    if trajectory_interval_ps and trajectory_interval_ps > 0:
        traj_steps = max(1, int(round(trajectory_interval_ps * steps_per_ps)))
        simulation.reporters.append(DCDReporter(str(out_dir / "trajectory.dcd"), traj_steps))

    kT = _KB_KJ_MOL_K * temperature_kelvin
    u_kn = np.empty((len(protocol_windows), n_samples), dtype=float)
    potential = np.empty(n_samples, dtype=float)
    volume = np.empty(n_samples, dtype=float)
    times_ps = np.empty(n_samples, dtype=float)
    pv_factor = (pressure_bar * _BAR_NM3_TO_KJ_MOL) if ensemble == "NPT" else 0.0
    for n in range(n_samples):
        simulation.step(interval_steps)
        energies = _evaluate_all_windows(simulation.context, protocol_windows, params)
        if not np.all(np.isfinite(energies)):
            raise RuntimeError(f"non-finite energy at window {window['index']} sample {n}")
        state = simulation.context.getState()
        box = state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)
        vol = float(abs(np.linalg.det(box))) if is_periodic else 0.0
        u_kn[:, n] = (energies + pv_factor * vol) / kT
        potential[n] = energies[window["index"]]
        volume[n] = vol
        times_ps[n] = (eq_steps + (n + 1) * interval_steps) / steps_per_ps
    for reporter in simulation.reporters:
        try:
            reporter._out.close()  # noqa: SLF001 - DCDReporter has no public close
        except Exception:  # noqa: BLE001
            pass
    simulation.reporters.clear()
    wall = time.time() - t0

    state_file = out_dir / "state.xml"
    _save_state_atomic(simulation, state_file)
    energies_file = out_dir / "energies.npz"
    np.savez_compressed(energies_file, u_kn=u_kn, time_ps=times_ps, potential_kj_mol=potential,
                        volume_nm3=volume, lambda_index=int(window["index"]))
    record = {
        "index": int(window["index"]),
        "lambda": window.get("lambda"),
        "parameters": params,
        "ensemble": ensemble,
        "platform": platform_used,
        "restarted_from": str(restart_state) if restart_state else None,
        "equilibration_time_ns": float(equilibration_time_ns),
        "sampling_time_ns": float(n_samples * sample_interval_ps / 1000.0),
        "sample_interval_ps": float(sample_interval_ps),
        "n_samples": int(n_samples),
        "steps": int(eq_steps + n_samples * interval_steps),
        "wall_time_s": round(wall, 1),
        "ns_per_day": round((eq_steps + n_samples * interval_steps) / steps_per_ps / 1000.0 / wall * 86400.0, 2)
        if wall > 0 else None,
        "mean_potential_kj_mol": float(potential.mean()),
        "mean_volume_nm3": float(volume.mean()) if is_periodic else None,
        "state_file": str(state_file),
        "energies_file": str(energies_file),
        "trajectory_file": str(out_dir / "trajectory.dcd") if trajectory_interval_ps else None,
    }
    (out_dir / "window.json").write_text(json.dumps(record, indent=2))
    del simulation, integrator, system
    return record


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
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Sample one or more lambda windows of a hybrid topology.

    Node mode (``fep`` node under an ``eq`` or ``fep`` parent) resolves the
    hybrid XML triple, ``fep_protocol.json`` and the equilibrated restart
    state from the DAG. With a ``fep`` parent the same windows continue from
    that node's per-window states and the new samples are chained to the
    parent's (extension); pass ``--lambda-indices`` to sample a subset.

    Args:
        lambda_indices: Windows to run: ``"all"`` (default; or the parent
            fep node's set when extending), ``"0-6"``, ``"0,3,7"``.
        sampling_time_ns: Production sampling per window (after
            ``equilibration_time_ns`` of discarded relaxation at that lambda).
        sample_interval_ps: Interval between reduced-potential evaluations;
            every sample evaluates the energy at all windows.
        temperature_kelvin / pressure_bar: Ensemble. ``pressure_bar`` omitted
            inherits the eq node's NPT pressure (1 bar default for periodic
            systems); ``0`` forces NVT.
        timestep_fs / hmr: Inherited from the topology (4 fs with HMR).
        trajectory_interval_ps: ``> 0`` writes ``trajectory.dcd`` per window
            (off by default; ddG needs only energies).
        platform / device_index / random_seed: as ``run_production``.
        system_xml_file / topology_pdb_file / state_xml_file /
            fep_protocol_file / restart_from: explicit inputs outside node
            mode.

    Returns:
        Dict with ``fep_windows`` (index file), per-window ``windows``
        records, ``lambda_indices``, timing, and ``code`` on failure
        (``fep_hybrid_topology_required``, ``fep_lambda_index_invalid``,
        ``fep_protocol_invalid``, ``fep_sampling_failed``).
    """
    result: dict = {
        "success": False,
        "tool": "run_fep",
        "errors": [],
        "warnings": [],
        "windows": [],
    }
    _node_mode = bool(job_dir and node_id)
    parent_windows: dict[int, dict] = {}
    eq_final_ensemble = None
    eq_pressure_bar = None
    topology_hmr = None
    if _node_mode:
        from mdclaw._node import resolve_node_inputs, validate_node_execution_context

        inputs = resolve_node_inputs(job_dir, node_id, "fep")
        if "input_resolution_error" in inputs:
            err = inputs["input_resolution_error"]
            code = "fep_hybrid_topology_required" if "fep_hybrid_topology_required" in err else "input_resolution_blocked"
            from mdclaw._node import begin_node, fail_node

            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=[err])
            return create_validation_error(
                "job_dir/node_id", err,
                expected="fep node under an eq (or fep) node whose topo was built by build_hybrid_system",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={"input_resolution_errors": inputs.get("input_resolution_errors", [])},
                code=code,
            )
        system_xml_file = system_xml_file or inputs.get("system_xml_file")
        topology_pdb_file = topology_pdb_file or inputs.get("topology_pdb_file")
        state_xml_file = state_xml_file or inputs.get("state_xml_file")
        fep_protocol_file = fep_protocol_file or inputs.get("fep_protocol_file")
        restart_from = restart_from or inputs.get("restart_from")
        parent_windows = _load_parent_windows(inputs.get("fep_parent_windows_file"))
        eq_final_ensemble = inputs.get("eq_final_ensemble")
        eq_pressure_bar = inputs.get("eq_pressure_bar")
        topology_hmr = inputs.get("topology_hmr")
    hmr, _implicit, timestep_fs = _resolve_topology_run_settings(
        hmr=hmr, implicit_solvent=None, topology_hmr=topology_hmr, timestep_fs=timestep_fs)

    if not (system_xml_file and topology_pdb_file and fep_protocol_file):
        return _fail_node_if_running(job_dir, node_id, {
            **result, **create_validation_error(
                "system_xml_file/topology_pdb_file/fep_protocol_file",
                "the hybrid XML triple and fep_protocol.json are required",
                expected="node mode under build_hybrid_system, or explicit paths",
                code="fep_hybrid_topology_required"),
        })
    try:
        protocol = load_protocol(fep_protocol_file)
        protocol_windows = protocol["windows"]
        if lambda_indices is None and parent_windows:
            indices = sorted(parent_windows)
        else:
            indices = parse_lambda_indices(lambda_indices, len(protocol_windows))
    except ProtocolError as exc:
        return _fail_node_if_running(job_dir, node_id, {
            **result, **create_validation_error("lambda_indices", str(exc), code=exc.code)})

    if pressure_bar is None:
        pressure_bar = float(eq_pressure_bar) if (eq_final_ensemble == "NPT" and eq_pressure_bar) else 1.0
    if pressure_bar is not None and pressure_bar <= 0:
        pressure_bar = None

    if _node_mode:
        from mdclaw._node import begin_node, fail_node_from_result

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

        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "fep")
    result["output_dir"] = str(out_dir)

    from mdclaw.simulation.xml_contract import _deserialize_xml_system, _load_xml_topology_inputs

    try:
        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=system_xml_file, topology_pdb_file=topology_pdb_file, state_xml_file=state_xml_file)
        probe = _deserialize_xml_system(xml_inputs)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"could not load the hybrid XML triple: {type(exc).__name__}: {exc}")
        result["code"] = "fep_hybrid_topology_required"
        return _fail_node_if_running(job_dir, node_id, result)
    globals_present = set()
    for force in probe.getForces():
        if hasattr(force, "getNumGlobalParameters"):
            globals_present.update(force.getGlobalParameterName(i) for i in range(force.getNumGlobalParameters()))
    missing = [p for p in FEP_PARAMETERS if p not in globals_present]
    if missing:
        result["errors"].append(f"system.xml is not a hybrid topology (missing global parameters {missing})")
        result["code"] = "fep_hybrid_topology_required"
        return _fail_node_if_running(job_dir, node_id, result)
    del probe

    platform_name = None
    if platform and platform.lower() != "auto":
        names = {"cuda": "CUDA", "opencl": "OpenCL", "cpu": "CPU", "reference": "Reference"}
        platform_name = names.get(platform.lower())
        if platform_name is None:
            result["errors"].append(f"Unknown platform '{platform}' (auto, CUDA, OpenCL, CPU, Reference)")
            result["code"] = "invalid_parameter_value"
            return _fail_node_if_running(job_dir, node_id, result)

    def _system_factory():
        return _deserialize_xml_system(xml_inputs)

    eq_restart = Path(restart_from) if restart_from else None
    if eq_restart is not None and not eq_restart.is_file():
        result["errors"].append(f"restart state not found: {eq_restart}")
        result["code"] = "file_not_found"
        return _fail_node_if_running(job_dir, node_id, result)
    if eq_restart is None and not parent_windows:
        result["warnings"].append(
            "No equilibrated restart state: windows start from the topology state.xml with fresh velocities.")

    windows_index: dict[str, dict] = {}
    t_start = time.time()
    for i in indices:
        window = protocol_windows[i]
        parent = parent_windows.get(i)
        restart_state = Path(parent["state_file"]) if parent and parent.get("state_file") else eq_restart
        if parent and not (parent.get("state_file") and Path(parent["state_file"]).is_file()):
            result["warnings"].append(f"window {i}: parent fep state missing; restarting from the eq state")
            restart_state = eq_restart
        logger.info("run_fep: window %d/%d (lambda=%s)", i, len(protocol_windows), window.get("lambda"))
        try:
            record = sample_window(
                window=window, protocol_windows=protocol_windows, xml_inputs=xml_inputs,
                system_factory=_system_factory, out_dir=out_dir / _window_dirname(i),
                restart_state=restart_state, temperature_kelvin=temperature_kelvin,
                pressure_bar=pressure_bar, timestep_fs=timestep_fs,
                equilibration_time_ns=equilibration_time_ns, sampling_time_ns=sampling_time_ns,
                sample_interval_ps=sample_interval_ps, trajectory_interval_ps=trajectory_interval_ps,
                platform_name=platform_name, device_index=device_index, random_seed=random_seed,
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"window {i} failed: {type(exc).__name__}: {exc}")
            result["code"] = "fep_sampling_failed"
            result["completed_windows"] = sorted(int(k) for k in windows_index)
            return _fail_node_if_running(job_dir, node_id, result)
        segments = list(parent.get("segments", [])) if parent else []
        segments.append({
            "node_id": node_id, "energies_file": record["energies_file"],
            "n_samples": record["n_samples"], "sampling_time_ns": record["sampling_time_ns"],
            "equilibration_time_ns": record["equilibration_time_ns"],
        })
        windows_index[str(i)] = {**record, "segments": segments,
                                 "total_sampling_time_ns": round(sum(s["sampling_time_ns"] for s in segments), 6)}
        result["windows"].append(record)
        result["platform"] = record["platform"]

    index_payload = {
        "schema_version": WINDOWS_SCHEMA_VERSION,
        "node_id": node_id,
        "fep_protocol_file": str(Path(fep_protocol_file).resolve()),
        "system_xml_file": str(xml_inputs.system_xml_path),
        "topology_pdb_file": str(xml_inputs.topology_pdb_path),
        "n_protocol_windows": len(protocol_windows),
        "lambda_indices": indices,
        "temperature_kelvin": float(temperature_kelvin),
        "pressure_bar": pressure_bar,
        "ensemble": "NPT" if pressure_bar else "NVT",
        "timestep_fs": float(timestep_fs),
        "hmr": bool(hmr),
        "windows": windows_index,
    }
    index_file = out_dir / "fep_windows.json"
    index_file.write_text(json.dumps(index_payload, indent=2))
    result.update({
        "success": True,
        "fep_windows": str(index_file),
        "lambda_indices": indices,
        "n_protocol_windows": len(protocol_windows),
        "ensemble": index_payload["ensemble"],
        "pressure_bar": pressure_bar,
        "temperature_kelvin": float(temperature_kelvin),
        "timestep_fs": float(timestep_fs),
        "hmr": bool(hmr),
        "wall_time_s": round(time.time() - t_start, 1),
    })
    if _node_mode:
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
                "ensemble": index_payload["ensemble"],
                "timestep_fs": float(timestep_fs),
                "hmr": bool(hmr),
                "platform": result.get("platform"),
                "extended_from_fep": bool(parent_windows),
                "ns_per_day": [w.get("ns_per_day") for w in result["windows"]],
            },
            warnings=result["warnings"],
        )
    return result


__all__ = ["run_fep", "sample_window"]
