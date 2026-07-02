"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (``GAFFTemplateGenerator``), and emit a portable
  ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology parameterizes the small molecules
  with ``GAFFTemplateGenerator``.
- Handling glycoproteins by converting deposited glycan residues to
  Amber/GLYCAM notation at topology time, preserving the generated bond plan,
  and completing only GLYCAM-specific hydrogens before System creation.

The XML triple is the only topology contract on the run side; tleap and
parm7/rst7 are not produced or consumed anywhere. AmberTools
(``pdb4amber`` and ``cpptraj``) remain available for structure-preparation
support; ligand parameterization is not a prep-stage mdclaw artifact.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402
from mdclaw._tool_meta import node_tool  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    CANONICAL_WATER_MODELS,
    ensure_directory, create_unique_subdir, generate_job_id,
    BaseToolWrapper, create_file_not_found_error, create_tool_not_available_error,
    create_validation_error,
    create_validation_error_from_guardrails, guardrail_messages,
    is_glycan_residue_name,
    split_guardrail_results,
)
from mdclaw import forcefield_catalog as _ff_catalog  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers.
# ``tleap`` is no longer used: the curated build path runs through
# ``openmmforcefields.SystemGenerator`` and emits the modern
# ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple (PR3 of the
# openmmforcefields-unification refactor). ``cpptraj`` is still used for
# the GLYCAM ``prepareforleap`` glycan conversion stage; see
# ``_prepare_glycam_pdb_with_cpptraj`` for context.
cpptraj_wrapper = BaseToolWrapper("cpptraj")


# =============================================================================
# Force Field Mappings (based on Amber Manual 2024 recommendations)
# =============================================================================

from mdclaw.amber.content_detection import _gemmi_available, _scan_pdb_ion_residue_names, _scan_pdb_text_for_ptm_residues, detect_glycan_content, detect_nucleic_content, detect_water_type  # noqa: E402
from mdclaw.amber.forcefield_constants import CANONICAL_PROTEIN_FORCEFIELDS, GLYCAN_FORCEFIELDS, NUCLEIC_FORCEFIELDS, PHOSAA_LIBRARY_FOR_FF  # noqa: E402
from mdclaw.amber.glycam_topology import _prepare_glycam_pdb_with_cpptraj  # noqa: E402
from mdclaw.amber.ligand_validation import implicit_ligand_diagnostics, validate_initial_ligand_contacts, validate_ligand_chemistry, validate_ligand_template_coverage, validate_metal_params, validate_modxna_params  # noqa: E402
from mdclaw.amber.openmm_build import _record_topology_build_stage, _run_openmmforcefields_build  # noqa: E402
from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds, _plan_glycan_topology_bonds  # noqa: E402
from mdclaw.amber.water_utils import _canonical_forcefield_name, _canonical_water_model_name, _evaluate_forcefield_water_guardrails, fix_histidine_protonation_consistency, fix_ligand_residue_names, strip_crystal_waters  # noqa: E402


def _resolve_build_amber_node_inputs(
    *,
    job_dir: str,
    node_id: str,
    actual_conditions: dict,
    pdb_file: Optional[str],
    ligand_chemistry: Optional[List[Dict[str, Any]]],
    modxna_params: Optional[List[Dict[str, Any]]],
    metal_params: Optional[List[Dict[str, str]]],
    disulfide_bonds: Optional[List[Dict[str, Any]]],
    glycan_metadata: Optional[Dict[str, Any]],
    glycan_linkages: Optional[List[Dict[str, Any]]],
    box_dimensions: Optional[Dict[str, float]],
    is_membrane: Optional[bool],
) -> dict:
    """Validate and merge DAG-resolved inputs for ``build_amber_system``."""
    from mdclaw._node import (
        fail_node_from_result,
        resolve_node_inputs,
        validate_node_execution_context,
    )

    ctx = validate_node_execution_context(
        job_dir,
        node_id,
        "topo",
        actual_conditions=actual_conditions,
    )
    if not ctx["success"]:
        return fail_node_from_result(
            job_dir,
            node_id,
            {"success": False, "error_type": "ValidationError", **ctx},
            default_error="build_amber_system node execution context invalid",
        )

    inputs = resolve_node_inputs(job_dir, node_id, "topo")
    if "input_resolution_error" in inputs:
        return fail_node_from_result(
            job_dir,
            node_id,
            create_validation_error(
                "job_dir/node_id",
                inputs["input_resolution_error"],
                expected="Completed solv/prep ancestor with topology input artifacts",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            ),
            default_error="build_amber_system input resolution blocked",
        )
    return {
        "success": True,
        "pdb_file": pdb_file or inputs.get("pdb_file"),
        "ligand_chemistry": (
            ligand_chemistry
            if ligand_chemistry is not None
            else inputs.get("ligand_chemistry")
        ),
        "modxna_params": modxna_params if modxna_params is not None else inputs.get("modxna_params"),
        "metal_params": metal_params if metal_params is not None else inputs.get("metal_params"),
        "disulfide_bonds": disulfide_bonds if disulfide_bonds is not None else inputs.get("disulfide_bonds"),
        "glycan_metadata": glycan_metadata if glycan_metadata is not None else inputs.get("glycan_metadata"),
        "glycan_linkages": glycan_linkages if glycan_linkages is not None else inputs.get("glycan_linkages"),
        "box_dimensions": box_dimensions if box_dimensions is not None else inputs.get("box_dimensions"),
        "is_membrane": is_membrane if is_membrane is not None else bool(inputs.get("is_membrane")),
        "solvation_water_model": inputs.get("solvation_water_model"),
    }


