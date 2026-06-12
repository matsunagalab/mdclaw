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

from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory,
    sha256_file,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)



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


class _XMLTopologyInputs:
    """Native loader for the ``system.xml`` + ``topology.pdb`` + ``state.xml``
    artifact triple emitted by ``build_amber_system`` /
    ``build_openmm_system``. The XML triple is the only topology contract
    on the run side.

    Attributes:
        topology: ``openmm.app.Topology`` loaded from ``topology.pdb``.
        positions: Initial positions, sourced from ``state.xml`` when
            available (carries the build-time minimized geometry),
            falling back to the PDB coordinates.
        box_vectors: Periodic box vectors, sourced from the same place as
            ``positions``. ``None`` for non-periodic systems.
        system_xml_path: Path to ``system.xml``. Held so callers can
            deserialize a fresh ``openmm.System`` per stage (NVT, NPT,
            production-clean checkpoint) without mutating a shared one.
        topology_pdb_path / state_xml_path: For provenance / signature
            hashing.
        is_periodic: ``box_vectors is not None``.
    """

    __slots__ = (
        "topology",
        "positions",
        "box_vectors",
        "system_xml_path",
        "topology_pdb_path",
        "state_xml_path",
        "is_periodic",
    )

    def __init__(
        self,
        *,
        topology,
        positions,
        box_vectors,
        system_xml_path: Path,
        topology_pdb_path: Path,
        state_xml_path: Optional[Path],
    ):
        self.topology = topology
        self.positions = positions
        self.box_vectors = box_vectors
        self.system_xml_path = system_xml_path
        self.topology_pdb_path = topology_pdb_path
        self.state_xml_path = state_xml_path
        self.is_periodic = box_vectors is not None


def _load_xml_topology_inputs(
    *,
    system_xml_file: str,
    topology_pdb_file: str,
    state_xml_file: Optional[str],
) -> _XMLTopologyInputs:
    """Load the XML triple into a single ``_XMLTopologyInputs`` record.

    Reads the ``topology.pdb`` for the OpenMM Topology, then prefers
    ``state.xml`` for positions / box (because that file carries the
    minimized post-build state); falls back to the PDB when ``state.xml``
    is absent. Imports OpenMM lazily so the helper stays cheap to call
    from validation paths that error out before reaching the loader.
    """
    from openmm.app import PDBFile
    from openmm import XmlSerializer

    pdb = PDBFile(str(topology_pdb_file))
    topology = pdb.topology
    positions = pdb.positions
    box_vectors = topology.getPeriodicBoxVectors()

    state_path = Path(state_xml_file) if state_xml_file else None
    if state_path is not None and state_path.is_file():
        with state_path.open("r") as fh:
            state = XmlSerializer.deserialize(fh.read())
        positions = state.getPositions()
        try:
            box_vectors = state.getPeriodicBoxVectors()
        except Exception:  # noqa: BLE001
            pass

    return _XMLTopologyInputs(
        topology=topology,
        positions=positions,
        box_vectors=box_vectors,
        system_xml_path=Path(system_xml_file).resolve(),
        topology_pdb_path=Path(topology_pdb_file).resolve(),
        state_xml_path=state_path.resolve() if state_path else None,
    )


def _deserialize_xml_system(xml_inputs: _XMLTopologyInputs):
    """Deserialize a fresh ``openmm.System`` from ``system.xml``.

    Each NVT / NPT / clean-handoff stage in run_equilibration and the
    production stage in run_production gets its own ``System`` from this
    helper; that lets the caller mutate the System (add restraints, add
    a barostat) without leaking those forces between stages.
    """
    from openmm import XmlSerializer

    with xml_inputs.system_xml_path.open("r") as fh:
        return XmlSerializer.deserialize(fh.read())


