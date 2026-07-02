"""genesis.chem submodule (behavior-preserving split)."""

from pathlib import Path
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

