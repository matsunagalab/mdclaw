"""
Genesis Server - Boltz-2 structure generation from sequence.

Provides tools for:
- AI-driven protein structure prediction using Boltz-2
- Protein-ligand complex structure prediction with binding affinity
- SMILES validation and canonicalization using RDKit
- Chemical name to SMILES conversion using PubChem
- Similar compound search in PubChem database
- Drug-likeness assessment (Lipinski's Rule of Five)
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from servers._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import datetime  # noqa: E402
import json  # noqa: E402
import string  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Dict, Any, Optional  # noqa: E402

import pubchempy as pcp  # noqa: E402
import yaml  # noqa: E402
from rdkit import Chem  # noqa: E402
from rdkit.Chem import Descriptors  # noqa: E402

from servers._common import ensure_directory, create_unique_subdir, generate_job_id, get_current_session, BaseToolWrapper  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize Boltz-2 wrapper
CORRECT_CONDA_ENV = "mcp-md"
boltz_wrapper = BaseToolWrapper("boltz", conda_env=CORRECT_CONDA_ENV)


# =============================================================================
# Boltz-2 Structure Prediction
# =============================================================================


def boltz2_protein_from_seq(
    amino_acid_sequence_list: list[str],
    smiles_list: list[str],
    affinity: bool = False,
    output_dir: Optional[str] = None
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
        output_dir: Output directory. If None, creates output/boltz/.
                    When provided (e.g., session directory), creates a "boltz"
                    subdirectory within it.

    Returns:
        Dict with:
            - success: bool - True if prediction completed successfully
            - job_id: str - Unique identifier for this prediction job
            - output_dir: str - Path to output directory
            - input_yaml_path: str - Path to Boltz-2 input YAML file
            - predicted_pdb_files: list[str] - Paths to predicted PDB structure files
            - affinity_scores: dict | None - Binding affinity predictions if requested
              Contains:
              - affinity_probability_binary: Higher = more confident binding
              - affinity_pred_value: Lower (more negative) = stronger binding [kcal/mol]
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

    # Setup output directory with human-readable name
    # Always prefer session directory to ensure files go to the correct location
    session_dir = get_current_session()
    if session_dir:
        base_dir = session_dir
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "boltz")

    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": str(out_dir),
        "input_yaml_path": None,
        "predicted_pdb_files": [],
        "affinity_scores": None,
        "errors": [],
        "warnings": []
    }

    # Validate inputs
    if not amino_acid_sequence_list:
        result["errors"].append("At least one amino acid sequence is required")
        return result

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
        yaml_data['sequences'].append({
            'protein': {
                'id': protein_id,
                'sequence': sequence,
            }
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
        result["errors"].append("Hint: Install Boltz-2 or activate the mcp-md conda environment")
        return result

    # Run Boltz-2
    # TODO: Add configurable options for use_msa, num_models, etc.
    boltz_command = [
        boltz_executable_path,
        "predict",
        yaml_filename,
        "--use_msa_server",
        "--output_format", "pdb"
    ]

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
        return result

    except Exception as e:
        logger.error(f"Boltz-2 prediction failed: {e}")
        result["errors"].append(f"Boltz-2 prediction failed: {type(e).__name__}: {str(e)}")
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
# RDKit and PubChem Tools
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
        compounds = pcp.get_compounds(chemical_name, 'name')
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





def pubchem_search_similar(smiles: str, n_results: int = 5, threshold: int = 80) -> dict:
    """Search PubChem for molecules similar to the input SMILES.

    Performs a structural similarity search in PubChem to find compounds
    with similar chemical structures. Useful for finding analogs or
    alternative compounds for drug discovery.

    Args:
        smiles: Reference SMILES string to search against
        n_results: Maximum number of results to retrieve (default: 5)
        threshold: Similarity percentage threshold (0-100).
                   Higher values return only more similar molecules.
                   Recommended: 80-95 (default: 80)

    Returns:
        Dict with:
            - success: bool - True if search completed successfully
            - query_smiles: str - The input SMILES string
            - similar_compounds: list[dict] - List of similar compounds, each with:
              - smiles: str - Canonical SMILES
              - cid: int - PubChem Compound ID
            - count: int - Number of compounds found
            - errors: list[str] - Error messages if search failed
    """
    logger.info(f"Searching similar compounds for: {smiles}")

    result = {
        "success": False,
        "query_smiles": smiles,
        "similar_compounds": [],
        "count": 0,
        "errors": []
    }

    if not smiles or not smiles.strip():
        result["errors"].append("Empty SMILES string provided")
        return result

    try:
        compounds = pcp.get_compounds(
            smiles,
            namespace='smiles',
            searchtype='similarity',
            listkey_count=n_results,
            threshold=threshold
        )

        result["similar_compounds"] = [
            {"smiles": c.canonical_smiles, "cid": c.cid}
            for c in compounds[:n_results]
        ]
        result["count"] = len(result["similar_compounds"])
        result["success"] = True

        logger.info(f"Found {result['count']} similar compounds")
        return result

    except Exception as e:
        logger.error(f"PubChem similarity search failed: {e}")
        result["errors"].append(f"PubChem search failed: {type(e).__name__}: {str(e)}")
        return result


def rdkit_calc_druglikeness(smiles: str) -> dict:
    """Calculate drug-likeness properties using Lipinski's Rule of Five.

    Evaluates a molecule's potential as an orally active drug candidate based on
    Lipinski's Rule of Five criteria. Use this tool to filter out unsuitable
    candidates before performing computationally expensive simulations like Boltz-2.

    Lipinski's Rule of Five states that poor absorption is more likely when:
    - Molecular weight > 500 Da
    - LogP (lipophilicity) > 5
    - Hydrogen bond donors > 5
    - Hydrogen bond acceptors > 10

    Args:
        smiles: SMILES string of the molecule to evaluate

    Returns:
        Dict with:
            - success: bool - True if calculation completed
            - smiles: str - Input SMILES string
            - properties: dict - Calculated molecular properties:
              - molecular_weight: float - Molecular weight in Daltons (ideal: <= 500)
              - logp: float - Partition coefficient (ideal: <= 5)
              - h_donors: int - Number of H-bond donors (ideal: <= 5)
              - h_acceptors: int - Number of H-bond acceptors (ideal: <= 10)
            - passes_lipinski_rule: bool - True if all criteria are met
            - violations: list[str] - List of violated criteria
            - errors: list[str] - Error messages if calculation failed
    """
    logger.info(f"Calculating drug-likeness for: {smiles}")

    result = {
        "success": False,
        "smiles": smiles,
        "properties": {},
        "passes_lipinski_rule": False,
        "violations": [],
        "errors": []
    }

    if not smiles or not smiles.strip():
        result["errors"].append("Empty SMILES string provided")
        return result

    mol = Chem.MolFromSmiles(smiles)
    if not mol:
        result["errors"].append(f"Invalid SMILES: {smiles}")
        return result

    # Calculate properties
    mw = Descriptors.MolWt(mol)
    logp = Descriptors.MolLogP(mol)
    hbd = Descriptors.NumHDonors(mol)
    hba = Descriptors.NumHAcceptors(mol)

    result["properties"] = {
        "molecular_weight": round(mw, 2),
        "logp": round(logp, 2),
        "h_donors": hbd,
        "h_acceptors": hba
    }

    # Check Lipinski's Rule of Five
    if mw > 500:
        result["violations"].append(f"Molecular weight ({mw:.1f}) > 500")
    if logp > 5:
        result["violations"].append(f"LogP ({logp:.2f}) > 5")
    if hbd > 5:
        result["violations"].append(f"H-bond donors ({hbd}) > 5")
    if hba > 10:
        result["violations"].append(f"H-bond acceptors ({hba}) > 10")

    result["passes_lipinski_rule"] = len(result["violations"]) == 0
    result["success"] = True

    logger.info(f"Drug-likeness: {'PASS' if result['passes_lipinski_rule'] else 'FAIL'}")
    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "boltz2_protein_from_seq": boltz2_protein_from_seq,
    "rdkit_validate_smiles": rdkit_validate_smiles,
    "pubchem_get_smiles_from_name": pubchem_get_smiles_from_name,
    "pubchem_search_similar": pubchem_search_similar,
    "rdkit_calc_druglikeness": rdkit_calc_druglikeness,
}