"""Well-tempered metadynamics on a centre-of-mass distance, as a production node.

``run_metadynamics`` runs one walker of well-tempered metadynamics (Barducci,
Bussi, Parrinello 2008) with OpenMM's built-in ``openmm.app.metadynamics``
machinery on the ``system.xml`` / ``topology.pdb`` / ``state.xml`` triple of
the topo ancestor.  The collective variable is the same mass-weighted
centre-of-mass distance as ``run_production --distance-restraints``
(``restraints.resolve_centroid_groups``), so metadynamics, umbrella windows
and plain MD of one system measure the same coordinate.  Gaussians of height
``bias_height_kj_mol`` (scaled down by the well-tempered factor as the bias
grows) and width ``bias_width_nm`` are deposited every
``deposition_interval_ps``; harmonic walls keep the coordinate inside the
bias grid ``[cv_min_nm, cv_max_nm]``.

One walker is one ``prod`` node.  Several walkers share their bias through
``bias_dir`` (a directory outside the nodes, typically under the study): each
walker writes its own bias file there and loads the others' files whenever it
saves, which is OpenMM's multiple-walker mechanism.  Without ``bias_dir`` the
walker is alone; ``--continue-from`` a completed ``run_metadynamics`` node then
starts from the parent's total bias.  The free energy along the coordinate is
``F = -(T + dT)/dT * V(s)`` from the total bias and is written as
``free_energy.csv``; the walker's own and the total bias grids are kept as
``.npy`` artifacts.
"""
# Configure logging early to suppress noisy third-party logs
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import new_simulation, setup_logger  # noqa: E402
logger = setup_logger(__name__)

import json  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import create_unique_subdir  # noqa: E402
from mdclaw._tool_meta import node_tool  # noqa: E402
from mdclaw.simulation._base import (  # noqa: E402
    _node_artifact_path,
    _resolve_topology_run_settings,
)
from mdclaw.simulation.custom_forces import (  # noqa: E402
    CUSTOM_FORCE_GROUP,
    CustomForceReporter,
    write_cv_metadata,
)
from mdclaw.simulation.restraints import (  # noqa: E402
    DistanceRestraintError,
    distance_cv_periodicity,
    normalize_distance_restraints,
    resolve_centroid_groups,
)
from mdclaw.simulation.restart import (  # noqa: E402
    _close_reporter_stream,
    _load_state_into_simulation,
    _save_state_atomic,
)
from mdclaw.simulation.xml_contract import (  # noqa: E402
    WORKING_DIR,
    _deserialize_xml_system,
    _integrator_signature,
    _load_xml_topology_inputs,
    _ModernSystemContractError,
    _system_signature,
    _validate_xml_system_contract,
)

SAMPLING_METHOD = "metadynamics"
_CV_FIELDS = {"name", "selection_group1", "selection_group2"}
MANIFEST_NAME = "metadynamics_manifest.json"
WALL_FORCE_GROUP = 30
# The bias grid reaches this many Gaussian widths beyond the walls, so a Gaussian
# deposited while the wall pushes the walker back lands on the grid instead of
# piling up on the edge point (which read as a spurious free-energy minimum).
GRID_MARGIN_SIGMAS = 4.0


