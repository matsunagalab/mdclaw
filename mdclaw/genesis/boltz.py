"""genesis.boltz submodule (behavior-preserving split)."""

import os
import datetime
import hashlib
import json
import shutil
import string
import subprocess
from pathlib import Path
from typing import Optional
import yaml
from mdclaw._common import create_unique_subdir, generate_job_id, tail_for_agent

from mdclaw.genesis._base import (
    WORKING_DIR,
    _boltz_model_index,
    _parse_boltz_results,
    _resolve_boltz_backend,
    _structure_format_from_path,
    logger,
)


def boltz2_protein_from_seq(
    amino_acid_sequence_list: list[str],
    smiles_list: Optional[list[str]] = None,
    affinity: bool = False,
    num_models: int = 1,
    output_dir: Optional[str] = None,
    msa_path: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Predict protein structures for one or more amino acid sequences using Boltz-2.

    This tool uses Boltz-2, an AI-driven structure prediction model, to generate
    3D protein structures from amino acid sequences. It supports:
    - Single protein structure prediction
    - Multi-chain/multimer structure prediction
    - Protein-ligand complex prediction
    - Binding affinity prediction

    Args:
        amino_acid_sequence_list: List of amino acid sequences in single-letter format.
                                  Multiple sequences will be predicted as a complex.
        smiles_list: Optional list of SMILES strings for ligands to include in
                     the prediction. Omit it or use [] if no ligands are needed.
        affinity: Set to True to predict binding affinity for the first ligand.
                  Default is False.
        num_models: Number of structure models to generate (default: 1).
                    Maps to Boltz-2's --diffusion_samples flag.
                    Higher values increase diversity at the cost of compute time.
        output_dir: Output directory. If None, creates output/boltz/.
                    When provided (e.g., session directory), creates a "boltz"
                    subdirectory within it. Ignored in node mode.
        msa_path: Optional path to a custom MSA (Multiple Sequence Alignment)
                  file. When provided, it is written into the Boltz input YAML
                  as the protein `msa` field. If None, the Boltz MSA server is
                  used instead.
        job_dir: Job directory for node-based tracking (schema v3).
        node_id: Source node ID. When both ``job_dir`` and ``node_id`` are provided,
                 the top-ranked predicted PDB/mmCIF is copied into
                 ``<job_dir>/nodes/<node_id>/artifacts/`` and the source node is
                 marked completed with ``source_type="boltz2"`` plus sequence
                 and SMILES metadata. All prediction models are normalized into
                 the source bundle as selectable candidates.

    Returns:
        Dict with:
            - success: bool - True if prediction completed successfully
            - job_id: str - Unique identifier for this prediction job
            - output_dir: str - Path to boltz output directory (all models)
            - input_yaml_path: str - Path to Boltz-2 input YAML file
            - predicted_pdb_files: list[str] - Paths to predicted PDB/mmCIF structure files
              (legacy key name retained for compatibility)
            - file_path: str | None - Path to the primary structure copied under the
              source node artifacts (node mode only)
            - confidence_scores: dict - First confidence JSON payload when present
            - confidence_records: list[dict] - Confidence JSON records with model indices
            - affinity_scores: dict | None - Binding affinity predictions if requested
              Contains:
              - affinity_probability_binary: Higher = more confident binding
              - affinity_pred_value: Lower = stronger predicted binding,
                reported as log10(IC50) with IC50 expressed in uM
            - errors: list[str] - Error messages if any
            - warnings: list[str] - Non-critical warnings

    Example:
        >>> result = boltz2_protein_from_seq(
        ...     amino_acid_sequence_list=["MVLSPADKTNVKAAW..."],
        ...     smiles_list=["CCO"],
        ...     affinity=True,
        ...     output_dir="/output/session_abc123"
        ... )
    """
    logger.info(f"Starting Boltz-2 job for {len(amino_acid_sequence_list)} sequences")

    # Initialize result structure
    job_id = generate_job_id()
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    # Node-mode setup: validate source node before touching state, then use the
    # node artifacts dir as the boltz output root so predictions land under it.
    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw.research.source_core import (
            _resolve_source_artifacts_dir,
            _validate_source_node,
        )
        from mdclaw._node import begin_node, fail_node
        _node_err = _validate_source_node(job_dir, node_id)
        if _node_err:
            return {
                "success": False,
                "code": "invalid_source_node",
                "job_id": job_id,
                "output_dir": None,
                "input_yaml_path": None,
                "predicted_pdb_files": [],
                "file_path": None,
                "confidence_scores": {},
                "affinity_scores": None,
                "errors": [_node_err],
                "warnings": [],
            }

    # Setup output directory with human-readable name
    if _node_mode:
        base_dir = _resolve_source_artifacts_dir(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "boltz")

    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": str(out_dir),
        "input_yaml_path": None,
        "predicted_pdb_files": [],
        "file_path": None,
        "confidence_scores": {},
        "affinity_scores": None,
        "errors": [],
        "warnings": []
    }

    smiles_list = smiles_list or []

    # Validate inputs
    if not amino_acid_sequence_list:
        result["errors"].append("At least one amino acid sequence is required")
        result["code"] = "boltz_sequence_required"
        return result

    if num_models < 1:
        result["errors"].append("num_models must be >= 1")
        result["code"] = "boltz_num_models_invalid"
        return result

    if msa_path and not Path(msa_path).expanduser().exists():
        result["errors"].append(f"msa_path does not exist: {msa_path}")
        result["code"] = "boltz_msa_file_missing"
        return result

    if msa_path and len(amino_acid_sequence_list) > 1:
        result["errors"].append(
            "Custom msa_path currently supports only single-protein inputs; "
            "Boltz expects per-chain msa entries for multi-protein complexes"
        )
        result["code"] = "boltz_custom_msa_multimer_unsupported"
        return result

    if _node_mode:
        begin_node(job_dir, node_id)

    # Create YAML input for Boltz-2
    yaml_filename = f"{timestamp}.yaml"
    yaml_path = out_dir / yaml_filename
    result["input_yaml_path"] = str(yaml_path)

    # Build chain IDs (A-Z, a-z, 0-9)
    chain_ids = list(string.ascii_uppercase) + list(string.ascii_lowercase) + [str(i) for i in range(10)]
    max_chains = len(chain_ids)

    yaml_data = {'version': 1, 'sequences': []}
    id_index = 0
    ligand_id_start = None

    # Add protein sequences
    for sequence in amino_acid_sequence_list:
        if id_index >= max_chains:
            result["errors"].append(f"Exceeded maximum number of chains ({max_chains})")
            result["code"] = "boltz_chain_count_exceeded"
            return result

        protein_id = chain_ids[id_index]
        protein_spec = {
            'id': protein_id,
            'sequence': sequence,
        }
        if msa_path:
            protein_spec['msa'] = msa_path

        yaml_data['sequences'].append({
            'protein': protein_spec
        })
        id_index += 1

    # Add ligands
    for smiles_sequence in smiles_list:
        if not smiles_sequence or smiles_sequence.isspace():
            continue
        if id_index >= max_chains:
            result["errors"].append(f"Exceeded maximum number of chains ({max_chains})")
            result["code"] = "boltz_chain_count_exceeded"
            return result

        ligand_id = chain_ids[id_index]
        if ligand_id_start is None:
            ligand_id_start = ligand_id

        yaml_data['sequences'].append({
            'ligand': {
                'id': ligand_id,
                'smiles': smiles_sequence
            }
        })
        id_index += 1

    # Add affinity prediction if requested
    if affinity:
        if not ligand_id_start:
            result["errors"].append("Affinity calculation requires at least one valid SMILES string")
            result["code"] = "boltz_affinity_requires_ligand"
            return result
        yaml_data['properties'] = [{
            'affinity': {
                'binder': ligand_id_start
            }
        }]

    # Write YAML file
    with open(yaml_path, 'w') as f:
        yaml.dump(yaml_data, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Created Boltz-2 input YAML: {yaml_path}")

    # Resolve the isolated Boltz-2 backend venv
    boltz_executable_path, boltz_check = _resolve_boltz_backend()
    if not boltz_executable_path:
        result["errors"].append("Boltz-2 backend venv is not installed or not importable")
        result["errors"].extend(boltz_check.get("errors", []))
        result["errors"].append(
            "Install it with: mdclaw setup_model_backend --model boltz --device cuda"
        )
        result["code"] = "boltz_backend_not_installed"
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    # Run Boltz-2
    boltz_command = [
        boltz_executable_path,
        "predict",
        yaml_filename,
        "--output_format", "pdb",
        "--diffusion_samples", str(num_models)
    ]

    # Boltz reads custom MSA paths from the YAML. The CLI flag only controls
    # whether to auto-generate MSAs server-side when no per-chain MSA is set.
    if not msa_path:
        boltz_command.append("--use_msa_server")

    try:
        # Copy environment and set library conflict workaround
        run_env = os.environ.copy()
        run_env["KMP_DUPLICATE_LIB_OK"] = "TRUE"

        subprocess.run(
            boltz_command,
            cwd=out_dir,
            env=run_env,
            capture_output=True,
            text=True,
            check=True
        )

        logger.info("Boltz-2 prediction completed successfully")

    except subprocess.CalledProcessError as e:
        logger.error(f"Boltz-2 prediction failed: {e.stderr}")
        result["errors"].append(f"Boltz-2 prediction failed: {tail_for_agent(e.stderr)}")
        result["code"] = "boltz_execution_failed"
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    except Exception as e:
        logger.error(f"Boltz-2 prediction failed: {e}")
        result["errors"].append(f"Boltz-2 prediction failed: {type(e).__name__}: {str(e)}")
        result["code"] = "boltz_execution_failed"
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    # Parse results
    result_dir = out_dir / f"boltz_results_{timestamp}"
    parsed_results = _parse_boltz_results(result_dir)

    result["predicted_pdb_files"] = parsed_results["structures"]
    result["confidence_scores"] = parsed_results.get("confidence", {})
    result["confidence_records"] = parsed_results.get("confidence_records", [])
    result["output_dir"] = str(result_dir)

    if not result["predicted_pdb_files"]:
        result["warnings"].append("No PDB/mmCIF structure files found in output directory")

    # Load affinity scores if requested
    if affinity:
        cif_file_dir = result_dir / "predictions" / timestamp
        affinity_path = cif_file_dir / f"affinity_{timestamp}.json"

        if affinity_path.exists():
            try:
                with open(affinity_path, 'r') as f:
                    result["affinity_scores"] = json.load(f)
                logger.info("Loaded affinity scores")
            except Exception as e:
                result["warnings"].append(f"Failed to load affinity scores: {e}")
        else:
            result["warnings"].append("Affinity file not found in output")

    # Node integration: promote the top-ranked predicted PDB as the source
    # node's primary structure artifact. Additional models stay under
    # out_dir for inspection.
    if _node_mode:
        if not result["predicted_pdb_files"]:
            result["errors"].append(
                "Boltz-2 produced no PDB/mmCIF files; cannot complete source node"
            )
            result["code"] = "boltz_no_structure_output"
            fail_node(job_dir, node_id, errors=result["errors"])
            return result
        try:
            from mdclaw.research.source_core import (
                _complete_source_node,
                _resolve_source_artifacts_dir,
            )
            predicted_paths = [Path(p) for p in result["predicted_pdb_files"]]
            confidence_records = parsed_results.get("confidence_records", [])
            confidence_by_model = {
                rec["model_index"]: rec
                for rec in confidence_records
                if rec.get("model_index") is not None
            }
            primary_src = predicted_paths[0]
            artifacts_dir = _resolve_source_artifacts_dir(job_dir, node_id)
            primary_dst = artifacts_dir / f"boltz2_prediction_{primary_src.name}"
            shutil.copy2(primary_src, primary_dst)

            yaml_digest = hashlib.sha256(
                yaml_path.read_bytes()
            ).hexdigest()[:12] if yaml_path.exists() else job_id
            extra = {
                "sequences": amino_acid_sequence_list,
                "smiles_list": smiles_list,
                "affinity_requested": affinity,
                "num_models_requested": num_models,
                "msa_path": msa_path,
                "num_predicted_models": len(result["predicted_pdb_files"]),
                "boltz_output_dir": str(result_dir),
                "input_yaml": str(yaml_path),
            }
            if result.get("affinity_scores"):
                extra["affinity_scores"] = result["affinity_scores"]
            candidate_metadata = []
            def _candidate_annotation(
                *,
                path: Path,
                idx: int,
                model_index: int | None = None,
                confidence: dict | None = None,
            ) -> dict:
                metrics = {}
                origin = {
                    "boltz_rank": idx + 1,
                    "boltz_output_file": str(path),
                }
                if model_index is not None:
                    origin["boltz_model_index"] = model_index
                if confidence:
                    origin["confidence_file"] = confidence.get("file")
                    if isinstance(confidence.get("data"), dict):
                        confidence_data = confidence["data"]
                        metrics["confidence"] = confidence_data
                        if "confidence_score" in confidence_data:
                            metrics["confidence_score"] = confidence_data["confidence_score"]
                return {
                    "label": f"Boltz-2 candidate {idx + 1}",
                    "metrics": metrics,
                    "origin": origin,
                }

            if len(predicted_paths) == 1 and len(confidence_records) > 1:
                path = predicted_paths[0]
                per_model_annotations = []
                for idx, conf in enumerate(confidence_records):
                    model_index = conf.get("model_index")
                    per_model_annotations.append(_candidate_annotation(
                        path=path,
                        idx=idx,
                        model_index=model_index if model_index is not None else idx,
                        confidence=conf,
                    ))
                candidate_metadata.append({
                    "origin": {"boltz_output_file": str(path)},
                    "models": per_model_annotations,
                })
            else:
                for idx, path in enumerate(predicted_paths):
                    model_index = _boltz_model_index(path)
                    conf = None
                    if model_index is not None:
                        conf = confidence_by_model.get(model_index)
                    if conf is None and len(confidence_records) == len(predicted_paths):
                        conf = confidence_records[idx]
                    candidate_metadata.append(_candidate_annotation(
                        path=path,
                        idx=idx,
                        model_index=model_index,
                        confidence=conf,
                    ))
            _complete_source_node(
                job_dir,
                node_id,
                primary_dst,
                source_type="boltz2",
                source_id=f"boltz2_{yaml_digest}",
                file_format=_structure_format_from_path(primary_dst),
                extra_metadata=extra,
                source_structures=[primary_dst, *predicted_paths[1:]],
                source_candidate_metadata=candidate_metadata,
            )
            result["file_path"] = str(primary_dst)
        except Exception as e:
            msg = f"Failed to attach Boltz-2 prediction to source node: {type(e).__name__}: {e}"
            logger.error(msg)
            result["errors"].append(msg)
            result["code"] = "boltz_source_attach_failed"
            fail_node(job_dir, node_id, errors=[msg])
            return result

    result["success"] = True
    logger.info(
        f"Job {job_id} finished. Found {len(result['predicted_pdb_files'])} structure files."
    )

    return result

