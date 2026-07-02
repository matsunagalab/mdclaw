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

import numpy as np  # noqa: E402
from mdclaw._common import (  # noqa: E402
    create_validation_error,
    ensure_directory,
    generate_job_id,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

from mdclaw.simulation._base import _check_topology_implicit_solvent_match, _fail_node_if_running, _node_artifact_path, _resolve_implicit_solvent_model  # noqa: E402
from mdclaw.simulation.integrator_plan import _resolve_equilibration_stage_steps  # noqa: E402
from mdclaw.simulation.restart import _close_reporter_stream, _load_state_into_simulation, _resolve_restart_node_id_for_run, _restart_node_type_for_run, _restart_random_seed, _save_checkpoint_atomic, _save_state_atomic  # noqa: E402
from mdclaw.simulation.xml_contract import _ModernSystemContractError, _deserialize_xml_system, _effective_pressure_bar, _integrator_signature, _load_xml_topology_inputs, _system_signature, _validate_xml_system_contract  # noqa: E402


@node_tool
def run_equilibration(
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = 1.0,
    nvt_steps: Optional[int] = None,
    npt_steps: Optional[int] = None,
    nvt_time_ns: Optional[float] = None,
    npt_time_ns: Optional[float] = None,
    restraint_atoms: str = "CA",
    restraint_force_constant: float = 100.0,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    is_membrane: bool = False,
    implicit_solvent: Optional[str] = None,
    platform: str = "auto",
    device_index: Optional[str] = None,
    random_seed: Optional[int] = None,
    hmr: bool = True,
    timestep_fs: float = 4.0,
    restart_from: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Run equilibration protocol with positional restraints.

    Both stages run at ``timestep_fs`` (default 4 fs) with Hydrogen Mass
    Repartitioning (``hmr=True`` by default), matching run_production's
    default integrator so that the saved checkpoint can be loaded directly
    by run_production without rebuilding the System.

    The protocol depends on the production ensemble:
      - Explicit water + NPT production (pressure_bar > 0):
          Stage 1 (NVT): Heat with restraints, timestep_fs + HMR
          Stage 2 (NPT): Equilibrate density with restraints, timestep_fs + HMR
      - Explicit water + NVT production (pressure_bar = 0 or None):
          Stage 1 (NVT) only
      - Implicit solvent:
          Stage 1 (NVT) only (NPT not applicable)

    The restraint is a harmonic potential on the initial positions using
    OpenMM's CustomExternalForce with periodicdistance. At the end of the
    protocol, a production-matching "clean" Simulation is built (same
    System/Integrator as run_production, no restraint force), the
    equilibrated positions/velocities/box are transferred into it, and its
    checkpoint is saved as ``equilibrated.chk``. Pass this checkpoint to
    ``run_production --restart-from`` to inherit the equilibrated state.

    Args:
        system_xml_file: Path to ``system.xml`` from the topo ancestor
            (``build_amber_system`` / ``build_openmm_system``). Source of
            truth for force-field parameters at run time — the run side
            never reconstructs the System from ForceField XML.
        topology_pdb_file: Path to ``topology.pdb`` from the same topo
            ancestor; provides the OpenMM ``Topology`` for restraint atom
            selection and final-structure writing.
        state_xml_file: Path to ``state.xml`` from the same topo ancestor.
            Carries the build-time minimized positions / velocities / box
            and is preferred over the PDB coordinates when present.
        temperature_kelvin: Temperature in Kelvin (default: 300.0)
        pressure_bar: Pressure in bar. Controls whether NPT stage runs:
            - > 0 (e.g., 1.0): NVT + NPT equilibration (for NPT production)
            - 0 or None: NVT only (for NVT production or implicit solvent)
            Default: 1.0
        nvt_steps: Number of NVT heating steps. Low-level explicit-step
            override; mutually exclusive with nvt_time_ns. Defaults to
            250000 (= 1 ns at 4 fs) when neither is specified.
        npt_steps: Number of NPT equilibration steps. Low-level explicit-step
            override; mutually exclusive with npt_time_ns. Defaults to
            250000 (= 1 ns at 4 fs) when neither is specified, and is set
            to 0 when NPT is not applicable.
        nvt_time_ns: User-facing NVT duration in ns. Prefer this for
            natural-language requests such as "0.1 ns NVT"; the tool
            converts it to steps using timestep_fs.
        npt_time_ns: User-facing NPT duration in ns. Prefer this for
            natural-language requests such as "0.1 ns NPT"; the tool
            converts it to steps using timestep_fs.
        restraint_atoms: Atom selection for restraints. Options:
            - "CA": alpha carbons only (default, recommended)
            - "backbone": backbone heavy atoms (N, CA, C, O)
            - "heavy": all non-hydrogen atoms
        restraint_force_constant: Restraint force constant in kJ/mol/nm^2
            (default: 100.0). Higher values = tighter restraints.
        name: Optional name prefix for output files
        output_dir: Output directory
        is_membrane: Set True for membrane systems (uses MonteCarloMembraneBarostat).
            Must match run_production's ``is_membrane`` to share the checkpoint.
        implicit_solvent: GB model name. If set, only NVT stage runs (no NPT).
            Must match run_production's ``implicit_solvent`` to share the checkpoint.
        platform: OpenMM platform - "CUDA", "OpenCL", "CPU", "Reference", or "auto"
        device_index: GPU device index (e.g. "0")
        random_seed: Random number seed for reproducibility
        hmr: Hydrogen Mass Repartitioning. When True (default), creates the
            System with ``hydrogenMass=4.0 amu`` so that ``timestep_fs=4.0``
            is stable. Must match run_production's ``hmr`` so the checkpoint
            produced here can be loaded (System particle masses must agree).
        timestep_fs: Integration timestep in femtoseconds (default: 4.0).
            Used for both NVT and NPT stages and the clean checkpoint
            Simulation. Must match run_production's ``timestep_fs`` for a
            clean handoff.
        restart_from: Path to a saved OpenMM state (``.xml`` preferred,
            ``.chk`` fallback) to resume from. When set, the pre-NVT
            staged minimization and warmup are skipped, and
            positions/velocities/box are loaded via the
            ensemble-agnostic loader (so an NPT-saved state can resume
            into an NVT stage and vice versa). In node mode this is
            auto-resolved from the nearest ``min``/``eq``/``prod``
            ancestor's ``state`` artifact. A ``min`` source skips only
            coordinate minimization; prior ``eq``/``prod`` sources skip
            the full minimization/warmup prelude for chaining.

    Returns:
        dict with:
          - success: bool
          - output_dir: str
          - final_structure: str - Path to equilibrated PDB
          - state_file: str - Path to OpenMM state XML. Kept for
              reproducibility/audit only — NOT used for restart; pass
              ``checkpoint_file`` to run_production instead.
          - checkpoint_file: str - Path to an OpenMM binary checkpoint
              written from a production-matching System (no restraints,
              HMR, same integrator). Pass this to
              run_production --restart-from to start production from the
              equilibrated coordinates/velocities/box without re-minimization.
          - nvt_steps: int - NVT steps completed
          - npt_steps: int - NPT steps completed
          - restraint_atoms: str - Atom selection used
          - restraint_count: int - Number of restrained atoms
          - errors: list[str]
          - warnings: list[str]
    """
    pressure_bar = _effective_pressure_bar(pressure_bar, implicit_solvent)

    try:
        (
            nvt_steps,
            requested_nvt_time_ns,
            effective_nvt_time_ns,
        ) = _resolve_equilibration_stage_steps(
            stage_name="nvt",
            steps=nvt_steps,
            time_ns=nvt_time_ns,
            default_steps=250000,
            timestep_fs=timestep_fs,
        )
        (
            npt_steps,
            requested_npt_time_ns,
            effective_npt_time_ns,
        ) = _resolve_equilibration_stage_steps(
            stage_name="npt",
            steps=npt_steps,
            time_ns=npt_time_ns,
            default_steps=250000,
            timestep_fs=timestep_fs,
        )
    except ValueError as exc:
        return create_validation_error(
            "equilibration_time",
            str(exc),
            expected=(
                "Use --nvt-time-ns/--npt-time-ns for durations, or "
                "--nvt-steps/--npt-steps for explicit step counts, but do not "
                "set both for the same stage"
            ),
            actual=(
                f"nvt_time_ns={nvt_time_ns!r}, nvt_steps={nvt_steps!r}, "
                f"npt_time_ns={npt_time_ns!r}, npt_steps={npt_steps!r}, "
                f"timestep_fs={timestep_fs!r}"
            ),
            hints=[
                "For a user-facing duration like 0.1 ns NVT, pass only --nvt-time-ns 0.1.",
                "For explicit step counts, remove the matching time flag.",
            ],
            code="equilibration_time_step_conflict",
        )

    _restart_from_node_id = None
    _restart_from_node_type = None

    # Auto-resolve inputs from DAG when in node mode
    if job_dir and node_id:
        from mdclaw._node import (
            begin_node, fail_node,
            resolve_node_inputs, validate_node_execution_context,
        )
        _inputs = resolve_node_inputs(job_dir, node_id, "eq")
        if "input_resolution_error" in _inputs:
            # Mirror run_production: an unresolvable DAG must transit
            # through fail_node so the eq node ends up ``failed`` rather
            # than perpetually ``pending``. ``begin_node`` is safe to call
            # before ``fail_node`` even though no work has run yet — the
            # node lifecycle is "running → failed", which is what we want
            # the audit trail to record.
            err = _inputs["input_resolution_error"]
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=[err])
            return create_validation_error(
                "job_dir/node_id",
                err,
                expected="Completed topo ancestor with system.xml + topology.pdb [+ state.xml] triple",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            )
        # An explicit ``continue_from`` pointing at an incomplete eq /
        # prod ancestor surfaces here as ``restart_from_error``. Mirror
        # the run_production handling: fail the node cleanly with the
        # structured message rather than silently dropping the request.
        if "restart_from_error" in _inputs:
            err = _inputs["restart_from_error"]
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=[err])
            return create_validation_error(
                "restart_from",
                err,
                expected="Completed continue_from eq/prod node with state or checkpoint artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                code="restart_from_unavailable",
            )
        if not system_xml_file and "system_xml_file" in _inputs:
            system_xml_file = _inputs["system_xml_file"]
        if not topology_pdb_file and "topology_pdb_file" in _inputs:
            topology_pdb_file = _inputs["topology_pdb_file"]
        if not state_xml_file and "state_xml_file" in _inputs:
            state_xml_file = _inputs["state_xml_file"]
        if not is_membrane and _inputs.get("is_membrane"):
            is_membrane = True
        # min → eq and eq → eq chaining: when a min/eq/prod ancestor exposes
        # a state artifact, resume from it. A min source skips only the
        # minimization part; prior eq/prod sources skip the full prelude.
        # Legacy first eq nodes from topo still run from topo state.xml.
        # Explicit ``--restart-from`` always wins over the resolver's
        # auto-pick; we then trust the resolver's
        # ``restart_from_node_id`` only when *we* used the resolver's
        # path. For an explicit path we re-derive the matching ancestor
        # via path comparison so ``read_ancestor_final_step`` cannot
        # bind ``simulation.currentStep`` to a different node than the
        # one whose state/checkpoint we actually load.
        _explicit_restart_from = bool(restart_from)
        if not _explicit_restart_from and "restart_from" in _inputs:
            restart_from = _inputs["restart_from"]
        _restart_from_node_id = _resolve_restart_node_id_for_run(
            job_dir=job_dir, node_id=node_id,
            restart_from=restart_from,
            explicit_restart_from=_explicit_restart_from,
            inputs=_inputs,
        )
        _restart_from_node_type = (
            _inputs.get("restart_from_node_type")
            if not _explicit_restart_from
            else _restart_node_type_for_run(job_dir, _restart_from_node_id)
        )
        # Catch implicit-solvent model mismatches between the topo node's
        # build-time metadata and the runtime --implicit-solvent flag
        # before any System is built. The run-side XML system validator's GB-force presence check
        # cannot tell ``OBC2``-built from ``GBn2``-built (both carry a
        # CustomGBForce), so a silent model swap would otherwise mis-
        # simulate quietly.
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
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "eq",
            actual_conditions={
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "nvt_steps": nvt_steps,
                "npt_steps": npt_steps,
                "nvt_time_ns": effective_nvt_time_ns,
                "npt_time_ns": effective_npt_time_ns,
                "requested_nvt_time_ns": requested_nvt_time_ns,
                "requested_npt_time_ns": requested_npt_time_ns,
                "restraint_atoms": restraint_atoms,
                "restraint_force_constant": restraint_force_constant,
                "is_membrane": is_membrane,
                "implicit_solvent": implicit_solvent,
                "platform": platform,
                "device_index": device_index,
                "random_seed": random_seed,
                "hmr": hmr,
                "timestep_fs": timestep_fs,
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}

    # The XML triple is the only supported topology contract on the run
    # side. ``state_xml_file`` is optional (built-in to ``build_amber_system``
    # output but not required for restart-from-checkpoint flows that
    # supply positions through ``--restart-from``).
    if not (system_xml_file and topology_pdb_file):
        return create_validation_error(
            "topology_inputs",
            "system_xml_file and topology_pdb_file are required",
            expected="XML triple from build_amber_system / build_openmm_system",
            actual=(
                f"system_xml_file={system_xml_file!r}, "
                f"topology_pdb_file={topology_pdb_file!r}"
            ),
            hints=["Run build_amber_system first or execute in node mode from an eq node."],
            code="missing_xml_topology_inputs",
        )

    logger.info(f"Starting equilibration at {temperature_kelvin}K")

    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "final_structure": None,
        "state_file": None,
        "checkpoint_file": None,
        "nvt_steps": 0,
        "npt_steps": 0,
        "requested_nvt_time_ns": requested_nvt_time_ns,
        "requested_npt_time_ns": requested_npt_time_ns,
        "effective_nvt_time_ns": effective_nvt_time_ns,
        "effective_npt_time_ns": effective_npt_time_ns,
        "timestep_fs": timestep_fs,
        "restraint_atoms": restraint_atoms,
        "restraint_count": 0,
        "relaxation_protocol": None,
        "low_temperature_warmup_steps": 0,
        "nan_failure_diagnostics": None,
        "platform": None,
        "restart_from_node_id": _restart_from_node_id,
        "restart_from_node_type": _restart_from_node_type,
        "errors": [],
        "warnings": [],
    }

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
    restart_path = Path(restart_from).resolve() if restart_from else None
    if restart_path is not None and not restart_path.is_file():
        result["errors"].append(f"Restart file not found: {restart_from}")
        return _fail_node_if_running(job_dir, node_id, result)

    try:
        from openmm.app import (
            Simulation, StateDataReporter,
            HCT, OBC1, OBC2, GBn, GBn2,
        )
        from openmm import (
            LangevinMiddleIntegrator, MonteCarloBarostat,
            MonteCarloMembraneBarostat, Platform, CustomExternalForce,
        )
        from openmm.unit import (
            nanometer, kelvin, picosecond, femtoseconds, bar,
            kilojoules_per_mole,
        )
    except ImportError:
        result["errors"].append("OpenMM not installed")
        return _fail_node_if_running(job_dir, node_id, result)

    # Canonical implicit-solvent names → OpenMM symbols. Resolved by
    # ``_resolve_implicit_solvent_model`` (same alias set as
    # forcefield_catalog), which never silently falls back to OBC2 when
    # the lookup misses.
    IMPLICIT_MODELS = {
        "HCT": HCT, "OBC1": OBC1, "OBC2": OBC2, "GBn": GBn, "GBn2": GBn2,
    }
    RESTRAINT_SELECTIONS = {
        "CA": {"CA"},
        "backbone": {"N", "CA", "C", "O"},
        "heavy": None,  # all non-hydrogen
    }

    # Restraints anchor *solute* atoms only. Iterating over every atom in
    # the topology (which includes solvent waters, ions, and OPC virtual
    # sites) would otherwise wrongly restrain the bulk water oxygens or
    # crash on virtual particles whose `element` is None. Filter by
    # residue name against the standard solvent set.
    from mdclaw.chemistry_constants import (
        COMMON_IONS,
        WATER_NAMES,
    )
    _NON_SOLUTE_RESNAMES = WATER_NAMES | COMMON_IONS

    def _is_solute_atom(atom) -> bool:
        return atom.residue.name.upper() not in _NON_SOLUTE_RESNAMES

    try:
        # Set up output directory
        _node_mode = job_dir and node_id
        if _node_mode:
            from mdclaw._node import begin_node
            out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            begin_node(job_dir, node_id)
        elif output_dir:
            out_dir = Path(output_dir) / "equilibration"
        else:
            out_dir = WORKING_DIR / job_id / "equilibration"
        ensure_directory(out_dir)
        result["output_dir"] = str(out_dir)

        # Load topology + initial positions / box vectors from the XML
        # triple. ``state.xml`` (when present) carries the build-time
        # minimized state; otherwise the PDB coordinates are used.
        logger.info("Loading XML topology triple (system.xml + topology.pdb + state.xml)")
        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=str(system_xml_path),
            topology_pdb_file=str(topology_pdb_path),
            state_xml_file=str(state_xml_path) if state_xml_path else None,
        )

        is_periodic = xml_inputs.is_periodic
        if implicit_solvent:
            solvent_type = "implicit"
        elif is_periodic:
            solvent_type = "explicit"
        else:
            solvent_type = "vacuum"
            result["errors"].append(
                "Non-periodic topology without implicit_solvent would run vacuum equilibration. "
                "Pass --implicit-solvent for GB simulations or build an explicit-solvent topology."
            )
            return _fail_node_if_running(job_dir, node_id, result)

        # Resolve the requested implicit-solvent symbol up-front so a
        # typo in the runtime flag fails before any System is built.
        # The build vs runtime model match is already enforced by the
        # topology metadata guard upstream; here we just need the symbol.
        if implicit_solvent:
            _gb_model, gb_err = _resolve_implicit_solvent_model(
                implicit_solvent, IMPLICIT_MODELS
            )
            if gb_err:
                result["errors"].extend(gb_err["errors"])
                result["code"] = gb_err["code"]
                return _fail_node_if_running(job_dir, node_id, result)

        if hmr:
            logger.info(f"HMR enabled: hydrogenMass=4.0 amu (timestep={timestep_fs}fs)")

        # Determine whether to run NPT stage
        # NPT equilibration only when production will use NPT (pressure_bar > 0)
        run_npt = (pressure_bar is not None and pressure_bar > 0
                   and not implicit_solvent and is_periodic)
        if not run_npt:
            npt_steps = 0
            effective_npt_time_ns = 0.0
            result["npt_steps"] = 0
            result["effective_npt_time_ns"] = 0.0
            if requested_npt_time_ns is not None and requested_npt_time_ns > 0:
                result["warnings"].append(
                    "npt_time_ns was provided, but NPT is disabled for this "
                    "equilibration context; no NPT steps will run."
                )
            if implicit_solvent:
                logger.info("Implicit solvent: NVT equilibration only")
            elif not pressure_bar or pressure_bar == 0:
                logger.info("NVT production planned: NVT equilibration only")

        # --- Stage 1: NVT heating ---
        logger.info(
            f"Stage 1: NVT heating ({nvt_steps} steps, {timestep_fs} fs, "
            f"restraints on {restraint_atoms})"
        )

        # Deserialize a fresh System for NVT. The build-time choices
        # (forcefield, constraints, nonbondedMethod, HMR, GB) are baked
        # into ``system.xml`` and validated against the run-time request
        # below; the run side never reconstructs the System.
        try:
            system_nvt = _deserialize_xml_system(xml_inputs)
            _validate_xml_system_contract(
                system_nvt, xml_inputs.topology,
                hmr_request=hmr,
                implicit_solvent_request=implicit_solvent,
            )
        except _ModernSystemContractError as exc:
            result["errors"].append(str(exc))
            result["code"] = exc.code
            return _fail_node_if_running(job_dir, node_id, result)
        # Stage variants (implicit / periodic / vacuum) are reflected in
        # integrator + barostat choices below; the System itself is
        # build-time fixed.

        # Add positional restraints
        restraint = CustomExternalForce(
            'k*periodicdistance(x, y, z, x0, y0, z0)^2'
        )
        restraint.addPerParticleParameter('k')
        restraint.addPerParticleParameter('x0')
        restraint.addPerParticleParameter('y0')
        restraint.addPerParticleParameter('z0')

        allowed_names = RESTRAINT_SELECTIONS.get(restraint_atoms, {"CA"})
        positions = xml_inputs.positions
        restraint_count = 0

        for atom in xml_inputs.topology.atoms():
            if not _is_solute_atom(atom):
                continue
            if allowed_names is None:
                # "heavy" = all non-hydrogen. Virtual sites (e.g. OPC's
                # EPW dummy particle) have no element — skip them too.
                if atom.element is None or atom.element.symbol == 'H':
                    continue
            elif atom.name not in allowed_names:
                continue

            k_value = restraint_force_constant * kilojoules_per_mole / (nanometer * nanometer)
            restraint.addParticle(atom.index, [
                k_value,
                positions[atom.index][0],
                positions[atom.index][1],
                positions[atom.index][2],
            ])
            restraint_count += 1

        system_nvt.addForce(restraint)
        result["restraint_count"] = restraint_count
        logger.info(f"Applied restraints to {restraint_count} atoms ({restraint_atoms})")

        restart_seed_step: Optional[int] = None
        if restart_path is not None and _node_mode:
            from mdclaw._node import read_ancestor_final_step
            restart_seed_step = read_ancestor_final_step(
                job_dir, node_id,
                restart_node_id=_restart_from_node_id,
            )
        effective_random_seed = (
            _restart_random_seed(random_seed, restart_seed_step)
            if restart_path is not None else random_seed
        )
        if random_seed is not None:
            result["random_seed"] = random_seed
            if restart_path is not None:
                result["effective_random_seed"] = effective_random_seed
                result["random_seed_restart_offset"] = max(
                    1, int(restart_seed_step or 0)
                )

        # NVT integrator (matches run_production: LangevinMiddle, same timestep, HMR via system)
        integrator_nvt = LangevinMiddleIntegrator(
            temperature_kelvin * kelvin,
            1.0 / picosecond,
            timestep_fs * femtoseconds,
        )
        if effective_random_seed is not None:
            integrator_nvt.setRandomNumberSeed(effective_random_seed)

        # Platform selection
        PLATFORM_MAP = {"cuda": "CUDA", "opencl": "OpenCL", "cpu": "CPU", "reference": "Reference"}
        platform_obj = None
        platform_properties = {}
        if platform.lower() != "auto":
            plat_key = platform.lower()
            if plat_key in PLATFORM_MAP:
                platform_obj = Platform.getPlatformByName(PLATFORM_MAP[plat_key])
                if device_index and plat_key in ("cuda", "opencl"):
                    platform_properties["DeviceIndex"] = device_index

        if platform_obj:
            sim_nvt = Simulation(xml_inputs.topology, system_nvt, integrator_nvt,
                                 platform_obj, platform_properties)
        else:
            sim_nvt = Simulation(xml_inputs.topology, system_nvt, integrator_nvt)

        result["platform"] = sim_nvt.context.getPlatform().getName()
        nvt_energy_file = out_dir / "nvt_energy.dat"
        if nvt_steps > 0:
            sim_nvt.reporters.append(StateDataReporter(
                str(nvt_energy_file),
                max(1, nvt_steps // 100),
                step=True,
                time=True,
                potentialEnergy=True,
                kineticEnergy=True,
                totalEnergy=True,
                temperature=True,
                volume=is_periodic,
                density=is_periodic,
            ))

        if restart_path is not None:
            # eq → eq chaining: pull positions/velocities/box from the
            # ancestor's saved state (XML preferred). The loader is
            # ensemble-agnostic — an NPT-saved state lands cleanly into
            # this NVT system because barostat parameters are dropped.
            _eq_load_info = _load_state_into_simulation(
                sim_nvt, restart_path, is_periodic=is_periodic,
                temperature_kelvin=temperature_kelvin,
                random_seed=effective_random_seed,
            )
            if _eq_load_info.get("velocities_rethermalized"):
                result["warnings"].append(
                    "Restart state had no velocities; re-thermalized "
                    f"at {temperature_kelvin} K."
                )
            if _eq_load_info.get("box_vectors_dropped"):
                result["warnings"].append(
                    "Restart state contained periodic box vectors, but this "
                    "equilibration is non-periodic; dropped box vectors."
                )
            logger.info(
                f"Equilibration restarted from {_eq_load_info['format']} "
                f"({restart_path})"
            )
            if _node_mode:
                from mdclaw._node import read_ancestor_final_step
                anc_step = read_ancestor_final_step(
                    job_dir, node_id,
                    restart_node_id=_restart_from_node_id,
                )
                if anc_step is not None:
                    sim_nvt.currentStep = anc_step
            result["restarted_from"] = str(restart_path)
            result["restart_from_node_id"] = _restart_from_node_id
            result["restart_from_node_type"] = _restart_from_node_type
        else:
            sim_nvt.context.setPositions(positions)
            if is_periodic and xml_inputs.box_vectors is not None:
                sim_nvt.context.setPeriodicBoxVectors(*xml_inputs.box_vectors)

        def _finite_energy_check(stage: str) -> dict:
            state = sim_nvt.context.getState(getEnergy=True, getForces=True)
            potential = state.getPotentialEnergy().value_in_unit(kilojoules_per_mole)
            forces = state.getForces(asNumpy=True)
            force_values = forces.value_in_unit(kilojoules_per_mole / nanometer)
            max_force = float(np.max(np.linalg.norm(force_values, axis=1))) if len(force_values) else 0.0
            check = {
                "stage": stage,
                "potential_energy_kj_per_mol": float(potential),
                "max_force_kj_per_mol_nm": max_force,
                "finite": bool(np.isfinite(potential) and np.isfinite(max_force)),
            }
            if not check["finite"]:
                result["nan_failure_diagnostics"] = {
                    "stage": stage,
                    "solvent_type": solvent_type,
                    "implicit_solvent": implicit_solvent,
                    "potential_energy_kj_per_mol": check["potential_energy_kj_per_mol"],
                    "max_force_kj_per_mol_nm": check["max_force_kj_per_mol_nm"],
                    "recommended_next_action": "inspect/repair the input structure or ligand parameters",
                }
                raise RuntimeError(f"Non-finite energy/force detected during {stage}")
            return check

        # Universal pre-NVT relaxation protocol. Legacy topo -> eq runs the
        # full prelude. New min -> eq runs only the low-temperature warmup,
        # because coordinate minimization already belongs to the min node.
        # eq/prod restarts skip the prelude to preserve the loaded state.
        restart_from_min_node = (
            restart_path is not None and _restart_from_node_type == "min"
        )
        if restart_path is None:
            logger.info("Running standard staged minimization before NVT...")
            relaxation_checks = []
            relaxation_checks.append(_finite_energy_check("initial"))
            for stage_name, max_iterations in (
                ("staged_minimization_a", 500),
                ("staged_minimization_b", 2000),
                ("staged_minimization_c", 5000),
            ):
                logger.info(f"{stage_name}: minimizeEnergy(maxIterations={max_iterations})")
                sim_nvt.minimizeEnergy(maxIterations=max_iterations)
                relaxation_checks.append(_finite_energy_check(stage_name))

            warmup_steps = min(1000, max(0, nvt_steps // 20))
            low_temperature = max(10.0, min(50.0, temperature_kelvin * 0.2))
            if warmup_steps > 0:
                logger.info(
                    f"Low-temperature NVT warmup: {warmup_steps} steps at {low_temperature:.1f} K"
                )
                integrator_nvt.setTemperature(low_temperature * kelvin)
                sim_nvt.context.setVelocitiesToTemperature(low_temperature * kelvin)
                sim_nvt.step(warmup_steps)
                relaxation_checks.append(_finite_energy_check("low_temperature_warmup"))
                integrator_nvt.setTemperature(temperature_kelvin * kelvin)
            result["low_temperature_warmup_steps"] = warmup_steps
            result["relaxation_protocol"] = {
                "name": "standard_staged_minimization_low_temperature_warmup",
                "applies_to": "all_nvt_equilibration",
                "stages": relaxation_checks,
                "low_temperature_kelvin": low_temperature if warmup_steps > 0 else None,
            }
            # Fresh start: reseed velocities at target temperature.
            sim_nvt.context.setVelocitiesToTemperature(temperature_kelvin * kelvin)
        elif restart_from_min_node:
            logger.info(
                "Minimized-state restart: skipping minimization and running "
                "low-temperature NVT warmup before normal NVT."
            )
            relaxation_checks = [_finite_energy_check("min_node_state")]
            warmup_steps = min(1000, max(0, nvt_steps // 20))
            low_temperature = max(10.0, min(50.0, temperature_kelvin * 0.2))
            if warmup_steps > 0:
                logger.info(
                    f"Low-temperature NVT warmup: {warmup_steps} steps "
                    f"at {low_temperature:.1f} K"
                )
                integrator_nvt.setTemperature(low_temperature * kelvin)
                sim_nvt.context.setVelocitiesToTemperature(low_temperature * kelvin)
                sim_nvt.step(warmup_steps)
                relaxation_checks.append(_finite_energy_check("low_temperature_warmup"))
                integrator_nvt.setTemperature(temperature_kelvin * kelvin)
            result["low_temperature_warmup_steps"] = warmup_steps
            result["relaxation_protocol"] = {
                "name": "min_node_low_temperature_warmup",
                "applies_to": "min_to_eq_equilibration",
                "stages": relaxation_checks,
                "low_temperature_kelvin": low_temperature if warmup_steps > 0 else None,
                "minimization_source_node_id": _restart_from_node_id,
            }
            sim_nvt.context.setVelocitiesToTemperature(temperature_kelvin * kelvin)
        else:
            logger.info(
                "Restart mode: skipping pre-NVT minimization and warmup; "
                "velocities and box are inherited from the saved state."
            )
            result["low_temperature_warmup_steps"] = 0
            result["relaxation_protocol"] = {
                "name": "skipped_due_to_restart",
                "applies_to": "all_nvt_equilibration",
                "stages": [],
                "low_temperature_kelvin": None,
            }

        # NVT run
        sim_nvt.step(nvt_steps)
        _finite_energy_check("normal_nvt_complete")
        for reporter in sim_nvt.reporters:
            _close_reporter_stream(reporter)
        result["nvt_steps"] = nvt_steps
        logger.info(f"NVT heating complete ({nvt_steps} steps)")

        # Save NVT state — also capture box vectors so that the NPT stage
        # inherits the box from the most-recent simulation, not the
        # build-time XML box. This matters when the NVT simulation itself
        # was restarted from a prior NPT state with a different box.
        nvt_state = sim_nvt.context.getState(
            getPositions=True,
            getVelocities=True,
            enforcePeriodicBox=is_periodic,
        )
        nvt_positions = nvt_state.getPositions()
        nvt_velocities = nvt_state.getVelocities()
        nvt_box_vectors = (
            nvt_state.getPeriodicBoxVectors() if is_periodic else None
        )

        # --- Stage 2: NPT equilibration (same timestep + HMR, with restraints) ---
        if npt_steps > 0:
            logger.info(
                f"Stage 2: NPT equilibration ({npt_steps} steps, {timestep_fs} fs, "
                f"restraints on {restraint_atoms})"
            )

            # Fresh System for the NPT stage. The build-time forces +
            # constraints are baked into ``system.xml``; we deserialize
            # again so the restraint + barostat we add here do not bleed
            # into the production-clean handoff System below.
            try:
                system_npt = _deserialize_xml_system(xml_inputs)
                _validate_xml_system_contract(
                    system_npt, xml_inputs.topology,
                    hmr_request=hmr,
                    implicit_solvent_request=implicit_solvent,
                )
            except _ModernSystemContractError as exc:
                result["errors"].append(str(exc))
                result["code"] = exc.code
                return _fail_node_if_running(job_dir, node_id, result)

            # Add same restraints
            restraint_npt = CustomExternalForce(
                'k*periodicdistance(x, y, z, x0, y0, z0)^2'
            )
            restraint_npt.addPerParticleParameter('k')
            restraint_npt.addPerParticleParameter('x0')
            restraint_npt.addPerParticleParameter('y0')
            restraint_npt.addPerParticleParameter('z0')

            for atom in xml_inputs.topology.atoms():
                if not _is_solute_atom(atom):
                    continue
                if allowed_names is None:
                    if atom.element is None or atom.element.symbol == 'H':
                        continue
                elif atom.name not in allowed_names:
                    continue
                k_value = restraint_force_constant * kilojoules_per_mole / (nanometer * nanometer)
                restraint_npt.addParticle(atom.index, [
                    k_value,
                    positions[atom.index][0],
                    positions[atom.index][1],
                    positions[atom.index][2],
                ])

            system_npt.addForce(restraint_npt)

            # Add barostat
            if is_membrane:
                npt_barostat = MonteCarloMembraneBarostat(
                    pressure_bar * bar, 0.0 * bar * nanometer,
                    temperature_kelvin * kelvin,
                    MonteCarloMembraneBarostat.XYIsotropic,
                    MonteCarloMembraneBarostat.ZFree,
                    25,
                )
            else:
                npt_barostat = MonteCarloBarostat(
                    pressure_bar * bar, temperature_kelvin * kelvin,
                )
            if effective_random_seed is not None:
                npt_barostat.setRandomNumberSeed(effective_random_seed)
            system_npt.addForce(npt_barostat)

            # NPT integrator (matches run_production: LangevinMiddle, same timestep)
            integrator_npt = LangevinMiddleIntegrator(
                temperature_kelvin * kelvin,
                1.0 / picosecond,
                timestep_fs * femtoseconds,
            )
            if effective_random_seed is not None:
                integrator_npt.setRandomNumberSeed(effective_random_seed)

            if platform_obj:
                sim_npt = Simulation(xml_inputs.topology, system_npt, integrator_npt,
                                     platform_obj, platform_properties)
            else:
                sim_npt = Simulation(xml_inputs.topology, system_npt, integrator_npt)
            npt_energy_file = out_dir / "npt_energy.dat"
            sim_npt.reporters.append(StateDataReporter(
                str(npt_energy_file),
                max(1, npt_steps // 100),
                step=True,
                time=True,
                potentialEnergy=True,
                kineticEnergy=True,
                totalEnergy=True,
                temperature=True,
                volume=True,
                density=True,
            ))

            sim_npt.context.setPositions(nvt_positions)
            sim_npt.context.setVelocities(nvt_velocities)
            if is_periodic and nvt_box_vectors is not None:
                sim_npt.context.setPeriodicBoxVectors(*nvt_box_vectors)
            elif is_periodic and xml_inputs.box_vectors is not None:
                sim_npt.context.setPeriodicBoxVectors(*xml_inputs.box_vectors)

            sim_npt.step(npt_steps)
            for reporter in sim_npt.reporters:
                _close_reporter_stream(reporter)
            result["npt_steps"] = npt_steps
            logger.info(f"NPT equilibration complete ({npt_steps} steps)")

            # Save final state from NPT
            final_state = sim_npt.context.getState(getPositions=True)
            final_positions = final_state.getPositions()
            _save_state_atomic(sim_npt, out_dir / "equilibration.xml")
        else:
            # Implicit solvent: save from NVT
            final_positions = nvt_positions
            _save_state_atomic(sim_nvt, out_dir / "equilibration.xml")

        result["state_file"] = str(out_dir / "equilibration.xml")
        result["stages_completed"] = ["NVT"] if npt_steps == 0 else ["NVT", "NPT"]

        # Save final structure as PDB
        pref = f"{name}_" if name else ""
        final_pdb = out_dir / f"{pref}equilibrated.pdb"
        # Restore the Amber/PTM/water residue names OpenMM's PDBFile loader
        # normalized away when topology.pdb was loaded (same shared exporter as
        # min/prod; pure text relabel, MD result/state.xml unaffected).
        from mdclaw.structure.pdb_utils import (
            render_simulation_pdb_preserving_resnames,
        )
        final_pdb.write_text(
            render_simulation_pdb_preserving_resnames(
                xml_inputs.topology, final_positions, topology_pdb_file
            )
        )
        result["final_structure"] = str(final_pdb)
        logger.info(f"Equilibrated structure saved: {final_pdb} (stages: {result['stages_completed']})")

        # === Build a production-matching clean Simulation and save as .chk ===
        # The restraint CustomExternalForce is intentionally omitted so that
        # the saved checkpoint can be loaded by run_production (which builds
        # its System without restraints). currentStep starts at 0 on the
        # fresh Simulation, so run_production will execute its full
        # simulation_time_ns when it loads this checkpoint.
        logger.info("Building production-matching system for checkpoint handoff...")

        # Pull the final state (positions, velocities, box) from whichever
        # restrained Simulation actually ran last.
        sim_src = sim_npt if npt_steps > 0 else sim_nvt
        final_state_full = sim_src.context.getState(
            getPositions=True,
            getVelocities=True,
            enforcePeriodicBox=is_periodic,
        )

        # Clean System — fresh deserialization so the NVT / NPT restraint
        # CustomExternalForce does not bleed into the production handoff
        # (run_production loads this checkpoint and expects a restraint-free
        # System). Build-time forcefield / HMR / GB choices are baked in;
        # the run-time barostat for NPT explicit is added after.
        try:
            system_clean = _deserialize_xml_system(xml_inputs)
            _validate_xml_system_contract(
                system_clean, xml_inputs.topology,
                hmr_request=hmr,
                implicit_solvent_request=implicit_solvent,
            )
        except _ModernSystemContractError as exc:
            result["errors"].append(str(exc))
            result["code"] = exc.code
            return _fail_node_if_running(job_dir, node_id, result)

        # Barostat — mirrors run_production's NPT setup. ``pressure_bar=0``
        # is conventionally NVT (see ``_effective_pressure_bar`` and the
        # ``pressure_bar`` docstring), so gate on ``> 0`` rather than
        # ``is not None`` to avoid silently saving a barostat at 0 bar.
        if (pressure_bar is not None and pressure_bar > 0
                and is_periodic and not implicit_solvent):
            if is_membrane:
                clean_barostat = MonteCarloMembraneBarostat(
                    pressure_bar * bar,
                    0.0 * bar * nanometer,
                    temperature_kelvin * kelvin,
                    MonteCarloMembraneBarostat.XYIsotropic,
                    MonteCarloMembraneBarostat.ZFree,
                    25,
                )
            else:
                clean_barostat = MonteCarloBarostat(
                    pressure_bar * bar,
                    temperature_kelvin * kelvin,
                )
            if effective_random_seed is not None:
                clean_barostat.setRandomNumberSeed(effective_random_seed)
            system_clean.addForce(clean_barostat)

        # Integrator — same type and parameters as run_production's default.
        integrator_clean = LangevinMiddleIntegrator(
            temperature_kelvin * kelvin,
            1.0 / picosecond,
            timestep_fs * femtoseconds,
        )
        if effective_random_seed is not None:
            integrator_clean.setRandomNumberSeed(effective_random_seed)

        if platform_obj:
            sim_clean = Simulation(
                xml_inputs.topology, system_clean, integrator_clean,
                platform_obj, platform_properties,
            )
        else:
            sim_clean = Simulation(xml_inputs.topology, system_clean, integrator_clean)

        sim_clean.context.setPositions(final_state_full.getPositions())
        sim_clean.context.setVelocities(final_state_full.getVelocities())
        if is_periodic:
            sim_clean.context.setPeriodicBoxVectors(*final_state_full.getPeriodicBoxVectors())
        # sim_clean.currentStep is 0 by construction → run_production will
        # execute the full requested simulation length.

        checkpoint_file = out_dir / f"{pref}equilibrated.chk"
        _save_checkpoint_atomic(sim_clean, checkpoint_file)
        result["checkpoint_file"] = str(checkpoint_file)
        logger.info(f"Saved equilibrated checkpoint (currentStep=0): {checkpoint_file}")

        # Save XML state as well — cross-node portable restart artifact.
        # loadCheckpoint requires identical GPU architecture (binary
        # context dump includes device-specific layouts); loadState is
        # portable because it only carries publicly-visible
        # positions/velocities/box. On a heterogeneous cluster this is
        # what run_production should use.
        state_file = out_dir / f"{pref}equilibrated.xml"
        _save_state_atomic(sim_clean, state_file)
        result["state_file_prod_ready"] = str(state_file)
        logger.info(f"Saved equilibrated state (cross-node portable): {state_file}")

        final_ensemble = (
            "NPT" if (pressure_bar and pressure_bar > 0
                      and npt_steps > 0) else "NVT"
        )
        result["system_signature"] = _system_signature(
            xml_inputs,
            solvent_type=solvent_type,
            ensemble=final_ensemble,
            pressure_bar=pressure_bar,
            is_membrane=is_membrane,
            implicit_solvent=implicit_solvent,
            hmr=hmr,
        )
        result["integrator_signature"] = _integrator_signature(
            temperature_kelvin=temperature_kelvin,
            timestep_fs=timestep_fs,
        )
        result["nvt_energy_file"] = str(nvt_energy_file) if nvt_energy_file.exists() else None
        if npt_steps > 0:
            result["npt_energy_file"] = str(npt_energy_file) if npt_energy_file.exists() else None
        missing_eq_logs = []
        if nvt_steps > 0 and not result["nvt_energy_file"]:
            missing_eq_logs.append("nvt_energy")
        if npt_steps > 0 and not result.get("npt_energy_file"):
            missing_eq_logs.append("npt_energy")
        if missing_eq_logs:
            result["errors"].append(
                "Equilibration reporter outputs missing: " + ", ".join(missing_eq_logs)
            )
            result["success"] = False
            raise RuntimeError(result["errors"][-1])

        result["success"] = True

    except _ModernSystemContractError as exc:
        logger.error("Equilibration aborted by modern-system contract: %s", exc)
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as e:
        logger.error(f"Equilibration failed: {e}")
        result["errors"].append(f"Equilibration failed: {e}")

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node
        if result.get("success"):
            artifacts = {
                    "checkpoint": f"artifacts/{pref}equilibrated.chk",
                    "state": f"artifacts/{pref}equilibrated.xml",
                    "final_structure": _node_artifact_path(result.get("final_structure")),
                    "state_file": _node_artifact_path(result.get("state_file")),
            }
            if result.get("nvt_energy_file"):
                artifacts["nvt_energy"] = _node_artifact_path(result.get("nvt_energy_file"))
            if result.get("npt_energy_file"):
                artifacts["npt_energy"] = _node_artifact_path(result.get("npt_energy_file"))
            complete_node(job_dir, node_id,
                artifacts=artifacts,
                metadata={
                    "platform": result.get("platform"),
                    "nvt_steps": nvt_steps,
                    "npt_steps": npt_steps,
                    "requested_nvt_time_ns": requested_nvt_time_ns,
                    "requested_npt_time_ns": requested_npt_time_ns,
                    "effective_nvt_time_ns": effective_nvt_time_ns,
                    "effective_npt_time_ns": effective_npt_time_ns,
                    "timestep_fs": timestep_fs,
                    "restraint_atoms": restraint_atoms,
                    "restraint_count": result.get("restraint_count"),
                    "temperature_kelvin": temperature_kelvin,
                    "pressure_bar": pressure_bar,
                    "restart_from_node_id": _restart_from_node_id,
                    "restart_from_node_type": _restart_from_node_type,
                    "minimization_source_node_id": (
                        _restart_from_node_id
                        if _restart_from_node_type == "min" else None
                    ),
                    # Final ensemble of the saved state.xml — NPT only when
                    # the NPT stage actually ran. Prod's auto-resolver reads
                    # this so a default-config prod inherits eq's ensemble
                    # and the loadState parameter set matches the System.
                    "final_ensemble": final_ensemble,
                    "final_step": 0,
                    "system_signature": result.get("system_signature"),
                    "integrator_signature": result.get("integrator_signature"),
                })
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result
