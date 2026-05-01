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

logger = setup_logger(__name__)

import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

import numpy as np  # noqa: E402
from mdclaw._common import ensure_directory, create_unique_subdir, generate_job_id  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


def _node_artifact_path(path: Optional[str]) -> str:
    """Convert an absolute output path into a node-relative artifact path."""
    if not path:
        return ""
    return f"artifacts/{Path(path).name}"


# DCD fixed-record-84 + "CORD" magic. OpenMM/CHARMM DCD always emit this
# as the first 8 bytes, so a file that lacks it cannot be appended to
# via DCDReporter(append=True).
_DCD_MAGIC = b"\x54\x00\x00\x00CORD"


def _dcd_has_valid_header(path: Path) -> bool:
    """Return True iff *path* is a non-empty DCD file whose first 8 bytes
    match the fixed 84-record + CORD magic.

    Used to guard ``DCDReporter(append=True)`` against 0-byte / truncated
    orphans left by a previously-failed run (e.g. reporter flushes delayed
    by a synced filesystem). Appending to such a file raises a cryptic
    ``ValueError: Cannot append to file with invalid DCD header`` inside
    OpenMM's constructor — much easier to handle up-front.
    """
    try:
        if not path.is_file() or path.stat().st_size < 8:
            return False
        with path.open("rb") as fh:
            return fh.read(8) == _DCD_MAGIC
    except OSError:
        return False


def _node_previously_failed(
    job_dir: Optional[str], node_id: Optional[str]
) -> bool:
    """Return True iff ``node.json`` exists and records ``status ==
    "failed"``. **Must be called before** :func:`begin_node` flips the
    sentinel to ``running`` — otherwise the prior failure is invisible."""
    if not (job_dir and node_id):
        return False
    from mdclaw._node import read_node
    try:
        return read_node(job_dir, node_id).get("status") == "failed"
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _resolve_dcd_append_mode(
    trajectory_file: Path,
    energy_file: Path,
    append_requested: bool,
    prior_failed: bool,
) -> tuple:
    """Decide whether DCD + energy reporters should open in append mode.

    Legacy mid-run restart into the same prod node requires a valid
    partial DCD; 0-byte / header-less files (e.g. reporter flush
    interrupted by synced-filesystem lag) and the ``failed``-status
    sentinel both mean the stale artifacts must be discarded before the
    reporters are constructed.

    Returns ``(do_append, warning_message, removed)`` where:

    - ``do_append``: final append flag to hand to both reporters (always
      shared, commit 1ccf607).
    - ``warning_message``: human-readable string to append to
      ``result["warnings"]`` when cleanup happens, else ``None``.
    - ``removed``: list of :class:`Path` objects whose contents were
      deleted (returned for logging/test assertions, even if the unlink
      itself raised — the caller sees exactly which files were targeted).
    """
    do_append = append_requested and trajectory_file.exists()
    if not do_append:
        return False, None, []
    if not (prior_failed or not _dcd_has_valid_header(trajectory_file)):
        return True, None, []

    reason = "failed status" if prior_failed else "invalid/empty DCD header"
    removed = []
    for stale in (trajectory_file, energy_file):
        removed.append(stale)
        try:
            if stale.exists():
                stale.unlink()
        except OSError as e:
            logger.warning(f"Could not remove stale artifact {stale}: {e}")
    warning = (
        f"Discarded stale artifacts from previous run "
        f"({reason}); starting trajectory/energy fresh while "
        f"resuming from checkpoint."
    )
    return False, warning, removed


def _flush_reporter_stream(reporter) -> None:
    """Best-effort flush for OpenMM reporters that own a file handle."""
    out = getattr(reporter, "_out", None)
    if out is not None and hasattr(out, "flush"):
        out.flush()


def _close_reporter_stream(reporter) -> None:
    """Best-effort close for OpenMM reporters that own a file handle."""
    out = getattr(reporter, "_out", None)
    if out is not None:
        if hasattr(out, "flush"):
            out.flush()
        if hasattr(out, "close"):
            out.close()


