"""
Genesis Server - Boltz-2 structure generation from sequence.

Provides tools for:
- AI-driven protein structure prediction using Boltz-2
- Protein-ligand complex structure prediction with binding affinity
- SMILES validation and canonicalization using RDKit
- Chemical name to SMILES conversion using PubChem
- Protein-ligand interaction profiling using PLIP
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import string  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Dict, Any, Optional  # noqa: E402

import yaml  # noqa: E402
from rdkit import Chem  # noqa: E402
from pubchempy import get_compounds  # noqa: E402

from mdclaw._common import ensure_directory, create_unique_subdir, generate_job_id, BaseToolWrapper  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize Boltz-2 wrapper
CORRECT_CONDA_ENV = "mdclaw"
boltz_wrapper = BaseToolWrapper("boltz", conda_env=CORRECT_CONDA_ENV)


# =============================================================================
# Boltz-2 Structure Prediction
# =============================================================================


def boltz2_protein_from_seq(
    amino_acid_sequence_list: list[str],
    smiles_list: list[str],
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
        smiles_list: List of SMILES strings for ligands to include in the prediction.
                     Use empty list [] if no ligands are needed.
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
        node_id: Fetch node ID. When both ``job_dir`` and ``node_id`` are provided,
                 the top-ranked predicted PDB is copied into
                 ``<job_dir>/nodes/<node_id>/artifacts/`` and the fetch node is
                 marked completed with ``source_type="boltz2"`` plus sequence
                 and SMILES metadata. Additional prediction models remain under
                 the boltz output directory for inspection.

    Returns:
        Dict with:
            - success: bool - True if prediction completed successfully
            - job_id: str - Unique identifier for this prediction job
            - output_dir: str - Path to boltz output directory (all models)
            - input_yaml_path: str - Path to Boltz-2 input YAML file
            - predicted_pdb_files: list[str] - Paths to predicted PDB structure files
            - file_path: str | None - Path to the primary PDB copied under the
              fetch node artifacts (node mode only)
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

    # Node-mode setup: validate fetch node before touching state, then use the
    # node artifacts dir as the boltz output root so predictions land under it.
    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw.research_server import (
            _validate_fetch_node,
            _resolve_fetch_artifacts_dir,
        )
        from mdclaw._node import begin_node, fail_node
        _node_err = _validate_fetch_node(job_dir, node_id)
        if _node_err:
            return {
                "success": False,
                "job_id": job_id,
                "output_dir": None,
                "input_yaml_path": None,
                "predicted_pdb_files": [],
                "file_path": None,
                "affinity_scores": None,
                "errors": [_node_err],
                "warnings": [],
            }

    # Setup output directory with human-readable name
    if _node_mode:
        base_dir = _resolve_fetch_artifacts_dir(job_dir, node_id)
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
        "affinity_scores": None,
        "errors": [],
        "warnings": []
    }

    # Validate inputs
    if not amino_acid_sequence_list:
        result["errors"].append("At least one amino acid sequence is required")
        return result

    if msa_path and len(amino_acid_sequence_list) > 1:
        result["errors"].append(
            "Custom msa_path currently supports only single-protein inputs; "
            "Boltz expects per-chain msa entries for multi-protein complexes"
        )
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

    # Check Boltz-2 availability
    boltz_executable_path = boltz_wrapper.executable
    if not boltz_executable_path:
        result["errors"].append("Boltz executable not found")
        result["errors"].append("Hint: Install Boltz-2 or activate the mdclaw conda environment")
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
        result["errors"].append(f"Boltz-2 prediction failed: {e.stderr[:500]}")
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    except Exception as e:
        logger.error(f"Boltz-2 prediction failed: {e}")
        result["errors"].append(f"Boltz-2 prediction failed: {type(e).__name__}: {str(e)}")
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    # Parse results
    result_dir = out_dir / f"boltz_results_{timestamp}"
    parsed_results = _parse_boltz_results(result_dir)

    result["predicted_pdb_files"] = parsed_results["structures"]
    result["output_dir"] = str(result_dir)

    if not result["predicted_pdb_files"]:
        result["warnings"].append("No PDB files found in output directory")

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

    # Node integration: promote the top-ranked predicted PDB as the fetch
    # node's primary structure artifact. Additional models stay under
    # out_dir for inspection.
    if _node_mode:
        if not result["predicted_pdb_files"]:
            result["errors"].append(
                "Boltz-2 produced no PDB files; cannot complete fetch node"
            )
            fail_node(job_dir, node_id, errors=result["errors"])
            return result
        try:
            from mdclaw.research_server import (
                _complete_fetch_node,
                _resolve_fetch_artifacts_dir,
            )
            primary_src = Path(result["predicted_pdb_files"][0])
            artifacts_dir = _resolve_fetch_artifacts_dir(job_dir, node_id)
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
            _complete_fetch_node(
                job_dir,
                node_id,
                primary_dst,
                source_type="boltz2",
                source_id=f"boltz2_{yaml_digest}",
                file_format="pdb",
                extra_metadata=extra,
            )
            result["file_path"] = str(primary_dst)
        except Exception as e:
            msg = f"Failed to attach Boltz-2 prediction to fetch node: {type(e).__name__}: {e}"
            logger.error(msg)
            result["errors"].append(msg)
            fail_node(job_dir, node_id, errors=[msg])
            return result

    result["success"] = True
    logger.info(f"Job {job_id} finished. Found {len(result['predicted_pdb_files'])} PDB files.")

    return result





def _parse_boltz_results(output_dir: Path) -> Dict[str, Any]:
    """Parse Boltz-2 output files.

    Args:
        output_dir: Path to Boltz-2 output directory

    Returns:
        Dict with:
            - structures: List of paths to PDB files
            - confidence: Confidence scores dict (if available)
    """
    results = {
        "structures": [],
        "confidence": {}
    }

    if not output_dir.exists():
        logger.warning(f"Output directory does not exist: {output_dir}")
        return results

    pdb_files = sorted(output_dir.glob("**/*.pdb"))
    results["structures"] = [str(f) for f in pdb_files]

    if not results["structures"]:
        logger.warning(f"No PDB structures found in {output_dir}")

    confidence_files = list(output_dir.glob("**/confidence_*.json"))
    if confidence_files:
        confidence_json = confidence_files[0]
        try:
            with open(confidence_json, 'r') as f:
                results["confidence"] = json.load(f)
            logger.info("Loaded confidence scores")
        except Exception as e:
            logger.warning(f"Failed to parse confidence.json: {e}")
    else:
        logger.warning("No confidence JSON file found")

    return results


# =============================================================================
# MODELLER Comparative Modeling
# =============================================================================


def _has_modeller_license_env() -> bool:
    """Return True when the user provided a MODELLER license via env vars."""
    return any(
        key.startswith("KEY_MODELLER") and bool(str(value).strip())
        for key, value in os.environ.items()
    )


def _sanitize_modeller_code(value: str, fallback: str) -> str:
    """Make a MODELLER-safe identifier from a filename stem or user value."""
    raw = (value or fallback).strip() or fallback
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
    return cleaned or fallback


def _wrap_modeller_sequence(sequence: str, width: int = 75) -> str:
    """Wrap a sequence for PIR/SEG alignment files."""
    compact = "".join(sequence.split())
    return "\n".join(compact[i:i + width] for i in range(0, len(compact), width))


def _write_modeller_seed_alignment(
    path: Path,
    *,
    target_code: str,
    target_sequence: str,
    template_code: str,
) -> None:
    """Write the minimal SEG/PIR seed file consumed by AutoModel.auto_align()."""
    wrapped_sequence = _wrap_modeller_sequence(target_sequence)
    path.write_text(
        "\n".join([
            f">P1;{target_code}",
            f"sequence:{target_code}:::::target:synthetic:-1.00:-1.00",
            f"{wrapped_sequence}*",
            f">P1;{template_code}",
            f"structureX:{template_code}:FIRST:@:LAST:@:template:synthetic:-1.00:-1.00",
            "*",
            "",
        ])
    )


def _write_modeller_runner(path: Path) -> None:
    """Write the isolated MODELLER runner script used by the wrapper tool."""
    path.write_text(
        r'''import json
import os
import re
import sys
import types
import importlib.util
from pathlib import Path

license_key = next(
    (value for key, value in os.environ.items() if key.startswith("KEY_MODELLER") and value),
    None,
)
spec = importlib.util.find_spec("modeller")
if spec is None:
    raise ModuleNotFoundError("No module named 'modeller'")

install_dir = None
search_locations = list(spec.submodule_search_locations or [])
if search_locations:
    config_path = Path(search_locations[0]) / "config.py"
    if config_path.exists():
        match = re.search(
            r"install_dir\s*=\s*r?['\"]([^'\"]+)['\"]",
            config_path.read_text(),
        )
        if match:
            install_dir = match.group(1)

if license_key:
    cfg = types.ModuleType("modeller.config")
    cfg.license = license_key
    if install_dir:
        cfg.install_dir = install_dir
    sys.modules["modeller.config"] = cfg

from modeller import Environ, log
from modeller.automodel import AutoModel, assess


def _jsonable(value):
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


config = json.loads(Path(sys.argv[1]).read_text())
log.verbose()

if config.get("random_seed") is None:
    env = Environ()
else:
    env = Environ(rand_seed=int(config["random_seed"]))

env.io.atom_files_directory = ["."]
env.io.hetatm = bool(config.get("hetatm", False))

model = AutoModel(
    env,
    alnfile=config["alignment_file"],
    knowns=config["template_code"],
    sequence=config["target_code"],
    assess_methods=(assess.DOPE, assess.GA341),
)
model.starting_model = 1
model.ending_model = int(config["num_models"])

if config.get("auto_align"):
    model.auto_align()

model.make()

models = []
for output in model.outputs:
    item = {
        "name": _jsonable(output.get("name")),
        "failure": _jsonable(output.get("failure")),
        "molpdf": _jsonable(output.get("molpdf")),
        "DOPE score": _jsonable(output.get("DOPE score")),
        "GA341 score": _jsonable(output.get("GA341 score")),
    }
    if item["name"]:
        item["path"] = str(Path(item["name"]).resolve())
    models.append(item)

ok_models = [item for item in models if item.get("failure") is None and item.get("name")]
if not ok_models:
    raise RuntimeError("MODELLER did not produce any successful models")

if all(item.get("DOPE score") is not None for item in ok_models):
    ok_models.sort(key=lambda item: item["DOPE score"])
    selection_reason = "lowest_dope_score"
else:
    selection_reason = "first_successful_model"

selected = ok_models[0]
Path(config["result_json"]).write_text(json.dumps({
    "all_models": models,
    "successful_models": ok_models,
    "selected_model": selected,
    "selection_reason": selection_reason,
}, indent=2))
'''
    )


def modeller_from_alignment(
    template_pdb: str,
    target_sequence: Optional[str] = None,
    num_models: int = 1,
    template_code: Optional[str] = None,
    target_code: str = "target",
    alignment_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    hetatm: bool = False,
    random_seed: Optional[int] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Build a comparative model with MODELLER and optionally attach it to a fetch node.

    MODELLER is an optional dependency. Users install it separately (for example,
    ``conda install salilab::modeller``) and provide their license via a
    ``KEY_MODELLER*`` environment variable.
    """
    logger.info("Starting MODELLER comparative modeling job")
    job_id = generate_job_id()

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw.research_server import (
            _resolve_fetch_artifacts_dir,
            _validate_fetch_node,
        )
        from mdclaw._node import begin_node, fail_node

        _node_err = _validate_fetch_node(job_dir, node_id)
        if _node_err:
            return {
                "success": False,
                "job_id": job_id,
                "output_dir": None,
                "file_path": None,
                "all_models": [],
                "selected_model": None,
                "errors": [_node_err],
                "warnings": [],
            }

    base_dir = _resolve_fetch_artifacts_dir(job_dir, node_id) if _node_mode else (
        Path(output_dir) if output_dir else WORKING_DIR
    )
    out_dir = create_unique_subdir(base_dir, "modeller")

    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": str(out_dir),
        "file_path": None,
        "all_models": [],
        "selected_model": None,
        "errors": [],
        "warnings": [],
    }

    template_path = Path(template_pdb).expanduser()
    if not template_path.exists():
        result["errors"].append(f"template_pdb does not exist: {template_pdb}")
        return result

    if num_models < 1:
        result["errors"].append("num_models must be >= 1")
        return result

    if not alignment_file and not target_sequence:
        result["errors"].append(
            "target_sequence is required when alignment_file is not provided"
        )
        return result

    if not _has_modeller_license_env():
        result["errors"].append(
            "MODELLER license environment variable not found "
            "(expected KEY_MODELLER10v8 or another KEY_MODELLER* variable)"
        )
        result["errors"].append(
            "Install MODELLER separately (for example: conda install salilab::modeller) "
            "and export KEY_MODELLER10v8=<your license key> before running"
        )
        result["code"] = "modeller_license_env_missing"
        return result

    template_code_clean = _sanitize_modeller_code(
        template_code or template_path.stem, "template"
    )
    target_code_clean = _sanitize_modeller_code(target_code or "target", "target")

    template_copy = out_dir / f"{template_code_clean}.pdb"
    shutil.copy2(template_path, template_copy)

    auto_align = alignment_file is None
    if alignment_file:
        src_alignment = Path(alignment_file).expanduser()
        if not src_alignment.exists():
            result["errors"].append(f"alignment_file does not exist: {alignment_file}")
            return result
        alignment_text = src_alignment.read_text()
        for code in (template_code_clean, target_code_clean):
            if f">P1;{code}" not in alignment_text:
                result["errors"].append(
                    f"alignment_file does not contain MODELLER entry '>P1;{code}'"
                )
        if result["errors"]:
            return result
        alignment_path = out_dir / src_alignment.name
        shutil.copy2(src_alignment, alignment_path)
    else:
        alignment_path = out_dir / f"{target_code_clean}_{template_code_clean}_seed.ali"
        _write_modeller_seed_alignment(
            alignment_path,
            target_code=target_code_clean,
            target_sequence=target_sequence or "",
            template_code=template_code_clean,
        )

    if _node_mode:
        begin_node(job_dir, node_id)

    runner_path = out_dir / "run_modeller.py"
    config_path = out_dir / "modeller_config.json"
    result_json = out_dir / "modeller_result.json"
    _write_modeller_runner(runner_path)
    config = {
        "alignment_file": alignment_path.name,
        "template_code": template_code_clean,
        "target_code": target_code_clean,
        "num_models": num_models,
        "hetatm": hetatm,
        "random_seed": random_seed,
        "auto_align": auto_align,
        "result_json": result_json.name,
    }
    config_path.write_text(json.dumps(config, indent=2))

    try:
        completed = subprocess.run(
            [sys.executable, runner_path.name, config_path.name],
            cwd=out_dir,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
        )
        if completed.stdout:
            (out_dir / "modeller.stdout").write_text(completed.stdout)
        if completed.stderr:
            (out_dir / "modeller.stderr").write_text(completed.stderr)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        stdout = e.stdout or ""
        msg = (
            "MODELLER modeling failed. "
            f"stdout: {stdout[:1000]} stderr: {stderr[:2000]}"
        )
        result["errors"].append(msg)
        if "No module named 'modeller'" in stderr:
            result["code"] = "modeller_not_installed"
            result["errors"].append(
                "Install MODELLER separately with: conda install salilab::modeller"
            )
        else:
            result["code"] = "modeller_execution_failed"
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result
    except Exception as e:
        msg = f"MODELLER modeling failed: {type(e).__name__}: {e}"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    if not result_json.exists():
        msg = "MODELLER runner did not write modeller_result.json"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    try:
        parsed = json.loads(result_json.read_text())
    except json.JSONDecodeError as e:
        msg = f"Could not parse MODELLER result JSON: {e}"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    successful_models = parsed.get("successful_models") or []
    selected_model = parsed.get("selected_model")
    if not successful_models or not selected_model:
        msg = "MODELLER produced no successful models"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    result["all_models"] = successful_models
    result["selected_model"] = {
        **selected_model,
        "selection_reason": parsed.get("selection_reason", "unknown"),
    }

    selected_path = Path(selected_model.get("path") or selected_model.get("name", ""))
    if not selected_path.is_absolute():
        selected_path = (out_dir / selected_path).resolve()
    if not selected_path.exists():
        msg = f"Selected MODELLER model does not exist: {selected_path}"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    if _node_mode:
        try:
            from mdclaw.research_server import (
                _complete_fetch_node,
                _resolve_fetch_artifacts_dir,
            )

            artifacts_dir = _resolve_fetch_artifacts_dir(job_dir, node_id)
            primary_dst = artifacts_dir / f"modeller_prediction_{target_code_clean}.pdb"
            shutil.copy2(selected_path, primary_dst)

            digest = hashlib.sha256()
            digest.update(template_copy.read_bytes())
            digest.update(alignment_path.read_bytes())
            digest.update(str(num_models).encode())
            source_digest = digest.hexdigest()[:12]
            extra = {
                "template_pdb": str(template_path),
                "template_code": template_code_clean,
                "target_code": target_code_clean,
                "target_sequence": target_sequence,
                "alignment_file": str(Path(alignment_file).expanduser()) if alignment_file else None,
                "generated_alignment": str(alignment_path),
                "auto_align": auto_align,
                "num_models_requested": num_models,
                "num_successful_models": len(successful_models),
                "modeller_output_dir": str(out_dir),
                "selected_model": result["selected_model"],
                "hetatm": hetatm,
                "random_seed": random_seed,
            }
            _complete_fetch_node(
                job_dir,
                node_id,
                primary_dst,
                source_type="modeller",
                source_id=f"modeller_{source_digest}",
                file_format="pdb",
                extra_metadata=extra,
            )
            result["file_path"] = str(primary_dst)
        except Exception as e:
            msg = f"Failed to attach MODELLER prediction to fetch node: {type(e).__name__}: {e}"
            logger.error(msg)
            result["errors"].append(msg)
            fail_node(job_dir, node_id, errors=[msg])
            return result

    result["success"] = True
    logger.info("MODELLER job %s finished successfully", job_id)
    return result


