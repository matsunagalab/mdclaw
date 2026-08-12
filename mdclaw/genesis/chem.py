"""genesis.chem submodule (behavior-preserving split)."""

from rdkit import Chem
from pubchempy import get_compounds

from mdclaw.genesis._base import (
    logger,
)



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