def _compute_step_plan(
    simulation_time_ns: float,
    timestep_fs: float,
    current_step: int,
) -> dict:
    """Translate a requested duration into a concrete step schedule.

    ``simulation_time_ns`` is always interpreted as time to run **in this
    call** (additional on top of ``current_step``). Callers pass
    ``simulation.currentStep`` for restart cases (prod→prod) and 0 for
    fresh runs; the eq→prod path saves its checkpoint with
    ``currentStep=0`` by design so legacy callers see unchanged behaviour.

    Returns a dict with:

    - ``start_step`` — step counter at restart (same as ``current_step``)
    - ``start_time_ns`` — that step count expressed as time
    - ``steps_to_run`` — MD steps scheduled for this call
    - ``num_steps`` — total step counter after this call completes
    """
    steps_to_run = int(simulation_time_ns * 1_000_000 / timestep_fs)
    return {
        "start_step": current_step,
        "start_time_ns": current_step * timestep_fs / 1e6,
        "steps_to_run": steps_to_run,
        "num_steps": current_step + steps_to_run,
    }


def run_equilibration(
    prmtop_file: Optional[str] = None,
    inpcrd_file: Optional[str] = None,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = 1.0,
    nvt_steps: int = 250000,
    npt_steps: int = 250000,
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
        prmtop_file: Amber topology file (.parm7 or .prmtop)
        inpcrd_file: Amber coordinate file (.rst7 or .inpcrd)
        temperature_kelvin: Temperature in Kelvin (default: 300.0)
        pressure_bar: Pressure in bar. Controls whether NPT stage runs:
            - > 0 (e.g., 1.0): NVT + NPT equilibration (for NPT production)
            - 0 or None: NVT only (for NVT production or implicit solvent)
            Default: 1.0
        nvt_steps: Number of NVT heating steps (default: 250000 = 1 ns at 4 fs).
            Override with e.g. `--nvt-steps 2500` (10 ps) for a fast
            sanity run.
        npt_steps: Number of NPT equilibration steps (default: 250000 = 1 ns at 4 fs).
            Only used when pressure_bar > 0; ignored otherwise. Override
            with e.g. `--npt-steps 5000` (20 ps) for a fast sanity run.
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
    # Auto-resolve inputs from DAG when in node mode
    if job_dir and node_id:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(job_dir, node_id, "eq")
        if not prmtop_file and "prmtop_file" in _inputs:
            prmtop_file = _inputs["prmtop_file"]
        if not inpcrd_file and "inpcrd_file" in _inputs:
            inpcrd_file = _inputs["inpcrd_file"]

    if not prmtop_file or not inpcrd_file:
        return {"success": False, "errors": ["prmtop_file and inpcrd_file are required (pass explicitly or use --job-dir/--node-id for DAG auto-resolve)"]}

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
        "restraint_atoms": restraint_atoms,
        "restraint_count": 0,
        "platform": None,
        "errors": [],
        "warnings": [],
    }

    prmtop_path = Path(prmtop_file).resolve()
    inpcrd_path = Path(inpcrd_file).resolve()

    if not prmtop_path.is_file():
        result["errors"].append(f"Topology file not found: {prmtop_file}")
        return result
    if not inpcrd_path.is_file():
        result["errors"].append(f"Coordinate file not found: {inpcrd_file}")
        return result

    try:
        from openmm.app import (
            AmberPrmtopFile, AmberInpcrdFile, PDBFile,
            Simulation, PME, NoCutoff, HBonds,
            HCT, OBC1, OBC2, GBn, GBn2,
        )
        from openmm import (
            LangevinMiddleIntegrator, MonteCarloBarostat,
            MonteCarloMembraneBarostat, Platform, CustomExternalForce,
        )
        from openmm.unit import (
            nanometer, kelvin, picosecond, femtoseconds, bar,
            kilojoules_per_mole, amu,
        )
    except ImportError:
        result["errors"].append("OpenMM not installed")
        return result

    IMPLICIT_MODELS = {
        "HCT": HCT, "OBC1": OBC1, "OBC2": OBC2, "GBn": GBn, "GBn2": GBn2,
    }
    RESTRAINT_SELECTIONS = {
        "CA": {"CA"},
        "backbone": {"N", "CA", "C", "O"},
        "heavy": None,  # all non-hydrogen
    }

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

        # Load topology and coordinates
        logger.info("Loading Amber files")
        prmtop = AmberPrmtopFile(str(prmtop_path))
        inpcrd = AmberInpcrdFile(str(inpcrd_path))

        is_periodic = inpcrd.boxVectors is not None

        # HMR kwargs shared by NVT, NPT, and the clean checkpoint System
        # (must mirror run_production's hmr handling so the saved checkpoint
        # is loadable).
        hmr_kwargs = {"hydrogenMass": 4.0 * amu} if hmr else {}
        if hmr:
            logger.info(f"HMR enabled: hydrogenMass=4.0 amu (timestep={timestep_fs}fs)")

        # Determine whether to run NPT stage
        # NPT equilibration only when production will use NPT (pressure_bar > 0)
        run_npt = (pressure_bar is not None and pressure_bar > 0
                   and not implicit_solvent and is_periodic)
        if not run_npt:
            npt_steps = 0
            if implicit_solvent:
                logger.info("Implicit solvent: NVT equilibration only")
            elif not pressure_bar or pressure_bar == 0:
                logger.info("NVT production planned: NVT equilibration only")

        # --- Stage 1: NVT heating ---
        logger.info(
            f"Stage 1: NVT heating ({nvt_steps} steps, {timestep_fs} fs, "
            f"restraints on {restraint_atoms})"
        )

        # Create system for NVT
        if implicit_solvent:
            gb_model = IMPLICIT_MODELS.get(implicit_solvent.upper(), OBC2)
            system_nvt = prmtop.createSystem(
                implicitSolvent=gb_model,
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                **hmr_kwargs,
            )
        elif is_periodic:
            system_nvt = prmtop.createSystem(
                nonbondedMethod=PME,
                nonbondedCutoff=1.0 * nanometer,
                constraints=HBonds,
                **hmr_kwargs,
            )
        else:
            system_nvt = prmtop.createSystem(
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                **hmr_kwargs,
            )

        # Add positional restraints
        restraint = CustomExternalForce(
            'k*periodicdistance(x, y, z, x0, y0, z0)^2'
        )
        restraint.addPerParticleParameter('k')
        restraint.addPerParticleParameter('x0')
        restraint.addPerParticleParameter('y0')
        restraint.addPerParticleParameter('z0')

        allowed_names = RESTRAINT_SELECTIONS.get(restraint_atoms, {"CA"})
        positions = inpcrd.positions
        restraint_count = 0

        for atom in prmtop.topology.atoms():
            if allowed_names is None:
                # "heavy" = all non-hydrogen
                if atom.element.symbol == 'H':
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

        # NVT integrator (matches run_production: LangevinMiddle, same timestep, HMR via system)
        integrator_nvt = LangevinMiddleIntegrator(
            temperature_kelvin * kelvin,
            1.0 / picosecond,
            timestep_fs * femtoseconds,
        )
        if random_seed is not None:
            integrator_nvt.setRandomNumberSeed(random_seed)

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
            sim_nvt = Simulation(prmtop.topology, system_nvt, integrator_nvt,
                                 platform_obj, platform_properties)
        else:
            sim_nvt = Simulation(prmtop.topology, system_nvt, integrator_nvt)

        result["platform"] = sim_nvt.context.getPlatform().getName()

        sim_nvt.context.setPositions(positions)
        if is_periodic and inpcrd.boxVectors is not None:
            sim_nvt.context.setPeriodicBoxVectors(*inpcrd.boxVectors)

        # Minimize
        logger.info("Minimizing energy...")
        sim_nvt.minimizeEnergy()

        # NVT run
        sim_nvt.context.setVelocitiesToTemperature(temperature_kelvin * kelvin)
        sim_nvt.step(nvt_steps)
        result["nvt_steps"] = nvt_steps
        logger.info(f"NVT heating complete ({nvt_steps} steps)")

        # Save NVT state
        nvt_state = sim_nvt.context.getState(getPositions=True, getVelocities=True)
        nvt_positions = nvt_state.getPositions()
        nvt_velocities = nvt_state.getVelocities()

        # --- Stage 2: NPT equilibration (same timestep + HMR, with restraints) ---
        if npt_steps > 0:
            logger.info(
                f"Stage 2: NPT equilibration ({npt_steps} steps, {timestep_fs} fs, "
                f"restraints on {restraint_atoms})"
            )

            # Create new system for NPT
            if is_periodic:
                system_npt = prmtop.createSystem(
                    nonbondedMethod=PME,
                    nonbondedCutoff=1.0 * nanometer,
                    constraints=HBonds,
                    **hmr_kwargs,
                )
            else:
                system_npt = prmtop.createSystem(
                    nonbondedMethod=NoCutoff,
                    constraints=HBonds,
                    **hmr_kwargs,
                )

            # Add same restraints
            restraint_npt = CustomExternalForce(
                'k*periodicdistance(x, y, z, x0, y0, z0)^2'
            )
            restraint_npt.addPerParticleParameter('k')
            restraint_npt.addPerParticleParameter('x0')
            restraint_npt.addPerParticleParameter('y0')
            restraint_npt.addPerParticleParameter('z0')

            for atom in prmtop.topology.atoms():
                if allowed_names is None:
                    if atom.element.symbol == 'H':
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
                system_npt.addForce(MonteCarloMembraneBarostat(
                    pressure_bar * bar, 0.0 * bar * nanometer,
                    MonteCarloMembraneBarostat.XYIsotropic,
                    MonteCarloMembraneBarostat.ZFree,
                    temperature_kelvin * kelvin,
                ))
            else:
                system_npt.addForce(MonteCarloBarostat(
                    pressure_bar * bar, temperature_kelvin * kelvin,
                ))

            # NPT integrator (matches run_production: LangevinMiddle, same timestep)
            integrator_npt = LangevinMiddleIntegrator(
                temperature_kelvin * kelvin,
                1.0 / picosecond,
                timestep_fs * femtoseconds,
            )
            if random_seed is not None:
                integrator_npt.setRandomNumberSeed(random_seed)

            if platform_obj:
                sim_npt = Simulation(prmtop.topology, system_npt, integrator_npt,
                                     platform_obj, platform_properties)
            else:
                sim_npt = Simulation(prmtop.topology, system_npt, integrator_npt)

            sim_npt.context.setPositions(nvt_positions)
            sim_npt.context.setVelocities(nvt_velocities)
            if is_periodic and inpcrd.boxVectors is not None:
                sim_npt.context.setPeriodicBoxVectors(*inpcrd.boxVectors)

            sim_npt.step(npt_steps)
            result["npt_steps"] = npt_steps
            logger.info(f"NPT equilibration complete ({npt_steps} steps)")

            # Save final state from NPT
            final_state = sim_npt.context.getState(getPositions=True)
            final_positions = final_state.getPositions()
            sim_npt.saveState(str(out_dir / "equilibration.xml"))
        else:
            # Implicit solvent: save from NVT
            final_positions = nvt_positions
            sim_nvt.saveState(str(out_dir / "equilibration.xml"))

        result["state_file"] = str(out_dir / "equilibration.xml")
        result["stages_completed"] = ["NVT"] if npt_steps == 0 else ["NVT", "NPT"]

        # Save final structure as PDB
        pref = f"{name}_" if name else ""
        final_pdb = out_dir / f"{pref}equilibrated.pdb"
        with open(final_pdb, 'w') as f:
            PDBFile.writeFile(prmtop.topology, final_positions, f)
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

        # Clean System — mirrors run_production's build exactly
        # (same nonbonded method, cutoff, constraints, HMR).
        if implicit_solvent:
            gb_model_clean = IMPLICIT_MODELS.get(implicit_solvent.upper(), OBC2)
            system_clean = prmtop.createSystem(
                implicitSolvent=gb_model_clean,
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                **hmr_kwargs,
            )
        elif is_periodic:
            system_clean = prmtop.createSystem(
                nonbondedMethod=PME,
                nonbondedCutoff=1.0 * nanometer,
                constraints=HBonds,
                **hmr_kwargs,
            )
        else:
            system_clean = prmtop.createSystem(
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                **hmr_kwargs,
            )

        # Barostat — mirrors run_production's NPT setup.
        if pressure_bar is not None and is_periodic and not implicit_solvent:
            if is_membrane:
                system_clean.addForce(MonteCarloMembraneBarostat(
                    pressure_bar * bar,
                    0.0 * bar * nanometer,
                    temperature_kelvin * kelvin,
                    MonteCarloMembraneBarostat.XYIsotropic,
                    MonteCarloMembraneBarostat.ZFree,
                    25,
                ))
            else:
                system_clean.addForce(MonteCarloBarostat(
                    pressure_bar * bar,
                    temperature_kelvin * kelvin,
                ))

        # Integrator — same type and parameters as run_production's default.
        integrator_clean = LangevinMiddleIntegrator(
            temperature_kelvin * kelvin,
            1.0 / picosecond,
            timestep_fs * femtoseconds,
        )

        if platform_obj:
            sim_clean = Simulation(
                prmtop.topology, system_clean, integrator_clean,
                platform_obj, platform_properties,
            )
        else:
            sim_clean = Simulation(prmtop.topology, system_clean, integrator_clean)

        sim_clean.context.setPositions(final_state_full.getPositions())
        sim_clean.context.setVelocities(final_state_full.getVelocities())
        if is_periodic:
            sim_clean.context.setPeriodicBoxVectors(*final_state_full.getPeriodicBoxVectors())
        # sim_clean.currentStep is 0 by construction → run_production will
        # execute the full requested simulation length.

        checkpoint_file = out_dir / f"{pref}equilibrated.chk"
        sim_clean.saveCheckpoint(str(checkpoint_file))
        result["checkpoint_file"] = str(checkpoint_file)
        logger.info(f"Saved equilibrated checkpoint (currentStep=0): {checkpoint_file}")

        # Save XML state as well — cross-node portable restart artifact.
        # loadCheckpoint requires identical GPU architecture (binary
        # context dump includes device-specific layouts); loadState is
        # portable because it only carries publicly-visible
        # positions/velocities/box. On a heterogeneous cluster this is
        # what run_production should use.
        state_file = out_dir / f"{pref}equilibrated.xml"
        sim_clean.saveState(str(state_file))
        result["state_file_prod_ready"] = str(state_file)
        logger.info(f"Saved equilibrated state (cross-node portable): {state_file}")

        result["success"] = True

    except Exception as e:
        logger.error(f"Equilibration failed: {e}")
        result["errors"].append(f"Equilibration failed: {e}")

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node
        if result.get("success"):
            complete_node(job_dir, node_id,
                artifacts={
                    "checkpoint": f"artifacts/{pref}equilibrated.chk",
                    "state": f"artifacts/{pref}equilibrated.xml",
                    "final_structure": _node_artifact_path(result.get("final_structure")),
                    "state_file": _node_artifact_path(result.get("state_file")),
                },
                metadata={
                    "platform": result.get("platform"),
                    "nvt_steps": nvt_steps,
                    "npt_steps": npt_steps,
                    "restraint_atoms": restraint_atoms,
                    "restraint_count": result.get("restraint_count"),
                    "temperature_kelvin": temperature_kelvin,
                    "pressure_bar": pressure_bar,
                    # Final ensemble of the saved state.xml — NPT only when
                    # the NPT stage actually ran. Prod's auto-resolver reads
                    # this so a default-config prod inherits eq's ensemble
                    # and the loadState parameter set matches the System.
                    "final_ensemble": (
                        "NPT" if (pressure_bar and pressure_bar > 0
                                  and npt_steps > 0) else "NVT"
                    ),
                    "final_step": 0,
                })
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result