# =============================================================================
# RDKit Tools
# =============================================================================


def rdkit_validate_smiles(smiles: str) -> dict:
    """Validate a SMILES string and convert to canonical form.

    This tool checks if a SMILES string is chemically valid and converts it
    to the canonical (standardized) form. Use this before passing SMILES to
    other tools like boltz2_protein_from_seq.

    Args:
        smiles: The SMILES string to validate

    Returns:
        Dict with:
            - success: bool - True if SMILES is valid
            - canonical_smiles: str - Standardized SMILES string (if valid)
            - errors: list[str] - Error messages if validation failed
    """
    logger.info(f"Validating SMILES: {smiles}")

    result = {
        "success": False,
        "canonical_smiles": None,
        "errors": []
    }

    if not smiles or not smiles.strip():
        result["errors"].append("Empty SMILES string provided")
        return result

    mol = Chem.MolFromSmiles(smiles)

    if mol is None:
        logger.error(f"Invalid SMILES string provided: {smiles}")
        result["errors"].append(f"Invalid SMILES: {smiles}")
        result["errors"].append("Hint: Check for syntax errors (unbalanced brackets, invalid atoms, etc.)")
        return result

    canonical_smiles = Chem.MolToSmiles(mol, canonical=True)
    logger.info(f"Validation successful. Canonical SMILES: {canonical_smiles}")

    result["success"] = True
    result["canonical_smiles"] = canonical_smiles
    return result



