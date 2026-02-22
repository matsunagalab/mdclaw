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
from servers._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

import numpy as np  # noqa: E402
from servers._common import ensure_directory, create_unique_subdir, generate_job_id, get_current_session, get_simulation_brief  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


def run_md_simulation(
    prmtop_file: str,
    inpcrd_file: str,
    simulation_time_ns: float = 1.0,
    temperature_kelvin: float = 300.0,
    pressure_bar: Optional[float] = None,
    timestep_fs: float = 2.0,
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
    hmr: bool = False,
) -> dict:
    """Run MD simulation using OpenMM.

    Performs molecular dynamics simulation with OpenMM, supporting both
    NVT and NPT ensembles with Langevin dynamics.

    Args:
        prmtop_file: Amber topology file (.parm7 or .prmtop)
        inpcrd_file: Amber coordinate file (.rst7 or .inpcrd)
        simulation_time_ns: Simulation time in nanoseconds (default: 1.0)
        temperature_kelvin: Temperature in Kelvin (default: 300.0)
        pressure_bar: Pressure in bar. Set for NPT, None for NVT (default: None)
        timestep_fs: Integration timestep in femtoseconds (default: 2.0)
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
        restart_from: Path to checkpoint file (.chk) to restart from.
                     Skips minimization, appends to existing DCD, and runs
                     only the remaining steps.
        hmr: Enable Hydrogen Mass Repartitioning (hydrogenMass=2 amu).
                     Allows 4 fs timestep for ~2x throughput improvement.

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
        "hmr": False,
        "errors": [],
        "warnings": []
    }

    # Setup output directory with human-readable name
    # Always prefer session directory to ensure files go to the correct location
    # (LLM may pass incorrect output_dir values)
    session_dir = get_current_session()
    if session_dir:
        base_dir = session_dir
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "md_simulation")
    result["output_dir"] = str(out_dir)

    # Validate input files - also search in topology subdirectory if not found
    prmtop_path = Path(prmtop_file)
    inpcrd_path = Path(inpcrd_file)

    # If file not found, try searching in topology subdirectory
    # This handles cases where LLM passes session_dir/file instead of session_dir/topology/file
    session_dir = get_current_session()

    if not prmtop_path.is_file() and session_dir:
        # Search for .parm7 files in session directory
        candidates = list(session_dir.glob("**/system.parm7")) + list(session_dir.glob("**/*.parm7"))
        if candidates:
            prmtop_path = candidates[0]
            logger.info(f"Found topology file: {prmtop_path}")

    if not inpcrd_path.is_file() and session_dir:
        # Search for .rst7 files in session directory
        candidates = list(session_dir.glob("**/system.rst7")) + list(session_dir.glob("**/*.rst7"))
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
        if implicit_solvent is None and not is_periodic:
            brief = get_simulation_brief()
            if brief and brief.get("solvation_type") == "implicit":
                implicit_solvent = brief.get("implicit_solvent_model", "OBC2")
                logger.info(f"Auto-detected implicit solvent from simulation_brief: {implicit_solvent}")

        # HMR (Hydrogen Mass Repartitioning)
        hmr_kwargs = {}
        if hmr:
            hmr_kwargs["hydrogenMass"] = 2.0 * amu
            logger.info(f"HMR enabled: hydrogenMass=2.0 amu (timestep={timestep_fs}fs)")
            if timestep_fs <= 2.0:
                result["warnings"].append(
                    f"HMR enabled but timestep is {timestep_fs}fs. "
                    f"Consider using --timestep-fs 4.0 for better throughput."
                )
            result["hmr"] = True

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
                result["errors"].append(f"Checkpoint file not found: {restart_from}")
                return result
            simulation.loadCheckpoint(str(restart_path))
            append_dcd = True
            result["restarted_from"] = restart_from
            logger.info(f"Restarted from checkpoint (step {simulation.currentStep})")
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

        # Only append if restarting AND the file already exists
        do_append = append_dcd and trajectory_file.exists()

        # Setup trajectory reporter
        report_interval = int(output_frequency_ps / timestep_fs * 1000)
        if trajectory_format.lower() == "dcd":
            simulation.reporters.append(DCDReporter(str(trajectory_file), report_interval, append=do_append))
        else:
            from openmm.app import PDBReporter
            simulation.reporters.append(PDBReporter(str(trajectory_file), report_interval))

        # Setup energy reporter
        simulation.reporters.append(StateDataReporter(
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
            append=(append_dcd and energy_file.exists()),
        ))

        # Checkpoint reporter - periodic checkpoint saves
        checkpoint_interval = max(report_interval * 10, 5000)
        simulation.reporters.append(CheckpointReporter(str(checkpoint_file), checkpoint_interval))
        result["checkpoint_file"] = str(checkpoint_file)

        # Get initial energy
        state = simulation.context.getState(getEnergy=True)
        initial_energy = state.getPotentialEnergy()
        result["initial_energy_kj_mol"] = float(initial_energy._value)
        logger.info(f"Initial energy: {initial_energy}")

        # Minimize energy (skip on restart)
        if not restart_from:
            logger.info("Minimizing energy...")
            simulation.minimizeEnergy(maxIterations=5000)

        # Run simulation
        simulation_steps = int(simulation_time_ns * 1000000 / timestep_fs)
        result["num_steps"] = simulation_steps

        # On restart, compute remaining steps
        steps_to_run = simulation_steps - simulation.currentStep
        if steps_to_run <= 0:
            result["warnings"].append(
                f"Already completed ({simulation.currentStep} steps done, "
                f"{simulation_steps} requested)"
            )
            steps_to_run = 0

        logger.info(f"Running {steps_to_run} steps (total: {simulation_steps}, current: {simulation.currentStep})")

        if steps_to_run > 0:
            simulation.step(steps_to_run)

        # Save final checkpoint (periodic reporter may not have fired for short runs)
        simulation.saveCheckpoint(str(checkpoint_file))
        logger.info(f"Final checkpoint saved: {checkpoint_file}")

        result["steps_completed"] = simulation.currentStep

        # Get final energy and positions
        state = simulation.context.getState(getEnergy=True, getPositions=True)
        final_energy = state.getPotentialEnergy()
        result["final_energy_kj_mol"] = float(final_energy._value)
        logger.info(f"Final energy: {final_energy}")

        # Save final structure
        final_pdb = out_dir / f"{pref}final_structure.pdb"
        positions = state.getPositions()
        with open(final_pdb, 'w') as f:
            PDBFile.writeFile(simulation.topology, positions, f)

        # Update result with file paths
        result["trajectory_file"] = str(trajectory_file)
        result["final_structure"] = str(final_pdb)
        result["energy_file"] = str(energy_file)
        result["success"] = True

        logger.info(f"Simulation complete. Trajectory saved: {trajectory_file}")

    except Exception as e:
        logger.error(f"MD simulation failed: {e}")
        result["errors"].append(f"MD simulation failed: {type(e).__name__}: {str(e)}")

    return result


def analyze_rmsd(
    trajectory_file: str,
    topology_file: str,
    reference_file: Optional[str] = None,
    selection: str = "protein and name CA",
    start_frame: int = 0,
    end_frame: Optional[int] = None
) -> dict:
    """Calculate RMSD using MDTraj
    
    Args:
        trajectory_file: Trajectory file (DCD or PDB)
        topology_file: Topology file (PDB or PRMTOP)
        reference_file: Reference structure (default: first frame)
        selection: Atom selection string
        start_frame: Starting frame index
        end_frame: Ending frame index (None for all)
    
    Returns:
        Dict with RMSD analysis results
    """
    logger.info(f"Calculating RMSD: {trajectory_file}")
    
    try:
        import mdtraj as mdt
    except ImportError:
        raise ImportError("MDTraj not installed. Install with: conda install -c conda-forge mdtraj")
    
    traj_path = Path(trajectory_file)
    topo_path = Path(topology_file)
    
    if not traj_path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {trajectory_file}")
    if not topo_path.is_file():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")
    
    # Load trajectory
    logger.info("Loading trajectory")
    if traj_path.suffix.lower() == ".dcd":
        traj = mdt.load_dcd(str(traj_path), top=str(topo_path))
    else:
        traj = mdt.load(str(traj_path))
    
    # Apply frame selection
    if end_frame is None:
        traj = traj[start_frame:]
    else:
        traj = traj[start_frame:end_frame]
    
    # Load reference
    if reference_file and Path(reference_file).is_file():
        ref = mdt.load(str(reference_file))
    else:
        ref = traj[0]
    
    # Select atoms
    selection_indices = traj.topology.select(selection)
    ref_selection = ref.topology.select(selection)
    
    # Calculate RMSD
    rmsd = mdt.rmsd(traj, ref, atom_indices=selection_indices, ref_atom_indices=ref_selection)
    
    # Calculate statistics
    mean_rmsd = float(np.mean(rmsd))
    std_rmsd = float(np.std(rmsd))
    min_rmsd = float(np.min(rmsd))
    max_rmsd = float(np.max(rmsd))
    
    logger.info(f"RMSD: mean={mean_rmsd:.2f}Å, std={std_rmsd:.2f}Å")
    
    return {
        "mean_rmsd_angstrom": mean_rmsd,
        "std_rmsd_angstrom": std_rmsd,
        "min_rmsd_angstrom": min_rmsd,
        "max_rmsd_angstrom": max_rmsd,
        "rmsd_values": rmsd.tolist(),
        "num_frames": len(rmsd),
        "selection": selection
    }


def analyze_rmsf(
    trajectory_file: str,
    topology_file: str,
    selection: str = "protein and name CA",
    start_frame: int = 0,
    end_frame: Optional[int] = None
) -> dict:
    """Calculate RMSF (Root Mean Square Fluctuation) using MDTraj
    
    Args:
        trajectory_file: Trajectory file (DCD or PDB)
        topology_file: Topology file (PDB or PRMTOP)
        selection: Atom selection string
        start_frame: Starting frame index
        end_frame: Ending frame index (None for all)
    
    Returns:
        Dict with RMSF analysis results
    """
    logger.info(f"Calculating RMSF: {trajectory_file}")
    
    try:
        import mdtraj as mdt
    except ImportError:
        raise ImportError("MDTraj not installed. Install with: conda install -c conda-forge mdtraj")
    
    traj_path = Path(trajectory_file)
    topo_path = Path(topology_file)
    
    if not traj_path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {trajectory_file}")
    if not topo_path.is_file():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")
    
    # Load trajectory
    logger.info("Loading trajectory")
    if traj_path.suffix.lower() == ".dcd":
        traj = mdt.load_dcd(str(traj_path), top=str(topo_path))
    else:
        traj = mdt.load(str(traj_path))
    
    # Apply frame selection
    if end_frame is None:
        traj = traj[start_frame:]
    else:
        traj = traj[start_frame:end_frame]
    
    # Select atoms
    selection_indices = traj.topology.select(selection)
    traj_selection = traj.atom_slice(selection_indices)
    
    # Calculate RMSF
    rmsf = mdt.rmsf(traj_selection, traj_selection)
    
    # Get residue information
    residues = [atom.residue for atom in traj_selection.topology.atoms]
    residue_names = [f"{res.name}{res.index}" for res in residues]
    
    mean_rmsf = float(np.mean(rmsf))
    max_rmsf = float(np.max(rmsf))
    max_rmsf_residue = residue_names[int(np.argmax(rmsf))]
    
    logger.info(f"RMSF: mean={mean_rmsf:.2f}Å, max={max_rmsf:.2f}Å ({max_rmsf_residue})")
    
    return {
        "mean_rmsf_angstrom": mean_rmsf,
        "max_rmsf_angstrom": max_rmsf,
        "max_rmsf_residue": max_rmsf_residue,
        "rmsf_values": rmsf.tolist(),
        "residue_names": residue_names,
        "num_residues": len(rmsf)
    }


def calculate_distance(
    trajectory_file: str,
    topology_file: str,
    atom1_selection: str,
    atom2_selection: str,
    start_frame: int = 0,
    end_frame: Optional[int] = None
) -> dict:
    """Calculate distance between two atom selections over trajectory
    
    Args:
        trajectory_file: Trajectory file (DCD or PDB)
        topology_file: Topology file (PDB or PRMTOP)
        atom1_selection: First atom selection string
        atom2_selection: Second atom selection string
        start_frame: Starting frame index
        end_frame: Ending frame index (None for all)
    
    Returns:
        Dict with distance analysis results
    """
    logger.info(f"Calculating distance: {trajectory_file}")
    
    try:
        import mdtraj as mdt
    except ImportError:
        raise ImportError("MDTraj not installed. Install with: conda install -c conda-forge mdtraj")
    
    traj_path = Path(trajectory_file)
    topo_path = Path(topology_file)
    
    if not traj_path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {trajectory_file}")
    if not topo_path.is_file():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")
    
    # Load trajectory
    logger.info("Loading trajectory")
    if traj_path.suffix.lower() == ".dcd":
        traj = mdt.load_dcd(str(traj_path), top=str(topo_path))
    else:
        traj = mdt.load(str(traj_path))
    
    # Apply frame selection
    if end_frame is None:
        traj = traj[start_frame:]
    else:
        traj = traj[start_frame:end_frame]
    
    # Select atoms
    indices1 = traj.topology.select(atom1_selection)
    indices2 = traj.topology.select(atom2_selection)
    
    if len(indices1) == 0:
        raise ValueError(f"No atoms selected: {atom1_selection}")
    if len(indices2) == 0:
        raise ValueError(f"No atoms selected: {atom2_selection}")
    
    # Calculate distances (between centers of mass if multiple atoms)
    if len(indices1) == 1 and len(indices2) == 1:
        # Single atom to single atom
        distances = mdt.compute_distances(traj, [[indices1[0], indices2[0]]])
        distances = distances.flatten()
    else:
        # Center of mass calculation
        distances = []
        for frame in traj:
            com1 = np.mean(frame.xyz[0, indices1, :], axis=0)
            com2 = np.mean(frame.xyz[0, indices2, :], axis=0)
            dist = np.linalg.norm(com1 - com2)
            distances.append(dist)
        distances = np.array(distances)
    
    # Calculate statistics
    mean_dist = float(np.mean(distances))
    std_dist = float(np.std(distances))
    min_dist = float(np.min(distances))
    max_dist = float(np.max(distances))
    
    logger.info(f"Distance: mean={mean_dist:.2f}Å, std={std_dist:.2f}Å")
    
    return {
        "mean_distance_angstrom": mean_dist,
        "std_distance_angstrom": std_dist,
        "min_distance_angstrom": min_dist,
        "max_distance_angstrom": max_dist,
        "distances": distances.tolist(),
        "num_frames": len(distances),
        "atom1_selection": atom1_selection,
        "atom2_selection": atom2_selection
    }


def analyze_hydrogen_bonds(
    trajectory_file: str,
    topology_file: str,
    donor_selection: str = "protein",
    acceptor_selection: str = "protein",
    distance_cutoff: float = 3.0,
    angle_cutoff: float = 120.0,
    start_frame: int = 0,
    end_frame: Optional[int] = None
) -> dict:
    """Analyze hydrogen bonds using MDTraj
    
    Args:
        trajectory_file: Trajectory file (DCD or PDB)
        topology_file: Topology file (PDB or PRMTOP)
        donor_selection: Donor atom selection
        acceptor_selection: Acceptor atom selection
        distance_cutoff: Distance cutoff in Angstroms
        angle_cutoff: Angle cutoff in degrees
        start_frame: Starting frame index
        end_frame: Ending frame index (None for all)
    
    Returns:
        Dict with hydrogen bond analysis results
    """
    logger.info(f"Analyzing hydrogen bonds: {trajectory_file}")
    
    try:
        import mdtraj as mdt
    except ImportError:
        raise ImportError("MDTraj not installed. Install with: conda install -c conda-forge mdtraj")
    
    traj_path = Path(trajectory_file)
    topo_path = Path(topology_file)
    
    if not traj_path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {trajectory_file}")
    if not topo_path.is_file():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")
    
    # Load trajectory
    logger.info("Loading trajectory")
    if traj_path.suffix.lower() == ".dcd":
        traj = mdt.load_dcd(str(traj_path), top=str(topo_path))
    else:
        traj = mdt.load(str(traj_path))
    
    # Apply frame selection
    if end_frame is None:
        traj = traj[start_frame:]
    else:
        traj = traj[start_frame:end_frame]
    
    # Select atoms
    donor_indices = traj.topology.select(donor_selection)
    acceptor_indices = traj.topology.select(acceptor_selection)
    
    # Calculate hydrogen bonds
    hbonds = mdt.baker_hubbard(
        traj,
        distance_cutoff=distance_cutoff / 10.0,  # Convert to nm
        angle_cutoff=np.deg2rad(angle_cutoff)
    )
    
    # Analyze hydrogen bonds
    hbond_list = []
    for frame_idx, frame_hbonds in enumerate(hbonds):
        for hbond in frame_hbonds:
            donor_idx = hbond[0]
            _h_idx = hbond[1]  # noqa: F841 (hydrogen atom index, kept for clarity)
            acceptor_idx = hbond[2]
            
            # Check if donor and acceptor are in selections
            if donor_idx in donor_indices and acceptor_idx in acceptor_indices:
                donor_atom = traj.topology.atom(donor_idx)
                acceptor_atom = traj.topology.atom(acceptor_idx)
                
                hbond_list.append({
                    "frame": int(frame_idx),
                    "donor": f"{donor_atom.residue.name}{donor_atom.residue.index}.{donor_atom.name}",
                    "acceptor": f"{acceptor_atom.residue.name}{acceptor_atom.residue.index}.{acceptor_atom.name}"
                })
    
    # Calculate frequency
    hbond_freq = {}
    for hbond in hbond_list:
        key = f"{hbond['donor']}-{hbond['acceptor']}"
        hbond_freq[key] = hbond_freq.get(key, 0) + 1
    
    total_hbonds = len(hbond_list)
    num_frames = len(hbonds)
    avg_hbonds_per_frame = total_hbonds / num_frames if num_frames > 0 else 0
    
    logger.info(f"Found {total_hbonds} hydrogen bonds (avg {avg_hbonds_per_frame:.1f} per frame)")
    
    return {
        "total_hbonds": total_hbonds,
        "num_frames": num_frames,
        "avg_hbonds_per_frame": avg_hbonds_per_frame,
        "hbond_frequency": hbond_freq,
        "distance_cutoff_angstrom": distance_cutoff,
        "angle_cutoff_degrees": angle_cutoff
    }


def analyze_secondary_structure(
    trajectory_file: str,
    topology_file: str,
    selection: str = "protein",
    start_frame: int = 0,
    end_frame: Optional[int] = None
) -> dict:
    """Analyze secondary structure using MDTraj
    
    Args:
        trajectory_file: Trajectory file (DCD or PDB)
        topology_file: Topology file (PDB or PRMTOP)
        selection: Atom selection string
        start_frame: Starting frame index
        end_frame: Ending frame index (None for all)
    
    Returns:
        Dict with secondary structure analysis results
    """
    logger.info(f"Analyzing secondary structure: {trajectory_file}")
    
    try:
        import mdtraj as mdt
    except ImportError:
        raise ImportError("MDTraj not installed. Install with: conda install -c conda-forge mdtraj")
    
    traj_path = Path(trajectory_file)
    topo_path = Path(topology_file)
    
    if not traj_path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {trajectory_file}")
    if not topo_path.is_file():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")
    
    # Load trajectory
    logger.info("Loading trajectory")
    if traj_path.suffix.lower() == ".dcd":
        traj = mdt.load_dcd(str(traj_path), top=str(topo_path))
    else:
        traj = mdt.load(str(traj_path))
    
    # Apply frame selection
    if end_frame is None:
        traj = traj[start_frame:]
    else:
        traj = traj[start_frame:end_frame]
    
    # Select atoms
    selection_indices = traj.topology.select(selection)
    traj_selection = traj.atom_slice(selection_indices)
    
    # Calculate secondary structure
    ss = mdt.compute_dssp(traj_selection, simplified=True)
    
    # Calculate statistics
    helix_fraction = np.mean(ss == 'H')
    sheet_fraction = np.mean(ss == 'E')
    coil_fraction = np.mean(ss == 'C')
    
    logger.info(f"Secondary structure: Helix={helix_fraction:.2%}, Sheet={sheet_fraction:.2%}, Coil={coil_fraction:.2%}")
    
    return {
        "helix_fraction": float(helix_fraction),
        "sheet_fraction": float(sheet_fraction),
        "coil_fraction": float(coil_fraction),
        "secondary_structure": ss.tolist(),
        "num_residues": ss.shape[1],
        "num_frames": ss.shape[0]
    }


def analyze_contacts(
    trajectory_file: str,
    topology_file: str,
    group1_selection: str,
    group2_selection: str,
    cutoff: float = 5.0,
    start_frame: int = 0,
    end_frame: Optional[int] = None
) -> dict:
    """Analyze contacts between two groups using MDTraj
    
    Args:
        trajectory_file: Trajectory file (DCD or PDB)
        topology_file: Topology file (PDB or PRMTOP)
        group1_selection: First group atom selection
        group2_selection: Second group atom selection
        cutoff: Distance cutoff in Angstroms
        start_frame: Starting frame index
        end_frame: Ending frame index (None for all)
    
    Returns:
        Dict with contact analysis results
    """
    logger.info(f"Analyzing contacts: {trajectory_file}")
    
    try:
        import mdtraj as mdt
    except ImportError:
        raise ImportError("MDTraj not installed. Install with: conda install -c conda-forge mdtraj")
    
    traj_path = Path(trajectory_file)
    topo_path = Path(topology_file)
    
    if not traj_path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {trajectory_file}")
    if not topo_path.is_file():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")
    
    # Load trajectory
    logger.info("Loading trajectory")
    if traj_path.suffix.lower() == ".dcd":
        traj = mdt.load_dcd(str(traj_path), top=str(topo_path))
    else:
        traj = mdt.load(str(traj_path))
    
    # Apply frame selection
    if end_frame is None:
        traj = traj[start_frame:]
    else:
        traj = traj[start_frame:end_frame]
    
    # Select atoms
    indices1 = traj.topology.select(group1_selection)
    indices2 = traj.topology.select(group2_selection)
    
    if len(indices1) == 0:
        raise ValueError(f"No atoms selected: {group1_selection}")
    if len(indices2) == 0:
        raise ValueError(f"No atoms selected: {group2_selection}")
    
    # Calculate contacts
    contacts = []
    for frame in traj:
        frame_contacts = []
        for i in indices1:
            for j in indices2:
                dist = np.linalg.norm(frame.xyz[0, i, :] - frame.xyz[0, j, :]) * 10.0  # Convert to Angstroms
                if dist < cutoff:
                    atom_i = traj.topology.atom(i)
                    atom_j = traj.topology.atom(j)
                    frame_contacts.append({
                        "atom1": f"{atom_i.residue.name}{atom_i.residue.index}.{atom_i.name}",
                        "atom2": f"{atom_j.residue.name}{atom_j.residue.index}.{atom_j.name}",
                        "distance": float(dist)
                    })
        contacts.append(frame_contacts)
    
    # Calculate statistics
    num_contacts_per_frame = [len(c) for c in contacts]
    avg_contacts = float(np.mean(num_contacts_per_frame))
    max_contacts = int(np.max(num_contacts_per_frame))
    
    logger.info(f"Contacts: avg={avg_contacts:.1f} per frame, max={max_contacts}")
    
    return {
        "avg_contacts_per_frame": avg_contacts,
        "max_contacts_per_frame": max_contacts,
        "contact_frames": contacts,
        "cutoff_angstrom": cutoff,
        "group1_selection": group1_selection,
        "group2_selection": group2_selection,
        "num_frames": len(contacts)
    }


def analyze_energy_timeseries(
    energy_file: str
) -> dict:
    """Analyze energy timeseries from simulation log
    
    Args:
        energy_file: Energy log file from OpenMM simulation
    
    Returns:
        Dict with energy analysis results
    """
    logger.info(f"Analyzing energy timeseries: {energy_file}")
    
    energy_path = Path(energy_file)
    if not energy_path.is_file():
        raise FileNotFoundError(f"Energy file not found: {energy_file}")
    
    # Parse energy file
    import pandas as pd
    
    try:
        # Try to read as CSV/TSV
        df = pd.read_csv(energy_path, sep='\s+', comment='#')
    except Exception:
        # Fallback: manual parsing
        data = []
        with open(energy_path, 'r') as f:
            header = None
            for line in f:
                if line.startswith('#'):
                    continue
                parts = line.strip().split()
                if len(parts) > 0:
                    if header is None:
                        header = parts
                    else:
                        if len(parts) == len(header):
                            data.append([float(p) for p in parts])
        
        if not data:
            raise ValueError("Could not parse energy file")
        df = pd.DataFrame(data, columns=header)
    
    # Extract energy columns
    energy_columns = {}
    for col in df.columns:
        col_lower = col.lower()
        if 'potential' in col_lower or 'pe' in col_lower:
            energy_columns['potential'] = col
        elif 'kinetic' in col_lower or 'ke' in col_lower:
            energy_columns['kinetic'] = col
        elif 'total' in col_lower or 'te' in col_lower:
            energy_columns['total'] = col
        elif 'temperature' in col_lower or 'temp' in col_lower:
            energy_columns['temperature'] = col
    
    # Calculate statistics
    results = {
        "num_frames": len(df)
    }
    
    for energy_type, col in energy_columns.items():
        if col in df.columns:
            values = df[col].values
            results[f"{energy_type}_mean"] = float(np.mean(values))
            results[f"{energy_type}_std"] = float(np.std(values))
            results[f"{energy_type}_min"] = float(np.min(values))
            results[f"{energy_type}_max"] = float(np.max(values))
    
    logger.info(f"Analyzed {len(df)} energy frames")
    
    return results

def compute_q_value(
    trajectory_file: str,
    topology: Optional[str] = None,
    reference_file: Optional[str] = None,
    frames: int = 10,
    output_contact: str = "contact",
    output_q: str = "q_value",
    output_dir: Optional[str] = None
) -> dict:
    """Compute Q value from trajectory file and visualize it.

    Calculates the fraction of native contacts (Q value) over the trajectory
    and generates contact map and Q-value visualizations.

    Args:
        trajectory_file: Trajectory file (.pdb or .dcd)
        topology: Topology file (required for .dcd trajectories)
        reference_file: Reference structure. If None, uses first frame
        frames: Number of frames to compute Q value from end of trajectory
        output_contact: Filename prefix for contact map output
        output_q: Filename prefix for Q-value map output
        output_dir: Output directory. If None, creates output/{job_id}/

    Returns:
        Dict with:
            - success: bool - True if computation completed successfully
            - job_id: str - Unique identifier for this computation
            - output_dir: str - Path to output directory
            - q_mean: float - Average Q-value of the entire structure
            - contact_path: str - Path to the contact map image
            - q_value_path: str - Path to the Q-value map image
            - num_native_contacts: int - Number of native contacts found
            - errors: list[str] - Error messages if any
            - warnings: list[str] - Non-critical warnings
    """
    logger.info(f"Computing Q-value: {trajectory_file}")

    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "q_mean": None,
        "contact_path": None,
        "q_value_path": None,
        "num_native_contacts": None,
        "errors": [],
        "warnings": []
    }

    # Setup output directory with human-readable name
    # If output_dir not specified, try to use current session directory
    if output_dir is None:
        session_dir = get_current_session()
        base_dir = session_dir if session_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "q_value")
    else:
        out_dir = create_unique_subdir(output_dir, "q_value")
    result["output_dir"] = str(out_dir)

    # Validate input files
    traj_path = Path(trajectory_file)
    if not traj_path.is_file():
        result["errors"].append(f"Trajectory file not found: {trajectory_file}")
        return result

    if trajectory_file.endswith('.dcd') and topology is None:
        result["errors"].append("Topology file required for .dcd trajectories")
        return result

    try:
        import mdtraj as mdt
    except ImportError:
        result["errors"].append("MDTraj not installed")
        result["errors"].append("Hint: Install with: conda install -c conda-forge mdtraj")
        return result

    try:
        # Load trajectory
        if trajectory_file.endswith('.pdb'):
            traj = mdt.load(trajectory_file, atom_indices=mdt.load(trajectory_file).topology.select('protein'))
        elif trajectory_file.endswith('.dcd'):
            traj = mdt.load_dcd(trajectory_file, top=topology, atom_indices=mdt.load(topology).topology.select('protein'))
        else:
            result["errors"].append(f"Unsupported trajectory format: {traj_path.suffix}")
            return result

        # Load reference
        if reference_file is None:
            ref = traj[0]
        else:
            if not Path(reference_file).is_file():
                result["errors"].append(f"Reference file not found: {reference_file}")
                return result
            ref = mdt.load(reference_file)

        # Use last N frames
        traj_cut = traj[-frames:]

        # Setup output paths
        contact_path = out_dir / f"{output_contact}.png"
        q_value_path = out_dir / f"{output_q}.png"

        # Compute Q-value
        q_list, native_contacts_with_indices, q_mean = compute_contact(traj_cut, ref)
        result["num_native_contacts"] = len(native_contacts_with_indices)

        # Generate plots
        plot_q_value(q_list, native_contacts_with_indices, traj.n_residues, contact_path, q_value_path)

        # Update result
        result["q_mean"] = float(q_mean)
        result["contact_path"] = str(contact_path)
        result["q_value_path"] = str(q_value_path)
        result["success"] = True

        logger.info(f"Q-value computation complete. Mean Q: {q_mean:.3f}")

    except Exception as e:
        logger.error(f"Q-value computation failed: {e}")
        result["errors"].append(f"Q-value computation failed: {type(e).__name__}: {str(e)}")

    return result


def compute_contact(traj, native):
    from itertools import combinations
    import mdtraj as mdt

    BETA_CONST = 50  # 1/nm
    LAMBDA_CONST = 1.8
    NATIVE_CUTOFF = 0.45  # nanometers
    
    # get the indices of all of the heavy atoms
    heavy = native.topology.select_atom_indices('heavy')
    # get the pairs of heavy atoms which are farther than 3
    # residues apart
    heavy_pairs = np.array(
        [(i,j) for (i,j) in combinations(heavy, 2)
            if abs(native.topology.atom(i).residue.index - \
                   native.topology.atom(j).residue.index) > 3])
    
    # compute the distances between these pairs in the native state
    heavy_pairs_distances = mdt.compute_distances(native[0], heavy_pairs)[0]
    
    # and get the pairs s.t. the distance is less than NATIVE_CUTOFF
    native_contacts = heavy_pairs[heavy_pairs_distances < NATIVE_CUTOFF]
    print("Number of native contacts", len(native_contacts))
    
    native_contacts_with_indices = [[] for _ in range(len(native_contacts))]
    
    contact_residue_indices = []

    for i in range(len(native_contacts)):
        index_i = native.topology.atom(native_contacts[i][0]).residue.index
        index_j = native.topology.atom(native_contacts[i][1]).residue.index
        indices = [index_i, index_j]
        if indices not in contact_residue_indices:
            contact_residue_indices.append(indices)
        native_contacts_with_indices[i].append(native_contacts[i].tolist())  # append atom indices
        native_contacts_with_indices[i].append(indices)  # append residue indices


    # now compute these distances for the whole trajectory
    r = mdt.compute_distances(traj, native_contacts)
    r0 = mdt.compute_distances(native[0], native_contacts)

    atom_q = 1.0 / (1 + np.exp(BETA_CONST * (r - LAMBDA_CONST * r0)))
    
    q_ave_over_frame = np.mean(atom_q, axis=0)
    q_ave_com = np.mean(q_ave_over_frame)

    q_ave_with_indices = [[q_ave_over_frame[i], native_contacts_with_indices[i][1]] for i in range(len(native_contacts))]

    # conmute average q over residue
    q_ave_over_residue = []

    for residue in contact_residue_indices:
        value_list = [q[0] for q in q_ave_with_indices if q[1] == residue]
        #print(value_list)
        mean = np.mean(value_list)
        q_ave_over_residue.append([mean, residue])


    #return  native_contacts, native_contacts_with_indices
    return q_ave_over_residue, native_contacts_with_indices, q_ave_com
    
def plot_q_value(q_list, native_contacts_with_indices, n_residue, output_contact, output_q):
    
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        from mpl_toolkits.axes_grid1 import make_axes_locatable
    except ImportError:
        raise ImportError("Matplotlib not installed. Install with: conda install matplotlib")


    q_matrix = np.zeros((n_residue, n_residue))
    #contact_matrix = np.zeros((n_residue, n_residue))

    # for pair in native_contacts_with_indices:
    #     i = pair[1][0]
    #     j = pair[1][1]
    #     contact_matrix[i][j] = contact_matrix[j][i] = 1

    contact_x = [pair[1][0] for pair in native_contacts_with_indices]
    contact_y = [pair[1][1] for pair in native_contacts_with_indices]

    tmp_x = contact_x.copy()
    tmp_y = contact_y.copy()

    contact_x.extend(tmp_y)
    contact_y.extend(tmp_x)

    for q in q_list:
        i = q[1][0]
        j = q[1][1]
        q_matrix[i][j] = q_matrix[j][i] = q[0]


    fig0= plt.figure(figsize=(12,12))
    ax0 = fig0.add_axes([0.1,0.1,0.8,0.8])
    fig1 = plt.figure(figsize=(12,12))
    ax1 = fig1.add_axes([0.05,0.05,0.85,0.9])

    # plt.xlim(0, residue_number)
    # plt.ylim(0, residue_number)

    #ax0.imshow(contact_matrix, cmap='Grays')
    ax0.set_xlim(0, n_residue)
    ax0.set_ylim(0, n_residue)
    ax0.invert_yaxis()
    ax0.scatter(contact_x, contact_y, marker=',')
    ax1.invert_yaxis()

    #カラーマップ調整
    cm = matplotlib.cm.Blues
    cm_list = cm(np.arange(cm.N))
    cm_list[0,3] = 0  # 0値の色を透明に変更
    cm_white = matplotlib.colors.ListedColormap(cm_list)

    #im = ax1.imshow(q_matrix, cmap='Blues')
    im = ax1.imshow(q_matrix, cmap=cm_white)

    #カラーバーの高さを合わせる
    divider = make_axes_locatable(ax1)
    color_ax = divider.append_axes('right', size='5%', pad=0.5)
    fig1.colorbar(im, ax=ax1, cax=color_ax)
    #fig1.subplots_adjust(left=1, right=2, top=1, bottom=0.5)

    fig0.savefig(output_contact)
    fig1.savefig(output_q)

    plt.show()



# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "run_md_simulation": run_md_simulation,
    "analyze_rmsd": analyze_rmsd,
    "analyze_rmsf": analyze_rmsf,
    "calculate_distance": calculate_distance,
    "analyze_hydrogen_bonds": analyze_hydrogen_bonds,
    "analyze_secondary_structure": analyze_secondary_structure,
    "analyze_contacts": analyze_contacts,
    "analyze_energy_timeseries": analyze_energy_timeseries,
    "compute_q_value": compute_q_value,
}