def run_production(
    prmtop_file: Optional[str] = None,
    inpcrd_file: Optional[str] = None,
    simulation_time_ns: float = 1.0,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = None,
    timestep_fs: float = 4.0,
    output_frequency_ps: float = 10.0,
    trajectory_format: str = "dcd",
    restraint_file: Optional[str] = None,
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
        prmtop_file: Amber topology file (.parm7 or .prmtop)
        inpcrd_file: Amber coordinate file (.rst7 or .inpcrd)
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
        restraint_file: Optional file with restraint definitions
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
        restart_from: Path to checkpoint file (.chk) to restart from. Skips
                     minimization and runs ``simulation_time_ns`` additional
                     nanoseconds on top of the restart step count. The
                     trajectory is written to this node's own ``artifacts/``
                     directory as a fresh DCD (no cross-node append) — to
                     stitch trajectories across nodes, concatenate with
                     mdtraj or similar.
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
    # Auto-resolve inputs from DAG when in node mode
    _eq_final_ensemble: Optional[str] = None
    _eq_pressure_bar: Optional[float] = None
    _pressure_bar_inherited = False
    if job_dir and node_id:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(job_dir, node_id, "prod")
        # Strict continue_from violation — fail before we touch OpenMM so
        # the user sees a clean error instead of a wrong-checkpoint run.
        if not restart_from and "restart_from_error" in _inputs:
            return {"success": False, "errors": [_inputs["restart_from_error"]]}
        if not prmtop_file and "prmtop_file" in _inputs:
            prmtop_file = _inputs["prmtop_file"]
        if not inpcrd_file and "inpcrd_file" in _inputs:
            inpcrd_file = _inputs["inpcrd_file"]
        if not restart_from and "restart_from" in _inputs:
            restart_from = _inputs["restart_from"]
        _eq_final_ensemble = _inputs.get("eq_final_ensemble")
        _eq_pressure_bar = _inputs.get("eq_pressure_bar")
        # Inherit eq's ensemble when caller did not specify pressure
        # explicitly. Without this an NPT-equilibrated state.xml cannot
        # be loaded into a default-config (NVT) prod context — OpenMM's
        # loadState fails with
        # ``setParameter() with invalid parameter name: MonteCarloPressure``
        # because the saved state references a MonteCarloBarostat that
        # the new context never received.
        if (pressure_bar is None
                and _eq_final_ensemble == "NPT"
                and _eq_pressure_bar is not None):
            pressure_bar = _eq_pressure_bar
            _pressure_bar_inherited = True
            logger.info(
                f"pressure_bar inherited from eq ancestor "
                f"(final_ensemble=NPT, {pressure_bar} bar)"
            )

    if not prmtop_file or not inpcrd_file:
        return {"success": False, "errors": ["prmtop_file and inpcrd_file are required (pass explicitly or use --job-dir/--node-id for DAG auto-resolve)"]}

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

    # Validate input files - also search in topology subdirectory if not found
    prmtop_path = Path(prmtop_file)
    inpcrd_path = Path(inpcrd_file)

    # If file not found, try searching in output_dir subdirectories
    search_dir = Path(output_dir) if output_dir else None
    if not prmtop_path.is_file() and search_dir:
        candidates = list(search_dir.glob("**/system.parm7")) + list(search_dir.glob("**/*.parm7"))
        if candidates:
            prmtop_path = candidates[0]
            logger.info(f"Found topology file: {prmtop_path}")

    if not inpcrd_path.is_file() and search_dir:
        candidates = list(search_dir.glob("**/system.rst7")) + list(search_dir.glob("**/*.rst7"))
        if candidates:
            inpcrd_path = candidates[0]
            logger.info(f"Found coordinate file: {inpcrd_path}")

    if not prmtop_path.is_file():
        result["errors"].append(f"Topology file not found: {prmtop_file}")
        result["errors"].append("Hint: Run build_amber_system first to create topology files")
        return result
    if not inpcrd_path.is_file():
        result["errors"].append(f"Coordinate file not found: {inpcrd_file}")
        result["errors"].append("Hint: Run build_amber_system first to create topology files")
        return result

    try:
        from openmm.app import AmberPrmtopFile, AmberInpcrdFile, PDBFile, DCDReporter, StateDataReporter, CheckpointReporter
        from openmm import LangevinMiddleIntegrator, MonteCarloBarostat, MonteCarloMembraneBarostat, Platform
        from openmm.app import Simulation, PME, NoCutoff, HBonds
        # Implicit solvent models (Generalized Born)
        from openmm.app import HCT, OBC1, OBC2, GBn, GBn2
        from openmm.unit import (
            nanometer, kelvin, picosecond, femtoseconds, bar, amu
        )
    except ImportError:
        result["errors"].append("OpenMM not installed")
        result["errors"].append("Hint: Install with: conda install -c conda-forge openmm")
        return result

    # Map implicit solvent model names to OpenMM objects
    IMPLICIT_MODELS = {
        "HCT": HCT,      # igb=1
        "OBC1": OBC1,    # igb=2
        "OBC2": OBC2,    # igb=5 (default, well-tested)
        "GBN": GBn,      # igb=7
        "GBN2": GBn2,    # igb=8 (recommended by Amber manual)
    }
    
    try:
        # Load system
        logger.info("Loading Amber files")
        prmtop = AmberPrmtopFile(str(prmtop_path))
        inpcrd = AmberInpcrdFile(str(inpcrd_path))

        # Detect if system is periodic (has box vectors)
        is_periodic = inpcrd.boxVectors is not None

        # Auto-detect implicit solvent from simulation_brief if not specified
        # This fixes the issue where LLM doesn't pass implicit_solvent parameter
        # For non-periodic systems without explicit implicit_solvent specification,
        # the user should pass --implicit-solvent explicitly.
        # (Previously auto-detected from session_dir/simulation_brief.json)

        # HMR (Hydrogen Mass Repartitioning)
        hmr_kwargs = {}
        if hmr:
            hmr_kwargs["hydrogenMass"] = 4.0 * amu
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

        # Create system - handle implicit vs explicit solvent
        logger.info("Creating OpenMM system")
        if implicit_solvent:
            # Implicit solvent mode (Generalized Born)
            gb_model = IMPLICIT_MODELS.get(implicit_solvent.upper(), OBC2)
            system = prmtop.createSystem(
                implicitSolvent=gb_model,
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                soluteDielectric=1.0,
                solventDielectric=78.5,
                **hmr_kwargs,
            )
            logger.info(f"Using implicit solvent ({implicit_solvent}) with NoCutoff")
            result["solvent_type"] = "implicit"
            result["implicit_model"] = implicit_solvent
        elif is_periodic:
            # Explicit solvent with periodic boundaries
            system = prmtop.createSystem(
                nonbondedMethod=PME,
                nonbondedCutoff=1.0*nanometer,
                constraints=HBonds,
                **hmr_kwargs,
            )
            result["solvent_type"] = "explicit"
        else:
            # Non-periodic without implicit model - use NoCutoff (vacuum)
            system = prmtop.createSystem(
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                **hmr_kwargs,
            )
            logger.warning("Non-periodic system without implicit solvent specified - using NoCutoff (vacuum)")
            result["solvent_type"] = "vacuum"

        # Add barostat if NPT (only for periodic explicit solvent systems)
        if pressure_bar is not None and is_periodic and not implicit_solvent:
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
            if random_seed is not None:
                barostat.setRandomNumberSeed(random_seed)
            system.addForce(barostat)
            ensemble = "NPT"
        elif implicit_solvent and pressure_bar is not None:
            # Warn user that NPT is not supported with implicit solvent
            logger.warning("Implicit solvent simulations use NVT ensemble - ignoring pressure setting")
            result["warnings"].append("NPT not supported with implicit solvent, using NVT")
            ensemble = "NVT"
        else:
            ensemble = "NVT"
        result["ensemble"] = ensemble
        result["is_membrane"] = is_membrane

        # Create integrator
        integrator = LangevinMiddleIntegrator(
            temperature_kelvin * kelvin,
            1.0 / picosecond,
            timestep_fs * femtoseconds
        )
        if random_seed is not None:
            integrator.setRandomNumberSeed(random_seed)
            result["random_seed"] = random_seed

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
                return result
            platform_obj = Platform.getPlatformByName(PLATFORM_MAP[plat_key])
            if device_index and plat_key in ("cuda", "opencl"):
                platform_properties["DeviceIndex"] = device_index

        # Create simulation
        if platform_obj:
            simulation = Simulation(
                prmtop.topology, system, integrator,
                platform=platform_obj, platformProperties=platform_properties,
            )
        else:
            simulation = Simulation(prmtop.topology, system, integrator)

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
                return result
            # Prefer saveState (XML) for cross-node portability.
            # saveCheckpoint (binary) is GPU-architecture-specific and
            # fails silently across heterogeneous clusters. .xml and
            # .chk both remain on disk; resolve_node_inputs picks .xml
            # first when available.
            if restart_path.suffix == ".xml":
                # Guardrail: ensemble mismatch between the saved state and
                # the current System. OpenMM's loadState restores all
                # context parameters by name, and rejects unknown ones
                # with an opaque ``setParameter() with invalid parameter
                # name: ...`` exception. Detect the common case
                # (NPT eq state → NVT prod context) up front so the user
                # sees a structured error and a concrete fix instead of
                # an internal OpenMM message.
                state_has_pressure = (
                    "MonteCarloPressure" in restart_path.read_text()
                )
                system_has_barostat = any(
                    isinstance(f, (MonteCarloBarostat,
                                   MonteCarloMembraneBarostat))
                    for f in system.getForces()
                )
                if state_has_pressure and not system_has_barostat:
                    suggested = (
                        f"--pressure-bar {_eq_pressure_bar}"
                        if _eq_pressure_bar is not None
                        else "--pressure-bar 1.0"
                    )
                    result["errors"].append(
                        "Ensemble mismatch: the equilibration state at "
                        f"{restart_path} contains an NPT barostat "
                        "(MonteCarloPressure parameter) but this prod "
                        "context was configured as NVT (no barostat). "
                        f"Pass {suggested} to add a matching barostat, "
                        "or rerun equilibration with --pressure-bar 0 to "
                        "produce an NVT state."
                    )
                    if _node_mode:
                        from mdclaw._node import fail_node
                        fail_node(job_dir, node_id, errors=result["errors"])
                    return result
                if system_has_barostat and not state_has_pressure:
                    result["warnings"].append(
                        "Ensemble mismatch (mild): prod has a barostat but "
                        "the eq state lacks NPT parameters. The initial "
                        "pressure may not match the eq's final state."
                    )

                simulation.loadState(str(restart_path))
                # loadState does NOT restore simulation.currentStep —
                # it only carries positions/velocities/box. Pull the
                # ancestor node's metadata.final_step so prod→prod
                # extension preserves the cumulative step counter.
                if _node_mode:
                    from mdclaw._node import read_ancestor_final_step
                    anc_step = read_ancestor_final_step(job_dir, node_id)
                    if anc_step is not None:
                        simulation.currentStep = anc_step
                logger.info(
                    f"Restarted from state XML (step {simulation.currentStep})"
                )
                if _pressure_bar_inherited:
                    result["warnings"].append(
                        f"pressure_bar={pressure_bar} inherited from eq "
                        f"ancestor (final_ensemble=NPT)."
                    )
            else:
                simulation.loadCheckpoint(str(restart_path))
                logger.info(
                    f"Restarted from checkpoint (step {simulation.currentStep})"
                )
            append_dcd = True
            result["restarted_from"] = restart_from
        else:
            append_dcd = False
            simulation.context.setPositions(inpcrd.positions)
            # Set box vectors for periodic explicit solvent systems (required for PME)
            if is_periodic and not implicit_solvent:
                if inpcrd.boxVectors is not None:
                    simulation.context.setPeriodicBoxVectors(*inpcrd.boxVectors)

        # Apply restraints if provided
        if restraint_file and Path(restraint_file).is_file():
            logger.info(f"Applying restraints from {restraint_file}")
            result["warnings"].append("Restraint file parsing not yet implemented")

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

        logger.info(
            f"Running {steps_to_run} steps "
            f"(start_step={start_step}, target_total={simulation_steps})"
        )

        if steps_to_run > 0:
            simulation.step(steps_to_run)

        # Save final checkpoint + state (periodic reporter may not have
        # fired for short runs). Both formats so downstream can choose.
        simulation.saveCheckpoint(str(checkpoint_file))
        simulation.saveState(str(state_file))
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

        # Save final structure
        final_pdb = out_dir / f"{pref}final_structure.pdb"
        positions = state.getPositions()
        with open(final_pdb, 'w') as f:
            PDBFile.writeFile(simulation.topology, positions, f)

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
            result["success"] = True

        logger.info(f"Simulation complete. Trajectory saved: {trajectory_file}")

    except Exception as e:
        logger.error(f"MD simulation failed: {e}")
        result["errors"].append(f"MD simulation failed: {type(e).__name__}: {str(e)}")

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node
        if result.get("success"):
            complete_node(job_dir, node_id,
                artifacts={
                    "trajectory": _node_artifact_path(result.get("trajectory_file")),
                    "final_structure": _node_artifact_path(result.get("final_structure")),
                    "checkpoint": _node_artifact_path(result.get("checkpoint_file")),
                    "state": _node_artifact_path(result.get("state_file")),
                    "energy": _node_artifact_path(result.get("energy_file")),
                },
                metadata={
                    "simulation_time_ns": simulation_time_ns,
                    "temperature_kelvin": temperature_kelvin,
                    "pressure_bar": pressure_bar,
                    "platform": result.get("platform"),
                    "hmr": hmr,
                    "timestep_fs": timestep_fs,
                    "random_seed": random_seed,
                    "num_steps": result.get("steps_completed"),
                    "start_step": result.get("start_step"),
                    "start_time_ns": result.get("start_time_ns"),
                    "final_step": result.get("steps_completed"),
                })
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "run_equilibration": run_equilibration,
    "run_production": run_production,
}