def pubchem_get_smiles_from_name(chemical_name: str) -> dict:
    """Get SMILES string from a chemical compound name using PubChem.

    Searches the PubChem database for a compound by its common name
    (e.g., 'aspirin', 'benzene', 'glucose') and returns the canonical SMILES.

    Args:
        chemical_name: The name of the chemical to search for

    Returns:
        Dict with:
            - success: bool - True if compound was found
            - smiles: str - Canonical SMILES string (if found)
            - compound_name: str - The search query
            - cid: int - PubChem Compound ID (if found)
            - errors: list[str] - Error messages if search failed
    """
    logger.info(f"Querying PubChem for name: {chemical_name}")

    result = {
        "success": False,
        "smiles": None,
        "compound_name": chemical_name,
        "cid": None,
        "errors": []
    }

    if not chemical_name or not chemical_name.strip():
        result["errors"].append("Empty chemical name provided")
        return result

    try:
        compounds = get_compounds(chemical_name, 'name')
        if not compounds:
            result["errors"].append(f"No compounds named '{chemical_name}' found in PubChem")
            result["errors"].append("Hint: Try alternative names or check spelling")
            return result

        result["success"] = True
        result["smiles"] = compounds[0].canonical_smiles
        result["cid"] = compounds[0].cid
        logger.info(f"Found SMILES: {result['smiles']}")
        return result

    except Exception as e:
        logger.error(f"PubChem search failed: {e}")
        result["errors"].append(f"PubChem search failed: {type(e).__name__}: {str(e)}")
        return result

