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
from typing import Any, Dict, Optional, Tuple  # noqa: E402

import numpy as np  # noqa: E402
from mdclaw._common import (  # noqa: E402
    create_unique_subdir,
    create_validation_error,
    ensure_directory,
    generate_job_id,
    sha256_file,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


def _node_artifact_path(path: Optional[str]) -> str:
    """Convert an absolute output path into a node-relative artifact path."""
    if not path:
        return ""
    return f"artifacts/{Path(path).name}"


def _fail_node_if_running(
    job_dir: Optional[str],
    node_id: Optional[str],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Mark the node as ``failed`` when an early-return path fires after
    ``begin_node()``. Idempotent: safe to call when not in node mode, and
    safe to call when the node has already been marked failed elsewhere.

    Returns ``result`` unchanged so call sites can ``return _fail_node_if_running(...)``.
    """
    if job_dir and node_id and not result.get("success"):
        from mdclaw._node import fail_node
        try:
            fail_node(job_dir, node_id, errors=result.get("errors") or ["run_* aborted"])
        except Exception:  # noqa: BLE001
            # fail_node is best-effort — never let a bookkeeping failure mask
            # the original error the caller is about to surface.
            pass
    return result


def _resolve_implicit_solvent_model(
    implicit_solvent: str,
    openmm_models: Dict[str, Any],
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve a user-supplied implicit-solvent name to an OpenMM GB symbol.

    Goes through ``forcefield_catalog.normalize_implicit_solvent`` so the
    same alias rules apply on the run side as on the build side
    (case-insensitive ``HCT`` / ``OBC1`` / ``OBC2`` / ``GBn`` / ``GBn2`` plus
    the ``gbneck2`` / ``igb1``–``igb8`` shortcuts). Unknown names — and the
    historical ``.upper()``-vs-mixed-case bug that silently mapped
    ``"GBn2"`` → ``"GBN2"`` → ``OBC2`` — return a structured error instead
    of falling back, so typos surface as a clean
    ``implicit_solvent_model_unsupported`` failure rather than a silent
    accuracy regression.

    Returns:
        Tuple ``(model, None)`` on success; ``(None, error_dict)`` on
        failure. ``error_dict`` carries ``code`` and ``errors`` ready to
        splice into ``result`` before ``_fail_node_if_running``.
    """
    from mdclaw import forcefield_catalog as _fc

    canon = _fc.normalize_implicit_solvent(implicit_solvent)
    if canon not in _fc.IMPLICIT_SOLVENT_XML:
        supported = ", ".join(_fc.supported_implicit_solvent_models())
        return None, {
            "code": "implicit_solvent_model_unsupported",
            "errors": [
                f"Unknown implicit-solvent model {implicit_solvent!r}. "
                f"Supported: {supported}."
            ],
        }
    if canon not in openmm_models:
        # Catalog and the run-side OpenMM symbol map disagree. This should
        # not happen — flag it as a structured failure so the bug is
        # visible rather than masked by an OBC2 fallback.
        return None, {
            "code": "implicit_solvent_model_unsupported",
            "errors": [
                f"Implicit-solvent model {canon!r} is in the catalog but the "
                f"run-side OpenMM symbol map is missing it; this is a bug."
            ],
        }
    return openmm_models[canon], None


class _ModernSystemContractError(RuntimeError):
    """Raised when run_* requests a System trait that build_amber_system did
    not bake into the saved system.xml (e.g. ``hmr=True`` against a non-HMR
    build, or ``implicit_solvent`` against a vacuum / explicit System).

    Carries a stable ``code`` attribute so the caller can branch deterministically.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _system_has_implicit_solvent_force(system) -> bool:
    """Return True if the OpenMM System carries a Generalized-Born force."""
    gb_class_names = {
        "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
    }
    for force in system.getForces():
        if type(force).__name__ in gb_class_names:
            return True
    return False


def _system_hydrogen_mass_amu(system, topology) -> Optional[float]:
    """Sample the mass of the first hydrogen atom from a System+Topology pair.

    Returns ``None`` when the topology has no hydrogen atoms (e.g. heavy-atom-
    only test stub). HMR systems put ``~4 amu`` on hydrogens; non-HMR systems
    keep ``~1.008 amu``.
    """
    from openmm.unit import dalton
    for atom in topology.atoms():
        if atom.element is not None and atom.element.symbol == "H":
            mass = system.getParticleMass(atom.index)
            return float(mass.value_in_unit(dalton))
    return None


class _ModernPrmtopShim:
    """Drop-in replacement for ``AmberPrmtopFile`` when build_amber_system has
    emitted a system.xml + topology.pdb triple instead of parm7/rst7.

    ``run_equilibration`` and ``run_production`` access two attributes on the
    ``prmtop`` object: ``.topology`` and ``.createSystem(**kwargs)``. By
    wrapping the modern artifacts, we keep the legacy branches intact while
    routing both call sites through ``XmlSerializer.deserialize``.

    The saved System already has ``HMR`` / ``nonbondedMethod`` /
    ``constraints`` / implicit-solvent baked in at build time. Of the
    ``createSystem`` kwargs that legacy run_* code paths pass, only
    ``hydrogenMass`` and ``implicitSolvent`` are **validated** against the
    saved System — a request that the build did not commit to raises
    ``_ModernSystemContractError`` with a structured code so the caller can
    surface a clean error. The other kwargs (``nonbondedMethod``,
    ``nonbondedCutoff``, ``constraints``, ``rigidWater``) are honored as
    whatever the saved System carries, since they were already chosen at
    build time and re-asserting them at run time cannot change the
    deserialized System. Callers that need to re-tune those settings must
    rebuild via ``build_amber_system`` / ``build_openmm_system`` rather than
    relying on run_* kwargs.
    """

    def __init__(self, topology, system_xml_path: Path):
        self.topology = topology
        self._system_xml_path = Path(system_xml_path)

    def createSystem(self, **kwargs):
        from openmm import XmlSerializer
        with self._system_xml_path.open("r") as fh:
            system = XmlSerializer.deserialize(fh.read())

        # Hydrogen-mass repartitioning contract.
        requested_hmass = kwargs.get("hydrogenMass")
        if requested_hmass is not None:
            try:
                from openmm.unit import dalton
                requested_amu = float(requested_hmass.value_in_unit(dalton))
            except AttributeError:
                requested_amu = float(requested_hmass)
            actual_amu = _system_hydrogen_mass_amu(system, self.topology)
            if actual_amu is not None and abs(requested_amu - actual_amu) > 0.05:
                raise _ModernSystemContractError(
                    code="modern_system_hmr_mismatch",
                    message=(
                        f"run_* requested hydrogenMass={requested_amu:.3f} amu but the "
                        f"saved system.xml has H={actual_amu:.3f} amu. HMR is a "
                        f"build-time decision under the openmmforcefields path — "
                        f"rebuild via build_amber_system(hmr=True) to bake HMR into "
                        f"system.xml, or pass hmr=False at run time."
                    ),
                )

        # Implicit-solvent contract: GB-aware Force must already exist on the
        # saved System. ``build_amber_system(..., implicit_solvent=...)``
        # bakes the matching ``implicit/*.xml`` (HCT / OBC1 / OBC2 / GBn /
        # GBn2) into ``system.xml`` so the deserialized System carries a
        # ``CustomGBForce`` / ``GBSAOBCForce``. A vacuum or explicit-solvent
        # build paired with ``--implicit-solvent`` at run time is the case
        # this guard catches.
        if kwargs.get("implicitSolvent") is not None and not _system_has_implicit_solvent_force(system):
            raise _ModernSystemContractError(
                code="modern_system_implicit_solvent_unsupported",
                message=(
                    f"run_* requested implicitSolvent={kwargs['implicitSolvent']!r} but "
                    f"the saved system.xml has no GB / implicit-solvent force. "
                    f"Rebuild the topo node with "
                    f"``build_amber_system(..., implicit_solvent="
                    f"{kwargs['implicitSolvent']!r})`` so the matching "
                    f"openmmforcefields ``implicit/*.xml`` is baked into "
                    f"``system.xml``. For non-shipped GB models, route "
                    f"through ``build_openmm_system`` with a GB-aware "
                    f"third-party ForceField XML (e.g. GB99dms.xml)."
                ),
            )

        return system


class _ModernInpcrdShim:
    """Drop-in replacement for ``AmberInpcrdFile`` keyed off state.xml.

    Exposes ``.positions`` and ``.boxVectors``. State velocities are not
    surfaced here — the equilibration/production helpers explicitly load
    velocities through ``_load_state_into_simulation`` when restarting.
    """

    def __init__(self, positions, box_vectors):
        self.positions = positions
        self.boxVectors = box_vectors


def _maybe_load_modern_topology(
    *,
    system_xml_file: Optional[str],
    topology_pdb_file: Optional[str],
    state_xml_file: Optional[str],
):
    """Build (prmtop_shim, inpcrd_shim) from the modern triple, or
    ``(None, None)`` when the legacy parm7/rst7 path applies.

    Imports OpenMM lazily so callers that never reach the modern branch
    don't pay the import cost twice.
    """
    if not (system_xml_file and topology_pdb_file):
        return None, None
    from openmm.app import PDBFile
    from openmm import XmlSerializer

    pdb = PDBFile(str(topology_pdb_file))
    topology = pdb.topology
    positions = pdb.positions
    box_vectors = topology.getPeriodicBoxVectors()

    # Prefer state.xml positions / box if it's available — this captures the
    # minimized geometry written by build_amber_system.
    if state_xml_file:
        state_path = Path(state_xml_file)
        if state_path.is_file():
            with state_path.open("r") as fh:
                state = XmlSerializer.deserialize(fh.read())
            positions = state.getPositions()
            try:
                box_vectors = state.getPeriodicBoxVectors()
            except Exception:  # noqa: BLE001
                pass

    return (
        _ModernPrmtopShim(topology, Path(system_xml_file)),
        _ModernInpcrdShim(positions, box_vectors),
    )


def _system_signature(
    prmtop_path: Path,
    inpcrd_path: Path,
    *,
    solvent_type: str,
    ensemble: str,
    pressure_bar: Optional[float],
    is_membrane: bool,
    implicit_solvent: Optional[str],
    hmr: bool,
) -> dict:
    return {
        "prmtop_sha256": sha256_file(prmtop_path),
        "inpcrd_sha256": sha256_file(inpcrd_path),
        "solvent_type": solvent_type,
        "ensemble": ensemble,
        "pressure_bar": pressure_bar,
        "is_membrane": bool(is_membrane),
        "implicit_solvent": implicit_solvent,
        "hmr": bool(hmr),
    }


def _effective_pressure_bar(
    pressure_bar: Optional[float],
    implicit_solvent: Optional[str],
) -> Optional[float]:
    """Return the pressure value that defines the actual OpenMM ensemble.

    Implicit-solvent simulations cannot have a barostat, so their runtime
    ensemble is NVT even though ``run_equilibration`` keeps an explicit-water
    default of 1 bar for historical CLI compatibility.
    """
    if implicit_solvent:
        return 0.0
    return pressure_bar


def _integrator_signature(
    *,
    temperature_kelvin: float,
    timestep_fs: float,
    friction_per_ps: float = 1.0,
) -> dict:
    return {
        "integrator": "LangevinMiddleIntegrator",
        "temperature_kelvin": float(temperature_kelvin),
        "timestep_fs": float(timestep_fs),
        "friction_per_ps": float(friction_per_ps),
    }


def _signature_mismatches(expected: dict, actual: dict, keys: tuple[str, ...]) -> list[str]:
    mismatches = []
    for key in keys:
        if key not in expected or key not in actual:
            continue
        lhs = expected[key]
        rhs = actual[key]
        if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
            if abs(float(lhs) - float(rhs)) <= 1e-9:
                continue
        elif lhs == rhs:
            continue
        mismatches.append(f"{key}: restart={lhs!r}, current={rhs!r}")
    return mismatches


def _restart_source_metadata(
    job_dir: Optional[str],
    node_id: Optional[str],
    restart_from: Optional[str],
) -> dict:
    if not (job_dir and node_id and restart_from):
        return {}
    from mdclaw._node import get_ancestors, read_node, resolve_artifact
    restart_path = str(Path(restart_from).resolve())
    for anc_id in get_ancestors(job_dir, node_id)[1:]:
        try:
            anc = read_node(job_dir, anc_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        for artifact_key in ("state", "checkpoint"):
            rel = anc.get("artifacts", {}).get(artifact_key)
            if not isinstance(rel, str):
                continue
            if str(resolve_artifact(job_dir, anc_id, rel)) == restart_path:
                return anc.get("metadata", {}) or {}
    return {}


def _detect_ensemble_mismatch(
    state_xml_path: Path, system_has_barostat: bool
) -> Optional[str]:
    """Classify a barostat / saved-state inconsistency for warning purposes.

    The ensemble-agnostic loader (``_load_state_into_simulation``)
    transfers only positions/velocities/box and does not touch Context
    parameters, so neither case raises. Callers may still surface a
    warning when the saved ensemble differs from the new System's
    ensemble — the simulation is safe to start, but a brief
    re-equilibration is expected (e.g. NVT→NPT will need a few ps for
    the barostat to settle the volume).

    Returns one of:
        - ``"npt_state_nvt_system"`` — state has NPT barostat parameters
          but the new System has no barostat.
        - ``"nvt_state_npt_system"`` — new System has a barostat but the
          saved state lacks NPT parameters.
        - ``None`` — matched (NPT/NPT or NVT/NVT).
    """
    state_has_pressure = "MonteCarloPressure" in state_xml_path.read_text()
    if state_has_pressure and not system_has_barostat:
        return "npt_state_nvt_system"
    if system_has_barostat and not state_has_pressure:
        return "nvt_state_npt_system"
    return None


def _load_state_into_simulation(
    simulation,
    restart_path: Path,
    *,
    is_periodic: bool,
) -> dict:
    """Load a saved OpenMM state into ``simulation`` regardless of ensemble.

    For ``.xml`` (saveState output): deserialize via
    ``XmlSerializer.deserialize`` and transfer only positions, velocities,
    and (when periodic) box vectors. Unlike ``simulation.loadState()`` —
    which routes through ``Context.setState`` and restores every saved
    Context parameter by name — this skips parameter restoration entirely
    and is therefore safe across ensemble changes:

    - NPT state → NVT system: barostat parameters in the saved state are
      ignored (the new System has no barostat to receive them).
    - NVT state → NPT system: the new barostat starts in its default
      relaxed state and re-equilibrates within a few ps.

    For ``.chk`` (binary checkpoint): fall back to
    ``simulation.loadCheckpoint`` which requires identical System layout
    and GPU architecture. This path is for same-machine bit-exact restart
    only; cross-ensemble or cross-GPU should use XML.

    Note: ``simulation.currentStep`` is NOT restored by this helper. The
    caller restores it from ``metadata.final_step`` (see
    ``read_ancestor_final_step``).

    Returns ``{"format": "xml"|"checkpoint"}``.
    """
    if restart_path.suffix == ".xml":
        from openmm import XmlSerializer
        state = XmlSerializer.deserialize(restart_path.read_text())
        simulation.context.setPositions(state.getPositions())
        try:
            simulation.context.setVelocities(state.getVelocities())
        except Exception as e:
            logger.warning(
                f"Saved state at {restart_path} has no velocities "
                f"({e}); resuming with re-thermalized velocities."
            )
        if is_periodic:
            box = state.getPeriodicBoxVectors()
            if box is not None:
                simulation.context.setPeriodicBoxVectors(*box)
        return {"format": "xml"}
    simulation.loadCheckpoint(str(restart_path))
    return {"format": "checkpoint"}


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


def _count_state_data_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    rows = 0
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            rows += 1
    return rows


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


def _record_production_node_result(
    *,
    result: dict,
    job_dir: str,
    node_id: str,
    simulation_time_ns: float,
    temperature_kelvin: float,
    pressure_bar: Optional[float],
    platform: str,
    hmr: bool,
    timestep_fs: float,
    output_frequency_ps: float,
    random_seed: Optional[int],
) -> None:
    """Persist production artifacts and metadata to the DAG node."""
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
                "platform": result.get("platform") or platform,
                "hmr": hmr,
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "random_seed": random_seed,
                "num_steps": result.get("steps_completed"),
                "start_step": result.get("start_step"),
                "start_time_ns": result.get("start_time_ns"),
                "final_step": result.get("steps_completed"),
                "system_signature": result.get("system_signature"),
                "integrator_signature": result.get("integrator_signature"),
            })
    else:
        fail_node(job_dir, node_id, errors=result.get("errors", []))


def run_equilibration(
    prmtop_file: Optional[str] = None,
    inpcrd_file: Optional[str] = None,
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
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
        restart_from: Path to a saved OpenMM state (``.xml`` preferred,
            ``.chk`` fallback) to resume from. When set, the pre-NVT
            staged minimization and warmup are skipped, and
            positions/velocities/box are loaded via the
            ensemble-agnostic loader (so an NPT-saved state can resume
            into an NVT stage and vice versa). In node mode this is
            auto-resolved from the nearest ``eq``/``prod`` ancestor's
            ``state`` artifact, enabling NPT → NVT → NPT chaining
            across multiple eq nodes.

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

    # Auto-resolve inputs from DAG when in node mode
    if job_dir and node_id:
        from mdclaw._node import resolve_node_inputs, validate_node_execution_context
        _inputs = resolve_node_inputs(job_dir, node_id, "eq")
        if "input_resolution_error" in _inputs:
            return create_validation_error(
                "job_dir/node_id",
                _inputs["input_resolution_error"],
                expected="Completed topo ancestor with parm7/rst7 artifacts",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            )
        if not prmtop_file and "prmtop_file" in _inputs:
            prmtop_file = _inputs["prmtop_file"]
        if not inpcrd_file and "inpcrd_file" in _inputs:
            inpcrd_file = _inputs["inpcrd_file"]
        if not system_xml_file and "system_xml_file" in _inputs:
            system_xml_file = _inputs["system_xml_file"]
        if not topology_pdb_file and "topology_pdb_file" in _inputs:
            topology_pdb_file = _inputs["topology_pdb_file"]
        if not state_xml_file and "state_xml_file" in _inputs:
            state_xml_file = _inputs["state_xml_file"]
        if not is_membrane and _inputs.get("is_membrane"):
            is_membrane = True
        # eq → eq chaining: when an eq/prod ancestor exposes a state
        # artifact, resume from it instead of running a fresh
        # minimization + warmup. The first eq node from topo has no
        # ancestor and runs from inpcrd as before.
        if not restart_from and "restart_from" in _inputs:
            restart_from = _inputs["restart_from"]
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "eq",
            actual_conditions={
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "nvt_steps": nvt_steps,
                "npt_steps": npt_steps,
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

    # The new openmmforcefields-unification path supplies system.xml +
    # topology.pdb + state.xml (PR3); the legacy path uses parm7 + rst7.
    # Either set is acceptable here.
    _modern_inputs = bool(system_xml_file and topology_pdb_file)
    _legacy_inputs = bool(prmtop_file and inpcrd_file)
    if not _modern_inputs and not _legacy_inputs:
        return create_validation_error(
            "topology_inputs",
            "Either (system_xml_file + topology_pdb_file) or "
            "(prmtop_file + inpcrd_file) is required",
            expected="Modern triple from build_amber_system (PR3), or legacy parm7/rst7",
            actual=(
                f"system_xml_file={system_xml_file!r}, topology_pdb_file={topology_pdb_file!r}, "
                f"prmtop_file={prmtop_file!r}, inpcrd_file={inpcrd_file!r}"
            ),
            hints=["Run build_amber_system first or execute in node mode from an eq node."],
            code="missing_topology_inputs",
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
        "restraint_atoms": restraint_atoms,
        "restraint_count": 0,
        "relaxation_protocol": None,
        "low_temperature_warmup_steps": 0,
        "nan_failure_diagnostics": None,
        "platform": None,
        "errors": [],
        "warnings": [],
    }

    if _modern_inputs:
        system_xml_path = Path(system_xml_file).resolve()
        topology_pdb_path = Path(topology_pdb_file).resolve()
        state_xml_path = Path(state_xml_file).resolve() if state_xml_file else None
        if not system_xml_path.is_file():
            result["errors"].append(f"system.xml not found: {system_xml_file}")
            return result
        if not topology_pdb_path.is_file():
            result["errors"].append(f"topology.pdb not found: {topology_pdb_file}")
            return result
        if state_xml_path and not state_xml_path.is_file():
            result["errors"].append(f"state.xml not found: {state_xml_file}")
            return result
        # Synthesize legacy paths for downstream code that still references
        # them (e.g. logging / signature). Both point at the modern artifacts.
        prmtop_path = system_xml_path
        inpcrd_path = state_xml_path or topology_pdb_path
    else:
        prmtop_path = Path(prmtop_file).resolve()
        inpcrd_path = Path(inpcrd_file).resolve()
        if not prmtop_path.is_file():
            result["errors"].append(f"Topology file not found: {prmtop_file}")
            return result
        if not inpcrd_path.is_file():
            result["errors"].append(f"Coordinate file not found: {inpcrd_file}")
            return result
    restart_path = Path(restart_from).resolve() if restart_from else None
    if restart_path is not None and not restart_path.is_file():
        result["errors"].append(f"Restart file not found: {restart_from}")
        return result

    try:
        from openmm.app import (
            AmberPrmtopFile, AmberInpcrdFile, PDBFile,
            Simulation, PME, NoCutoff, HBonds, StateDataReporter,
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
    from mdclaw.research_server import WATER_NAMES, COMMON_IONS
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

        # Load topology and coordinates — modern path uses Pablo-built
        # system.xml + topology.pdb + state.xml; legacy path uses parm7/rst7.
        if _modern_inputs:
            logger.info("Loading modern artifact triple (system.xml + topology.pdb + state.xml)")
            prmtop, inpcrd = _maybe_load_modern_topology(
                system_xml_file=str(system_xml_path),
                topology_pdb_file=str(topology_pdb_path),
                state_xml_file=str(state_xml_path) if state_xml_path else None,
            )
        else:
            logger.info("Loading Amber files")
            prmtop = AmberPrmtopFile(str(prmtop_path))
            inpcrd = AmberInpcrdFile(str(inpcrd_path))

        is_periodic = inpcrd.boxVectors is not None
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
            gb_model, gb_err = _resolve_implicit_solvent_model(
                implicit_solvent, IMPLICIT_MODELS
            )
            if gb_err:
                result["errors"].extend(gb_err["errors"])
                result["code"] = gb_err["code"]
                return _fail_node_if_running(job_dir, node_id, result)
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
            )
            logger.info(
                f"Equilibration restarted from {_eq_load_info['format']} "
                f"({restart_path})"
            )
            if _node_mode:
                from mdclaw._node import read_ancestor_final_step
                anc_step = read_ancestor_final_step(job_dir, node_id)
                if anc_step is not None:
                    sim_nvt.currentStep = anc_step
            result["restarted_from"] = str(restart_path)
        else:
            sim_nvt.context.setPositions(positions)
            if is_periodic and inpcrd.boxVectors is not None:
                sim_nvt.context.setPeriodicBoxVectors(*inpcrd.boxVectors)

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

        # Universal pre-NVT relaxation protocol. Skipped on restart —
        # the ancestor state is already minimized and thermalized, and
        # re-minimizing would discard the loaded velocities and box.
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
        # prmtop's initial box. This matters when the NVT simulation
        # itself was restarted from a prior NPT state with a different box.
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
                system_npt.addForce(MonteCarloMembraneBarostat(
                    pressure_bar * bar, 0.0 * bar * nanometer,
                    temperature_kelvin * kelvin,
                    MonteCarloMembraneBarostat.XYIsotropic,
                    MonteCarloMembraneBarostat.ZFree,
                    25,
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
            elif is_periodic and inpcrd.boxVectors is not None:
                sim_npt.context.setPeriodicBoxVectors(*inpcrd.boxVectors)

            sim_npt.step(npt_steps)
            for reporter in sim_npt.reporters:
                _close_reporter_stream(reporter)
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
            gb_model_clean, gb_err = _resolve_implicit_solvent_model(
                implicit_solvent, IMPLICIT_MODELS
            )
            if gb_err:
                result["errors"].extend(gb_err["errors"])
                result["code"] = gb_err["code"]
                return _fail_node_if_running(job_dir, node_id, result)
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

        final_ensemble = (
            "NPT" if (pressure_bar and pressure_bar > 0
                      and npt_steps > 0) else "NVT"
        )
        result["system_signature"] = _system_signature(
            prmtop_path,
            inpcrd_path,
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
                    "restraint_atoms": restraint_atoms,
                    "restraint_count": result.get("restraint_count"),
                    "temperature_kelvin": temperature_kelvin,
                    "pressure_bar": pressure_bar,
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


def run_production(
    prmtop_file: Optional[str] = None,
    inpcrd_file: Optional[str] = None,
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
        restart_from: Path to a state file to restart from. Prefer ``.xml``
                     (saveState, cross-node portable); ``.chk``
                     (saveCheckpoint, GPU-architecture-specific) is a
                     legacy fallback. In node mode this is auto-resolved
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
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}
        if not prmtop_file and "prmtop_file" in _inputs:
            prmtop_file = _inputs["prmtop_file"]
        if not inpcrd_file and "inpcrd_file" in _inputs:
            inpcrd_file = _inputs["inpcrd_file"]
        if not system_xml_file and "system_xml_file" in _inputs:
            system_xml_file = _inputs["system_xml_file"]
        if not topology_pdb_file and "topology_pdb_file" in _inputs:
            topology_pdb_file = _inputs["topology_pdb_file"]
        if not state_xml_file and "state_xml_file" in _inputs:
            state_xml_file = _inputs["state_xml_file"]
        if not restart_from and "restart_from" in _inputs:
            restart_from = _inputs["restart_from"]

    _modern_inputs = bool(system_xml_file and topology_pdb_file)
    _legacy_inputs = bool(prmtop_file and inpcrd_file)
    if not _modern_inputs and not _legacy_inputs:
        return create_validation_error(
            "topology_inputs",
            "Either (system_xml_file + topology_pdb_file) or "
            "(prmtop_file + inpcrd_file) is required",
            expected="Modern triple from build_amber_system (PR3), or legacy parm7/rst7",
            actual=(
                f"system_xml_file={system_xml_file!r}, topology_pdb_file={topology_pdb_file!r}, "
                f"prmtop_file={prmtop_file!r}, inpcrd_file={inpcrd_file!r}"
            ),
            hints=["Run build_amber_system first or execute in node mode from a prod node."],
            code="missing_topology_inputs",
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

    # Validate input files (modern triple takes precedence over legacy
    # parm7/rst7 — see PR3 of openmmforcefields-unification).
    # Every early-return path below this point happens AFTER begin_node(),
    # so it must transit through _fail_node_if_running to flip the node out
    # of "running" — otherwise the DAG sees a perpetually in-flight node.
    if _modern_inputs:
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
        prmtop_path = system_xml_path
        inpcrd_path = state_xml_path or topology_pdb_path
    else:
        prmtop_path = Path(prmtop_file)
        inpcrd_path = Path(inpcrd_file)
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
            return _fail_node_if_running(job_dir, node_id, result)
        if not inpcrd_path.is_file():
            result["errors"].append(f"Coordinate file not found: {inpcrd_file}")
            result["errors"].append("Hint: Run build_amber_system first to create topology files")
            return _fail_node_if_running(job_dir, node_id, result)

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
        # Load system — modern path uses Pablo-built system.xml + topology.pdb
        # + state.xml; legacy path uses parm7/rst7 from a pre-PR3 build.
        if _modern_inputs:
            logger.info("Loading modern artifact triple (system.xml + topology.pdb + state.xml)")
            prmtop, inpcrd = _maybe_load_modern_topology(
                system_xml_file=str(system_xml_path),
                topology_pdb_file=str(topology_pdb_path),
                state_xml_file=str(state_xml_path) if state_xml_path else None,
            )
        else:
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
            # Implicit solvent mode (Generalized Born). Resolve via the
            # shared helper so user aliases (``gbneck2``, ``igb8``, case
            # variants) canonicalize the same way build_amber_system did,
            # and unknown names fail-fast instead of silently mapping to
            # OBC2.
            gb_model, gb_err = _resolve_implicit_solvent_model(
                implicit_solvent, IMPLICIT_MODELS
            )
            if gb_err:
                result["errors"].extend(gb_err["errors"])
                result["code"] = gb_err["code"]
                return _fail_node_if_running(job_dir, node_id, result)
            from mdclaw import forcefield_catalog as _fc
            canonical_implicit = _fc.normalize_implicit_solvent(implicit_solvent)
            system = prmtop.createSystem(
                implicitSolvent=gb_model,
                nonbondedMethod=NoCutoff,
                constraints=HBonds,
                soluteDielectric=1.0,
                solventDielectric=78.5,
                **hmr_kwargs,
            )
            logger.info(f"Using implicit solvent ({canonical_implicit}) with NoCutoff")
            result["solvent_type"] = "implicit"
            result["implicit_model"] = canonical_implicit
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
            result["errors"].append(
                "Non-periodic topology without implicit_solvent would run vacuum production. "
                "Pass --implicit-solvent for GB simulations or build an explicit-solvent topology."
            )
            return _fail_node_if_running(job_dir, node_id, result)

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
        current_system_signature = _system_signature(
            prmtop_path,
            inpcrd_path,
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
                return _fail_node_if_running(job_dir, node_id, result)
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
                "prmtop_sha256", "inpcrd_sha256", "solvent_type",
                "is_membrane", "implicit_solvent", "hmr",
            ) if _restart_is_xml else (
                "prmtop_sha256", "inpcrd_sha256", "solvent_type",
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
            )
            # Restore the cumulative step counter from the ancestor so
            # eq→prod and prod→prod extension preserves the timeline.
            # The state file itself does not carry currentStep.
            if _node_mode:
                from mdclaw._node import read_ancestor_final_step
                anc_step = read_ancestor_final_step(job_dir, node_id)
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
        )

    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "run_equilibration": run_equilibration,
    "run_production": run_production,
}