@node_tool
def build_amber_system(
    pdb_file: Optional[str] = None,
    ligand_chemistry: Optional[List[Dict[str, Any]]] = None,
    modxna_params: Optional[List[Dict[str, Any]]] = None,
    metal_params: Optional[List[Dict[str, str]]] = None,
    disulfide_bonds: Optional[List[Dict[str, Any]]] = None,
    glycan_metadata: Optional[Dict[str, Any]] = None,
    glycan_linkages: Optional[List[Dict[str, Any]]] = None,
    box_dimensions: Optional[Dict[str, float]] = None,
    forcefield: str = "ff19SB",
    water_model: str = "opc",
    nucleic_forcefield: str = "auto",
    glycan_forcefield: str = "auto",
    is_membrane: Optional[bool] = None,
    hmr: bool = True,
    implicit_solvent: Optional[str] = None,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    minimize_max_iterations: int = 10,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Build an OpenMM ``System`` for a prepared PDB via openmmforcefields.

    Internally runs ``openmmforcefields``' ``SystemGenerator`` over an OpenFF
    Pablo-loaded topology, applies the Amber XML bundle resolved through
    ``forcefield_catalog``, parameterizes ligands with
    ``GAFFTemplateGenerator``, optionally bakes in HMR via
    ``hydrogenMass=4 amu``, and serializes the result as the modern
    artifact triple ``system.xml`` + ``topology.pdb`` + ``state.xml``
    (consumed by ``run_minimization`` / ``run_equilibration`` /
    ``run_production`` in node mode).

    The solvent type is determined from ``box_dimensions`` and
    ``implicit_solvent``:
    - ``box_dimensions`` set, ``implicit_solvent`` unset → explicit solvent
      with PBC (PME, default ff19SB + OPC).
    - ``box_dimensions`` unset, ``implicit_solvent`` set → implicit solvent
      (Generalized Born). The matching ``implicit/*.xml`` is loaded by
      ``SystemGenerator`` so the saved ``system.xml`` carries a
      ``CustomGBForce`` / ``GBSAOBCForce``.
    - Both set → ``code="implicit_solvent_explicit_box_conflict"``.
    - Neither set → vacuum NoCutoff System (research only; the run-side
      shim rejects vacuum for default eq/prod workflows).

    Example (explicit solvent, default HMR=True)::

        solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
        amber_result = build_amber_system(
            pdb_file=solvate_result["output_file"],
            box_dimensions=solvate_result["box_dimensions"],
            water_model="opc",
        )

    Args:
        pdb_file: Input PDB. For implicit solvent use an ion-free
                  ``merged.pdb`` from ``merge_structures``; for explicit
                  solvent use ``solvated.pdb`` from ``solvate_structure``.
        ligand_chemistry: List of ligand chemistry dicts from
                       ``prepare_complex``; each should carry ``sdf`` or
                       ``smiles`` plus ``residue_name``. Topology passes the
                       OpenFF ``Molecule`` objects to ``GAFFTemplateGenerator``.
        modxna_params / metal_params: Currently unsupported under the
                       openmmforcefields path; non-empty lists return
                       structured codes ``modxna_openmm_xml_required`` /
                       ``metal_openmm_xml_required``. Supply a
                       pre-converted OpenMM ForceField XML for the
                       residue through ``build_openmm_system`` (research
                       escape hatch) or via the ``extra_xml`` follow-up
                       in the catalog until the ParmEd → OpenMM XML
                       bridge ships.
        box_dimensions: ``{"box_a", "box_b", "box_c"}`` in Å from
                        ``solvate_structure``; ``None`` selects implicit /
                        vacuum.
        forcefield: Protein FF (default: ``"ff19SB"``).
        water_model: Water model for explicit solvent (default: ``"opc"``).
                     OPC is strongly recommended with ff19SB (Amber25 ch.3.6).
        nucleic_forcefield: ``"auto"`` loads DNA OL15 / RNA OL3 when
                            standard nucleic residues are present;
                            ``"none"`` disables it.
        is_membrane: Loads lipid21 when ``True``; resolved from DAG
                     metadata in node mode.
        hmr: When ``True`` (default), bakes ``hydrogenMass=4 amu`` into
             ``system.xml`` so min/eq/prod can validate and run with the
             standard timestep settings. Defaults match ``run_minimization`` /
             ``run_equilibration`` / ``run_production`` so the standard
             default workflow (build → min → eq → prod, no kwargs) succeeds.
        implicit_solvent: GB model name (case-insensitive). Supported:
                          ``"HCT"``, ``"OBC1"``, ``"OBC2"``, ``"GBn"``,
                          ``"GBn2"``. When set, the matching
                          ``implicit/*.xml`` from openmmforcefields is
                          added to the SystemGenerator bundle and the
                          resulting ``system.xml`` carries a
                          Generalized-Born force. Cannot be combined with
                          ``box_dimensions`` (returns code
                          ``implicit_solvent_explicit_box_conflict``).
                          ``forcefield="ff14SB"`` is auto-substituted to
                          ``"ff14SBonlysc"`` (the GBneck2-tuned variant)
                          when ``implicit_solvent`` is set.
        output_name: Stem for the artifact filenames; emits
                     ``{output_name}.system.xml``,
                     ``{output_name}.topology.pdb``,
                     ``{output_name}.state.xml``, and
                     ``{output_name}.minimization_report.json``.
        minimize_max_iterations: L-BFGS iteration cap for the build-time
                     energy minimization (default 10; 0 = run to
                     convergence). This is intentionally a short fail-fast
                     pass: it only confirms the built System minimizes with
                     finite forces and settles the worst solvation close
                     contacts. Full relaxation of the solvated system is the
                     downstream ``min`` node's job, so the build-time
                     artifact may still sit at a mildly positive potential
                     energy. Raise this only if you want the topo artifact
                     itself pre-relaxed.
        output_dir / job_dir / node_id: Standard mdclaw I/O knobs. In
                     node mode, the topo node's metadata is stamped with
                     ``system_artifact_kind="openmm_system_xml"`` and a
                     ``forcefield_provenance`` dict (``method.hmr``,
                     ``openmm_xml`` bundle, sha256 table, OpenMM /
                     openmmforcefields versions).

    Returns:
        Dict with:
            - ``success``: bool — True when the System built and
              serialized cleanly.
            - ``job_id``, ``output_dir``: bookkeeping.
            - ``system_xml``, ``topology_pdb``, ``state_xml``: absolute
              paths to the modern artifact triple.
            - ``minimization_report``: absolute path to the topology-time
              minimization evidence JSON.
            - ``solvent_type``: ``"explicit"``, ``"implicit"``, or
              ``"vacuum"``.
            - ``parameters``: copy of the input parameter selection.
            - ``forcefield_provenance``: dict capturing the resolved
              OpenMM XML bundle, topology-time ligand template sources,
              ``method.hmr``, versions of OpenMM / openmmforcefields /
              openff-toolkit.
            - ``statistics``: ``{"num_atoms", "num_residues"}``.
            - ``code``: structured failure code on failure (e.g.
              ``metal_openmm_xml_required``,
              ``implicit_solvent_explicit_box_conflict``,
              ``implicit_solvent_model_unsupported``,
              ``implicit_solvent_force_missing``).
            - ``errors`` / ``warnings``: lists of strings.
    
    Example (explicit solvent, ligand, default HMR=True):
        >>> solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
        >>> result = build_amber_system(
        ...     pdb_file=solvate_result["output_file"],
        ...     ligand_chemistry=[{
        ...         "sdf": "output/job1/ligand.sdf",
        ...         "residue_name": "LIG",
        ...     }],
        ...     box_dimensions=solvate_result["box_dimensions"],
        ...     water_model="opc",
        ... )
        >>> result["system_xml"], result["topology_pdb"], result["state_xml"]

    Example (vacuum, no implicit solvent — research only):
        >>> result = build_amber_system(
        ...     pdb_file="output/job1/merged.pdb",
        ...     # no box_dimensions and no implicit_solvent — produces a
        ...     # vacuum NoCutoff System; eq/prod will reject it because
        ...     # vacuum is not a recommended ensemble for default workflows.
        ... )
    """
    solvation_water_model = None
    # Auto-resolve input from DAG when in node mode and pdb_file not provided
    if job_dir and node_id:
        _resolved = _resolve_build_amber_node_inputs(
            job_dir=job_dir,
            node_id=node_id,
            actual_conditions={
                "forcefield": forcefield,
                "water_model": water_model,
                "nucleic_forcefield": nucleic_forcefield,
                "glycan_forcefield": glycan_forcefield,
                "is_membrane": is_membrane,
                "hmr": hmr,
                "implicit_solvent": implicit_solvent,
                "output_name": output_name,
            },
            pdb_file=pdb_file,
            ligand_chemistry=ligand_chemistry,
            modxna_params=modxna_params,
            metal_params=metal_params,
            disulfide_bonds=disulfide_bonds,
            glycan_metadata=glycan_metadata,
            glycan_linkages=glycan_linkages,
            box_dimensions=box_dimensions,
            is_membrane=is_membrane,
        )
        if not _resolved["success"]:
            return _resolved
        pdb_file = _resolved["pdb_file"]
        ligand_chemistry = _resolved["ligand_chemistry"]
        modxna_params = _resolved["modxna_params"]
        metal_params = _resolved["metal_params"]
        disulfide_bonds = _resolved["disulfide_bonds"]
        glycan_metadata = _resolved["glycan_metadata"]
        glycan_linkages = _resolved["glycan_linkages"]
        box_dimensions = _resolved["box_dimensions"]
        is_membrane = _resolved["is_membrane"]
        solvation_water_model = _resolved["solvation_water_model"]

    if is_membrane is None:
        is_membrane = False

    if not pdb_file:
        blocked = create_validation_error(
            "pdb_file",
            "pdb_file is required",
            expected="Explicit PDB path, or --job-dir/--node-id for DAG auto-resolve",
            actual=pdb_file,
            hints=["Run solvate_structure first for explicit solvent, or prepare_complex for implicit topology."],
            code="missing_pdb_file",
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system missing pdb_file",
            )
        return blocked

    logger.info(f"Building Amber system from: {pdb_file}")
    pdb_path = Path(pdb_file)

    # Auto-detect ligand_chemistry.json if not provided. This is the standard
    # prepare_complex -> build_amber_system handoff: prep records chemistry,
    # topology parameterizes ligands with GAFFTemplateGenerator.
    if ligand_chemistry is None:
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            lig_json = search_dir / "ligand_chemistry.json"
            if lig_json.exists():
                try:
                    ligand_chemistry = json.loads(lig_json.read_text())
                    logger.info(
                        f"Auto-loaded ligand_chemistry "
                        f"({len(ligand_chemistry)} ligands) from {lig_json}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    blocked = create_validation_error(
                        "ligand_chemistry",
                        f"Found {lig_json} but could not read it: {e}",
                        expected="valid ligand_chemistry.json from prepare_complex",
                        actual=str(lig_json),
                        hints=["Re-run prepare_complex to refresh ligand chemistry artifacts."],
                        code="ligand_chemistry_load_failed",
                    )
                    if job_dir and node_id:
                        from mdclaw._node import fail_node_from_result
                        return fail_node_from_result(
                            job_dir,
                            node_id,
                            blocked,
                            default_error="build_amber_system ligand_chemistry load failed",
                        )
                    return blocked
                break

    # Auto-detect disulfide_bonds.json if not provided (written by prepare_complex
    # as a prep-node artifact; same parent-directory search as ligand chemistry).
    if disulfide_bonds is None:
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            ss_json = search_dir / "disulfide_bonds.json"
            if ss_json.exists():
                try:
                    disulfide_bonds = json.loads(ss_json.read_text())
                    logger.info(
                        f"Auto-loaded disulfide_bonds ({len(disulfide_bonds)} pairs) from {ss_json}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {ss_json} but could not read: {e}")
                break

    # Auto-detect glycan prep artifacts if not provided.
    if glycan_metadata is None:
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            gly_json = search_dir / "glycan_metadata.json"
            if gly_json.exists():
                try:
                    glycan_metadata = json.loads(gly_json.read_text())
                    logger.info(f"Auto-loaded glycan_metadata from {gly_json}")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {gly_json} but could not read: {e}")
                break
    if glycan_linkages is None:
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            gly_link_json = search_dir / "glycan_linkages.json"
            if gly_link_json.exists():
                try:
                    glycan_linkages = json.loads(gly_link_json.read_text())
                    logger.info(
                        f"Auto-loaded glycan_linkages ({len(glycan_linkages)} linkages) from {gly_link_json}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {gly_link_json} but could not read: {e}")
                break

    # Auto-detect box_dimensions.json if not provided
    if box_dimensions is None:
        box_json = pdb_path.parent / "box_dimensions.json"
        if box_json.exists():
            try:
                box_dimensions = json.loads(box_json.read_text())
                logger.info(f"Auto-loaded box_dimensions from {box_json}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Found {box_json} but could not read: {e}")

    # Validate box_dimensions: empty dict {} should be treated as None
    # This prevents the bug where solvent_type="explicit" but no PBC is set
    box_dim_warning = None
    original_box_dim = box_dimensions  # Store original for warning
    explicit_requested = False
    if job_dir:
        try:
            progress_path = Path(job_dir) / "progress.json"
            progress = json.loads(progress_path.read_text())
            explicit_requested = progress.get("params", {}).get("solvation_type") == "explicit"
        except (json.JSONDecodeError, OSError):
            explicit_requested = False
    if box_dimensions is not None:
        if not isinstance(box_dimensions, dict) or not box_dimensions:
            box_dim_warning = f"CRITICAL: box_dimensions was invalid (empty or not dict): {original_box_dim}. Building non-periodic system. If you wanted explicit solvent, ensure solvate step returned box_dimensions and it was passed correctly."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(key in box_dimensions for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions missing required keys (box_a/b/c): {original_box_dim}. Building non-periodic system."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(box_dimensions.get(key, 0) > 0 for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions has zero or negative values: {original_box_dim}. Building non-periodic system."
            logger.warning(box_dim_warning)
            box_dimensions = None
    if explicit_requested and box_dimensions is None:
        blocked = {
            "success": False,
            "error_type": "ValidationError",
            "code": "explicit_solvent_box_dimensions_missing",
            "message": (
                "This job is marked as explicit solvent but build_amber_system "
                "has no valid box_dimensions. Re-run solvate_structure or fix "
                "the solv node artifact before building topology."
            ),
            "errors": [
                box_dim_warning or "Explicit solvent topology requires valid box_dimensions"
            ],
            "warnings": [box_dim_warning] if box_dim_warning else [],
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
        return blocked

    # --- Implicit-solvent guardrails ------------------------------------
    # Mutual exclusion with an explicit periodic box (these come from
    # different solvation paths and must not be combined).
    if implicit_solvent is not None and box_dimensions is not None:
        blocked = {
            "success": False,
            "error_type": "ValidationError",
            "code": "implicit_solvent_explicit_box_conflict",
            "message": (
                f"implicit_solvent={implicit_solvent!r} cannot be combined "
                f"with explicit box_dimensions. Drop one: implicit GB systems "
                f"are non-periodic, explicit-solvent systems do not need a "
                f"GB model."
            ),
            "errors": [
                "implicit_solvent and box_dimensions are mutually exclusive."
            ],
            "warnings": [],
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked["errors"])
        return blocked

    # Normalize the GB model name against the catalog. Unknown / typo'd
    # names fail-fast with a structured code so callers can surface a
    # clean recommendation.
    canonical_implicit_solvent: Optional[str] = None
    if implicit_solvent is not None:
        canonical_implicit_solvent = _ff_catalog.normalize_implicit_solvent(
            implicit_solvent
        )
        if canonical_implicit_solvent not in _ff_catalog.IMPLICIT_SOLVENT_XML:
            supported = ", ".join(_ff_catalog.supported_implicit_solvent_models())
            blocked = {
                "success": False,
                "error_type": "ValidationError",
                "code": "implicit_solvent_model_unsupported",
                "message": (
                    f"Unknown implicit-solvent model "
                    f"{implicit_solvent!r}. Supported: {supported}."
                ),
                "errors": [
                    f"implicit_solvent={implicit_solvent!r} is not in the catalog."
                ],
                "warnings": [],
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"])
            return blocked

    # Initialize result structure.
    # The curated build path emits the modern XML triple. Callers should
    # consume ``system_xml``, ``topology_pdb``, and ``state_xml`` (set by
    # ``_run_openmmforcefields_build`` on success). The XML triple is the
    # only topology contract; downstream code (DAG resolver, min/eq/prod)
    # never reads anything else.
    job_id = generate_job_id()
    solvent_type = (
        "explicit"
        if box_dimensions is not None
        else ("implicit" if canonical_implicit_solvent else "vacuum")
    )
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "solvent_type": solvent_type,
        "parameters": {
            "forcefield": forcefield,
            "nucleic_forcefield": nucleic_forcefield,
            "glycan_forcefield": glycan_forcefield,
            "water_model": water_model if solvent_type == "explicit" else None,
            "water_model_status": (
                "used_for_explicit_solvent"
                if solvent_type == "explicit"
                else f"not_used_for_{solvent_type}_solvent"
            ),
            "box_dimensions": box_dimensions,
            "is_membrane": is_membrane if box_dimensions else False,
            "ligand_count": len(ligand_chemistry) if ligand_chemistry else 0,
            "modxna_param_count": len(modxna_params) if modxna_params else 0,
            "glycan_count": len((glycan_metadata or {}).get("glycans", [])) if isinstance(glycan_metadata, dict) else 0,
            "glycan_linkage_count": len(glycan_linkages) if glycan_linkages else 0,
            "metal_count": len(metal_params) if metal_params else 0
        },
        "statistics": {},
        "errors": [],
        "warnings": [],
        "topology_notes": [],
        "pdb_info_added": False,
        "pdb_flags_added": [],
    }

    # Add box_dimensions validation warning to result
    if box_dim_warning:
        result["warnings"].append(box_dim_warning)

    # Validate force field
    canonical_forcefield = _canonical_forcefield_name(forcefield)
    if not canonical_forcefield:
        logger.error(f"Unknown force field: {forcefield}")
        blocked = {
            **result,
            **create_validation_error(
                "forcefield",
                f"Unknown force field: {forcefield}",
                expected=f"One of: {sorted(CANONICAL_PROTEIN_FORCEFIELDS.values())}",
                actual=forcefield,
                warnings=result["warnings"],
                code="unknown_forcefield",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system unknown forcefield",
            )
        return blocked
    forcefield = canonical_forcefield
    result["parameters"]["forcefield"] = forcefield

    # Normalize water model up front, even for implicit solvent, so typos never pass silently.
    canonical_water_model = _canonical_water_model_name(water_model)
    if not canonical_water_model:
        logger.error(f"Unknown water model: {water_model}")
        blocked = {
            **result,
            **create_validation_error(
                "water_model",
                f"Unknown water model: {water_model}",
                expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
                actual=water_model,
                warnings=result["warnings"],
                code="unknown_water_model",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system unknown water_model",
            )
        return blocked
    water_model = canonical_water_model
    result["parameters"]["water_model"] = (
        water_model if solvent_type == "explicit" else None
    )
    if solvent_type == "implicit":
        result["parameters"]["validated_water_model"] = water_model

    retained_ion_residue_names = _scan_pdb_ion_residue_names(pdb_path)
    if retained_ion_residue_names:
        result["parameters"]["retained_ion_residue_names"] = retained_ion_residue_names
        if solvent_type == "vacuum":
            result["parameters"]["ion_parameter_water_model"] = water_model
            result["parameters"]["water_model_status"] = (
                "used_for_vacuum_ion_templates"
            )
    if retained_ion_residue_names and solvent_type == "implicit":
        blocked = {
            **result,
            "code": "explicit_ions_in_implicit_solvent",
            "error_type": "ValidationError",
            "message": (
                "The input PDB contains explicit ion residue(s) "
                f"{retained_ion_residue_names}, but solvent_type={solvent_type!r} "
                "uses a continuum solvent model. Exclude explicit ion particles "
                "before building an implicit topology, or use explicit solvent "
                "or a deliberately vacuum/no-solvent topology instead."
            ),
        }
        blocked["errors"].append(blocked["message"])
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system explicit ions in implicit solvent",
            )
        return blocked

    if solvation_water_model and solvation_water_model != water_model:
        blocked = {
            **result,
            **create_validation_error(
                "water_model",
                "Topology water_model does not match the solv node water_model",
                expected=solvation_water_model,
                actual=water_model,
                warnings=result["warnings"],
            ),
            "code": "solvation_topology_water_model_mismatch",
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked.get("errors", [blocked.get("message", "")]))
        return blocked

    # Validate explicit-solvent compatibility before any filesystem or external-tool checks.
    if box_dimensions:
        compatibility_results = _evaluate_forcefield_water_guardrails(forcefield, water_model)
        blocking_results, warning_results = split_guardrail_results(compatibility_results)
        if blocking_results:
            blocked = {
                **result,
                **create_validation_error_from_guardrails(
                    "water_model",
                    compatibility_results,
                    summary=compatibility_results[0]["message"],
                    expected="ff19SB + opc (recommended) or ff14SB + tip3p (legacy)",
                    actual=f"{forcefield} + {water_model}",
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system water model blocked",
                )
            return blocked
        result["warnings"].extend(guardrail_messages(warning_results))

    # Validate input PDB file and detect standard nucleic content after
    # parameter guardrails, preserving existing error precedence.
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        logger.error(f"Input PDB file not found: {pdb_file}")
        blocked = create_file_not_found_error(str(pdb_file), "Input PDB file")
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system input PDB file not found",
            )
        return blocked

    nucleic_content = detect_nucleic_content(pdb_path)
    result["nucleic_content"] = nucleic_content
    result["parameters"]["nucleic_subtypes"] = nucleic_content["subtypes"]
    result["parameters"]["nucleic_residue_names"] = nucleic_content["standard_residue_names"]
    glycan_content = detect_glycan_content(pdb_path)
    result["glycan_content"] = glycan_content
    result["parameters"]["glycan_residue_names"] = glycan_content["residue_names"]

    valid_modxna_params = []
    if modxna_params:
        valid_modxna_params, modxna_errors = validate_modxna_params(modxna_params, pdb_path)
        if modxna_errors:
            result["errors"].extend(modxna_errors)
            blocked = {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_modxna_parameters",
                "message": (
                    "Invalid modXNA parameter records; refusing to run "
                    "openmmforcefields build."
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
            return blocked
    modxna_residue_names = {p["residue_name"] for p in valid_modxna_params}
    result["parameters"]["modxna_params"] = valid_modxna_params
    result["parameters"]["modxna_validation"] = [
        p.get("validation", {}) for p in valid_modxna_params
    ]
    for validation in result["parameters"]["modxna_validation"]:
        for warning in validation.get("warnings", []):
            result["warnings"].append(f"modXNA {validation.get('label')}: {warning}")

    unsupported_modified = [
        r for r in nucleic_content["unsupported_modified_residues"]
        if r.get("resname") not in modxna_residue_names
    ]
    if unsupported_modified:
        err = create_validation_error(
            "pdb_file",
            "Unsupported modified nucleic residue(s) detected. Standard DNA/RNA support "
            "does not parameterize modified nucleotides; use modXNA parameters in a "
            "follow-up workflow.",
            expected="Standard DNA/RNA residues only",
            actual=unsupported_modified,
            warnings=result["warnings"],
        )
        err["code"] = "unsupported_modified_nucleic_residue"
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=err.get("errors", []))
        return {**result, **err}

    # Check that the openmmforcefields stack is available — replaces the
    # legacy tleap availability check. (PR3 of openmmforcefields-unification.)
    try:
        import openmmforcefields  # noqa: F401
    except ImportError:
        logger.error("openmmforcefields not available")
        blocked = create_tool_not_available_error(
            "openmmforcefields",
            "Run `conda env update -f environment.yml` to install the openmmforcefields-unification deps"
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system dependency missing",
            )
        return blocked

    # Validate water model (for explicit solvent)
    actual_water_model = water_model  # May be overridden by detection
    if box_dimensions:
        # Detect water type in input PDB to prevent mismatch
        # (e.g., packmol-memgen produces TIP3P, but user requests OPC)
        detected = detect_water_type(pdb_path)
        if detected["has_waters"]:
            detected_type = detected["detected_type"]
            requested_type = water_model.lower()

            # Map water models to their atom counts
            # TIP3P: 3 atoms (O, H1, H2)
            # OPC: 4 atoms (O, H1, H2, EPW)
            # TIP4P: 4 atoms (O, H1, H2, M)
            three_site = {"tip3p", "spc", "spce"}
            four_site = {"opc", "opc3", "tip4p", "tip4pew"}

            # Check for mismatch — under the openmmforcefields path,
            # ``Modeller.addExtraParticles`` will add virtual sites (EPW, etc.)
            # for 4-site waters, so a 3-site → 4-site request is fine.
            if detected_type == "tip3p" and requested_type in four_site:
                logger.info(
                    f"Input PDB has TIP3P-format waters ({detected['atoms_per_water']:.1f} atoms/water). "
                    f"Modeller.addExtraParticles will add missing atoms for '{water_model}' (e.g., EPW for OPC)."
                )
                result["warnings"].append(
                    f"Note: Input has 3-atom waters; addExtraParticles will inject virtual sites for {water_model}."
                )
            elif detected_type in ["opc", "tip4p"] and requested_type in three_site:
                logger.warning(
                    f"Water model mismatch! Input has 4-site waters but '{water_model}' requested. "
                    f"Using detected type '{detected_type}'."
                )
                result["warnings"].append(
                    f"Auto-corrected water model: Input has 4-site waters but '{water_model}' requested."
                )
                actual_water_model = detected_type

        if not _ff_catalog.normalize_water(actual_water_model):
            logger.error(f"Unknown water model: {actual_water_model}")
            blocked = create_validation_error(
                "water_model",
                f"Unknown water model: {actual_water_model}",
                expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
                actual=actual_water_model,
            )
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system unknown detected water_model",
                )
            return blocked

        # Update metadata with actual water model (may differ from requested)
        result["parameters"]["water_model"] = actual_water_model
        if actual_water_model != water_model:
            result["parameters"]["requested_water_model"] = water_model
    else:
        result["parameters"]["water_model"] = None
        if solvent_type == "vacuum" and retained_ion_residue_names:
            result["parameters"]["ion_parameter_water_model"] = actual_water_model
            result["parameters"]["water_model_status"] = (
                "used_for_vacuum_ion_templates"
            )

    nucleic_mode = (nucleic_forcefield or "auto").lower()
    nucleic_libraries = []
    if nucleic_mode in {"none", "off", "false", "no"}:
        nucleic_libraries = []
    elif nucleic_mode == "auto":
        if "dna" in nucleic_content["subtypes"]:
            nucleic_libraries.append(NUCLEIC_FORCEFIELDS["dna"])
        if "rna" in nucleic_content["subtypes"]:
            nucleic_libraries.append(NUCLEIC_FORCEFIELDS["rna"])
    elif nucleic_mode in {"dna", "rna"}:
        nucleic_libraries.append(NUCLEIC_FORCEFIELDS[nucleic_mode])
    elif nucleic_mode in {"both", "dna,rna", "rna,dna"}:
        nucleic_libraries.extend([NUCLEIC_FORCEFIELDS["dna"], NUCLEIC_FORCEFIELDS["rna"]])
    else:
        return {
            **result,
            **create_validation_error(
                "nucleic_forcefield",
                f"Unknown nucleic_forcefield: {nucleic_forcefield}",
                expected="'auto', 'none', 'dna', 'rna', or 'both'",
                actual=nucleic_forcefield,
                warnings=result["warnings"],
            ),
        }
    result["parameters"]["nucleic_libraries"] = nucleic_libraries

    glycan_library = None
    glycan_mode = (glycan_forcefield or "auto").lower()
    if glycan_mode in {"none", "off", "false", "no"}:
        glycan_library = None
    elif glycan_mode == "auto":
        glycan_library = GLYCAN_FORCEFIELDS["auto"] if glycan_content["has_glycan"] else None
    elif glycan_mode in GLYCAN_FORCEFIELDS:
        glycan_library = GLYCAN_FORCEFIELDS[glycan_mode]
    else:
        return {
            **result,
            **create_validation_error(
                "glycan_forcefield",
                f"Unknown glycan_forcefield: {glycan_forcefield}",
                expected="'auto', 'none', or 'glycam06j-1'",
                actual=glycan_forcefield,
                warnings=result["warnings"],
            ),
        }
    if glycan_content["has_glycan"] and not glycan_library:
        return {
            **result,
            **create_validation_error(
                "glycan_forcefield",
                "Glycan residues are present, but glycan force-field loading is disabled.",
                expected="'auto' or a GLYCAM force field",
                actual=glycan_forcefield,
                warnings=result["warnings"],
            ),
            "code": "glycan_forcefield_disabled",
        }
    if glycan_metadata and isinstance(glycan_metadata, dict):
        metadata_residues = {
            str(r.get("source_resname") or r.get("resname") or "").upper()
            for r in glycan_metadata.get("residue_mapping", [])
        }
        unsupported_glycans = sorted(
            name for name in metadata_residues
            if name and not is_glycan_residue_name(name)
        )
        if unsupported_glycans:
            blocked = {
                **result,
                **create_validation_error(
                    "glycan_metadata",
                    "Unsupported glycan residue(s) detected; refusing to treat them as GAFF ligands.",
                    expected="Known PDB glycan or GLYCAM residue names",
                    actual=unsupported_glycans,
                    warnings=result["warnings"],
                ),
                "code": "unsupported_glycan_residue",
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system unsupported glycan residue",
                )
            return blocked
    result["parameters"]["glycan_library"] = glycan_library

    # Validate ligand chemistry. Ligand force-field resolution is intentionally
    # topology-time only: prep records SDF/SMILES/charge provenance, and this
    # build parameterizes ligands with GAFFTemplateGenerator.
    valid_ligands = []
    if ligand_chemistry:
        valid_ligands, ligand_errors = validate_ligand_chemistry(ligand_chemistry)
        if ligand_errors:
            result["errors"].extend(ligand_errors)
            logger.error(f"Ligand chemistry validation failed: {ligand_errors}")
            blocked = {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_ligand_chemistry",
                "message": (
                    "Invalid ligand chemistry records; refusing to run "
                    "openmmforcefields build."
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system invalid ligand chemistry",
                )
            return blocked
    
    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node, fail_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "topology")
    result["output_dir"] = str(out_dir)

    def _fail_running_topo(blocked: dict) -> dict:
        if _node_mode:
            fail_node(
                job_dir,
                node_id,
                errors=blocked.get("errors", []),
                warnings=blocked.get("warnings", []),
            )
        return blocked
    
    # Output files. ``build_amber_system`` emits the XML triple consumed
    # by run_minimization / run_equilibration / run_production through
    # the DAG resolver.
    system_xml_file = out_dir / f"{output_name}.system.xml"
    topology_pdb_file = out_dir / f"{output_name}.topology.pdb"
    state_xml_file = out_dir / f"{output_name}.state.xml"
    minimization_report_file = out_dir / f"{output_name}.minimization_report.json"
    topology_validation_file = out_dir / f"{output_name}.topology_validation.json"
    
    # Copy and fix PDB file (fix UNL residue names if needed)
    working_pdb = out_dir / f"{output_name}.prepared.pdb"
    ligand_res_names = [lig["residue_name"] for lig in valid_ligands] if valid_ligands else []

    # Fix ligand residue names (UNL -> correct name)
    # Note: N-terminal hydrogen naming is handled by pdb4amber --reduce in structure_server.py
    fix_lig_result = fix_ligand_residue_names(pdb_path, working_pdb, ligand_res_names)
    if not fix_lig_result.get("success", True):
        result["errors"].extend(fix_lig_result.get("errors", []))
        logger.error(f"Ligand residue-name repair failed: {fix_lig_result.get('errors', [])}")
        return _fail_running_topo({
            **result,
            "error_type": "ValidationError",
            "code": "ambiguous_ligand_residue_repair",
            "message": (
                "Ambiguous ligand residue-name repair before openmmforcefields build."
            ),
        })
    if fix_lig_result["unl_count"] > 0:
        result["warnings"].extend(fix_lig_result["replacements"])

    # Fix histidine residue name consistency (HID/HIE/HIP vs HD1/HE2)
    disulfide_plan_warnings: list[str] = []

    try:
        his_fix = fix_histidine_protonation_consistency(working_pdb, working_pdb)
        if his_fix.get("changed", 0) > 0:
            # Keep concise: only show first few changes
            preview = his_fix.get("changes", [])[:5]
            result["warnings"].append(
                f"Histidine residue name fix applied ({his_fix['changed']} atoms updated): {preview}"
            )
            logger.info(f"Applied histidine residue name fix: {his_fix['changed']} atom lines updated")
    except Exception as e:
        result["warnings"].append(f"Histidine residue name fix failed (continuing): {type(e).__name__}: {e}")
    
    # Use the residue-name-repaired PDB as the input to the
    # openmmforcefields build path below.
    pdb_path = working_pdb

    valid_metal_params = []
    if metal_params:
        valid_metal_params, metal_errors = validate_metal_params(metal_params, pdb_path)
        if metal_errors:
            result["errors"].extend(metal_errors)
            logger.error(f"Metal parameter validation failed: {metal_errors}")
            blocked = {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_metal_parameters",
                "message": (
                    "Invalid metal parameter records; refusing to run "
                    "openmmforcefields build."
                ),
            }
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
            return blocked
    result["parameters"]["metal_params"] = valid_metal_params

    ligand_coverage_errors = validate_ligand_template_coverage(pdb_path, valid_ligands)
    if ligand_coverage_errors:
        result["errors"].extend(ligand_coverage_errors)
        logger.error(f"Ligand template coverage failed: {ligand_coverage_errors}")
        return _fail_running_topo({
            **result,
            "error_type": "ValidationError",
            "code": "ligand_template_coverage_failed",
            "message": "Ligand parameter residue names do not match the topology input PDB.",
        })

    if valid_ligands:
        ligand_contact_diagnostics = validate_initial_ligand_contacts(
            str(pdb_path),
            [lig["residue_name"] for lig in valid_ligands],
        )
        result["ligand_contact_diagnostics"] = ligand_contact_diagnostics
        if ligand_contact_diagnostics.get("ligand_clash_detected"):
            result["warnings"].append(
                "Ligand-protein close contact detected; standard staged equilibration "
                "will still be used. See ligand_contact_diagnostics."
            )
        if box_dimensions is None:
            result["implicit_ligand_diagnostics"] = implicit_ligand_diagnostics(valid_ligands)
    
    # PTM detection: scan the input PDB for SEP/TPO/PTR. If present, ask
    # ``forcefield_catalog`` to add the matching ``amber/phosaa*.xml``
    # bundle (e.g. ``amber/phosaa19SB.xml`` for ff19SB) on top of the
    # protein force field so the SystemGenerator can apply the phospho-
    # residue templates against the OG / OG1 / OH oxygen retained by
    # ``phosphorylate_residues``.
    from mdclaw.research_server import detect_ptm_sites
    if _gemmi_available():
        ptm_residues_in_input = detect_ptm_sites(str(pdb_path))
    else:
        ptm_residues_in_input = _scan_pdb_text_for_ptm_residues(pdb_path)
        if ptm_residues_in_input:
            err = create_validation_error(
                "gemmi",
                "gemmi is required to validate phosphorylated residues before "
                "building a topology.",
                expected="gemmi import succeeds when SEP/TPO/PTR residues are present",
                actual=f"gemmi unavailable; PTM residues={ptm_residues_in_input}",
                warnings=result["warnings"],
                code="phospho_detection_requires_gemmi",
            )
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=err.get("errors", []))
            return {**result, **err}
        result["warnings"].append(
            "gemmi is not installed; phosphorylated-residue detection was "
            "limited to PDB text residue-name scanning."
        )
    phosaa_library = None
    if ptm_residues_in_input:
        phosaa_library = PHOSAA_LIBRARY_FOR_FF.get(forcefield)
        if phosaa_library is None:
            err = create_validation_error(
                "forcefield",
                f"Forcefield '{forcefield}' has no matching openmmforcefields "
                f"phosaa XML (e.g. ``amber/phosaa19SB.xml``), but the input "
                f"PDB contains PTM residues "
                f"({sorted({s['name'] for s in ptm_residues_in_input})}).",
                expected="ff19SB or ff14SB (which pair with phosaa19SB / phosaa14SB)",
                actual=forcefield,
                warnings=result["warnings"],
                code="phospho_forcefield_unsupported",
            )
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=err.get("errors", []))
            return {**result, **err}
        # openmmforcefields 0.16.0 ships ``amber/protein.ff14SB.xml`` with
        # prefixed atom types (``protein-N``…) but ``amber/phosaa14SB.xml``
        # with unprefixed types — loading both raises ``KeyError: 'N'``
        # inside ``app.ForceField.loadFile``. Surface a structured fail-fast
        # so callers get an actionable suggestion (switch to ff19SB +
        # phosaa19SB) instead of the cryptic upstream KeyError.
        _PHOSAA_TYPE_PREFIX_BROKEN = {
            ("ff14SB", "phosaa14SB"),
            ("ff14SBonlysc", "phosaa14SB"),
        }
        if (forcefield, phosaa_library.split(".")[-1]) in _PHOSAA_TYPE_PREFIX_BROKEN:
            err = create_validation_error(
                "forcefield",
                f"Forcefield '{forcefield}' uses the openmmforcefields "
                f"prefixed-atom-type protein XML (``protein-N``…), but "
                f"``amber/{phosaa_library.split('.')[-1]}.xml`` ships with "
                f"unprefixed types — pairing them raises KeyError 'N' inside "
                f"``app.ForceField`` (atom-type asymmetry not yet fixed "
                f"upstream). PTM residues detected in input: "
                f"{sorted({s['name'] for s in ptm_residues_in_input})}.",
                expected="ff19SB (pairs with phosaa19SB; OPC water recommended)",
                actual=forcefield,
                warnings=result["warnings"],
                code="phospho_forcefield_atom_type_mismatch",
            )
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=err.get("errors", []))
            return {**result, **err}
        result["parameters"]["phosaa_library"] = phosaa_library
        result["parameters"]["ptm_residues"] = ptm_residues_in_input

    glycam_prepare = None
    if glycan_content["has_glycan"] and glycan_library:
        glycam_prepare = _prepare_glycam_pdb_with_cpptraj(
            pdb_path=pdb_path,
            out_dir=out_dir,
            output_name=output_name,
            glycan_linkages=glycan_linkages,
        )
        if not glycam_prepare["success"]:
            result["errors"].extend(glycam_prepare.get("errors", []))
            result["warnings"].extend(glycam_prepare.get("warnings", []))
            code = glycam_prepare.get("code") or "glycam_prepareforleap_failed"
            message = (
                "Could not map deposited protein-glycan linkage endpoints onto the topology input PDB."
                if code == "glycan_linkage_mapping_failed"
                else "cpptraj prepareforleap failed while converting PDB glycans to GLYCAM notation."
            )
            blocked = {
                **result,
                "error_type": "ToolExecutionError",
                "code": code,
                "message": message,
                "glycam_prepareforleap": glycam_prepare,
            }
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
            return blocked
        result["glycam_prepareforleap"] = glycam_prepare
        result["parameters"]["glycam_prepareforleap"] = {
            "prepared_pdb": glycam_prepare["prepared_pdb"],
            "leap_script": glycam_prepare["leap_script"],
            "glycam_bond_plan_file": glycam_prepare.get("glycam_bond_plan_file"),
        }
        if glycam_prepare.get("glycam_bond_plan"):
            result["glycam_bond_plan"] = glycam_prepare["glycam_bond_plan"]
        pdb_path = Path(glycam_prepare["prepared_pdb"]).resolve()

    try:
        # Implicit-solvent crystal-water cleanup (preserved from the legacy
        # path): GB models cannot accept discrete water molecules, so strip
        # any waters that survived the prep stage.
        if not box_dimensions:
            detected_water = detect_water_type(pdb_path)
            if detected_water["has_waters"]:
                logger.info(
                    f"Removing {detected_water['water_count']} crystal waters for implicit solvent system"
                )
                strip_result = strip_crystal_waters(pdb_path, pdb_path)
                if strip_result["success"] and strip_result["waters_removed"] > 0:
                    result["warnings"].append(
                        f"Removed {strip_result['waters_removed']} crystal water(s) for implicit solvent. "
                        f"GB models don't support discrete water molecules."
                    )

        # Disulfide and glycan-linkage planning resolves residue-pair
        # provenance for the openmmforcefields build; the actual SG-SG /
        # glycan bonds are added to the OpenMM topology inside
        # ``_run_openmmforcefields_build``. The resolved-plan shape is
        # stable agent-facing metadata, so the result keys
        # (``disulfide_bond_plan``, ``glycan_linkage_plan``) and the
        # per-record ``topology_residues`` field are part of the public
        # node metadata contract.
        if disulfide_bonds:
            ss_plan = _plan_disulfide_topology_bonds(Path(pdb_path), disulfide_bonds)
            if ss_plan["warnings"]:
                disulfide_plan_warnings.extend(ss_plan["warnings"])
            result["disulfide_bond_plan"] = ss_plan["resolved"]

        if glycan_linkages and not glycam_prepare:
            glycan_plan = _plan_glycan_topology_bonds(Path(pdb_path), glycan_linkages)
            if glycan_plan["warnings"]:
                result["warnings"].extend(glycan_plan["warnings"])
            result["glycan_linkage_plan"] = glycan_plan["resolved"]
        elif glycan_linkages and glycam_prepare:
            result["glycan_linkage_plan"] = [
                {**linkage, "status": "handled_by_prepareforleap"}
                for linkage in glycan_linkages
            ]

        # Stamp the implicit-solvent decision before resolving the
        # effective force field — both feed result["parameters"] /
        # node metadata and need to survive even if the build later fails.
        result["parameters"]["implicit_solvent"] = canonical_implicit_solvent

        # Implicit solvent: pick the effective protein force field. ff14SB is
        # the standard implicit pair (GBneck2 was parameterized against the
        # ff99SB-derived ff14SB backbone), but Amber25 ships an explicit
        # implicit-tuned variant (``ff14SBonlysc``) which uses the same
        # backbone with sidechains tuned for GB. Auto-substitute it when the
        # caller picks ff14SB so the standard skill recipe (``--forcefield
        # ff14SB --implicit-solvent OBC2``) lands on the implicit-tuned XML
        # without surprising users that explicitly request ff14SBonlysc.
        # ff19SB + implicit_solvent gets a warning (ff19SB is OPC-tuned and
        # not endorsed for GB by Amber25 ch.3).
        effective_forcefield = forcefield
        if canonical_implicit_solvent is not None:
            canon_protein_for_implicit = _ff_catalog.normalize_protein(forcefield)
            if canon_protein_for_implicit == "ff14SB":
                effective_forcefield = "ff14SBonlysc"
                result["warnings"].append(
                    "implicit_solvent: auto-switched protein force field "
                    "ff14SB -> ff14SBonlysc (the GBneck2-tuned variant). "
                    "Pass forcefield='ff14SBonlysc' explicitly to silence "
                    "this notice."
                )
            elif canon_protein_for_implicit == "ff19SB":
                result["warnings"].append(
                    "implicit_solvent: ff19SB was parameterized for OPC "
                    "explicit water and is not Amber25's recommended choice "
                    "for GB models. Prefer ff14SB / ff14SBonlysc for "
                    "implicit-solvent runs."
                )
        result["parameters"]["effective_forcefield"] = effective_forcefield

        topology_water_model = (
            actual_water_model
            if box_dimensions
            or (solvent_type == "vacuum" and retained_ion_residue_names)
            else None
        )
        om_result = _run_openmmforcefields_build(
            pdb_path=pdb_path,
            output_name=output_name,
            out_dir=out_dir,
            system_xml_file=system_xml_file,
            topology_pdb_file=topology_pdb_file,
            state_xml_file=state_xml_file,
            minimization_report_file=minimization_report_file,
            forcefield=effective_forcefield,
            water_model=topology_water_model,
            phosaa_library=phosaa_library,
            nucleic_libraries=nucleic_libraries,
            glycan_library=glycan_library,
            is_membrane=bool(is_membrane),
            box_dimensions=box_dimensions,
            valid_ligands=valid_ligands or [],
            valid_metal_params=valid_metal_params or [],
            valid_modxna_params=valid_modxna_params or [],
            disulfide_bonds=disulfide_bonds,
            glycam_bond_plan=(
                glycam_prepare.get("glycam_bond_plan") if glycam_prepare else None
            ),
            glycam_normalization_file=out_dir / f"{output_name}.glycam_normalization.json",
            hmr=hmr,
            implicit_solvent=canonical_implicit_solvent,
            minimize_max_iterations=minimize_max_iterations,
            stage_callback=(
                (lambda stage: _record_topology_build_stage(job_dir, node_id, stage))
                if _node_mode else None
            ),
        )
        result["warnings"].extend(om_result.get("warnings", []))
        result["topology_notes"].extend(om_result.get("topology_notes", []))
        if om_result.get("topology_validation"):
            topology_validation = om_result["topology_validation"]
            disulfide_validation = topology_validation.get("disulfides", {})
            if disulfide_plan_warnings:
                if disulfide_validation.get("status") == "passed":
                    result["topology_notes"].extend(disulfide_plan_warnings)
                    notes = topology_validation.setdefault(
                        "non_authoritative_notes",
                        [],
                    )
                    notes.extend(disulfide_plan_warnings)
                else:
                    result["warnings"].extend(disulfide_plan_warnings)
            result["topology_validation"] = topology_validation
        if om_result.get("glycam_bond_plan"):
            result["glycam_bond_plan"] = om_result["glycam_bond_plan"]
        if om_result.get("glycam_normalization"):
            result["glycam_normalization"] = om_result["glycam_normalization"]
        if om_result.get("success"):
            _record_topology_build_stage(job_dir, node_id, "completed")
            result["system_xml"] = om_result["system_xml"]
            result["topology_pdb"] = om_result["topology_pdb"]
            result["state_xml"] = om_result["state_xml"]
            if om_result.get("minimization_report"):
                result["minimization_report"] = om_result["minimization_report"]
            if om_result.get("minimization"):
                result["minimization"] = om_result["minimization"]
            if om_result.get("topology_validation"):
                result["topology_validation"] = om_result["topology_validation"]
            result["statistics"] = {
                "num_atoms": om_result["num_atoms"],
                "num_residues": om_result["num_residues"],
            }
            result["forcefield_provenance"] = om_result["forcefield_provenance"]
            result["success"] = True
            logger.info("Successfully built System via openmmforcefields:")
            logger.info(f"  system.xml: {system_xml_file}")
            logger.info(f"  topology.pdb: {topology_pdb_file}")
            logger.info(f"  state.xml: {state_xml_file}")
            logger.info(f"  Atoms: {om_result['num_atoms']}")
        else:
            result["errors"].extend(om_result.get("errors", []))
            if om_result.get("topology_validation"):
                result["topology_validation"] = om_result["topology_validation"]
            # Propagate the helper's structured ``code`` (e.g.
            # ``metal_openmm_xml_required``) so callers can branch on the
            # specific failure mode instead of grepping the error string.
            if not result.get("code"):
                result["code"] = (
                    om_result.get("code") or "openmmforcefields_build_failed"
                )
            logger.error(
                "openmmforcefields build failed: %s",
                "; ".join(om_result.get("errors", [])) or "(no error message)",
            )

    except TimeoutError as e:
        error_msg = f"Error during Amber system building: TimeoutError: {str(e)}"
        result["errors"].append(error_msg)
        result["errors"].append(
            "Hint: ligand charge fitting (antechamber/sqm AM1-BCC) timed out. "
            "Recovery: re-run this same build tool on a fresh node (sqm timing "
            "varies between runs), or raise the budget with the "
            "MDCLAW_CHARGE_FIT_TIMEOUT environment variable (seconds) for an "
            "exceptionally large ligand. Do NOT hand-roll a custom build "
            "script or shorten the timeout."
        )
        result["code"] = "openmmforcefields_build_timeout"
        logger.error(error_msg)
    except MemoryError as e:
        error_msg = f"Error during Amber system building: MemoryError: {str(e)}"
        result["errors"].append(error_msg)
        result["code"] = "openmmforcefields_build_memory_error"
        logger.error(error_msg)
    except Exception as e:
        error_msg = f"Error during Amber system building: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        result["code"] = result.get("code") or "openmmforcefields_build_failed"
        logger.error(error_msg)
    
    result["topology_notes"] = list(dict.fromkeys(result.get("topology_notes", [])))
    if result.get("topology_validation"):
        topology_validation_file.write_text(
            json.dumps(result["topology_validation"], indent=2, default=str),
            encoding="utf-8",
        )
        result["topology_validation_file"] = str(topology_validation_file)

    # Save metadata
    metadata_file = out_dir / "amber_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            artifacts = {
                "system_xml": f"artifacts/{output_name}.system.xml",
                "topology_pdb": f"artifacts/{output_name}.topology.pdb",
                "state_xml": f"artifacts/{output_name}.state.xml",
            }
            if result.get("minimization_report"):
                artifacts["minimization_report"] = (
                    f"artifacts/{output_name}.minimization_report.json"
                )
            if result.get("topology_validation_file"):
                artifacts["topology_validation"] = (
                    f"artifacts/{output_name}.topology_validation.json"
                )
            if glycam_prepare:
                artifacts.update({
                    "glycam_prepared_pdb": f"artifacts/{output_name}.glycam.pdb",
                    "glycam_prepareforleap_pdb": f"artifacts/{output_name}.prepareforleap.pdb",
                    "glycam_prepareforleap_script": f"artifacts/{output_name}.prepareforleap.in",
                    "glycam_prepareforleap_leap": f"artifacts/{output_name}.glycam.leap.in",
                    "glycam_bond_plan": f"artifacts/{output_name}.glycam_bond_plan.json",
                    "glycam_normalization": f"artifacts/{output_name}.glycam_normalization.json",
                    "glycam_prepareforleap_log": f"artifacts/{output_name}.prepareforleap.log",
                })
            complete_node(job_dir, node_id,
                artifacts=artifacts,
                metadata={
                    "forcefield": result["parameters"].get("forcefield"),
                    "effective_forcefield": effective_forcefield,
                    "water_model": water_model if solvent_type == "explicit" else None,
                    "ion_parameter_water_model": result["parameters"].get(
                        "ion_parameter_water_model"
                    ),
                    "solvent_type": solvent_type,
                    "implicit_solvent": canonical_implicit_solvent,
                    "hmr": bool(hmr),
                    "is_membrane": is_membrane,
                    "system_artifact_kind": "openmm_system_xml",
                    "forcefield_provenance": result.get("forcefield_provenance"),
                    "minimization": result.get("minimization"),
                    "topology_validation": result.get("topology_validation"),
                    "topology_notes": result.get("topology_notes"),
                    "nucleic_libraries": nucleic_libraries or None,
                    "nucleic_content": nucleic_content if nucleic_content.get("has_nucleic") else None,
                    "glycan_library": glycan_library,
                    "glycan_content": glycan_content if glycan_content.get("has_glycan") else None,
                    "glycan_linkage_plan": result.get("glycan_linkage_plan"),
                    "glycam_bond_plan": result.get("glycam_bond_plan"),
                    "glycam_normalization": result.get("glycam_normalization"),
                    "glycam_prepareforleap": result.get("parameters", {}).get("glycam_prepareforleap"),
                    "modxna_params": valid_modxna_params or None,
                    "phosaa_library": phosaa_library,
                    "ptm_residues": ptm_residues_in_input or None,
                })
            summary_params = {
                "forcefield": result["parameters"].get("forcefield"),
                "nucleic_libraries": nucleic_libraries or None,
                "glycan_library": glycan_library,
                "solvation_type": solvent_type,
                "water_model": water_model if solvent_type == "explicit" else None,
                "ion_parameter_water_model": result["parameters"].get(
                    "ion_parameter_water_model"
                ),
            }
            update_job_summaries(job_dir, params=summary_params)
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result



# =============================================================================
# openmmforcefields + Pablo build helper
# =============================================================================
# Replaces the legacy tleap-script generation + tleap-execution path. Inputs
# are the canonical force-field names (catalog keys, not leaprc strings); the
# helper resolves the OpenMM XML bundle, loads the PDB via Pablo with a
# PDBFile fallback, runs SystemGenerator, and serializes the modern artifact
# triple (system.xml + topology.pdb + state.xml).