# =============================================================================
# PLIP - Protein-Ligand Interaction Profiler
# =============================================================================


def analyze_plip_interactions(pdb_file: str) -> dict:
    """Analyze protein-ligand interactions using PLIP (Protein-Ligand Interaction Profiler).

    This tool uses PLIP to analyze non-covalent interactions between a protein
    and ligand(s) in a PDB structure. It detects:
    - Hydrogen bonds
    - Hydrophobic interactions
    - π-π stacking
    - π-cation interactions
    - Halogen bonds
    - Salt bridges
    - Metal coordination

    Args:
        pdb_file: Path to PDB file containing protein-ligand complex

    Returns:
        Dict with:
            - success: bool - True if analysis completed successfully
            - pdb_file: str - Input PDB file path
            - ligands: list[dict] - List of ligands detected, each with:
              - ligand_name: str - Ligand identifier (e.g., 'LIG:B:1')
              - interactions: dict - Interaction summary:
                - hydrogen_bonds: list[dict] - HB details
                - hydrophobic: list[dict] - Hydrophobic contacts
                - pi_stacking: list[dict] - π-π interactions
                - salt_bridges: list[dict] - Salt bridge interactions
            - errors: list[str] - Error messages if analysis failed
    """
    logger.info(f"Analyzing PLIP interactions for: {pdb_file}")

    result = {
        "success": False,
        "pdb_file": pdb_file,
        "ligands": [],
        "errors": []
    }

    if not pdb_file or not Path(pdb_file).exists():
        result["errors"].append(f"PDB file not found: {pdb_file}")
        return result

    try:
        from plip.structure.preparation import PDBComplex
    except ImportError:
        result["errors"].append("PLIP not installed. Install with: conda install -c bioconda plip")
        return result

    try:
        # Load protein structure using PLIP 3.0.0 API
        pdb_complex = PDBComplex()
        pdb_complex.load_pdb(str(pdb_file))
        pdb_complex.analyze()  # Required to populate interaction_sets
        logger.info(f"Loaded PDB complex with {len(pdb_complex.ligands)} ligand(s)")

        # Analyze interactions for each ligand
        for ligand_obj in pdb_complex.ligands:
            # Create ligand ID string
            ligand_id = f"{ligand_obj.hetid}:{ligand_obj.chain}:{ligand_obj.position}"
            logger.info(f"Analyzing ligand: {ligand_id}")

            interactions_dict = {
                "ligand_name": ligand_id,
                "interactions": {
                    "hydrogen_bonds": [],
                    "hydrophobic": [],
                    "pi_stacking": [],
                    "pi_cation": [],
                    "halogen_bonds": [],
                    "salt_bridges": [],
                    "metal_coordination": []
                }
            }

            # Get the binding site for this ligand
            # interaction_sets uses "HETID:CHAIN:POSITION" string keys
            interaction_key = f"{ligand_obj.hetid}:{ligand_obj.chain}:{ligand_obj.position}"
            if interaction_key in pdb_complex.interaction_sets:
                binding_site = pdb_complex.interaction_sets[interaction_key]

                # Extract hydrogen bonds (combine donor and acceptor types)
                hbonds = (binding_site.hbonds_ldon if hasattr(binding_site, 'hbonds_ldon') else []) + \
                         (binding_site.hbonds_pdon if hasattr(binding_site, 'hbonds_pdon') else [])
                for hbond in hbonds:
                    interactions_dict["interactions"]["hydrogen_bonds"].append({
                        "protein_residue": f"{hbond.resnr}{hbond.restype}",
                        "protein_chain": hbond.reschain,
                        "distance": round(hbond.distance_ad, 2)
                    })

                # Extract hydrophobic interactions
                hydrophobic = binding_site.hydrophobic_contacts if hasattr(binding_site, 'hydrophobic_contacts') else []
                for hydro in hydrophobic:
                    interactions_dict["interactions"]["hydrophobic"].append({
                        "protein_residue": f"{hydro.resnr}{hydro.restype}",
                        "protein_chain": hydro.reschain,
                        "distance": round(hydro.distance, 2)
                    })

                # Extract π-π stacking
                pi_stacking = binding_site.pistacking if hasattr(binding_site, 'pistacking') else []
                for pi_stack in pi_stacking:
                    interactions_dict["interactions"]["pi_stacking"].append({
                        "protein_residue": f"{pi_stack.resnr}{pi_stack.restype}",
                        "protein_chain": pi_stack.reschain,
                        "distance": round(pi_stack.distance, 2)
                    })

                # Extract π-cation interactions (combine aromatic and ligand aromatic types)
                pi_cation = (binding_site.pication_laro if hasattr(binding_site, 'pication_laro') else []) + \
                            (binding_site.pication_paro if hasattr(binding_site, 'pication_paro') else [])
                for pi_cat in pi_cation:
                    interactions_dict["interactions"]["pi_cation"].append({
                        "protein_residue": f"{pi_cat.resnr}{pi_cat.restype}",
                        "protein_chain": pi_cat.reschain,
                        "distance": round(pi_cat.distance, 2)
                    })

                # Extract halogen bonds
                halogen = binding_site.halogen_bonds if hasattr(binding_site, 'halogen_bonds') else []
                for halogen_bond in halogen:
                    interactions_dict["interactions"]["halogen_bonds"].append({
                        "protein_residue": f"{halogen_bond.resnr}{halogen_bond.restype}",
                        "protein_chain": halogen_bond.reschain,
                        "distance": round(halogen_bond.distance, 2)
                    })

                # Extract salt bridges (combine ligand negative and protein negative types)
                salt_bridges = (binding_site.saltbridge_lneg if hasattr(binding_site, 'saltbridge_lneg') else []) + \
                               (binding_site.saltbridge_pneg if hasattr(binding_site, 'saltbridge_pneg') else [])
                for salt_bridge in salt_bridges:
                    interactions_dict["interactions"]["salt_bridges"].append({
                        "protein_residue": f"{salt_bridge.resnr}{salt_bridge.restype}",
                        "protein_chain": salt_bridge.reschain,
                        "distance": round(salt_bridge.distance, 2)
                    })

                # Extract metal coordination
                metal = binding_site.metal_complexes if hasattr(binding_site, 'metal_complexes') else []
                for metal_complex in metal:
                    interactions_dict["interactions"]["metal_coordination"].append({
                        "protein_residue": f"{metal_complex.resnr}{metal_complex.restype}",
                        "protein_chain": metal_complex.reschain,
                        "distance": round(metal_complex.distance, 2)
                    })

            result["ligands"].append(interactions_dict)

        result["success"] = True
        logger.info(f"PLIP analysis completed for {len(result['ligands'])} ligand(s)")
        return result

    except Exception as e:
        logger.error(f"PLIP analysis failed: {e}")
        result["errors"].append(f"PLIP analysis failed: {type(e).__name__}: {str(e)}")
        return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "boltz2_protein_from_seq": boltz2_protein_from_seq,
    "modeller_from_alignment": modeller_from_alignment,
    "rdkit_validate_smiles": rdkit_validate_smiles,
    "pubchem_get_smiles_from_name": pubchem_get_smiles_from_name,
    "analyze_plip_interactions": analyze_plip_interactions,
}
