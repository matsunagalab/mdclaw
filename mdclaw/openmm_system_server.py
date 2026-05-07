"""OpenMM-XML / TorchForce research-mode topology builder.

Companion to ``mdclaw.amber_server.build_amber_system``. Where
``build_amber_system`` is the curated Amber-catalog path (ff19SB / ff14SB /
phosaa / lipid21 / GLYCAM, all routed through the openmmforcefields
catalog), ``build_openmm_system`` is the research-mode escape hatch: the
user supplies a list of OpenMM ``ForceField`` XML files (e.g.
``GB99dms.xml`` from the Greener group) together with optional ligand
molecules, and the tool emits the same ``system.xml + topology.pdb +
state.xml`` artifact triple so eq/prod can consume the result through the
same DAG resolver.

The tool is intentionally permissive — there is no FF×water guardrail
matrix here, because by definition the user is bringing their own XML
that mdclaw's Amber25 catalog does not know about. We only block on
critical correctness conditions (e.g. GB99dms requires OpenMM 8.0+).

TorchForce / .pt overlays (garnet-style ML potentials) are explicitly out
of scope for this PR.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,  # noqa: F401  (kept for parity / future extension)
    create_file_not_found_error,
    create_tool_not_available_error,
    create_unique_subdir,
    ensure_directory,
    setup_logger,
)
from mdclaw import _topology_pablo  # noqa: E402

logger = setup_logger(__name__)

WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> Optional[str]:
    try:
        import hashlib
        with path.open("rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return None


def _check_gb99_openmm_version_compatible(forcefield_xml: List[str]) -> Optional[str]:
    """Return an error message if any GB99* XML is paired with OpenMM < 8.0.

    GB99dms is the Greener group's GBNeck2-derived implicit-solvent FF; it
    requires OpenMM >= 8.0 because earlier versions silently miscompute the
    GB integral for the parameter set.
    """
    needs_openmm_8 = any(
        "gb99" in (Path(p).name.lower()) for p in forcefield_xml
    )
    if not needs_openmm_8:
        return None
    try:
        import openmm
        major = int(openmm.version.short_version.split(".")[0])
    except Exception:  # noqa: BLE001
        return None
    if major < 8:
        return (
            f"GB99dms-style implicit-solvent XML requires OpenMM >= 8.0; "
            f"current OpenMM is {openmm.version.full_version}. Upgrade via "
            f"`conda env update -f environment.yml`."
        )
    return None


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


def build_openmm_system(
    pdb_file: Optional[str] = None,
    forcefield_xml: Optional[List[str]] = None,
    additional_smiles: Optional[List[List[str]]] = None,
    nonbonded_method: str = "PME",
    nonbonded_cutoff_nm: float = 1.0,
    constraints: str = "HBonds",
    rigid_water: bool = True,
    minimize: bool = True,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an OpenMM ``System`` from arbitrary user-supplied ForceField XML.

    This is the research-mode counterpart to ``build_amber_system``. It does
    not consult mdclaw's Amber25 force-field catalog or guardrail matrix —
    by definition the user is bringing third-party / experimental XML that
    sits outside the catalog (e.g. ``GB99dms.xml``).

    Args:
        pdb_file: Path to the prepared (hydrogenated) PDB. Loaded via Pablo
            with a ``openmm.app.PDBFile`` fallback for residues Pablo cannot
            match.
        forcefield_xml: List of OpenMM ForceField XML paths or names. Looked
            up against ``openmm.app.ForceField``'s search path; absolute
            paths work too. Order matters when residue templates overlap.
        additional_smiles: ``[(residue_name, smiles), ...]`` pairs for
            non-standard residues so Pablo can match them via
            ``ResidueDefinition.anon_from_smiles``.
        nonbonded_method: ``"PME"`` (periodic), ``"NoCutoff"`` (gas-phase or
            implicit), or ``"CutoffNonPeriodic"``.
        nonbonded_cutoff_nm: Real-space cutoff in nm; ignored for
            ``NoCutoff``.
        constraints: ``"HBonds"`` (default) / ``"AllBonds"`` / ``"None"``.
        rigid_water: Pass-through to ``ForceField.createSystem``.
        minimize: Run a short LocalEnergyMinimizer pass before
            serializing the state. Disable for debugging.
        output_name: Stem for the artifact file names.
        output_dir / job_dir / node_id: Standard mdclaw I/O knobs.

    Returns: dict with ``success``, ``errors``, ``warnings``, plus on
    success ``system_xml``, ``topology_pdb``, ``state_xml``,
    ``num_atoms``, ``num_residues``, ``forcefield_provenance``.
    """
    result: Dict[str, Any] = {
        "success": False,
        "errors": [],
        "warnings": [],
        "parameters": {
            "forcefield_xml": list(forcefield_xml or []),
            "nonbonded_method": nonbonded_method,
            "nonbonded_cutoff_nm": nonbonded_cutoff_nm,
            "constraints": constraints,
            "rigid_water": rigid_water,
            "minimize": minimize,
        },
    }

    _node_mode = bool(job_dir and node_id)
    if not pdb_file:
        return create_file_not_found_error(str(pdb_file), file_type="pdb_file")

    pdb_path = Path(pdb_file)
    if not pdb_path.is_file():
        return create_file_not_found_error(str(pdb_path), file_type="pdb_file")

    if not forcefield_xml:
        result["errors"].append(
            "forcefield_xml is required: supply at least one OpenMM ForceField XML."
        )
        return result

    try:
        import openmmforcefields  # noqa: F401
    except ImportError:
        return create_tool_not_available_error(
            "openmmforcefields",
            "Run `conda env update -f environment.yml` to install the openmmforcefields-unification deps",
        )

    incompat = _check_gb99_openmm_version_compatible(forcefield_xml)
    if incompat:
        result["errors"].append(incompat)
        return {
            **result,
            "code": "openmm_version_too_old",
        }

    if output_dir:
        out_dir = Path(output_dir)
        ensure_directory(out_dir)
    else:
        out_dir = create_unique_subdir(WORKING_DIR, "openmm_system")
    result["output_dir"] = str(out_dir)

    system_xml_file = out_dir / f"{output_name}.system.xml"
    topology_pdb_file = out_dir / f"{output_name}.topology.pdb"
    state_xml_file = out_dir / f"{output_name}.state.xml"

    try:
        from openmm import app, unit, XmlSerializer, LangevinIntegrator
        from openmm.app import ForceField, Modeller, PDBFile, Simulation
    except ImportError as exc:
        result["errors"].append(
            f"OpenMM stack not importable: {exc}. Run `conda env update -f environment.yml`."
        )
        return result

    extra_smiles_pairs: List[tuple[str, str]] = []
    for item in additional_smiles or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            result["warnings"].append(
                f"additional_smiles entry must be a 2-element [residue_name, smiles]; "
                f"got {item!r}"
            )
            continue
        extra_smiles_pairs.append((str(item[0]), str(item[1])))

    pablo_result = _topology_pablo.load_topology(
        pdb_path, extra_smiles=extra_smiles_pairs
    )
    result["warnings"].extend(pablo_result.warnings)
    omm_topology = pablo_result.topology
    omm_positions = pablo_result.positions

    nb_method_map = {
        "PME": app.PME,
        "NoCutoff": app.NoCutoff,
        "CutoffNonPeriodic": app.CutoffNonPeriodic,
        "Ewald": app.Ewald,
        "CutoffPeriodic": app.CutoffPeriodic,
    }
    if nonbonded_method not in nb_method_map:
        result["errors"].append(
            f"nonbonded_method={nonbonded_method!r} not recognized; "
            f"choose from {sorted(nb_method_map)}."
        )
        return result

    constraints_map = {
        "HBonds": app.HBonds,
        "AllBonds": app.AllBonds,
        "None": None,
        None: None,
    }
    if constraints not in constraints_map:
        result["errors"].append(
            f"constraints={constraints!r} not recognized; "
            f"choose from HBonds | AllBonds | None."
        )
        return result

    try:
        ff = ForceField(*forcefield_xml)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"ForceField init failed: {type(exc).__name__}: {exc}. "
            f"Bundle: {forcefield_xml}"
        )
        return result

    modeller = Modeller(omm_topology, omm_positions)
    try:
        modeller.addExtraParticles(ff)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"addExtraParticles failed (continuing without virtual sites): "
            f"{type(exc).__name__}: {exc}"
        )

    create_system_kwargs: Dict[str, Any] = {
        "nonbondedMethod": nb_method_map[nonbonded_method],
        "constraints": constraints_map[constraints],
        "rigidWater": rigid_water,
    }
    if nonbonded_method != "NoCutoff":
        create_system_kwargs["nonbondedCutoff"] = nonbonded_cutoff_nm * unit.nanometer

    try:
        system = ff.createSystem(modeller.topology, **create_system_kwargs)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"ForceField.createSystem failed: {type(exc).__name__}: {exc}"
        )
        return result

    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 2.0 * unit.femtoseconds
        )
        simulation = Simulation(modeller.topology, system, integrator)
        simulation.context.setPositions(modeller.positions)
        if minimize:
            simulation.minimizeEnergy(maxIterations=200)
        state = simulation.context.getState(
            getPositions=True, getVelocities=True,
            enforcePeriodicBox=(nonbonded_method == "PME"),
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Energy minimization failed: {type(exc).__name__}: {exc}"
        )
        return result

    # Coerce Pablo's int residue.id to str so PDBFile.writeFile(keepIds=True)
    # doesn't choke on `len(int_id)`.
    for res in modeller.topology.residues():
        if not isinstance(res.id, str):
            res.id = str(res.id)

    try:
        with system_xml_file.open("w") as fh:
            fh.write(XmlSerializer.serialize(system))
        with state_xml_file.open("w") as fh:
            fh.write(XmlSerializer.serialize(state))
        with topology_pdb_file.open("w") as fh:
            PDBFile.writeFile(modeller.topology, state.getPositions(), fh, keepIds=True)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Serialization failed: {type(exc).__name__}: {exc}"
        )
        return result

    sha256_table: Dict[str, str] = {}
    for xml_path in forcefield_xml:
        candidate = Path(xml_path)
        if candidate.is_file():
            digest = _hash_file(candidate)
            if digest:
                sha256_table[xml_path] = digest

    provenance: Dict[str, Any] = {
        "kind": "openmm_xml",
        "forcefield_xml": list(forcefield_xml),
        "extra_smiles": extra_smiles_pairs,
        "sha256": sha256_table,
        "method": {
            "nonbonded": nonbonded_method,
            "cutoff_nm": nonbonded_cutoff_nm if nonbonded_method != "NoCutoff" else None,
            "constraints": constraints,
            "rigid_water": rigid_water,
            "barostat": None,
            "includes_restraints": False,
        },
        "addExtraParticles": True,
    }
    try:
        import openmm
        provenance["openmm_version"] = openmm.version.full_version
    except Exception:  # noqa: BLE001
        pass
    try:
        from openff.toolkit import __version__ as off_ver
        provenance["openff_toolkit_version"] = off_ver
    except Exception:  # noqa: BLE001
        pass

    num_atoms = modeller.topology.getNumAtoms()
    num_residues = sum(1 for _ in modeller.topology.residues())

    result.update({
        "success": True,
        "system_xml": str(system_xml_file),
        "topology_pdb": str(topology_pdb_file),
        "state_xml": str(state_xml_file),
        "num_atoms": num_atoms,
        "num_residues": num_residues,
        "forcefield_provenance": provenance,
        "code": "openmm_system_built",
    })

    if _node_mode:
        from mdclaw._node import complete_node
        artifacts = {
            "system_xml": f"artifacts/{output_name}.system.xml",
            "topology_pdb": f"artifacts/{output_name}.topology.pdb",
            "state_xml": f"artifacts/{output_name}.state.xml",
        }
        complete_node(
            job_dir, node_id,
            artifacts=artifacts,
            metadata={
                "system_artifact_kind": "openmm_system_xml",
                "forcefield_provenance": provenance,
                "forcefield_xml": list(forcefield_xml),
            },
        )

    logger.info(
        "Built OpenMM System via custom XML: %d atoms, %d residues, bundle=%s",
        num_atoms, num_residues, forcefield_xml,
    )
    return result


# =============================================================================
# Tool registry
# =============================================================================

TOOLS = {
    "build_openmm_system": build_openmm_system,
}