def _validate_xml_system_contract(
    system,
    topology,
    *,
    hmr_request: Optional[bool],
    implicit_solvent_request: Optional[str],
) -> None:
    """Run-time validator for the ``system.xml`` deserialized into a
    fresh ``openmm.System``. Each run stage (NVT, NPT, clean handoff,
    production) calls this helper after ``_deserialize_xml_system`` so
    a runtime kwarg that contradicts the build-time choice fails-fast.

    Raises ``_ModernSystemContractError`` with a structured ``code`` if a
    run-time choice contradicts what was baked into ``system.xml``.
    Stable codes (matched by callers / docs):

      - ``modern_system_hmr_mismatch`` — runtime ``hmr`` opts in but the
        deserialized System has standard hydrogen masses (or vice
        versa). Hydrogen mass is sampled from the first H atom in the
        topology; tolerance 0.05 amu.
      - ``modern_system_implicit_solvent_unsupported`` — runtime asked
        for implicit GB but the saved System carries no
        ``GBSAOBCForce`` / ``CustomGBForce`` /
        ``AmoebaGeneralizedKirkwoodForce``.

    The other build-time choices (``nonbondedMethod``, ``cutoff``,
    ``constraints``, ``rigidWater``) are baked into the System once and
    cannot be changed at run time, so this helper does not re-validate
    them — rebuilding the topo node is the only way to alter those.
    """
    if hmr_request is not None:
        actual_amu = _system_hydrogen_mass_amu(system, topology)
        # 4.0 amu is HMR; 1.008 amu is standard. Use 2.0 amu as a
        # decision boundary so a small numerical drift on either side
        # does not flip classification.
        if actual_amu is not None:
            actual_is_hmr = actual_amu > 2.0
            if actual_is_hmr != bool(hmr_request):
                raise _ModernSystemContractError(
                    code="modern_system_hmr_mismatch",
                    message=(
                        f"run_* requested hmr={bool(hmr_request)} but the "
                        f"saved system.xml has H={actual_amu:.3f} amu "
                        f"({'HMR' if actual_is_hmr else 'standard'} mass). "
                        f"HMR is a build-time decision — rebuild via "
                        f"build_amber_system(hmr={bool(hmr_request)}) to "
                        f"bake the matching mass into system.xml, or pass "
                        f"hmr={not bool(hmr_request)} at run time."
                    ),
                )

    if implicit_solvent_request is not None and not _system_has_implicit_solvent_force(system):
        raise _ModernSystemContractError(
            code="modern_system_implicit_solvent_unsupported",
            message=(
                f"run_* requested implicit_solvent="
                f"{implicit_solvent_request!r} but the saved system.xml "
                f"has no GB / implicit-solvent force. Rebuild the topo "
                f"node with build_amber_system(..., implicit_solvent="
                f"{implicit_solvent_request!r}) so the matching "
                f"openmmforcefields ``implicit/*.xml`` is baked into "
                f"``system.xml``. For non-shipped GB models, route "
                f"through build_openmm_system with a GB-aware ForceField "
                f"XML and pass --implicit-solvent <MODEL> there too."
            ),
        )


def _system_signature(
    xml_inputs: _XMLTopologyInputs,
    *,
    solvent_type: str,
    ensemble: str,
    pressure_bar: Optional[float],
    is_membrane: bool,
    implicit_solvent: Optional[str],
    hmr: bool,
) -> dict:
    """Reproducibility signature keyed off the XML triple.

    Hash names (``system_xml_sha256`` etc.) match the on-disk artifact
    names emitted by build_amber_system / build_openmm_system; the
    signature is recorded on every restart so eq → prod handoff can
    detect ancestor edits between runs.
    """
    return {
        "system_xml_sha256": sha256_file(xml_inputs.system_xml_path),
        "topology_pdb_sha256": sha256_file(xml_inputs.topology_pdb_path),
        "state_xml_sha256": (
            sha256_file(xml_inputs.state_xml_path)
            if xml_inputs.state_xml_path is not None else None
        ),
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