class MetadynamicsToolError(RuntimeError):
    """Structured failure with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------------

def _validate_cv(distance_cv) -> dict:
    """Normalize the CV spec (a distance restraint without k / r0)."""
    if not isinstance(distance_cv, dict):
        raise MetadynamicsToolError(
            code="metadynamics_cv_invalid",
            message="distance_cv must be a JSON object with name, selection_group1 and "
            "selection_group2 (mdtraj selections), e.g. "
            '{"name":"e2e","selection_group1":"resname ACE and name CH3",'
            '"selection_group2":"resname NME and name C"}.',
        )
    missing = _CV_FIELDS - set(distance_cv)
    unknown = set(distance_cv) - _CV_FIELDS
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            parts.append("unknown " + ", ".join(sorted(unknown)))
        raise MetadynamicsToolError(code="metadynamics_cv_invalid",
                                    message="distance_cv has " + "; ".join(parts) + ".")
    try:
        normalized = normalize_distance_restraints([{
            **distance_cv, "force_constant_kj_mol_nm2": 1.0, "target_distance_nm": 0.0,
        }])[0]
    except DistanceRestraintError as exc:
        raise MetadynamicsToolError(code="metadynamics_cv_invalid", message=str(exc)) from exc
    return {k: normalized[k] for k in ("name", "selection_group1", "selection_group2")}


def _positive(name, value, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0 or (value == 0 and not allow_zero):
        raise MetadynamicsToolError(code="metadynamics_parameters_invalid",
                                    message=f"{name} must be a finite positive number, got {value!r}")
    return float(value)


def _validate_grid(cv_min_nm, cv_max_nm, bias_width_nm, grid_points) -> dict:
    lo = _positive("cv_min_nm", cv_min_nm, allow_zero=True)
    hi = _positive("cv_max_nm", cv_max_nm)
    width = _positive("bias_width_nm", bias_width_nm)
    if hi <= lo:
        raise MetadynamicsToolError(code="metadynamics_grid_invalid",
                                    message=f"cv_max_nm ({hi}) must exceed cv_min_nm ({lo}).")
    if width >= (hi - lo) / 2:
        raise MetadynamicsToolError(code="metadynamics_grid_invalid",
                                    message=f"bias_width_nm ({width}) is too wide for the grid [{lo}, {hi}] nm.")
    margin = GRID_MARGIN_SIGMAS * width
    grid_lo, grid_hi = max(0.0, lo - margin), hi + margin
    if grid_points is None:
        points = int(math.ceil(5 * (grid_hi - grid_lo) / width)) + 1
    else:
        points = int(grid_points)
        if points < 10:
            raise MetadynamicsToolError(code="metadynamics_grid_invalid",
                                        message="grid_points must be at least 10.")
    return {"cv_min_nm": lo, "cv_max_nm": hi, "bias_width_nm": width, "grid_points": points,
            "grid_min_nm": grid_lo, "grid_max_nm": grid_hi}


def _manifest(cv, grid, temperature_kelvin, bias_factor, bias_height_kj_mol, deposition_interval_ps) -> dict:
    return {
        "sampling_method": SAMPLING_METHOD,
        "distance_cv": cv,
        **grid,
        "temperature_kelvin": float(temperature_kelvin),
        "bias_factor": float(bias_factor),
        "bias_height_kj_mol": float(bias_height_kj_mol),
        "deposition_interval_ps": float(deposition_interval_ps),
    }


def _check_shared_dir(bias_dir: Path, manifest: dict, *, retry_seconds: float = 30.0) -> dict:
    """Create or verify the manifest of a shared bias directory.

    Walkers packed on one GPU start within the same second, so the manifest
    is written atomically (temp file + rename) and a reader that finds it
    missing or half-written retries for a while before deciding."""
    bias_dir.mkdir(parents=True, exist_ok=True)
    path = bias_dir / MANIFEST_NAME
    if not path.exists():
        tmp = path.with_name(f".{MANIFEST_NAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        try:
            # O_EXCL-like: the first rename wins; a loser sees the winner's file below
            os.link(tmp, path)
            os.unlink(tmp)
            return {"created": True, "path": str(path)}
        except FileExistsError:
            os.unlink(tmp)
    previous = None
    deadline = time.time() + retry_seconds
    while True:
        try:
            previous = json.loads(path.read_text())
            break
        except (ValueError, OSError) as exc:
            if time.time() > deadline:
                raise MetadynamicsToolError(code="metadynamics_shared_bias_mismatch",
                                            message=f"{path} is not readable JSON after {retry_seconds:.0f} s: {exc}") from exc
            time.sleep(0.5)
    if True:
        if json.dumps(previous, sort_keys=True) != json.dumps(manifest, sort_keys=True):
            diff = {k: (previous.get(k), manifest.get(k)) for k in set(previous) | set(manifest)
                    if previous.get(k) != manifest.get(k)}
            raise MetadynamicsToolError(
                code="metadynamics_shared_bias_mismatch",
                message=f"bias_dir {bias_dir} was started with different metadynamics settings: "
                f"{diff}. Every walker sharing a bias directory must use the same CV, grid, width, "
                "height, bias factor, temperature and deposition interval; use another directory.",
            )
        return {"created": False, "path": str(path)}


# ----------------------------------------------------------------------------
# forces
# ----------------------------------------------------------------------------

def _build_cv_force(*, system, topology, cv: dict, is_periodic: bool, max_target_nm: float):
    """The centre-of-mass distance as a CustomCentroidBondForce (for the
    BiasVariable), an evaluator for the CV reporter and group info."""
    import numpy as np
    from openmm import CustomCentroidBondForce

    normalized = normalize_distance_restraints([{**cv, "force_constant_kj_mol_nm2": 1.0,
                                                 "target_distance_nm": 0.0}])
    (group1, weights1, group2, weights2), = resolve_centroid_groups(
        topology, normalized, n_particles=system.getNumParticles()
    )
    periodic = distance_cv_periodicity(
        system=system, topology=topology, groups=[(group1, weights1, group2, weights2)],
        is_periodic=is_periodic, max_target_nm=max_target_nm, label="distance_cv",
    )

    def make():
        inner = CustomCentroidBondForce(2, "distance(g1,g2)")
        inner.setUsesPeriodicBoundaryConditions(periodic)
        g1 = inner.addGroup(group1, weights1)
        g2 = inner.addGroup(group2, weights2)
        inner.addBond([g1, g2], [])
        return inner

    def _evaluator(positions_np, box_np):
        c1 = np.average(positions_np[group1], axis=0, weights=weights1)
        c2 = np.average(positions_np[group2], axis=0, weights=weights2)
        disp = c2 - c1
        if periodic and box_np is not None:
            frac = disp @ np.linalg.inv(box_np)
            disp = disp - np.rint(frac) @ box_np
        return {cv["name"]: float(np.linalg.norm(disp))}

    return make, _evaluator, {"group1_atoms": len(group1), "group2_atoms": len(group2),
                             "minimum_image": periodic}


def _wall_force(inner_force, lo: float, hi: float, k: float):
    """Flat-bottom harmonic walls that keep the coordinate inside the bias grid."""
    from openmm import CustomCVForce

    wall = CustomCVForce("0.5*kw*(step(d-hi)*(d-hi)^2 + step(lo-d)*(lo-d)^2)")
    wall.addCollectiveVariable("d", inner_force)
    wall.addGlobalParameter("kw", float(k))
    wall.addGlobalParameter("lo", float(lo))
    wall.addGlobalParameter("hi", float(hi))
    wall.setForceGroup(WALL_FORCE_GROUP)
    return wall


# ----------------------------------------------------------------------------
# the tool
# ----------------------------------------------------------------------------

@node_tool(node_type="prod")
def run_metadynamics(
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    distance_cv: Optional[dict] = None,
    cv_min_nm: float = 0.3,
    cv_max_nm: float = 2.0,
    bias_width_nm: float = 0.05,
    bias_height_kj_mol: float = 2.5,
    bias_factor: float = 10.0,
    deposition_interval_ps: float = 1.0,
    grid_points: Optional[int] = None,
    wall_force_constant_kj_mol_nm2: float = 1000.0,
    bias_dir: Optional[str] = None,
    save_interval_ps: float = 100.0,
    simulation_time_ns: float = 1.0,
    output_frequency_ps: float = 10.0,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = None,
    timestep_fs: Optional[float] = None,
    restart_bias_file: Optional[str] = None,
    random_seed: Optional[int] = None,
    platform: str = "auto",
    device_index: Optional[str] = None,
    hmr: Optional[bool] = None,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Run one walker of well-tempered metadynamics on a centre-of-mass distance.

    Gaussians are deposited on the coordinate every ``deposition_interval_ps``;
    their height shrinks as the bias grows (well-tempered, ``bias_factor``),
    so the bias converges to ``-(1 - 1/bias_factor) F``.  Harmonic walls hold
    the coordinate inside ``[cv_min_nm, cv_max_nm]``.  Several walkers of one
    system share their bias through ``bias_dir``.

    Args:
        system_xml_file: ``system.xml`` of the topo ancestor (auto-resolved
            in node mode).
        topology_pdb_file: ``topology.pdb`` of the same topo ancestor.
        state_xml_file: State to start from (auto-resolved: the eq or the
            continued prod ``state``).
        distance_cv: JSON object ``{"name", "selection_group1",
            "selection_group2"}``; two disjoint mdtraj selections whose
            centre-of-mass distance is the coordinate (same rules as one
            entry of ``run_production --distance-restraints``).  A distance
            inside one molecule is measured on raw coordinates; between
            molecules it is a minimum-image distance and ``cv_max_nm`` must
            stay below half the box.
        cv_min_nm: Lower edge of the bias grid and of the wall.
        cv_max_nm: Upper edge of the bias grid and of the wall.
        bias_width_nm: Gaussian width (sigma); about the coordinate's
            fluctuation in a free run, 0.02-0.1 nm for a distance.
        bias_height_kj_mol: Initial Gaussian height (default 2.5 kJ/mol,
            about 1 kT at 300 K).
        bias_factor: Well-tempered bias factor gamma (> 1; roughly the
            barrier to cross divided by a few kT, default 10).
        deposition_interval_ps: Interval between Gaussians (default 1 ps).
        grid_points: Bias grid points (default 5 per width plus one).
        wall_force_constant_kj_mol_nm2: Harmonic wall stiffness outside the
            grid (default 1000).
        bias_dir: Shared directory for several walkers (outside the nodes,
            e.g. ``<study_dir>/metadynamics/<label>``); the first walker
            writes a manifest, later walkers must match it.
        save_interval_ps: How often the walker writes its bias to
            ``bias_dir`` and reads the other walkers' (default 100 ps;
            rounded to a multiple of the deposition interval).
        simulation_time_ns: Time to run in this call.
        output_frequency_ps: Trajectory / energy / CV frame interval.
        temperature_kelvin: Temperature (default 300 K).
        pressure_bar: NPT pressure; None or 0 runs NVT.
        timestep_fs: Default 4 fs with HMR, 2 fs otherwise.
        restart_bias_file: Total bias grid (``.npy``) of a previous walker to
            start from (auto-resolved from a ``--continue-from`` parent's
            ``metadynamics_total_bias`` when no ``bias_dir`` is shared).
        random_seed: Seed for the integrator and barostat.
        platform: OpenMM platform (``auto``, ``CUDA``, ``CPU``...).
        device_index: CUDA/OpenCL device index.
        hmr: Inherited from the topo ancestor when omitted.
        name: Output prefix.
        output_dir: Output location outside node mode.
        job_dir: Study job directory (node mode).
        node_id: Node id (node mode).

    Returns:
        dict with ``success``, artifact paths and ``metadynamics`` (grid,
        depositions, coordinate range visited, free-energy minimum and
        range, files loaded from ``bias_dir``).
    """
    result: dict = {
        "success": False,
        "sampling_method": SAMPLING_METHOD,
        "errors": [],
        "warnings": [],
    }
    _node_mode = bool(job_dir and node_id)
    restart_from_node_id = None
    if _node_mode:
        from mdclaw._node import fail_node_from_result, resolve_node_inputs, validate_node_execution_context

        _inputs = resolve_node_inputs(job_dir, node_id, "prod")
        system_xml_file = system_xml_file or _inputs.get("system_xml_file")
        topology_pdb_file = topology_pdb_file or _inputs.get("topology_pdb_file")
        if not state_xml_file:
            state_xml_file = _inputs.get("restart_from") or _inputs.get("state_xml_file")
        restart_from_node_id = _inputs.get("restart_from_node_id")
        hmr, _implicit, timestep_fs = _resolve_topology_run_settings(
            hmr=hmr,
            implicit_solvent=None,
            topology_hmr=_inputs.get("topology_hmr"),
            topology_implicit_solvent=_inputs.get("topology_implicit_solvent"),
            timestep_fs=timestep_fs,
        )
        if restart_bias_file is None and restart_from_node_id and not bias_dir:
            from mdclaw._node import read_node

            parent = read_node(job_dir, restart_from_node_id)
            side = (parent.get("artifacts") or {}).get("metadynamics_total_bias")
            if side:
                restart_bias_file = str(Path(job_dir) / "nodes" / restart_from_node_id / side)
        if _inputs.get("input_resolution_error") or _inputs.get("input_resolution_errors"):
            result["errors"].append(_inputs.get("input_resolution_error") or "; ".join(_inputs.get("input_resolution_errors")))
            result["code"] = "input_resolution_blocked"
            return fail_node_from_result(job_dir, node_id, result)
        _ctx = validate_node_execution_context(
            job_dir, node_id, "prod",
            actual_conditions={
                "sampling_method": SAMPLING_METHOD,
                "simulation_time_ns": simulation_time_ns,
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "ensemble": "NPT" if (pressure_bar is not None and pressure_bar > 0) else "NVT",
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "distance_cv": distance_cv,
                "cv_min_nm": cv_min_nm,
                "cv_max_nm": cv_max_nm,
                "bias_width_nm": bias_width_nm,
                "bias_height_kj_mol": bias_height_kj_mol,
                "bias_factor": bias_factor,
                "deposition_interval_ps": deposition_interval_ps,
                "bias_dir": bias_dir,
                "platform": platform,
                "device_index": device_index,
                "hmr": hmr,
                "random_seed": random_seed,
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}
        result["warnings"].extend(_ctx.get("warnings") or [])
    else:
        hmr, _implicit, timestep_fs = _resolve_topology_run_settings(
            hmr=hmr, implicit_solvent=None, timestep_fs=timestep_fs,
        )

    for label, path in (("system_xml_file", system_xml_file), ("topology_pdb_file", topology_pdb_file)):
        if not path or not Path(path).is_file():
            result["errors"].append(f"{label} is missing: {path!r}")
            result["code"] = "file_not_found"
            if _node_mode:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(job_dir, node_id, result)
            return result

    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "metadynamics")
    result["output_dir"] = str(out_dir)
    pref = f"{name}_" if name else ""

    reporters = []
    cv_reporter = None
    try:
        import numpy as np
        from openmm import LangevinMiddleIntegrator, MonteCarloBarostat, Platform, XmlSerializer
        from openmm.app import DCDReporter, StateDataReporter
        from openmm.app.metadynamics import BiasVariable, Metadynamics
        from openmm.unit import (
            MOLAR_GAS_CONSTANT_R, bar, femtoseconds, kelvin, kilojoule_per_mole, picosecond,
        )

        cv = _validate_cv(distance_cv)
        grid = _validate_grid(cv_min_nm, cv_max_nm, bias_width_nm, grid_points)
        height = _positive("bias_height_kj_mol", bias_height_kj_mol)
        if isinstance(bias_factor, bool) or not isinstance(bias_factor, (int, float)) \
                or not math.isfinite(bias_factor) or bias_factor <= 1.0:
            raise MetadynamicsToolError(code="metadynamics_parameters_invalid",
                                        message=f"bias_factor must be > 1, got {bias_factor!r}")
        dep_ps = _positive("deposition_interval_ps", deposition_interval_ps)
        save_ps = _positive("save_interval_ps", save_interval_ps)
        k_wall = _positive("wall_force_constant_kj_mol_nm2", wall_force_constant_kj_mol_nm2)
        for label, value in (("simulation_time_ns", simulation_time_ns),
                             ("output_frequency_ps", output_frequency_ps)):
            _positive(label, value)
        dep_steps = int(round(dep_ps * 1000.0 / timestep_fs))
        report_interval = int(round(output_frequency_ps * 1000.0 / timestep_fs))
        n_depositions = int(round(simulation_time_ns * 1000.0 / dep_ps))
        if dep_steps <= 0 or n_depositions <= 0 or report_interval <= 0:
            raise MetadynamicsToolError(code="metadynamics_parameters_invalid",
                                        message="deposition_interval_ps / simulation_time_ns / output_frequency_ps "
                                        f"are too short for a {timestep_fs} fs timestep.")
        steps_to_run = n_depositions * dep_steps
        if report_interval > steps_to_run:
            raise MetadynamicsToolError(code="metadynamics_parameters_invalid",
                                        message="output_frequency_ps is longer than this segment; no frame would be written.")
        save_steps = max(dep_steps, int(round(save_ps / dep_ps)) * dep_steps)
        manifest = _manifest(cv, grid, temperature_kelvin, float(bias_factor), height, dep_ps)

        # bias directory: shared (several walkers) or private to this run
        shared = bool(bias_dir)
        if shared:
            bias_path = Path(bias_dir).expanduser().resolve()
            shared_info = _check_shared_dir(bias_path, manifest)
        else:
            bias_path = out_dir / f"{pref}bias"
            bias_path.mkdir(parents=True, exist_ok=True)
            shared_info = None
            if restart_bias_file:
                rp = Path(restart_bias_file)
                if not rp.is_file():
                    raise MetadynamicsToolError(code="metadynamics_restart_missing",
                                                message=f"restart_bias_file {restart_bias_file!r} does not exist.")
                parent_meta = rp.with_name("metadynamics.json")
                if parent_meta.is_file():
                    try:
                        pm = json.loads(parent_meta.read_text()).get("manifest")
                    except ValueError:
                        pm = None
                    if pm is not None and pm != manifest:
                        raise MetadynamicsToolError(
                            code="metadynamics_restart_mismatch",
                            message="A continued walker must keep the parent's CV, grid, width, height, bias "
                            "factor, temperature and deposition interval; branch a new walker from eq instead.",
                        )
                data = np.load(rp)
                if data.shape != (grid["grid_points"],):
                    raise MetadynamicsToolError(code="metadynamics_restart_mismatch",
                                                message=f"restart bias has {data.shape} points; the grid has {grid['grid_points']}.")
                # OpenMM loads any bias_<id>_<index>.npy in the directory as another walker's bias
                np.save(bias_path / "bias_0_1.npy", data)

        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=str(system_xml_file), topology_pdb_file=str(topology_pdb_file),
            state_xml_file=None,
        )
        is_periodic = xml_inputs.is_periodic
        system = _deserialize_xml_system(xml_inputs)
        _validate_xml_system_contract(system, xml_inputs.topology, hmr_request=hmr,
                                      implicit_solvent_request=None)
        result["hmr"] = bool(hmr)

        make_cv, evaluator, group_info = _build_cv_force(
            system=system, topology=xml_inputs.topology, cv=cv, is_periodic=is_periodic,
            max_target_nm=grid["cv_max_nm"],
        )
        system.addForce(_wall_force(make_cv(), grid["cv_min_nm"], grid["cv_max_nm"], k_wall))

        ensemble = "NVT"
        if pressure_bar is not None and pressure_bar > 0 and is_periodic:
            barostat = MonteCarloBarostat(pressure_bar * bar, temperature_kelvin * kelvin)
            if random_seed is not None:
                barostat.setRandomNumberSeed(int(random_seed))
            system.addForce(barostat)
            ensemble = "NPT"
        elif pressure_bar is not None and pressure_bar > 0:
            result["warnings"].append("Non-periodic system: pressure_bar ignored, running NVT.")
        result["ensemble"] = ensemble

        variable = BiasVariable(make_cv(), grid["grid_min_nm"], grid["grid_max_nm"], grid["bias_width_nm"],
                                periodic=False, gridWidth=grid["grid_points"])
        meta = Metadynamics(system, [variable], temperature_kelvin * kelvin, float(bias_factor),
                            height * kilojoule_per_mole, dep_steps, saveFrequency=save_steps,
                            biasDir=str(bias_path))
        meta._force.setForceGroup(CUSTOM_FORCE_GROUP)
        loaded_at_start = sorted(meta._loadedBiases)

        integrator = LangevinMiddleIntegrator(temperature_kelvin * kelvin, 1.0 / picosecond,
                                              timestep_fs * femtoseconds)
        if random_seed is not None:
            integrator.setRandomNumberSeed(int(random_seed))
        result["system_signature"] = _system_signature(
            xml_inputs, solvent_type="explicit" if is_periodic else "vacuum", ensemble=ensemble,
            pressure_bar=pressure_bar, is_membrane=False, implicit_solvent=None, hmr=hmr,
        )
        result["integrator_signature"] = _integrator_signature(
            temperature_kelvin=temperature_kelvin, timestep_fs=timestep_fs,
        )
        (out_dir / f"{pref}integrator.xml").write_text(XmlSerializer.serialize(integrator))
        (out_dir / f"{pref}runtime_system.xml").write_text(XmlSerializer.serialize(system))
        result["integrator_file"] = str(out_dir / f"{pref}integrator.xml")
        result["runtime_system_file"] = str(out_dir / f"{pref}runtime_system.xml")

        platform_map = {"cuda": "CUDA", "opencl": "OpenCL", "cpu": "CPU", "reference": "Reference"}
        if platform.lower() == "auto":
            simulation = new_simulation(xml_inputs.topology, system, integrator)
        else:
            if platform.lower() not in platform_map:
                raise MetadynamicsToolError(code="invalid_parameter_value",
                                            message=f"Unknown platform {platform!r}; use auto, CUDA, OpenCL, CPU or Reference")
            props = {}
            if device_index and platform.lower() in ("cuda", "opencl"):
                props["DeviceIndex"] = str(device_index)
            simulation = new_simulation(xml_inputs.topology, system, integrator,
                                        platform=Platform.getPlatformByName(platform_map[platform.lower()]),
                                        platformProperties=props)
        result["platform"] = simulation.context.getPlatform().getName()

        start_step = 0
        if state_xml_file and Path(state_xml_file).is_file():
            info = _load_state_into_simulation(simulation, Path(state_xml_file), is_periodic=is_periodic,
                                               temperature_kelvin=temperature_kelvin, random_seed=random_seed)
            if info.get("velocities_rethermalized"):
                result["warnings"].append(f"Start state had no velocities; re-thermalized at {temperature_kelvin} K.")
            if _node_mode:
                from mdclaw._node import read_ancestor_final_step
                try:
                    start_step = int(read_ancestor_final_step(job_dir, node_id) or 0)
                except Exception:  # noqa: BLE001
                    start_step = 0
            simulation.currentStep = start_step
            result["restarted_from"] = str(state_xml_file)
        else:
            simulation.context.setPositions(xml_inputs.positions)
            if is_periodic and xml_inputs.box_vectors is not None:
                simulation.context.setPeriodicBoxVectors(*xml_inputs.box_vectors)
            simulation.minimizeEnergy(maxIterations=1000)
            if random_seed is not None:
                simulation.context.setVelocitiesToTemperature(temperature_kelvin * kelvin, int(random_seed))
            else:
                simulation.context.setVelocitiesToTemperature(temperature_kelvin * kelvin)
        # Metadynamics deposits whenever currentStep is a multiple of the interval; start on the grid.
        if simulation.currentStep % dep_steps:
            result["warnings"].append(
                f"start step {simulation.currentStep} is not a multiple of the deposition interval; "
                "the first Gaussian comes at the next multiple.")

        xi0 = float(meta.getCollectiveVariables(simulation)[0])
        if not grid["cv_min_nm"] <= xi0 <= grid["cv_max_nm"]:
            result["warnings"].append(
                f"{cv['name']} starts at {xi0:.3f} nm, outside the bias grid [{grid['cv_min_nm']}, "
                f"{grid['cv_max_nm']}] nm; the wall pulls it in first.")
        logger.info("metadynamics start: %s=%.4f nm, walls [%.3f, %.3f], grid [%.3f, %.3f] x %d, sigma %.3f, h %.2f kJ/mol, gamma %.1f, "
                    "every %.2f ps, %d walkers' biases loaded",
                    cv["name"], xi0, grid["cv_min_nm"], grid["cv_max_nm"], grid["grid_min_nm"], grid["grid_max_nm"], grid["grid_points"],
                    grid["bias_width_nm"], height, bias_factor, dep_ps, len(loaded_at_start))

        trajectory_file = out_dir / f"{pref}trajectory.dcd"
        energy_file = out_dir / f"{pref}energy.dat"
        cv_file = out_dir / f"{pref}collective_variables.csv"
        state_file = out_dir / f"{pref}state.xml"
        report_file = out_dir / f"{pref}metadynamics.csv"
        sidecar_file = out_dir / f"{pref}metadynamics.json"
        reporters.append(DCDReporter(str(trajectory_file), report_interval))
        reporters.append(StateDataReporter(str(energy_file), report_interval, step=True, time=True,
                                           potentialEnergy=True, kineticEnergy=True, totalEnergy=True,
                                           temperature=True, volume=(ensemble == "NPT"),
                                           density=(ensemble == "NPT")))
        cv_reporter = CustomForceReporter(str(cv_file), report_interval, force_group=CUSTOM_FORCE_GROUP,
                                          evaluator=evaluator, cv_names=[cv["name"]])
        reporters.append(cv_reporter)
        for r in reporters:
            simulation.reporters.append(r)
        signature = {"kind": "openmm_well_tempered_metadynamics", "mass_weighting": "physical_element",
                     **manifest, "minimum_image": group_info["minimum_image"],
                     "wall_force_constant_kj_mol_nm2": k_wall}
        write_cv_metadata(str(out_dir / f"{pref}collective_variables.meta.json"), signature=signature,
                          cv_names=[cv["name"]], temperature_kelvin=temperature_kelvin,
                          parameters={**manifest, "bias_dir": str(bias_path), "shared_bias_dir": shared,
                                      "bias_energy_column": "bias_energy_kj_mol is the metadynamics bias V(s) at the frame"})

        kT = (MOLAR_GAS_CONSTANT_R * temperature_kelvin * kelvin).value_in_unit(kilojoule_per_mole)
        delta_T = temperature_kelvin * (float(bias_factor) - 1.0)
        grid_x = np.linspace(grid["grid_min_nm"], grid["grid_max_nm"], grid["grid_points"])
        inside_walls = (grid_x >= grid["cv_min_nm"] - 1e-9) & (grid_x <= grid["cv_max_nm"] + 1e-9)

        def write_outputs(final: bool):
            total = np.array(meta._totalBias, dtype=float)
            own = np.array(meta._selfBias, dtype=float)
            np.save(out_dir / f"{pref}metadynamics_total_bias.npy", total)
            np.save(out_dir / f"{pref}metadynamics_self_bias.npy", own)
            F = -((temperature_kelvin + delta_T) / delta_T) * total
            F = F - F[inside_walls].min()
            if visited:
                inside = inside_walls & (grid_x >= min(visited) - grid["bias_width_nm"]) & (grid_x <= max(visited) + grid["bias_width_nm"])
                f_range = float(F[inside].max() - F[inside].min()) if inside.any() else float(F[inside_walls].max())
            else:
                f_range = float(F[inside_walls].max())
            with open(out_dir / f"{pref}free_energy.csv", "w") as fh:
                fh.write(f"{cv['name']}_nm,free_energy_kj_mol,bias_kj_mol\n")
                for x, f, v in zip(grid_x[inside_walls], F[inside_walls], total[inside_walls]):
                    fh.write(f"{x:.6f},{f:.6f},{v:.6f}\n")
            side = {
                "sampling_method": SAMPLING_METHOD, "manifest": manifest, "grid_nm": grid_x.tolist(),
                "bias_dir": str(bias_path), "shared_bias_dir": shared, "walker_id": int(meta._id),
                "loaded_walkers": sorted(int(i) for i in meta._loadedBiases),
                "depositions": int(deposited), "step": int(simulation.currentStep), "start_step": start_step,
                "restart_bias_file": restart_bias_file, "groups": group_info, "final": final,
                "distance_cv": cv, "wall_force_constant_kj_mol_nm2": k_wall,
                "cv_visited_min_nm": float(min(visited)) if visited else None,
                "cv_visited_max_nm": float(max(visited)) if visited else None,
                "free_energy_range_kj_mol": f_range, "kT_kj_mol": kT,
            }
            tmp = sidecar_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(side, indent=2))
            os.replace(tmp, sidecar_file)
            return side

        deposited = 0
        visited: list = []
        checkpoint_every = max(1, int(round(max(report_interval * 10, 5000) / dep_steps)))
        t_start = time.time()
        with open(report_file, "w") as rep:
            rep.write(f"step,time_ps,{cv['name']}_nm,bias_at_cv_kj_mol,gaussian_height_kj_mol\n")
            for i in range(n_depositions):
                meta.step(simulation, dep_steps)
                deposited += 1
                xi = float(meta.getCollectiveVariables(simulation)[0])
                visited.append(xi)
                v = float(np.interp(xi, grid_x, meta._totalBias))
                h = height * math.exp(-v / (kT / temperature_kelvin * delta_T)) if delta_T > 0 else height
                rep.write(f"{simulation.currentStep},{simulation.currentStep * timestep_fs / 1000.0:.3f},"
                          f"{xi:.6f},{v:.4f},{h:.4f}\n")
                if (i + 1) % 1000 == 0:
                    elapsed = time.time() - t_start
                    logger.info("metadynamics %d/%d %s=%.3f V=%.1f kJ/mol h=%.2f  %.0f ns/day", i + 1, n_depositions,
                                cv["name"], xi, v, h,
                                (i + 1) * dep_steps * timestep_fs * 1e-6 / elapsed * 86400 if elapsed else 0)
                if (i + 1) % checkpoint_every == 0:
                    rep.flush()
                    _save_state_atomic(simulation, state_file)
                    write_outputs(final=False)
        meta._syncWithDisk()
        _save_state_atomic(simulation, state_file)
        for r in reporters:
            _close_reporter_stream(r)
        if cv_reporter is not None:
            cv_reporter.close()
        side = write_outputs(final=True)
        elapsed = time.time() - t_start
        side["ns_per_day"] = steps_to_run * timestep_fs * 1e-6 / elapsed * 86400 if elapsed else None
        sidecar_file.write_text(json.dumps(side, indent=2))

        from openmm.app import PDBFile
        final_state = simulation.context.getState(getPositions=True, enforcePeriodicBox=False)
        final_pdb = out_dir / f"{pref}final_structure.pdb"
        with open(final_pdb, "w") as fh:
            PDBFile.writeFile(simulation.topology, final_state.getPositions(), fh, keepIds=True)

        F = np.loadtxt(out_dir / f"{pref}free_energy.csv", delimiter=",", skiprows=1)
        result.update({
            "success": True,
            "trajectory_file": str(trajectory_file),
            "energy_file": str(energy_file),
            "state_file": str(state_file),
            "final_structure": str(final_pdb),
            "collective_variables_file": str(cv_file),
            "collective_variables_meta_file": str(out_dir / f"{pref}collective_variables.meta.json"),
            "metadynamics_report_file": str(report_file),
            "metadynamics_state_file": str(sidecar_file),
            "free_energy_file": str(out_dir / f"{pref}free_energy.csv"),
            "total_bias_file": str(out_dir / f"{pref}metadynamics_total_bias.npy"),
            "self_bias_file": str(out_dir / f"{pref}metadynamics_self_bias.npy"),
            "steps_completed": int(simulation.currentStep),
            "start_step": start_step,
            "num_steps": int(simulation.currentStep),
            "ns_per_day": side["ns_per_day"],
            "metadynamics": {
                "distance_cv": cv, **grid,
                "bias_height_kj_mol": height, "bias_factor": float(bias_factor),
                "deposition_interval_ps": dep_ps, "depositions": deposited,
                "bias_dir": str(bias_path), "shared_bias_dir": shared,
                "shared_manifest": shared_info, "walker_id": int(meta._id),
                "loaded_walkers_at_start": [int(i) for i in loaded_at_start],
                "loaded_walkers_at_end": side["loaded_walkers"],
                "cv_visited_min_nm": side["cv_visited_min_nm"], "cv_visited_max_nm": side["cv_visited_max_nm"],
                "free_energy_minimum_nm": float(F[np.argmin(F[:, 1]), 0]),
                "free_energy_range_kj_mol": side["free_energy_range_kj_mol"],
                "final_gaussian_height_kj_mol": h,
                "groups": group_info,
            },
        })
    except MetadynamicsToolError as exc:
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except (DistanceRestraintError, _ModernSystemContractError) as exc:
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as exc:  # noqa: BLE001
        logger.error("metadynamics failed: %s", exc)
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["code"] = "unhandled_exception"
    finally:
        if not result["success"]:
            for r in reporters:
                _close_reporter_stream(r)
            if cv_reporter is not None:
                cv_reporter.close()

    if _node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            artifacts = {
                "trajectory": _node_artifact_path(result["trajectory_file"]),
                "energy": _node_artifact_path(result["energy_file"]),
                "state": _node_artifact_path(result["state_file"]),
                "final_structure": _node_artifact_path(result["final_structure"]),
                "collective_variables": _node_artifact_path(result["collective_variables_file"]),
                "collective_variables_meta": _node_artifact_path(result["collective_variables_meta_file"]),
                "metadynamics_report": _node_artifact_path(result["metadynamics_report_file"]),
                "metadynamics_state": _node_artifact_path(result["metadynamics_state_file"]),
                "metadynamics_total_bias": _node_artifact_path(result["total_bias_file"]),
                "metadynamics_self_bias": _node_artifact_path(result["self_bias_file"]),
                "free_energy": _node_artifact_path(result["free_energy_file"]),
                "runtime_system": _node_artifact_path(result["runtime_system_file"]),
                "integrator": _node_artifact_path(result["integrator_file"]),
            }
            metadata = {
                "sampling_method": SAMPLING_METHOD,
                "sampling_role": "metadynamics",
                "simulation_time_ns": simulation_time_ns,
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "ensemble": result.get("ensemble"),
                "platform": result.get("platform", platform),
                "hmr": hmr,
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "random_seed": random_seed,
                "num_steps": result["num_steps"],
                "start_step": result["start_step"],
                "final_step": result["steps_completed"],
                "system_signature": result.get("system_signature"),
                "integrator_signature": result.get("integrator_signature"),
                "distance_cv": result["metadynamics"]["distance_cv"],
                "metadynamics": result["metadynamics"],
            }
            complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata,
                          warnings=result.get("warnings") or None)
        else:
            fail_node(job_dir, node_id, errors=result["errors"], code=result.get("code"))
    return result

