"""
MD Simulation Server - Molecular dynamics simulation & analysis tools.

Provides MCP tools for:
- OpenMM MD simulation (NVT/NPT equilibration, production)
- MDTraj trajectory analysis (RMSD, RMSF, distances, hydrogen bonds, etc.)
- Energy analysis
- Secondary structure analysis
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402
from mdclaw._tool_meta import node_tool  # noqa: E402

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    create_unique_subdir,
    create_validation_error,
    ensure_directory,
    generate_job_id,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

from mdclaw.simulation._base import _check_topology_implicit_solvent_match, _fail_node_if_running, _resolve_implicit_solvent_model  # noqa: E402
from mdclaw.simulation.custom_forces import CUSTOM_FORCE_GROUP, CustomForceError, CustomForceReporter, custom_force_signature, load_custom_forces, write_cv_metadata  # noqa: E402
from mdclaw.simulation.integrator_plan import _compute_step_plan, _record_production_node_result  # noqa: E402
from mdclaw.simulation.restart import _close_reporter_stream, _count_state_data_rows, _detect_ensemble_mismatch, _flush_reporter_stream, _load_state_into_simulation, _node_previously_failed, _resolve_dcd_append_mode, _resolve_restart_node_id_for_run, _restart_random_seed, _restart_source_metadata, _save_checkpoint_atomic, _save_state_atomic  # noqa: E402
from mdclaw.simulation.xml_contract import _ModernSystemContractError, _deserialize_xml_system, _effective_pressure_bar, _integrator_signature, _load_xml_topology_inputs, _signature_mismatches, _system_signature, _validate_xml_system_contract  # noqa: E402


def _safe_custom_force_signature(
    custom_force_script: Optional[str],
    custom_force_parameters: Optional[dict],
) -> Optional[dict]:
    """Best-effort custom-force signature for node identity.

    Returns ``None`` when no custom force is configured. Never raises — a
    missing/unreadable file degrades to a signature without the hash so node
    validation can still proceed (the real load below surfaces the error).
    """
    if not custom_force_script:
        return None
    try:
        return custom_force_signature(
            custom_force_script=custom_force_script,
            custom_force_parameters=custom_force_parameters,
        )
    except Exception:  # noqa: BLE001
        return {
            "kind": "torch_script_energy",
            "sha256": None,
            "parameters": custom_force_parameters or {},
        }


@node_tool
def run_production(
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    simulation_time_ns: float = 1.0,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = None,
    timestep_fs: float = 4.0,
    output_frequency_ps: float = 10.0,
    trajectory_format: str = "dcd",
    restraint_file: Optional[str] = None,
    custom_force_script: Optional[str] = None,
    custom_force_parameters: Optional[dict] = None,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    is_membrane: bool = False,
    implicit_solvent: Optional[str] = None,
    platform: str = "auto",
    device_index: Optional[str] = None,
    restart_from: Optional[str] = None,
    hmr: bool = True,
    random_seed: Optional[int] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Run MD simulation using OpenMM.

    Performs molecular dynamics simulation with OpenMM, supporting both
    NVT and NPT ensembles with Langevin dynamics.

    Args:
        system_xml_file: Path to ``system.xml`` from the topo ancestor
            (``build_amber_system`` / ``build_openmm_system``). The run
            side never reconstructs the System from ForceField XML —
            the saved triple is the source of truth.
        topology_pdb_file: Path to ``topology.pdb`` from the same topo
            ancestor.
        state_xml_file: Path to ``state.xml`` from the same topo
            ancestor; preferred over the PDB coordinates when present.
        simulation_time_ns: Simulation time to run IN THIS CALL in nanoseconds
                     (default: 1.0). On restart (``restart_from`` set) this is
                     the *additional* time to append after the checkpoint —
                     e.g. prod_001 ran 10 ns, prod_002 with ``simulation_time_ns=5``
                     runs 5 more ns. (The eq checkpoint is written with
                     ``currentStep=0`` by design, so the eq→prod case is
                     unchanged: ``simulation_time_ns`` there is the full
                     production duration.)
        temperature_kelvin: Temperature in Kelvin (default: 300.0)
        pressure_bar: Pressure in bar. Set for NPT, None for NVT (default: None)
        timestep_fs: Integration timestep in femtoseconds (default: 4.0)
        output_frequency_ps: Output frequency in picoseconds (default: 10.0)
        trajectory_format: Trajectory format - "dcd" or "pdb" (default: "dcd")
        restraint_file: DEPRECATED and ignored. Use ``custom_force_script``
                     for production biases.
        custom_force_script: Path to a Python script defining a single
                     ``energy(positions, ctx) -> torch.Tensor`` function (a
                     scalar potential energy in kJ/mol). MDClaw computes the
                     forces by autograd (``forces = -dE/dx``) and wraps it in
                     an ``openmmtorch.PythonTorchForce`` added to the System
                     before the Simulation is built. ``positions`` is an
                     (N,3) nm tensor; ``ctx.select(sel)`` returns mdtraj-style
                     atom indices that match the System, ``ctx.reference`` is
                     the fixed reference geometry, and ``ctx.params`` exposes
                     ``custom_force_parameters``. The function may return
                     ``(energy, {cv_name: value})`` to log collective
                     variables. A pre-trained model is loaded inside this
                     function (e.g. ``torch.load``). Requires an
                     ``openmm-torch`` build that provides ``PythonTorchForce``.
        custom_force_parameters: Optional dict passed to the script as
                     ``ctx.params``. Recognized keys include ``pbc`` (bool).
        name: Optional name prefix for output files
        output_dir: Output directory. If None, creates output/{job_id}/
        is_membrane: Set True for membrane systems to use MonteCarloMembraneBarostat
                     with semi-isotropic pressure coupling (XY coupled, Z independent).
                     Uses surface tension = 0 bar*nm for NPγT ensemble. (default: False)
        implicit_solvent: Generalized Born implicit solvent model. Options:
                     - None (default): Use explicit solvent with PME
                     - "HCT": Hawkins-Cramer-Truhlar (igb=1)
                     - "OBC1": Onufriev-Bashford-Case I (igb=2)
                     - "OBC2": Onufriev-Bashford-Case II (igb=5, recommended)
                     - "GBn": GBn model (igb=7)
                     - "GBn2": GBn2 model (igb=8, Amber recommended)
                     Note: NPT not supported with implicit solvent - uses NVT.
        platform: OpenMM platform - "CUDA", "OpenCL", "CPU", "Reference", or
                     "auto" (default). "auto" lets OpenMM choose the fastest.
        device_index: GPU device index (e.g. "0", "0,1"). Only used with
                     CUDA or OpenCL platforms.
        restart_from: Path to a state file to restart from. Prefer ``.xml``
                     (saveState, cross-node portable); ``.chk``
                     (saveCheckpoint, GPU-architecture-specific) is kept
                     for same-GPU bit-exact replay (committor / sensitivity
                     analyses) but no code path reads it when a ``.xml``
                     is also present. In node mode this is auto-resolved
                     via ``resolve_node_inputs`` (state first, checkpoint
                     second). Skips minimization and runs
                     ``simulation_time_ns`` additional nanoseconds on top
                     of the restart step count. The trajectory is written
                     to this node's own ``artifacts/`` directory as a
                     fresh DCD (no cross-node append) — to stitch
                     trajectories across nodes, concatenate with mdtraj
                     or similar.
        hmr: Enable Hydrogen Mass Repartitioning (hydrogenMass=4 amu).
                     Enabled by default. Allows 4 fs timestep for ~2x throughput.
                     Use --no-hmr to disable (timestep should then be <= 2 fs).
        random_seed: Random number seed for reproducible simulations.
                     Controls integrator and initial velocity randomization.
                     If None (default), OpenMM uses system entropy.
                     Different seeds produce independent trajectories from
                     the same initial configuration.

    Returns:
        Dict with:
            - success: bool - True if simulation completed successfully
            - job_id: str - Unique identifier for this simulation
            - output_dir: str - Path to output directory
            - ensemble: str - "NVT" or "NPT"
            - simulation_time_ns: float - Actual simulation time
            - trajectory_file: str - Path to trajectory file
            - final_structure: str - Path to final PDB structure
            - energy_file: str - Path to energy log file
            - initial_energy_kj_mol: float - Initial potential energy
            - final_energy_kj_mol: float - Final potential energy
            - num_steps: int - Total simulation steps
            - errors: list[str] - Error messages if any
            - warnings: list[str] - Non-critical warnings
    """
    pressure_bar = _effective_pressure_bar(pressure_bar, implicit_solvent)

    # Auto-resolve inputs from DAG when in node mode
    _eq_final_ensemble: Optional[str] = None
    _eq_pressure_bar: Optional[float] = None
    _pressure_bar_inherited = False
    if job_dir and node_id:
        from mdclaw._node import resolve_node_inputs, validate_node_execution_context
        _inputs = resolve_node_inputs(job_dir, node_id, "prod")
        if not is_membrane and _inputs.get("is_membrane"):
            is_membrane = True
        _eq_final_ensemble = _inputs.get("eq_final_ensemble")
        _eq_pressure_bar = _inputs.get("eq_pressure_bar")
        # Inherit the custom force from a ``--continue-from`` parent so a
        # biased production can be extended without re-specifying the bias.
        # Explicit flags always win over the inherited value.
        if not custom_force_script:
            if _inputs.get("custom_force_script"):
                custom_force_script = _inputs["custom_force_script"]
            if (custom_force_parameters is None
                    and _inputs.get("custom_force_parameters") is not None):
                custom_force_parameters = _inputs["custom_force_parameters"]
        if (pressure_bar is None
                and _eq_final_ensemble == "NPT"
                and _eq_pressure_bar is not None):
            pressure_bar = _eq_pressure_bar
            _pressure_bar_inherited = True
            logger.info(
                f"pressure_bar inherited from eq ancestor "
                f"(final_ensemble=NPT, {pressure_bar} bar)"
            )
        # Resolver-level failures are recorded on the node so a failed
        # extension does not remain pending after the tool exits.
        if not restart_from and "restart_from_error" in _inputs:
            err = _inputs["restart_from_error"]
            from mdclaw._node import begin_node, fail_node
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=[err])
            return create_validation_error(
                "restart_from",
                err,
                expected="Completed continue_from prod node with state or checkpoint artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                code="restart_from_unavailable",
            )
        if "input_resolution_error" in _inputs:
            err = _inputs["input_resolution_error"]
            from mdclaw._node import begin_node, fail_node
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=[err])
            return create_validation_error(
                "job_dir/node_id",
                err,
                expected="Completed topo and restart ancestors with required artifacts",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            )
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "prod",
            actual_conditions={
                "simulation_time_ns": simulation_time_ns,
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "trajectory_format": trajectory_format,
                "is_membrane": is_membrane,
                "implicit_solvent": implicit_solvent,
                "platform": platform,
                "device_index": device_index,
                "hmr": hmr,
                "random_seed": random_seed,
                "custom_force": _safe_custom_force_signature(
                    custom_force_script, custom_force_parameters,
                ),
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}
        if not system_xml_file and "system_xml_file" in _inputs:
            system_xml_file = _inputs["system_xml_file"]
        if not topology_pdb_file and "topology_pdb_file" in _inputs:
            topology_pdb_file = _inputs["topology_pdb_file"]
        if not state_xml_file and "state_xml_file" in _inputs:
            state_xml_file = _inputs["state_xml_file"]
        # See run_equilibration for the rationale: explicit
        # ``--restart-from`` wins over the resolver's auto-pick, and we
        # trust the resolver's ``restart_from_node_id`` only when we
        # actually used the resolver's path. An explicit path is matched
        # back to a DAG ancestor by absolute-path equality so the step
        # counter cannot drift from the artifact we load.
        _explicit_restart_from = bool(restart_from)
        if not _explicit_restart_from and "restart_from" in _inputs:
            restart_from = _inputs["restart_from"]
        _restart_from_node_id = _resolve_restart_node_id_for_run(
            job_dir=job_dir, node_id=node_id,
            restart_from=restart_from,
            explicit_restart_from=_explicit_restart_from,
            inputs=_inputs,
        )
        # Catch implicit-solvent model mismatches between the topo node's
        # build-time metadata and the runtime --implicit-solvent flag
        # before any System is built. Mirror of the run_equilibration
        # guard; both sites must agree because min/eq/prod share the topo
        # ancestor's saved system.xml.
        _topo_solvent_mismatch = _check_topology_implicit_solvent_match(
            topology_implicit_solvent=_inputs.get("topology_implicit_solvent"),
            runtime_implicit_solvent=implicit_solvent,
        )
        if _topo_solvent_mismatch is not None:
            from mdclaw._node import begin_node, fail_node
            begin_node(job_dir, node_id)
            fail_node(
                job_dir, node_id,
                errors=_topo_solvent_mismatch["errors"],
            )
            err = create_validation_error(
                "implicit_solvent",
                _topo_solvent_mismatch["message"],
                expected=(
                    "build-time and runtime implicit_solvent agree "
                    "after canonicalization (HCT / OBC1 / OBC2 / GBn / GBn2)"
                ),
                actual=(
                    f"build={_inputs.get('topology_implicit_solvent')!r}, "
                    f"runtime={implicit_solvent!r}"
                ),
                code=_topo_solvent_mismatch["code"],
            )
            err["errors"] = _topo_solvent_mismatch["errors"]
            return err

    if not (system_xml_file and topology_pdb_file):
        return create_validation_error(
            "topology_inputs",
            "system_xml_file and topology_pdb_file are required",
            expected="XML triple from build_amber_system / build_openmm_system",
            actual=(
                f"system_xml_file={system_xml_file!r}, "
                f"topology_pdb_file={topology_pdb_file!r}"
            ),
            hints=["Run build_amber_system first or execute in node mode from a prod node."],
            code="missing_xml_topology_inputs",
        )
    logger.info(f"Starting MD simulation: {simulation_time_ns}ns at {temperature_kelvin}K")

    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "ensemble": None,
        "simulation_time_ns": simulation_time_ns,
        "temperature_kelvin": temperature_kelvin,
        "pressure_bar": pressure_bar,
        "timestep_fs": timestep_fs,
        "trajectory_file": None,
        "final_structure": None,
        "energy_file": None,
        "initial_energy_kj_mol": None,
        "final_energy_kj_mol": None,
        "num_steps": None,
        "platform": None,
        "device_index": None,
        "checkpoint_file": None,
        "restarted_from": None,
        "steps_completed": None,
        "start_step": None,
        "start_time_ns": None,
        "hmr": False,
        "random_seed": None,
        "errors": [],
        "warnings": []
    }

    # Setup output directory. Capture the prior node status *before*
    # begin_node() flips it to "running" — the sentinel drives the
    # append-mode guard further down (stale artifacts from a previously-
    # failed retry must be discarded rather than silently appended to).
    _node_mode = job_dir and node_id
    _prior_failed = (
        _node_previously_failed(job_dir, node_id) if _node_mode else False
    )
    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "production")
    result["output_dir"] = str(out_dir)

    # Copy a node-mode custom-force script/module into the node's artifacts so
    # the exact bias used is preserved (provenance) and survives ``--continue
    # -from``. Repoint the variable at the copy so loading and the recorded
    # artifact reference the same file.
    _custom_force_script_artifact: Optional[str] = None
    if _node_mode and custom_force_script:
        import shutil
        src = Path(custom_force_script)
        if src.is_file():
            dest = out_dir / "custom_force_script.py"
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
            custom_force_script = str(dest)
            _custom_force_script_artifact = "artifacts/custom_force_script.py"

    # Validate input files. Every early-return path below this point
    # happens AFTER begin_node(), so it must transit through
    # _fail_node_if_running to flip the node out of "running" —
    # otherwise the DAG sees a perpetually in-flight node.
    system_xml_path = Path(system_xml_file).resolve()
    topology_pdb_path = Path(topology_pdb_file).resolve()
    state_xml_path = Path(state_xml_file).resolve() if state_xml_file else None
    if not system_xml_path.is_file():
        result["errors"].append(f"system.xml not found: {system_xml_file}")
        return _fail_node_if_running(job_dir, node_id, result)
    if not topology_pdb_path.is_file():
        result["errors"].append(f"topology.pdb not found: {topology_pdb_file}")
        return _fail_node_if_running(job_dir, node_id, result)
    if state_xml_path and not state_xml_path.is_file():
        result["errors"].append(f"state.xml not found: {state_xml_file}")
        return _fail_node_if_running(job_dir, node_id, result)

    try:
        from openmm.app import (
            DCDReporter, StateDataReporter, CheckpointReporter,
            Simulation,
            HCT, OBC1, OBC2, GBn, GBn2,
        )
        from openmm import (
            LangevinMiddleIntegrator, MonteCarloBarostat,
            MonteCarloMembraneBarostat, Platform,
        )
        from openmm.unit import (
            nanometer, kelvin, picosecond, femtoseconds, bar,
        )
    except ImportError:
        result["errors"].append("OpenMM not installed")
        result["errors"].append("Hint: Install with: conda install -c conda-forge openmm")
        return _fail_node_if_running(job_dir, node_id, result)

    # Map canonical implicit-solvent names (matching forcefield_catalog
    # and the openmmforcefields ``implicit/<name>.xml`` keys) to OpenMM
    # symbols. Resolution goes through ``_resolve_implicit_solvent_model``
    # so user-provided aliases (``gbneck2``, ``igb8``, case variants)
    # canonicalize the same way build_amber_system does — and unknown
    # names fail-fast instead of silently falling back to OBC2.
    IMPLICIT_MODELS = {
        "HCT":  HCT,    # igb=1
        "OBC1": OBC1,   # igb=2
        "OBC2": OBC2,   # igb=5 (default, well-tested)
        "GBn":  GBn,    # igb=7
        "GBn2": GBn2,   # igb=8 (recommended by Amber manual)
    }
    
    try:
        # Load topology + initial positions / box vectors from the XML
        # triple. The build-time forcefield, constraints, and HMR are
        # baked into ``system.xml`` and validated against the runtime
        # ``hmr`` / ``implicit_solvent`` request below.
        logger.info("Loading XML topology triple (system.xml + topology.pdb + state.xml)")
        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=str(system_xml_path),
            topology_pdb_file=str(topology_pdb_path),
            state_xml_file=str(state_xml_path) if state_xml_path else None,
        )
        is_periodic = xml_inputs.is_periodic

        if hmr:
            logger.info(f"HMR enabled: hydrogenMass=4.0 amu (timestep={timestep_fs}fs)")
            if timestep_fs <= 2.0:
                result["warnings"].append(
                    f"HMR enabled but timestep is {timestep_fs}fs. "
                    f"Consider using --timestep-fs 4.0 for better throughput."
                )
            result["hmr"] = True
        else:
            if timestep_fs > 2.0:
                result["warnings"].append(
                    f"HMR is disabled but timestep is {timestep_fs}fs. "
                    f"Without HMR, timestep > 2 fs may cause instability. "
                    f"Consider --hmr or --timestep-fs 2.0."
                )
            result["hmr"] = False

        # Resolve the GB symbol up-front so a runtime typo fails before
        # the System is built. The build vs runtime model match was
        # already enforced by the topology metadata guard upstream.
        if implicit_solvent:
            _gb_model, gb_err = _resolve_implicit_solvent_model(
                implicit_solvent, IMPLICIT_MODELS
            )
            if gb_err:
                result["errors"].extend(gb_err["errors"])
                result["code"] = gb_err["code"]
                return _fail_node_if_running(job_dir, node_id, result)
            from mdclaw import forcefield_catalog as _fc
            canonical_implicit = _fc.normalize_implicit_solvent(implicit_solvent)
            result["solvent_type"] = "implicit"
            result["implicit_model"] = canonical_implicit
        elif is_periodic:
            result["solvent_type"] = "explicit"
        else:
            result["errors"].append(
                "Non-periodic topology without implicit_solvent would run vacuum production. "
                "Pass --implicit-solvent for GB simulations or build an explicit-solvent topology."
            )
            return _fail_node_if_running(job_dir, node_id, result)

        # Deserialize a fresh System from system.xml. The build-time
        # forcefield / constraints / HMR / GB are baked in; the run side
        # never reconstructs the System from ForceField XML. The
        # contract check raises if HMR or implicit-solvent requests
        # contradict what was baked at build time.
        logger.info("Deserializing system.xml")
        try:
            system = _deserialize_xml_system(xml_inputs)
            _validate_xml_system_contract(
                system, xml_inputs.topology,
                hmr_request=hmr,
                implicit_solvent_request=implicit_solvent,
            )
        except _ModernSystemContractError as exc:
            result["errors"].append(str(exc))
            result["code"] = exc.code
            return _fail_node_if_running(job_dir, node_id, result)

        # Custom force / CV bias. Resolved and added to the System *before*
        # the Simulation is built (OpenMM requires forces be present at
        # Context creation). The bias goes in a dedicated force group so its
        # energy can be logged in isolation for CV analysis / reweighting.
        _custom_force_loaded = None
        if custom_force_script:
            try:
                _custom_force_loaded = load_custom_forces(
                    system=system,
                    topology_pdb_file=str(topology_pdb_path),
                    reference_positions=xml_inputs.positions,
                    custom_force_script=custom_force_script,
                    custom_force_parameters=custom_force_parameters,
                )
            except CustomForceError as exc:
                result["errors"].append(str(exc))
                result["code"] = exc.code
                return _fail_node_if_running(job_dir, node_id, result)
            for _f in _custom_force_loaded["forces"]:
                _f.setForceGroup(CUSTOM_FORCE_GROUP)
                system.addForce(_f)
            result["custom_force"] = {
                "kind": _custom_force_loaded["kind"],
                "signature": _custom_force_loaded["signature"],
                "has_cv": _custom_force_loaded["has_cv"],
                "cv_names": _custom_force_loaded["cv_names"],
            }
            if restart_from:
                result["warnings"].append(
                    "Custom force changes the System; binary .chk restart is "
                    "unsupported with a custom force. The portable XML state "
                    "restart is used instead."
                )
            logger.info(
                "Custom force loaded (kind=%s, cv=%s)",
                _custom_force_loaded["kind"], _custom_force_loaded["cv_names"],
            )

        restart_seed_step: Optional[int] = None
        if restart_from and _node_mode:
            from mdclaw._node import read_ancestor_final_step
            restart_seed_step = read_ancestor_final_step(
                job_dir, node_id,
                restart_node_id=_restart_from_node_id,
            )
        effective_random_seed = (
            _restart_random_seed(random_seed, restart_seed_step)
            if restart_from else random_seed
        )

        # Add barostat if NPT (only for periodic explicit solvent systems).
        # ``pressure_bar=0`` is conventionally NVT (matches
        # ``_effective_pressure_bar`` and the docstring's "0 or None: NVT"
        # rule); without this guard, a 0-bar barostat would be added and
        # the ``_detect_ensemble_mismatch`` warning for NPT-state-into-NVT
        # restarts would never fire.
        if (pressure_bar is not None and pressure_bar > 0
                and is_periodic and not implicit_solvent):
            if is_membrane:
                # Membrane systems: MonteCarloMembraneBarostat with semi-isotropic coupling
                # XYIsotropic: X and Y axes scale together (membrane plane)
                # ZFree: Z axis scales independently (membrane thickness)
                # Surface tension = 0 bar*nm for NPγT ensemble
                barostat = MonteCarloMembraneBarostat(
                    pressure_bar * bar,
                    0.0 * bar * nanometer,  # Surface tension = 0 (NPγT)
                    temperature_kelvin * kelvin,
                    MonteCarloMembraneBarostat.XYIsotropic,
                    MonteCarloMembraneBarostat.ZFree,
                    25  # Frequency (default)
                )
                logger.info("Using MonteCarloMembraneBarostat (XYIsotropic, ZFree, γ=0)")
            else:
                # Non-membrane systems: standard MonteCarloBarostat
                barostat = MonteCarloBarostat(
                    pressure_bar * bar,
                    temperature_kelvin * kelvin
                )
            if effective_random_seed is not None:
                barostat.setRandomNumberSeed(effective_random_seed)
            system.addForce(barostat)
            ensemble = "NPT"
        elif implicit_solvent and pressure_bar is not None and pressure_bar > 0:
            # Warn user that NPT is not supported with implicit solvent
            logger.warning("Implicit solvent simulations use NVT ensemble - ignoring pressure setting")
            result["warnings"].append("NPT not supported with implicit solvent, using NVT")
            ensemble = "NVT"
        else:
            ensemble = "NVT"
        result["ensemble"] = ensemble
        result["is_membrane"] = is_membrane
        current_system_signature = _system_signature(
            xml_inputs,
            solvent_type=result.get("solvent_type", "unknown"),
            ensemble=ensemble,
            pressure_bar=pressure_bar,
            is_membrane=is_membrane,
            implicit_solvent=implicit_solvent,
            hmr=hmr,
        )
        current_integrator_signature = _integrator_signature(
            temperature_kelvin=temperature_kelvin,
            timestep_fs=timestep_fs,
        )
        result["system_signature"] = current_system_signature
        result["integrator_signature"] = current_integrator_signature

        # Create integrator
        integrator = LangevinMiddleIntegrator(
            temperature_kelvin * kelvin,
            1.0 / picosecond,
            timestep_fs * femtoseconds
        )
        if effective_random_seed is not None:
            integrator.setRandomNumberSeed(effective_random_seed)
        if random_seed is not None:
            result["random_seed"] = random_seed
            if restart_from:
                result["effective_random_seed"] = effective_random_seed
                result["random_seed_restart_offset"] = max(
                    1, int(restart_seed_step or 0)
                )

        # Platform selection
        PLATFORM_MAP = {"cuda": "CUDA", "opencl": "OpenCL", "cpu": "CPU", "reference": "Reference"}
        platform_obj = None
        platform_properties = {}
        if platform.lower() != "auto":
            plat_key = platform.lower()
            if plat_key not in PLATFORM_MAP:
                result["errors"].append(
                    f"Unknown platform '{platform}'. "
                    f"Valid options: auto, CUDA, OpenCL, CPU, Reference"
                )
                return _fail_node_if_running(job_dir, node_id, result)
            platform_obj = Platform.getPlatformByName(PLATFORM_MAP[plat_key])
            if device_index and plat_key in ("cuda", "opencl"):
                platform_properties["DeviceIndex"] = device_index

        # Create simulation
        if platform_obj:
            simulation = Simulation(
                xml_inputs.topology, system, integrator,
                platform=platform_obj, platformProperties=platform_properties,
            )
        else:
            simulation = Simulation(xml_inputs.topology, system, integrator)

        result["platform"] = simulation.context.getPlatform().getName()
        if device_index:
            result["device_index"] = device_index

        # File name prefix
        pref = f"{name}_" if name else ""
        checkpoint_file = out_dir / f"{pref}checkpoint.chk"

        # Load checkpoint or set initial positions
        if restart_from:
            restart_path = Path(restart_from)
            if not restart_path.is_file():
                result["errors"].append(f"Restart file not found: {restart_from}")
                return _fail_node_if_running(job_dir, node_id, result)
            restart_meta = _restart_source_metadata(job_dir, node_id, restart_from)
            restart_system_signature = restart_meta.get("system_signature")
            restart_integrator_signature = restart_meta.get("integrator_signature")
            # XML state is the portable, ensemble-agnostic restart vehicle:
            # _load_state_into_simulation transfers positions/velocities/box
            # without re-applying barostat parameters, so NPT ↔ NVT switches
            # are safe. Binary .chk restart, by contrast, requires the
            # System and integrator to be byte-identical. Partition the
            # signature keys accordingly.
            _restart_is_xml = restart_path.suffix == ".xml"
            _system_hard_keys: tuple[str, ...] = (
                "system_xml_sha256", "topology_pdb_sha256", "solvent_type",
                "is_membrane", "implicit_solvent", "hmr",
            ) if _restart_is_xml else (
                "system_xml_sha256", "topology_pdb_sha256", "solvent_type",
                "ensemble", "pressure_bar", "is_membrane",
                "implicit_solvent", "hmr",
            )
            _system_soft_keys: tuple[str, ...] = (
                ("ensemble", "pressure_bar") if _restart_is_xml else ()
            )
            if isinstance(restart_system_signature, dict):
                hard_mismatches = _signature_mismatches(
                    restart_system_signature, current_system_signature,
                    _system_hard_keys,
                )
                if hard_mismatches:
                    result["errors"].append(
                        "Restart system signature mismatch: " + "; ".join(hard_mismatches)
                    )
                if _system_soft_keys:
                    soft_mismatches = _signature_mismatches(
                        restart_system_signature, current_system_signature,
                        _system_soft_keys,
                    )
                    if soft_mismatches:
                        result["warnings"].append(
                            "Restart ensemble switch (XML state): "
                            + "; ".join(soft_mismatches)
                            + " — _load_state_into_simulation drops barostat "
                            "parameters; positions / velocities / box vectors "
                            "transfer cleanly across NPT ↔ NVT."
                        )
            if isinstance(restart_integrator_signature, dict):
                # Integrator settings are still hard-error material — temperature,
                # timestep, and friction must match for the saved velocities to
                # remain physically meaningful even under XML restart.
                mismatches = _signature_mismatches(
                    restart_integrator_signature,
                    current_integrator_signature,
                    ("integrator", "temperature_kelvin", "timestep_fs", "friction_per_ps"),
                )
                if mismatches:
                    result["errors"].append(
                        "Restart integrator signature mismatch: " + "; ".join(mismatches)
                    )
            if result["errors"]:
                return _fail_node_if_running(job_dir, node_id, result)
            # Use the ensemble-agnostic loader: XML is read via
            # XmlSerializer.deserialize and only positions/velocities/box
            # are transferred, so an NPT-saved state can resume into an
            # NVT System (and vice versa) without barostat-parameter
            # rejection. Binary .chk falls back to loadCheckpoint and
            # still requires identical System and GPU architecture.
            system_has_barostat = any(
                isinstance(f, (MonteCarloBarostat,
                               MonteCarloMembraneBarostat))
                for f in system.getForces()
            )
            if restart_path.suffix == ".xml":
                _kind = _detect_ensemble_mismatch(
                    restart_path, system_has_barostat
                )
                if _kind == "npt_state_nvt_system":
                    result["warnings"].append(
                        "Ensemble switch: the saved state contains NPT "
                        "barostat parameters but this run is NVT — barostat "
                        "parameters are dropped, positions/velocities/box "
                        "are preserved."
                    )
                elif _kind == "nvt_state_npt_system":
                    result["warnings"].append(
                        "Ensemble switch: NVT state into NPT system — the "
                        "barostat starts in its default relaxed state and "
                        "will re-equilibrate the volume over the first few ps."
                    )

            _load_info = _load_state_into_simulation(
                simulation, restart_path, is_periodic=is_periodic,
                temperature_kelvin=temperature_kelvin,
                random_seed=effective_random_seed,
            )
            if _load_info.get("velocities_rethermalized"):
                result["warnings"].append(
                    "Restart state had no velocities; re-thermalized "
                    f"at {temperature_kelvin} K."
                )
            if _load_info.get("box_vectors_dropped"):
                result["warnings"].append(
                    "Restart state contained periodic box vectors, but this "
                    "production is non-periodic; dropped box vectors."
                )
            # Restore the cumulative step counter from the *same*
            # ancestor whose artifact we just loaded — eq→prod and
            # prod→prod extension preserves the timeline. The state
            # file itself does not carry currentStep.
            if _node_mode:
                from mdclaw._node import read_ancestor_final_step
                anc_step = read_ancestor_final_step(
                    job_dir, node_id,
                    restart_node_id=_restart_from_node_id,
                )
                if anc_step is not None:
                    simulation.currentStep = anc_step
            logger.info(
                f"Restarted from {_load_info['format']} "
                f"(step {simulation.currentStep})"
            )
            if _pressure_bar_inherited:
                result["warnings"].append(
                    f"pressure_bar={pressure_bar} inherited from eq "
                    f"ancestor (final_ensemble=NPT)."
                )
            append_dcd = True
            result["restarted_from"] = restart_from
        else:
            append_dcd = False
            simulation.context.setPositions(xml_inputs.positions)
            # Set box vectors for periodic explicit solvent systems (required for PME)
            if is_periodic and not implicit_solvent:
                if xml_inputs.box_vectors is not None:
                    simulation.context.setPeriodicBoxVectors(*xml_inputs.box_vectors)

        # ``restraint_file`` is deprecated and ignored; production biases now
        # go through custom_force_script (added to the System before the
        # Simulation was built, above).
        if restraint_file:
            result["warnings"].append(
                "restraint_file is deprecated and ignored; use "
                "custom_force_script instead."
            )

        # Setup output file paths
        trajectory_file = out_dir / f"{pref}trajectory.{trajectory_format}"
        energy_file = out_dir / f"{pref}energy.dat"

        # Trajectory and energy reporters must fire on the SAME schedule
        # and share the SAME append state. A single report_interval and a
        # single do_append variable ensures they cannot drift apart (e.g.
        # energy_file existing but trajectory_file missing, or vice versa,
        # would otherwise silently diverge the `append=` argument).
        report_interval = int(output_frequency_ps / timestep_fs * 1000)
        do_append, _stale_warning, _ = _resolve_dcd_append_mode(
            trajectory_file, energy_file, append_dcd, _prior_failed
        )
        if _stale_warning:
            result["warnings"].append(_stale_warning)

        trajectory_reporter = None
        if trajectory_format.lower() == "dcd":
            trajectory_reporter = DCDReporter(
                str(trajectory_file), report_interval, append=do_append
            )
        else:
            from openmm.app import PDBReporter
            trajectory_reporter = PDBReporter(str(trajectory_file), report_interval)
        simulation.reporters.append(trajectory_reporter)

        energy_reporter = StateDataReporter(
            str(energy_file),
            report_interval,
            step=True,
            time=True,
            potentialEnergy=True,
            kineticEnergy=True,
            totalEnergy=True,
            temperature=True,
            volume=(ensemble == "NPT"),
            density=(ensemble == "NPT"),
            append=do_append,
        )
        simulation.reporters.append(energy_reporter)

        # Checkpoint + state reporters — periodic saves. Both fire on
        # the same interval. The .chk is bit-identical-restart material
        # (same GPU only); the .xml is the portable artifact that
        # downstream prod and `--continue-from` extensions will load.
        checkpoint_interval = max(report_interval * 10, 5000)
        simulation.reporters.append(
            CheckpointReporter(str(checkpoint_file), checkpoint_interval)
        )
        state_file = out_dir / f"{pref}state.xml"
        simulation.reporters.append(
            CheckpointReporter(str(state_file), checkpoint_interval, writeState=True)
        )
        result["checkpoint_file"] = str(checkpoint_file)
        result["state_file"] = str(state_file)

        # Custom-force / CV logging. Bias potential energy is read from the
        # dedicated force group every report; CV values (if the script
        # returned a cv_dict) are logged alongside. Shares the trajectory /
        # energy report interval and append state.
        _cv_reporter = None
        if _custom_force_loaded is not None:
            cv_file = out_dir / f"{pref}collective_variables.csv"
            _cv_reporter = CustomForceReporter(
                str(cv_file),
                report_interval,
                force_group=CUSTOM_FORCE_GROUP,
                evaluator=_custom_force_loaded["evaluator"],
                cv_names=_custom_force_loaded["cv_names"],
                append=do_append,
            )
            simulation.reporters.append(_cv_reporter)
            result["collective_variables_file"] = str(cv_file)
            meta_file = out_dir / f"{pref}collective_variables.meta.json"
            write_cv_metadata(
                str(meta_file),
                signature=_custom_force_loaded["signature"],
                cv_names=_custom_force_loaded["cv_names"],
                temperature_kelvin=temperature_kelvin,
                parameters=custom_force_parameters,
            )
            result["collective_variables_meta_file"] = str(meta_file)

        # Get initial energy
        state = simulation.context.getState(getEnergy=True)
        initial_energy = state.getPotentialEnergy()
        result["initial_energy_kj_mol"] = float(initial_energy._value)
        logger.info(f"Initial energy: {initial_energy}")

        # Minimize energy (skip on restart)
        if not restart_from:
            logger.info("Minimizing energy...")
            simulation.minimizeEnergy(maxIterations=5000)
            # Set initial velocities from Maxwell-Boltzmann distribution
            if random_seed is not None:
                simulation.context.setVelocitiesToTemperature(
                    temperature_kelvin * kelvin, random_seed
                )
            else:
                simulation.context.setVelocitiesToTemperature(
                    temperature_kelvin * kelvin
                )

        # Run simulation. See _compute_step_plan for the semantics —
        # simulation_time_ns is always "run this much in this call", and
        # eq→prod's legacy "full production length" meaning is preserved
        # because the eq checkpoint is saved with currentStep=0.
        plan = _compute_step_plan(
            simulation_time_ns, timestep_fs, simulation.currentStep
        )
        start_step = plan["start_step"]
        steps_to_run = plan["steps_to_run"]
        simulation_steps = plan["num_steps"]
        result["num_steps"] = simulation_steps
        result["start_step"] = start_step
        result["start_time_ns"] = plan["start_time_ns"]

        if steps_to_run <= 0:
            result["errors"].append(
                "simulation_time_ns is too short for the timestep; production would run 0 steps"
            )
            raise ValueError(result["errors"][-1])
        if report_interval <= 0:
            result["errors"].append(
                "output_frequency_ps is too small for the timestep; report interval is 0 steps"
            )
            raise ValueError(result["errors"][-1])
        if report_interval > steps_to_run:
            result["errors"].append(
                "output_frequency_ps is longer than this production segment; "
                "trajectory and energy reporters would not emit any frames"
            )
            raise ValueError(result["errors"][-1])

        logger.info(
            f"Running {steps_to_run} steps "
            f"(start_step={start_step}, target_total={simulation_steps})"
        )

        if steps_to_run > 0:
            simulation.step(steps_to_run)

        # Save final checkpoint + state (periodic reporter may not have
        # fired for short runs). Both formats so downstream can choose.
        _save_checkpoint_atomic(simulation, checkpoint_file)
        _save_state_atomic(simulation, state_file)
        logger.info(f"Final checkpoint saved: {checkpoint_file}")
        logger.info(f"Final state saved: {state_file}")

        result["steps_completed"] = simulation.currentStep

        # Get final energy and positions
        state = simulation.context.getState(getEnergy=True, getPositions=True)
        final_energy = state.getPotentialEnergy()
        result["final_energy_kj_mol"] = float(final_energy._value)
        logger.info(f"Final energy: {final_energy}")

        expected_reports = steps_to_run // report_interval if report_interval > 0 else 0
        if expected_reports > 0:
            fallback_outputs = []
            for reporter, output_path, label in (
                (trajectory_reporter, trajectory_file, "trajectory"),
                (energy_reporter, energy_file, "energy"),
            ):
                _flush_reporter_stream(reporter)
                if not output_path.exists() or output_path.stat().st_size == 0:
                    reporter.report(simulation, state)
                    _flush_reporter_stream(reporter)
                    fallback_outputs.append(label)
            if fallback_outputs:
                result["warnings"].append(
                    "Reporter outputs were empty after simulation; wrote final "
                    + ", ".join(fallback_outputs)
                    + " snapshot(s)."
                )

        for reporter in (trajectory_reporter, energy_reporter):
            _close_reporter_stream(reporter)
        if _cv_reporter is not None:
            _cv_reporter.close()

        # Save final structure
        final_pdb = out_dir / f"{pref}final_structure.pdb"
        positions = state.getPositions()
        # Restore the Amber/PTM/water residue names OpenMM's PDBFile loader
        # normalized away when topology.pdb was loaded (same shared exporter as
        # min/eq; pure text relabel, MD result/state.xml unaffected).
        from mdclaw.structure.pdb_utils import (
            render_simulation_pdb_preserving_resnames,
        )
        final_pdb.write_text(
            render_simulation_pdb_preserving_resnames(
                simulation.topology, positions, topology_pdb_file
            )
        )

        # Update result with file paths
        result["trajectory_file"] = str(trajectory_file)
        result["final_structure"] = str(final_pdb)
        result["energy_file"] = str(energy_file)

        # Trajectory and energy reporters share identical report_interval
        # and append state by construction, so they fire at the same steps
        # against the same simulation state. Either both files are populated
        # or both are empty — a divergence (one ok, one empty) would mean
        # the alignment has silently broken. Treat either missing file as a
        # hard failure so that divergence surfaces loudly rather than hiding
        # behind a warning.
        missing_outputs = []
        if expected_reports > 0:
            if not trajectory_file.exists() or trajectory_file.stat().st_size == 0:
                missing_outputs.append("trajectory")
            if not energy_file.exists() or energy_file.stat().st_size == 0:
                missing_outputs.append("energy")

        if missing_outputs:
            result["errors"].append(
                "Reporter outputs missing after simulation: "
                + ", ".join(missing_outputs)
            )
        else:
            energy_rows = _count_state_data_rows(energy_file)
            result["energy_rows"] = energy_rows
            if expected_reports > 0 and energy_rows < expected_reports:
                result["errors"].append(
                    f"Energy reporter wrote {energy_rows} rows, expected at least {expected_reports}"
                )
            else:
                result["success"] = True

        logger.info(f"Simulation complete. Trajectory saved: {trajectory_file}")

    except _ModernSystemContractError as exc:
        logger.error("Production aborted by modern-system contract: %s", exc)
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as e:
        logger.error(f"MD simulation failed: {e}")
        result["errors"].append(f"MD simulation failed: {type(e).__name__}: {str(e)}")

    # Node state update
    if _node_mode:
        _record_production_node_result(
            result=result,
            job_dir=job_dir,
            node_id=node_id,
            simulation_time_ns=simulation_time_ns,
            temperature_kelvin=temperature_kelvin,
            pressure_bar=pressure_bar,
            platform=platform,
            hmr=hmr,
            timestep_fs=timestep_fs,
            output_frequency_ps=output_frequency_ps,
            random_seed=random_seed,
            custom_force_script_artifact=_custom_force_script_artifact,
        )

    return result
