"""Water/ion solvation tool (``solvate_structure``) and its OpenMM fallback."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    CANONICAL_WATER_MODELS,
    count_atoms_in_pdb,
    create_unique_subdir,
    create_validation_error,
    create_validation_error_from_guardrails,
    generate_job_id,
    guardrail_messages,
    split_guardrail_results,
)
from mdclaw._common import get_timeout
from mdclaw._tool_meta import node_tool
from mdclaw.solvation.box import (
    _write_box_dimensions_json,
    extract_box_size_from_cryst1,
)
from mdclaw.solvation.constants import (
    OPENMM_FALLBACK_WATER_MAP,
    _evaluate_solvation_water_model_guardrails,
    _normalize_water_model_name,
)
from mdclaw.solvation.pdb_identity import (
    _auto_metal_ion_packmol_charge_pdb_delta,
    _auto_nucleic_packmol_charge_pdb_delta,
    _ligand_chemistry_packmol_charge_pdb_delta,
    _restore_packmol_solute_identity,
)

from mdclaw.solvation._base import (
    WORKING_DIR,
    logger,
    packmol_memgen_wrapper,
    _append_salt_override_arg,
    _diagnostics_require_salt_override,
    _packmol_memgen_diagnostics,
    _record_packmol_memgen_output,
    _record_salt_override_fallback,
    _run_packmol_if_needed,
)


def _solvate_with_openmm(
    pdb_path: Path,
    result: dict,
    output_dir: Optional[str],
    output_name: str,
    dist: float,
    cubic: bool,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    water_model: str,
    *,
    subdirectory: bool = True,
) -> dict:
    """Fallback solvation using OpenMM/PDBFixer when packmol-memgen is unavailable.

    Uses OpenMM Modeller.addSolvent() with a padding-based box. When
    ``subdirectory`` is True (default, used by direct CLI calls) a unique
    ``solvate_<id>/`` directory is created under ``output_dir``. When False
    (used by node-mode callers that pass ``output_dir=nodes/<id>/artifacts/``),
    output files land directly in ``output_dir`` so the artifact paths
    registered on ``node.json`` match the real on-disk layout.
    """
    logger.info("Using OpenMM/PDBFixer fallback for solvation")
    try:
        from openmm.app import PDBFile, Modeller, ForceField
        from openmm import unit
    except ImportError:
        result["errors"].append("OpenMM not available for fallback solvation")
        return result

    base_dir = Path(output_dir) if output_dir else WORKING_DIR
    if subdirectory:
        out_dir = create_unique_subdir(base_dir, "solvate")
    else:
        out_dir = base_dir
        out_dir.mkdir(parents=True, exist_ok=True)
    result["output_dir"] = str(out_dir)
    output_file = out_dir / f"{output_name}.pdb"

    try:
        # Load structure
        pdb = PDBFile(str(pdb_path))
        modeller = Modeller(pdb.topology, pdb.positions)

        # Select force field and water model
        # Map water_model to OpenMM water XML
        water_xml = OPENMM_FALLBACK_WATER_MAP[water_model.lower()]

        # Use amber14 force field (compatible with most water models)
        ff = ForceField("amber14-all.xml", water_xml)

        # Add solvent with padding
        padding_nm = dist / 10.0  # Convert Angstrom to nm
        modeller.addSolvent(
            ff,
            model=water_model.lower(),
            padding=padding_nm * unit.nanometer,
            ionicStrength=(saltcon if salt else 0.0) * unit.molar,
            positiveIon=salt_c,
            negativeIon=salt_a,
        )

        # Write output
        with open(output_file, "w") as f:
            PDBFile.writeFile(modeller.topology, modeller.positions, f)

        # OpenMM's PDBFile loader normalized Amber/PTM residue names (ASH->ASP,
        # HID->HIS, GLH->GLU, ...) when the input was loaded; restore them from
        # the input by residue key so the solvated artifact — and the topology
        # built from it — keeps the prepared protonation state. Added water/ions
        # are absent from the source and keep their OpenMM names.
        from mdclaw.structure.pdb_utils import restore_resnames_by_residue_key
        _restored = restore_resnames_by_residue_key(
            output_file.read_text(), str(pdb_path)
        )
        if _restored is not None:
            output_file.write_text(_restored)

        # Extract box size from PDB
        box_dims = extract_box_size_from_cryst1(str(output_file))

        # Persist box_dimensions.json next to the PDB so downstream tools
        # (build_amber_system) can resolve it as a node artifact uniformly
        # across packmol-memgen and OpenMM-fallback paths.
        if box_dims:
            box_json_path = _write_box_dimensions_json(out_dir, box_dims)
            if box_json_path is None:
                result["errors"].append(
                    "OpenMM fallback: failed to persist box_dimensions.json"
                )
                return result
            result["box_dimensions_file"] = str(box_json_path)

        # Count atoms
        atom_count = count_atoms_in_pdb(str(output_file))

        result["success"] = True
        result["output_file"] = str(output_file)
        result["box_dimensions"] = box_dims or {}
        result["statistics"] = {
            "total_atoms": atom_count,
            "method": "openmm_fallback",
        }
        result["warnings"].append("Used OpenMM fallback (packmol-memgen not available)")
        logger.info(f"OpenMM solvation complete: {output_file}")

    except Exception as e:
        result["errors"].append(f"OpenMM solvation failed: {type(e).__name__}: {e}")
        logger.error(f"OpenMM solvation error: {e}")

    return result


@node_tool(node_type="solv")
def solvate_structure(
    pdb_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "solvated",
    dist: float = 15.0,
    cubic: bool = True,
    salt: bool = True,
    salt_c: str = "Na+",
    salt_a: str = "Cl-",
    saltcon: float = 0.15,
    salt_override: bool = False,
    overwrite: bool = True,
    notprotonate: bool = True,
    preoriented: bool = True,
    keepligs: bool = True,
    water_model: str = "opc",
    ligand_chemistry: Optional[list[dict[str, Any]]] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Solvate a protein-ligand complex in a water box using packmol-memgen.
    
    This tool creates a solvated system by surrounding the input structure
    with water molecules and optionally adding salt ions for physiological
    conditions.
    
    The output PDB file feeds into ``build_amber_system``, which uses
    ``openmmforcefields.SystemGenerator`` over an OpenFF Pablo–loaded
    topology to emit the ``system.xml`` + ``topology.pdb`` + ``state.xml``
    triple consumed by ``run_minimization`` / ``run_equilibration`` /
    ``run_production``.

    Args:
        pdb_file: Input PDB file path (e.g., merged.pdb from merge_structures)
        output_dir: Output directory (auto-generated if None)
        output_name: Base name for output file (default: "solvated")
        dist: Minimum distance from solute to box boundary in Angstroms (default: 15.0)
        cubic: Use cubic box shape (default: True). If False, uses rectangular.
               NOTE: Cubic boxes can be significantly larger for elongated proteins
               because packmol-memgen calculates box size from the maximum XY distance
               from the protein's centroid (max_rad). For proteins with asymmetric
               mass distribution, rectangular boxes (cubic=False) can reduce water
               count by 50-70%.
        salt: Add salt ions (default: True)
        salt_c: Cation type (default: "Na+"). Options: Na+, K+, etc.
        salt_a: Anion type (default: "Cl-"). Options: Cl-, etc.
        saltcon: Salt concentration in Molar (default: 0.15)
        salt_override: Continue if neutralization requires more ions than
                      the requested salt concentration. If False, MDClaw first
                      tries the requested saltcon and automatically reruns once
                      with packmol-memgen's --salt_override when that is the
                      only blocker.
        overwrite: Overwrite existing output files (default: True)
        notprotonate: Skip protonation by reduce (default: True, assumes pre-protonated)
        preoriented: (Ignored for --solvate mode, automatically set to True by packmol-memgen)
        keepligs: Keep ligands in the structure (default: True). Important when
                  processing protein-ligand complexes.
        water_model: Water model type (default: "opc").
                     Options: "tip3p", "opc", "opc3", "tip4pew", "spce".
                     IMPORTANT: Must match the water model used in build_amber_system for
                     topology generation. Using mismatched models causes severe atom clashes.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
        ligand_chemistry: Ligand chemistry records from prepare_complex. Formal
                          charges are included in packmol-memgen's
                          --charge_pdb_delta so arbitrary GAFF/OpenFF ligands
                          are neutralized consistently with topology.
    
    Returns:
        Dict with:
            - success: bool - True if solvation completed successfully
            - job_id: str - Unique identifier for this operation
            - output_file: str - Path to the solvated PDB file
            - output_dir: str - Output directory path
            - input_file: str - Input PDB file path
            - parameters: dict - Parameters used for solvation
            - packmol_log: str - Path to packmol log file (if available)
            - statistics: dict - Atom counts, etc.
            - box_dimensions: dict - Box size extracted from CRYST1 record:
                - box_a, box_b, box_c: Box dimensions in Angstroms
                - alpha, beta, gamma: Box angles in degrees
                - is_cubic: True if all sides equal and all angles 90°
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = solvate_structure(
        ...     "output/job1/merged.pdb",
        ...     dist=15.0,
        ...     cubic=True,
        ...     salt=True,
        ...     saltcon=0.15
        ... )
        >>> print(result["output_file"])
        'output/abc123/solvated.pdb'
        >>> print(result["box_dimensions"])
        {'box_a': 86.32, 'box_b': 86.32, 'box_c': 86.32, ...}
    """
    logger.info(f"Solvating structure: {pdb_file}")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_file": None,
        "output_dir": None,
        "input_file": str(pdb_file),
        "parameters": {
            "water_model": water_model,
            "dist": dist,
            "cubic": cubic,
            "salt": salt,
            "salt_c": salt_c,
            "salt_a": salt_a,
            "saltcon": saltcon,
            "salt_override": salt_override,
        },
        "packmol_log": None,
        "statistics": {},
        "errors": [],
        "warnings": []
    }

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        blocked = create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="solvate_structure unknown water_model",
            )
        return blocked
    water_model = canonical_water_model
    result["parameters"]["water_model"] = water_model

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "solv",
            actual_conditions={
                "water_model": water_model,
                "dist": dist,
                "cubic": cubic,
                "salt": salt,
                "salt_c": salt_c,
                "salt_a": salt_a,
                "saltcon": saltcon,
                "salt_override": salt_override,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="solvate_structure node execution context invalid",
            )
    
    # Node mode always uses the canonical DAG input. An explicit path may only
    # repeat that same artifact; it cannot replace the parent provenance.
    if job_dir and node_id:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(
            job_dir,
            node_id,
            "solv",
            explicit_paths={"pdb_file": pdb_file} if pdb_file else None,
        )
        if "input_resolution_error" in _inputs:
            blocked = create_validation_error(
                "job_dir/node_id",
                _inputs["input_resolution_error"],
                expected="Completed prep ancestor with merged_pdb artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            )
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(job_dir, node_id, blocked)
        if "pdb_file" in _inputs:
            pdb_file = _inputs["pdb_file"]
        if ligand_chemistry is None and "ligand_chemistry" in _inputs:
            ligand_chemistry = _inputs["ligand_chemistry"]

    if not pdb_file:
        blocked = create_validation_error(
            "pdb_file",
            "pdb_file is required",
            expected="Explicit PDB path, or --job-dir/--node-id for DAG auto-resolve",
            actual=pdb_file,
            hints=["Run prepare_complex first or execute in node mode from a solv node."],
            code="missing_pdb_file",
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked.get("errors", []))
        return blocked

    # Validate input file (resolve to absolute path for conda run compatibility)
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        logger.error(f"Input PDB file not found: {pdb_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
        return result

    # Check packmol-memgen availability; fall back to OpenMM if not available
    if not packmol_memgen_wrapper.is_available():
        guardrail_results = _evaluate_solvation_water_model_guardrails(
            water_model,
            backend="openmm_fallback",
        )
        blocking_results, warning_results = split_guardrail_results(guardrail_results)
        if blocking_results:
            blocked = {
                **result,
                **create_validation_error_from_guardrails(
                    "water_model",
                    guardrail_results,
                    summary=guardrail_results[0]["message"],
                    actual=water_model,
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked.get("errors", [blocked.get("message", "")]))
            return blocked
        result["warnings"].extend(guardrail_messages(warning_results))
        logger.warning("packmol-memgen not available, trying OpenMM fallback")
        _node_mode = job_dir and node_id
        if _node_mode:
            from mdclaw._node import begin_node
            fallback_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
            fallback_dir.mkdir(parents=True, exist_ok=True)
            begin_node(job_dir, node_id)
            output_dir = str(fallback_dir)
        fallback_result = _solvate_with_openmm(
            pdb_path=pdb_path,
            result=result,
            output_dir=output_dir,
            output_name=output_name,
            dist=dist,
            cubic=cubic,
            salt=salt,
            salt_c=salt_c,
            salt_a=salt_a,
            saltcon=saltcon,
            water_model=water_model,
            subdirectory=not _node_mode,
        )
        if _node_mode:
            from mdclaw._node import complete_node, fail_node, update_job_summaries
            if fallback_result.get("success"):
                if not fallback_result.get("box_dimensions"):
                    fallback_result["success"] = False
                    fallback_result["errors"].append(
                        "OpenMM fallback solvation did not produce box_dimensions"
                    )
            if fallback_result.get("success"):
                complete_node(job_dir, node_id,
                    artifacts={
                        "solvated_pdb": f"artifacts/{output_name}.pdb",
                        "box_dimensions": "artifacts/box_dimensions.json",
                    },
                    metadata={
                        "water_model": water_model,
                        "backend": "openmm_fallback",
                        "buffer_distance_angstrom": dist,
                        "salt_cation": salt_c,
                        "salt_anion": salt_a,
                        "salt_concentration_M": saltcon,
                        "total_atoms": fallback_result.get("statistics", {}).get("total_atoms"),
                    },
                    warnings=fallback_result.get("warnings", []))
                update_job_summaries(job_dir, params={
                    "solvation_type": "explicit",
                    "water_model": water_model,
                })
            else:
                fail_node(job_dir, node_id, errors=fallback_result.get("errors", []))
        return fallback_result

    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "solvate")
    result["output_dir"] = str(out_dir)

    # Copy input file to output directory
    input_copy = out_dir / pdb_path.name
    shutil.copy(pdb_path, input_copy)

    auto_charge_delta_report = {
        "charge_pdb_delta": 0,
        "segments": [],
        "applied_segment_count": 0,
        "reason": "not evaluated",
    }
    try:
        auto_charge_delta_report = _auto_nucleic_packmol_charge_pdb_delta(input_copy)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            "Could not evaluate automatic nucleic-acid charge_pdb_delta "
            f"for packmol-memgen: {type(exc).__name__}: {exc}"
        )
    nucleic_charge_delta = int(auto_charge_delta_report.get("charge_pdb_delta", 0))

    metal_charge_delta_report = {
        "charge_pdb_delta": 0,
        "ions": [],
        "applied_ion_count": 0,
        "reason": "not evaluated",
    }
    try:
        metal_charge_delta_report = _auto_metal_ion_packmol_charge_pdb_delta(input_copy)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            "Could not evaluate automatic metal-ion charge_pdb_delta "
            f"for packmol-memgen: {type(exc).__name__}: {exc}"
        )
    metal_charge_delta = int(metal_charge_delta_report.get("charge_pdb_delta", 0))

    ligand_charge_delta_report = {
        "charge_pdb_delta": 0,
        "ligands": [],
        "applied_ligand_count": 0,
        "reason": "not evaluated",
    }
    try:
        ligand_charge_delta_report = _ligand_chemistry_packmol_charge_pdb_delta(
            ligand_chemistry
        )
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            "Could not evaluate automatic ligand charge_pdb_delta "
            f"for packmol-memgen: {type(exc).__name__}: {exc}"
        )
    ligand_charge_delta = int(ligand_charge_delta_report.get("charge_pdb_delta", 0))

    auto_charge_delta = nucleic_charge_delta + metal_charge_delta + ligand_charge_delta
    auto_charge_delta_applied = bool(salt and auto_charge_delta)
    _reasons = [
        r for r in (
            auto_charge_delta_report.get("reason"),
            metal_charge_delta_report.get("reason"),
            ligand_charge_delta_report.get("reason"),
        )
        if r
    ]
    result["auto_charge_pdb_delta"] = auto_charge_delta
    result["auto_charge_pdb_delta_applied"] = auto_charge_delta_applied
    result["auto_charge_pdb_delta_reason"] = "; ".join(_reasons) or None
    result["nucleic_charge_segments"] = auto_charge_delta_report.get("segments", [])
    result["metal_ion_charge_delta"] = metal_charge_delta
    result["metal_ion_charge_entries"] = metal_charge_delta_report.get("ions", [])
    result["ligand_charge_delta"] = ligand_charge_delta
    result["ligand_charge_delta_entries"] = ligand_charge_delta_report.get("ligands", [])
    result["parameters"]["auto_charge_pdb_delta"] = auto_charge_delta
    result["parameters"]["auto_charge_pdb_delta_applied"] = auto_charge_delta_applied

    # Output file
    output_file = out_dir / f"{output_name}.pdb"
    packlog = out_dir / f"{output_name}_packmol"

    try:
        # Build packmol-memgen command
        args = [
            '--solvate',
            '--dist', str(dist),
            '--pdb', str(input_copy),
            '-o', str(output_file),
            '--packlog', str(packlog),
            '--ffwat', water_model.lower(),  # Water model for solvation
            '--tolerance', '2.0'  # Default packmol tolerance
        ]

        if auto_charge_delta_applied:
            args.extend(['--charge_pdb_delta', str(auto_charge_delta)])

        if cubic:
            args.append('--cubic')

        if salt:
            args.extend([
                '--salt',
                '--salt_c', salt_c,
                '--salt_a', salt_a,
                '--saltcon', str(saltcon)
            ])
            if salt_override:
                _append_salt_override_arg(args)
        
        if overwrite:
            args.append('--overwrite')
        
        if notprotonate:
            args.append('--notprotonate')
        
        if preoriented:
            args.append('--preoriented')
        
        if keepligs:
            args.append('--keepligs')
        
        # Add packmol path as command-line argument (packmol-memgen doesn't read PACKMOL_PATH env var)
        packmol_path = shutil.which("packmol")
        if packmol_path:
            args.extend(['--packmol', packmol_path])
            logger.info(f"Using packmol: {packmol_path}")

        logger.info(f"Running packmol-memgen with args: {' '.join(args)}")

        # Run packmol-memgen (no need for env_vars since we pass --packmol)
        solvation_timeout = get_timeout("solvation")
        packmol_inp_file = out_dir / f"{output_name}_packmol.inp"
        try:
            proc_result = packmol_memgen_wrapper.run(
                args, cwd=out_dir, timeout=solvation_timeout
            )
        except subprocess.CalledProcessError as exc:
            diagnostics = _packmol_memgen_diagnostics(
                out_dir=out_dir,
                output_name=output_name,
                exc=exc,
            )
            if salt and not salt_override and _diagnostics_require_salt_override(diagnostics):
                _record_salt_override_fallback(
                    result=result,
                    out_dir=out_dir,
                    output_name=output_name,
                    saltcon=saltcon,
                    mode="solvated",
                )
                _append_salt_override_arg(args)
                proc_result = packmol_memgen_wrapper.run(
                    args, cwd=out_dir, timeout=solvation_timeout
                )
            else:
                raise
        else:
            diagnostics = _packmol_memgen_diagnostics(
                out_dir=out_dir,
                output_name=output_name,
                proc_result=proc_result,
            )
            if (
                salt
                and not salt_override
                and not output_file.exists()
                and _diagnostics_require_salt_override(diagnostics)
            ):
                _record_salt_override_fallback(
                    result=result,
                    out_dir=out_dir,
                    output_name=output_name,
                    saltcon=saltcon,
                    mode="solvated",
                )
                _append_salt_override_arg(args)
                proc_result = packmol_memgen_wrapper.run(
                    args, cwd=out_dir, timeout=solvation_timeout
                )

        _run_packmol_if_needed(
            output_file=output_file,
            packmol_inp_file=packmol_inp_file,
            packmol_path=packmol_path,
            out_dir=out_dir,
            output_name=output_name,
            timeout=solvation_timeout,
            result=result,
        )
        _record_packmol_memgen_output(
            output_file=output_file,
            packmol_inp_file=packmol_inp_file,
            out_dir=out_dir,
            output_name=output_name,
            proc_result=proc_result,
            result=result,
            success_message="Successfully solvated structure",
        )
        if result.get("success") and output_file.exists():
            restore_report = _restore_packmol_solute_identity(input_copy, output_file)
            result.update(restore_report)
            result["warnings"].extend(restore_report.get("solute_identity_restore_warnings", []))
        
    except Exception as e:
        error_msg = f"Error during solvation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "timeout" in str(e).lower():
            result["errors"].append("Hint: Solvation timed out. Try reducing box size or simplifying the structure.")
    
    # Save metadata
    metadata_file = out_dir / "solvation_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            _box = result.get("box_dimensions", {})
            if not _box:
                result["success"] = False
                result["errors"].append(
                    "Explicit solvation completed but box_dimensions could not be extracted"
                )
                fail_node(job_dir, node_id, errors=result.get("errors", []))
                return result
            complete_node(job_dir, node_id,
                artifacts={
                    "solvated_pdb": f"artifacts/{output_name}.pdb",
                    "box_dimensions": "artifacts/box_dimensions.json",
                },
                metadata={
                    "water_model": water_model,
                    "box_shape": "cubic" if _box.get("is_cubic") else "rectangular",
                    "buffer_distance_angstrom": dist,
                    "salt_concentration_M": saltcon,
                    "auto_charge_pdb_delta": result.get("auto_charge_pdb_delta"),
                    "auto_charge_pdb_delta_applied": result.get(
                        "auto_charge_pdb_delta_applied"
                    ),
                    "ligand_charge_delta": result.get("ligand_charge_delta"),
                    "total_atoms": result.get("statistics", {}).get("total_atoms"),
                })
            update_job_summaries(job_dir, params={
                "solvation_type": "explicit",
                "water_model": water_model,
            })
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result
