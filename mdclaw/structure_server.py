"""
Structure Server - PDB retrieval and structure cleaning tools.

Provides tools for:
- Automatic retrieval of structure files from PDB/AlphaFold/PDB-REDO (prefers mmCIF)
- Chain separation and classification using gemmi
- Structure cleaning, missing residue modeling, water/heterogen removal, and protonation using PDBFixer
- Automatic detection of disulfide bonds and CYS->CYX renaming
- Mutation modeling with HPacker
- Ligand chemistry preparation with SMILES/SDF template matching
- LLM-friendly structure validation and error reporting at each step
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any, Tuple  # noqa: E402

from pdbfixer import PDBFixer  # noqa: E402
from openmm.app import PDBFile  # noqa: E402
from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    classify_glycan_residues,
    create_unique_subdir,
    create_validation_error,
    ensure_directory,
    generate_job_id,
    is_glycan_residue_name,
)
from mdclaw.research_server import (  # noqa: E402
    MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE,
    classify_nucleic_residues,
    modified_nucleic_support_report,
)

# Default working directory for prepare_complex when output_dir is not specified
WORKING_DIR = Path(".")
PDB_CHAIN_ID_POOL = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
)
_DEUTERIUM_FALLBACK_ATOM_NAME_RE = re.compile(r"^D[0-9]*$")
DEFAULT_TERMINAL_CAP_FORCEFIELD = "ff19SB"
SUPPORTED_N_TERMINAL_CAPS = {"ACE"}
SUPPORTED_C_TERMINAL_CAPS = {"NME"}
TERMINAL_CAP_RESIDUES = SUPPORTED_N_TERMINAL_CAPS | SUPPORTED_C_TERMINAL_CAPS

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")


def _pdb_chain_id_for_index(index: int) -> str:
    """Return a PDB-compatible one-character chain label.

    The label is intentionally not a canonical identity.  PDB only has one
    chain-ID column, so large assemblies must reuse labels and rely on the
    chain identity map for unambiguous component tracking.
    """
    return PDB_CHAIN_ID_POOL[index % len(PDB_CHAIN_ID_POOL)]


def _path_lookup_keys(path: str | Path | None) -> set[str]:
    """Return stable path spellings for joins across preparation steps."""
    if path is None:
        return set()
    p = Path(path)
    keys = {str(path), str(p)}
    try:
        keys.add(str(p.resolve()))
    except OSError:
        pass
    return keys


def _pdb_atom_descriptor(line: str) -> dict[str, Any]:
    """Return a compact, serializable descriptor for a PDB atom record."""
    chain = line[21].strip() if len(line) > 21 else ""
    return {
        "serial": line[6:11].strip(),
        "atom_name": line[12:16].strip(),
        "resname": line[17:20].strip(),
        "chain": chain,
        "resnum": line[22:26].strip(),
        "icode": line[26].strip() if len(line) > 26 else "",
        "element": line[76:78].strip() if len(line) >= 78 else "",
    }


def _is_deuterium_atom_record(line: str) -> bool:
    """Return True for experimental deuterium atom records in PDB text."""
    if not line.startswith(("ATOM", "HETATM")):
        return False
    atom_name = line[12:16].strip().upper()
    element = line[76:78].strip().upper() if len(line) >= 78 else ""
    if element == "D":
        return True
    if element:
        return False
    return bool(_DEUTERIUM_FALLBACK_ATOM_NAME_RE.fullmatch(atom_name))


def _component_disposition_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the v1 component disposition artifact payload."""
    excluded_entries = [entry for entry in entries if entry.get("action_taken") == "excluded"]
    isotope_atoms = sum(
        int(entry.get("atom_count", 0))
        for entry in excluded_entries
        if entry.get("classification") == "experimental_isotope"
    )
    excluded_atoms = sum(int(entry.get("atom_count", 0)) for entry in excluded_entries)
    return {
        "schema_version": "mdclaw.component_disposition.v1",
        "summary": {
            "experimental_isotope_atoms_excluded": isotope_atoms,
            "excluded_atom_count": excluded_atoms,
            "excluded_component_count": len(excluded_entries),
        },
        "entries": entries,
    }


def _exclude_deuterium_atoms_from_pdb(input_path: Path, output_path: Path) -> dict[str, Any]:
    """Write *output_path* with experimental deuterium atom records removed."""
    lines = input_path.read_text().splitlines()
    kept: list[str] = []
    excluded_atoms: list[dict[str, Any]] = []
    for line in lines:
        if _is_deuterium_atom_record(line):
            excluded_atoms.append(_pdb_atom_descriptor(line))
            continue
        kept.append(line)

    if excluded_atoms:
        output_path.write_text("\n".join(kept) + "\n")
        entries = [
            {
                "component_id": "experimental_isotope_deuterium",
                "classification": "experimental_isotope",
                "default_action": "exclude",
                "action_taken": "excluded",
                "atom_count": len(excluded_atoms),
                "reason": (
                    "Experimental deuterium atoms are excluded from the default "
                    "classical MD preparation path; standard hydrogens are rebuilt downstream."
                ),
                "sample_atoms": excluded_atoms[:20],
            }
        ]
    else:
        entries = []
    return _component_disposition_payload(entries)


def _reconcile_cyx_cys_in_pdb(pdb_file: str, disulfide_bonds: List[dict]) -> Dict[str, int]:
    """Rewrite CYS/CYX residue names in *pdb_file* to match *disulfide_bonds*.

    pdb2pqr geometrically detects SS-bonded cysteines and renames them to
    CYX independently of what ``clean_protein`` is told. When the caller
    supplies an explicit ``disulfide_pairs`` list (complete replacement),
    ``result["disulfide_bonds"]`` is the authoritative view and this
    helper brings the merged PDB in line with it:

    - CYX residues *not* in ``disulfide_bonds`` are demoted back to CYS
      (otherwise the Amber CYX template would be applied to a residue
      without an SS bond, leaving SG unprotonated — chemically wrong).
    - CYS residues that *are* in ``disulfide_bonds`` are promoted to CYX.

    Additionally, every final CYX residue has its ``HG`` thiol hydrogen
    stripped. SS-bonded cysteines have their SG bonded to another SG,
    not to a proton, and the Amber CYX template has no ``HG`` atom — a
    surviving HG fails template matching at openmmforcefields build time
    (and historically caused tleap to abort with
    ``FATAL: Atom .R<CYX N>.A<HG> does not have a type``).
    Observed for 5vm0_A and 7on5_A in the 2422-row batch.

    Runs unconditionally after merge; it is a no-op whenever the
    auto-detection path agrees with pdb2pqr (the common case).
    """
    target_cyx: set = set()
    for bond in disulfide_bonds:
        for key in ("cys1", "cys2"):
            entry = bond.get(key) or {}
            chain = entry.get("chain")
            resnum = entry.get("resnum")
            if chain is not None and resnum is not None:
                target_cyx.add((chain, int(resnum)))

    path = Path(pdb_file)
    lines = path.read_text().splitlines()
    out: List[str] = []
    renamed_to_cys = 0
    renamed_to_cyx = 0
    stripped_hg = 0

    for line in lines:
        if len(line) >= 27 and line.startswith(("ATOM", "HETATM")):
            resname = line[17:20].strip()
            chain = line[21].strip()
            try:
                resnum = int(line[22:26].strip())
            except ValueError:
                out.append(line)
                continue
            key = (chain, resnum)
            final_resname = resname
            if resname == "CYX" and key not in target_cyx:
                line = line[:17] + "CYS" + line[20:]
                final_resname = "CYS"
                renamed_to_cys += 1
            elif resname == "CYS" and key in target_cyx:
                line = line[:17] + "CYX" + line[20:]
                final_resname = "CYX"
                renamed_to_cyx += 1

            # Drop the thiol hydrogen from every CYX record. This covers
            # both the CYS→CYX promotion path above and pre-existing CYX
            # residues from pdb2pqr that still carry HG (which would fail
            # template matching against the Amber CYX residue template at
            # openmmforcefields build time).
            if final_resname == "CYX" and line[12:16].strip() == "HG":
                stripped_hg += 1
                continue
        out.append(line)

    path.write_text("\n".join(out) + ("\n" if lines and not lines[-1].endswith("\n") else ""))
    return {
        "renamed_to_cys": renamed_to_cys,
        "renamed_to_cyx": renamed_to_cyx,
        "stripped_hg_from_cyx": stripped_hg,
    }


def _merge_disulfide_pairs(
    ssbond_pairs: List[dict],
    distance_pairs: List[dict],
    select_chains: Optional[List[str]] = None,
) -> List[dict]:
    """Merge explicit SSBOND records with distance-based candidates.

    Dedupes on the unordered pair of ``(chain, resnum)``. When the same
    pair appears in both sources, the SSBOND entry wins but its
    ``source`` is updated to ``"pdb_ssbond+distance"`` and the measured
    ``distance_angstrom`` from the distance-based result is preferred
    (since the SSBOND column value may be absent for non-1555 symmetry).

    When ``select_chains`` is given, pairs are filtered to those where
    BOTH residues' chains are selected — pairs that span dropped chains
    cannot exist in the merged PDB downstream.
    """
    def _key(pair: dict) -> frozenset:
        return frozenset({
            (pair["cys1"]["chain"], pair["cys1"]["resnum"]),
            (pair["cys2"]["chain"], pair["cys2"]["resnum"]),
        })

    selected = set(select_chains) if select_chains else None

    def _passes_chain_filter(pair: dict) -> bool:
        if selected is None:
            return True
        return (
            pair["cys1"]["chain"] in selected
            and pair["cys2"]["chain"] in selected
        )

    merged: Dict[frozenset, dict] = {}
    for pair in ssbond_pairs:
        if not _passes_chain_filter(pair):
            continue
        merged[_key(pair)] = dict(pair)  # shallow copy

    for pair in distance_pairs:
        if not _passes_chain_filter(pair):
            continue
        k = _key(pair)
        if k in merged:
            existing = merged[k]
            existing["source"] = "pdb_ssbond+distance"
            if pair.get("distance_angstrom") is not None:
                existing["distance_angstrom"] = pair["distance_angstrom"]
        else:
            merged[k] = dict(pair)

    return list(merged.values())


# =============================================================================
# Known Ligand SMILES Dictionary (for template matching)
# =============================================================================
# These SMILES are from PDB Chemical Component Dictionary (CCD)
# Used as fallback when CCD API is unavailable

KNOWN_LIGAND_SMILES = {
    # Nucleotides and derivatives
    "ATP": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]1O",
    "ADP": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]1O",
    "AMP": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(O)=O)[C@@H](O)[C@H]1O",
    "GTP": "Nc1nc2c(ncn2[C@@H]2O[C@H](COP(O)(=O)OP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]2O)c(=O)[nH]1",
    "GDP": "Nc1nc2c(ncn2[C@@H]2O[C@H](COP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]2O)c(=O)[nH]1",
    
    # Coenzymes
    "NAD": "NC(=O)c1ccc[n+](c1)[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](O)[C@@H]2O)[C@@H](O)[C@H]1O",
    "NADP": "NC(=O)c1ccc[n+](c1)[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](OP(O)(O)=O)[C@@H]2O)[C@@H](O)[C@H]1O",
    "FAD": "Cc1cc2nc3c(=O)[nH]c(=O)nc-3n(C[C@H](O)[C@H](O)[C@H](O)COP(O)(=O)OP(O)(=O)OC[C@H]3O[C@@H](n4cnc5c(N)ncnc54)[C@H](O)[C@@H]3O)c2cc1C",
    "SAH": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](CSCC[C@H](N)C(=O)O)[C@@H](O)[C@H]1O",
    "SAM": "C[S+](CC[C@H](N)C(O)=O)C[C@H]1O[C@@H](n2cnc3c(N)ncnc32)[C@H](O)[C@@H]1O",
    
    # Phosphate derivatives
    "AP5": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OP(O)(=O)OP(O)(=O)OP(O)(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](O)[C@@H]2O)[C@@H](O)[C@H]1O",
    
    # Common drug-like molecules
    "HEM": "CC1=C(CCC(O)=O)C2=[N+]3C1=Cc1c(C)c(C=C)c4C=C5C(C)=C(C=C)C6=[N+]5[Fe-]3(n14)n1c(=C6)c(C)c(CCC(O)=O)c1=C2",
    
    # Add more as needed
}

# =============================================================================
# Ligand Preparation Helper Functions
# =============================================================================


def _fetch_smiles_from_ccd(ligand_id: str, timeout: int = 10) -> Optional[str]:
    """Fetch canonical SMILES from PDB Chemical Component Dictionary.
    
    Queries the RCSB PDB REST API to get the canonical SMILES for a ligand.
    This provides the "source of truth" for bond orders.
    
    Args:
        ligand_id: 3-letter ligand residue name (e.g., 'ATP', 'SAH')
        timeout: Request timeout in seconds
    
    Returns:
        Canonical SMILES string, or None if not found
    
    Example:
        >>> smiles = _fetch_smiles_from_ccd("ATP")
        >>> print(smiles)
        'c1nc(c2c(n1)n(cn2)[C@H]3[C@@H]([C@@H]([C@H](O3)COP...'
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests library not installed. Cannot fetch from CCD API.")
        return None
    
    ligand_id = ligand_id.upper().strip()
    url = f"https://data.rcsb.org/rest/v1/core/chemcomp/{ligand_id}"
    
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code != 200:
            logger.debug(f"CCD API returned status {response.status_code} for {ligand_id}")
            return None
        
        data = response.json()
        
        # rcsb_chem_comp_descriptor is a dict with keys: smiles, smilesstereo, in_ch_i, etc.
        rcsb_desc = data.get('rcsb_chem_comp_descriptor', {})
        if isinstance(rcsb_desc, dict):
            # Prefer stereochemistry-aware SMILES
            smiles = rcsb_desc.get('smilesstereo') or rcsb_desc.get('smiles')
            if smiles:
                logger.info(f"Fetched SMILES for {ligand_id} from CCD: {smiles[:50]}...")
                return smiles
        
        # Fallback: try pdbx_chem_comp_descriptor (list of descriptors)
        pdbx_desc = data.get('pdbx_chem_comp_descriptor', [])
        if isinstance(pdbx_desc, list):
            for desc in pdbx_desc:
                if isinstance(desc, dict):
                    desc_type = desc.get('type', '')
                    if 'SMILES' in desc_type.upper():
                        smiles = desc.get('descriptor')
                        if smiles:
                            logger.info(f"Fetched SMILES for {ligand_id} from CCD (pdbx): {smiles[:50]}...")
                            return smiles
        
        logger.debug(f"No SMILES found in CCD for {ligand_id}")
        return None
        
    except requests.exceptions.Timeout:
        logger.warning(f"CCD API request timed out for {ligand_id}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"CCD API request failed for {ligand_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error fetching SMILES from CCD for {ligand_id}: {e}")
        return None


def _get_ligand_smiles(ligand_id: str, user_smiles: Optional[str] = None, 
                       fetch_from_ccd: bool = True) -> Optional[str]:
    """Get SMILES for a ligand with fallback chain.
    
    Priority order:
    1. User-provided SMILES (if given)
    2. CCD API lookup (if fetch_from_ccd=True)
    3. KNOWN_LIGAND_SMILES dictionary
    
    Args:
        ligand_id: 3-letter ligand residue name
        user_smiles: User-provided SMILES (highest priority)
        fetch_from_ccd: Whether to query CCD API
    
    Returns:
        SMILES string, or None if not found
    """
    ligand_id = ligand_id.upper().strip()
    
    # Priority 1: User-provided SMILES
    if user_smiles:
        logger.info(f"Using user-provided SMILES for {ligand_id}")
        return user_smiles
    
    # Priority 2: CCD API
    if fetch_from_ccd:
        smiles = _fetch_smiles_from_ccd(ligand_id)
        if smiles:
            return smiles
    
    # Priority 3: Known ligands dictionary
    if ligand_id in KNOWN_LIGAND_SMILES:
        logger.info(f"Using known SMILES for {ligand_id} from dictionary")
        return KNOWN_LIGAND_SMILES[ligand_id]
    
    logger.warning(f"No SMILES found for ligand {ligand_id}")
    return None


def _assign_bond_orders_from_smiles(pdb_mol, smiles: str):
    """Assign correct bond orders to PDB molecule using SMILES template.
    
    This is the key function for robust ligand preparation. It takes a molecule
    read from PDB (which may have incorrect/missing bond orders) and assigns
    the correct bond orders from a SMILES template.
    
    Args:
        pdb_mol: RDKit molecule from PDB (with coordinates but uncertain bonds)
        smiles: Canonical SMILES with correct bond orders
    
    Returns:
        RDKit molecule with correct bond orders and original coordinates
    
    Raises:
        ValueError: If template matching fails (atom count mismatch, etc.)
    
    Example:
        >>> pdb_mol = Chem.MolFromPDBFile("ligand.pdb", sanitize=False, removeHs=False)
        >>> correct_mol = _assign_bond_orders_from_smiles(pdb_mol, "c1ccccc1O")
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem
    
    # Create template molecule from SMILES
    template = Chem.MolFromSmiles(smiles)
    if template is None:
        raise ValueError(f"Invalid SMILES: {smiles}")
    
    # Add hydrogens to template for matching
    template = Chem.AddHs(template)
    
    # Assign bond orders from template to PDB molecule
    try:
        new_mol = AllChem.AssignBondOrdersFromTemplate(template, pdb_mol)
    except Exception as e:
        raise ValueError(f"Template matching failed: {e}. "
                        f"PDB atoms: {pdb_mol.GetNumAtoms()}, "
                        f"Template atoms: {template.GetNumAtoms()}")
    
    # Sanitize the molecule to ensure chemical validity
    try:
        Chem.SanitizeMol(new_mol)
    except Exception as e:
        raise ValueError(f"Sanitization failed after template matching: {e}")
    
    logger.info("Successfully assigned bond orders from SMILES template")
    return new_mol


def _optimize_ligand_rdkit(mol, max_iters: int = 200, force_field: str = "MMFF94") -> Tuple[Any, bool]:
    """Optimize ligand structure using RDKit force field.
    
    Light structure optimization to relax strained crystal structures before
    emitting ligand chemistry records for topology generation.
    
    Args:
        mol: RDKit molecule with 3D coordinates
        max_iters: Maximum optimization iterations
        force_field: Force field to use ("MMFF94" or "UFF")
    
    Returns:
        Tuple of (optimized molecule, success flag)
    
    Example:
        >>> mol = Chem.MolFromMolFile("ligand.sdf")
        >>> opt_mol, success = _optimize_ligand_rdkit(mol)
    """
    from rdkit.Chem import AllChem
    
    # Ensure molecule has 3D coordinates
    if mol.GetNumConformers() == 0:
        logger.warning("Molecule has no conformers, generating 3D coordinates")
        AllChem.EmbedMolecule(mol, AllChem.ETKDGv3())
    
    success = False
    
    if force_field.upper() == "MMFF94":
        # Try MMFF94 (better for drug-like molecules)
        try:
            ff = AllChem.MMFFGetMoleculeForceField(mol, AllChem.MMFFGetMoleculeProperties(mol))
            if ff is not None:
                result = ff.Minimize(maxIts=max_iters)
                success = (result == 0)  # 0 = converged
                logger.info(f"MMFF94 optimization {'converged' if success else 'did not converge'}")
            else:
                logger.warning("MMFF94 force field setup failed, trying UFF")
                force_field = "UFF"
        except Exception as e:
            logger.warning(f"MMFF94 optimization failed: {e}, trying UFF")
            force_field = "UFF"
    
    if force_field.upper() == "UFF":
        # Fallback to UFF (more general)
        try:
            result = AllChem.UFFOptimizeMolecule(mol, maxIters=max_iters)
            success = (result == 0)
            logger.info(f"UFF optimization {'converged' if success else 'did not converge'}")
        except Exception as e:
            logger.warning(f"UFF optimization failed: {e}")
            success = False
    
    return mol, success


def _apply_ph_protonation(smiles: str, target_ph: float = 7.4) -> Tuple[str, int]:
    """Apply pH-dependent protonation state to SMILES using Dimorphite-DL.
    
    This converts neutral CCD SMILES to the correct protonation state at the target pH.
    For example:
    - Carboxylic acids (COOH) → Carboxylates (COO-) at pH 7.4
    - Primary amines (NH2) → Protonated amines (NH3+) at pH 7.4
    
    Args:
        smiles: Input SMILES (typically neutral from CCD)
        target_ph: Target pH for protonation (default: 7.4)
    
    Returns:
        Tuple of (protonated_smiles, net_charge)
    
    Example:
        >>> smiles = "CC(=O)O"  # Acetic acid (neutral)
        >>> prot_smiles, charge = _apply_ph_protonation(smiles, 7.4)
        >>> print(prot_smiles, charge)  # "CC(=O)[O-]", -1
    """
    try:
        from dimorphite_dl import protonate_smiles
        from rdkit import Chem
        
        logger.info(f"Applying pH {target_ph} protonation to SMILES...")
        
        # Run Dimorphite-DL
        # Use narrow pH range (ph_min=ph_max) and max_variants=1 to get single dominant state
        # precision=1.0 (default) represents 1 standard deviation from mean pKa
        protonated_smiles_list = protonate_smiles(
            smiles,
            ph_min=target_ph,
            ph_max=target_ph,
            precision=1.0,  # Default: 1 std dev from mean pKa
            max_variants=1  # Only get the most likely state
        )
        
        if not protonated_smiles_list:
            logger.warning("Dimorphite-DL returned no results, using original SMILES")
            # Calculate charge from original
            mol = Chem.MolFromSmiles(smiles)
            net_charge = Chem.GetFormalCharge(mol) if mol else 0
            return smiles, net_charge
        
        # Take the first (most probable) protonation state
        protonated_smiles = protonated_smiles_list[0]
        
        # Calculate net charge from protonated SMILES
        mol = Chem.MolFromSmiles(protonated_smiles)
        if mol is None:
            logger.warning(f"Invalid protonated SMILES: {protonated_smiles}, using original")
            mol = Chem.MolFromSmiles(smiles)
            net_charge = Chem.GetFormalCharge(mol) if mol else 0
            return smiles, net_charge
        
        net_charge = Chem.GetFormalCharge(mol)
        
        logger.info(f"Protonation result: {smiles[:30]}... → {protonated_smiles[:30]}... (charge: {net_charge})")
        
        return protonated_smiles, net_charge
        
    except ImportError:
        logger.warning("Dimorphite-DL not installed, falling back to estimate_net_charge")
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if mol:
            # Use simple estimation as fallback
            charge_info = _estimate_charge_rdkit(mol)
            net_charge = _estimate_physiological_charge(charge_info, target_ph)
        else:
            net_charge = 0
        return smiles, net_charge
    except Exception as e:
        logger.warning(f"Dimorphite-DL failed: {e}, using original SMILES")
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        net_charge = Chem.GetFormalCharge(mol) if mol else 0
        return smiles, net_charge


def _estimate_charge_rdkit(mol) -> Dict[str, Any]:
    """Estimate net charge using RDKit.
    
    Args:
        mol: RDKit molecule object
    
    Returns:
        Dict with charge estimation results
    """
    from rdkit import Chem
    
    result = {
        "formal_charge": Chem.GetFormalCharge(mol),
        "ionizable_groups": [],
        "method": "rdkit"
    }
    
    # Identify ionizable groups
    # Carboxylic acids (typically deprotonated at pH 7.4)
    # Use the simplest working pattern
    carboxylic_pattern = Chem.MolFromSmarts("C(=O)O")
    if carboxylic_pattern is not None:
        matches = mol.GetSubstructMatches(carboxylic_pattern)
        if matches:
            result["ionizable_groups"].append({
                "type": "carboxylic_acid",
                "count": len(matches),
                "typical_charge": -1,
                "pka_range": "3-5"
            })
    
    # Primary amines (typically protonated at pH 7.4)
    # Excludes amides (NC=O) and aromatic amines (lower pKa)
    amine_pattern = Chem.MolFromSmarts("[NX3;H2;!$(NC=O);!$(Nc)]")
    if amine_pattern and mol.HasSubstructMatch(amine_pattern):
        matches = mol.GetSubstructMatches(amine_pattern)
        result["ionizable_groups"].append({
            "type": "primary_amine",
            "count": len(matches),
            "typical_charge": +1,
            "pka_range": "9-11"
        })
    
    # Secondary amines (excludes amides and aromatic)
    sec_amine_pattern = Chem.MolFromSmarts("[NX3;H1;!$(NC=O);!$(Nc)]([#6])[#6]")
    if sec_amine_pattern and mol.HasSubstructMatch(sec_amine_pattern):
        matches = mol.GetSubstructMatches(sec_amine_pattern)
        result["ionizable_groups"].append({
            "type": "secondary_amine",
            "count": len(matches),
            "typical_charge": +1,
            "pka_range": "9-11"
        })
    
    # Tertiary amines (excludes amides)
    tert_amine_pattern = Chem.MolFromSmarts("[NX3;H0;!$(NC=O);!$(Nc)]([#6])([#6])[#6]")
    if tert_amine_pattern and mol.HasSubstructMatch(tert_amine_pattern):
        matches = mol.GetSubstructMatches(tert_amine_pattern)
        result["ionizable_groups"].append({
            "type": "tertiary_amine",
            "count": len(matches),
            "typical_charge": +1,
            "pka_range": "9-11"
        })
    
    # Phenols
    phenol_pattern = Chem.MolFromSmarts("[OX2H1]c1ccccc1")
    if mol.HasSubstructMatch(phenol_pattern):
        matches = mol.GetSubstructMatches(phenol_pattern)
        result["ionizable_groups"].append({
            "type": "phenol",
            "count": len(matches),
            "typical_charge": 0,
            "pka_range": "9-10"
        })
    
    # Sulfonic acids
    sulfonic_pattern = Chem.MolFromSmarts("[SX4](=O)(=O)[OX1H1]")
    if mol.HasSubstructMatch(sulfonic_pattern):
        matches = mol.GetSubstructMatches(sulfonic_pattern)
        result["ionizable_groups"].append({
            "type": "sulfonic_acid",
            "count": len(matches),
            "typical_charge": -1,
            "pka_range": "<1"
        })
    
    # Phosphates
    phosphate_pattern = Chem.MolFromSmarts("[PX4](=O)([OX1H1])([OX1H1])")
    if mol.HasSubstructMatch(phosphate_pattern):
        matches = mol.GetSubstructMatches(phosphate_pattern)
        result["ionizable_groups"].append({
            "type": "phosphate",
            "count": len(matches),
            "typical_charge": -2,
            "pka_range": "2, 7"
        })
    
    return result


def _estimate_physiological_charge(charge_info: Dict[str, Any], ph: float = 7.4) -> int:
    """Estimate net charge at physiological pH.
    
    Args:
        charge_info: Output from _estimate_charge_rdkit
        ph: Target pH
    
    Returns:
        Estimated integer net charge
    """
    estimated_charge = charge_info["formal_charge"]
    
    for group in charge_info.get("ionizable_groups", []):
        group_type = group["type"]
        count = group["count"]
        
        # Adjust based on typical protonation at pH 7.4
        if group_type in ["carboxylic_acid", "sulfonic_acid"]:
            # Typically deprotonated (negative)
            estimated_charge -= count
        elif group_type in ["primary_amine", "secondary_amine"]:
            # Typically protonated (positive) 
            estimated_charge += count
        elif group_type == "phosphate":
            # Typically -2 at pH 7.4
            estimated_charge -= 2 * count
    
    return estimated_charge


def _extract_histidine_states(pdb_file: Path) -> dict:
    """Extract histidine protonation states from PDB file.

    Parses the PDB file to identify HID, HIE, and HIP residues assigned
    by pdb2pqr/propka.

    Args:
        pdb_file: Path to PDB file with protonation assigned

    Returns:
        Dict mapping residue identifier to protonation state
        e.g., {"A:126": "HIE", "A:134": "HID", "B:172": "HIP"}
    """
    his_states = {}
    try:
        with open(pdb_file) as f:
            for line in f:
                if line.startswith(("ATOM", "HETATM")):
                    resname = line[17:20].strip()
                    if resname in ("HID", "HIE", "HIP"):
                        chain = line[21].strip() or "A"
                        resnum = line[22:26].strip()
                        key = f"{chain}:{resnum}"
                        if key not in his_states:
                            his_states[key] = resname
    except Exception as e:
        logger.warning(f"Could not extract histidine states: {e}")
    return his_states


def _apply_histidine_states(pdb_file: Path, histidine_states: dict) -> None:
    """Apply user-specified histidine protonation states to a PDB file.

    Renames HIS/HID/HIE/HIP residues to the user-specified state.
    Modifies the file in place.

    Args:
        pdb_file: Path to PDB file to modify
        histidine_states: Dict mapping "chain:resnum" to state ("HID", "HIE", "HIP")
                         e.g., {"A:126": "HIE", "A:152": "HID"}
    """
    if not histidine_states:
        return

    try:
        with open(pdb_file) as f:
            lines = f.readlines()

        modified_lines = []
        for line in lines:
            if line.startswith(("ATOM", "HETATM")):
                resname = line[17:20].strip()
                if resname in ("HIS", "HID", "HIE", "HIP"):
                    chain = line[21].strip() or "A"
                    resnum = line[22:26].strip()
                    key = f"{chain}:{resnum}"
                    if key in histidine_states:
                        new_state = histidine_states[key]
                        # Replace residue name (columns 18-20, 1-indexed)
                        line = line[:17] + f"{new_state:>3}" + line[20:]
            modified_lines.append(line)

        with open(pdb_file, 'w') as f:
            f.writelines(modified_lines)

        logger.info(f"Applied {len(histidine_states)} histidine state(s) to {pdb_file}")
    except Exception as e:
        logger.warning(f"Could not apply histidine states: {e}")


_PROTONATION_STATE_ALIASES = {
    "HSD": "HID",
    "HSE": "HIE",
    "HSP": "HIP",
}

_PROTONATION_STATE_SPECS: Dict[str, Dict[str, Any]] = {
    "ASP": {
        "base": "ASP",
        "modeller_variant": "ASP",
        "input_names": {"ASP", "ASH"},
        "present": set(),
        "absent": {"HD2"},
    },
    "ASH": {
        "base": "ASP",
        "modeller_variant": "ASH",
        "input_names": {"ASP", "ASH"},
        "present": {"HD2"},
        "absent": set(),
    },
    "GLU": {
        "base": "GLU",
        "modeller_variant": "GLU",
        "input_names": {"GLU", "GLH"},
        "present": set(),
        "absent": {"HE2"},
    },
    "GLH": {
        "base": "GLU",
        "modeller_variant": "GLH",
        "input_names": {"GLU", "GLH"},
        "present": {"HE2"},
        "absent": set(),
    },
    "HID": {
        "base": "HIS",
        "modeller_variant": "HID",
        "input_names": {"HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP"},
        "present": {"HD1"},
        "absent": {"HE2"},
    },
    "HIE": {
        "base": "HIS",
        "modeller_variant": "HIE",
        "input_names": {"HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP"},
        "present": {"HE2"},
        "absent": {"HD1"},
    },
    "HIP": {
        "base": "HIS",
        "modeller_variant": "HIP",
        "input_names": {"HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP"},
        "present": {"HD1", "HE2"},
        "absent": set(),
    },
    "LYS": {
        "base": "LYS",
        "modeller_variant": "LYS",
        "input_names": {"LYS", "LYN"},
        "present": {"HZ3"},
        "absent": set(),
    },
    "LYN": {
        "base": "LYS",
        "modeller_variant": "LYN",
        "input_names": {"LYS", "LYN"},
        "present": set(),
        "absent": {"HZ3"},
    },
    "CYS": {
        "base": "CYS",
        "modeller_variant": "CYS",
        "input_names": {"CYS", "CYM", "CYX"},
        "present": {"HG"},
        "absent": set(),
    },
    "CYX": {
        "base": "CYS",
        "modeller_variant": "CYX",
        "input_names": {"CYS", "CYM", "CYX"},
        "present": set(),
        "absent": {"HG"},
    },
    # OpenMM's hydrogen-definition variant for a deprotonated cysteine and
    # disulfide cysteine is the same no-HG pattern (CYX). Amber's force-field
    # template distinguishes the thiolate as CYM, so we stamp CYM after H
    # rebuilding while asking Modeller for the CYX hydrogen pattern.
    "CYM": {
        "base": "CYS",
        "modeller_variant": "CYX",
        "input_names": {"CYS", "CYM", "CYX"},
        "present": set(),
        "absent": {"HG"},
    },
}


def _parse_protonation_site_key(key: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in str(key).split(":")]
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise ValueError(
            "Protonation site keys must be '<chain>:<resnum>' or "
            "'<chain>:<resnum>:<icode>'"
        )
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def _canonical_protonation_state(state: Any) -> str:
    canonical = _PROTONATION_STATE_ALIASES.get(str(state).strip().upper(), str(state).strip().upper())
    if canonical not in _PROTONATION_STATE_SPECS:
        supported = ", ".join(sorted(_PROTONATION_STATE_SPECS))
        raise ValueError(f"Unsupported protonation state {state!r}. Supported states: {supported}")
    return canonical


def _normalize_protonation_state_overrides(
    protonation_states: Optional[Dict[str, Any]] = None,
    histidine_states: Optional[dict[str, str]] = None,
) -> list[dict[str, str]]:
    """Normalize user-specified site protonation overrides.

    Supported inputs:
    - [{"chain": "A", "resnum": 57, "state": "HIP"}, ...]
    - {"A:57": "HIP", "A:25": "ASH"}
    - legacy histidine_states={"A:57": "HIP"}
    """
    records: list[dict[str, str]] = []

    def add_record(chain: Any, resnum: Any, state: Any, icode: Any = "") -> None:
        if chain is None or str(chain).strip() == "":
            raise ValueError("Protonation state records require a non-empty 'chain'")
        if resnum is None or str(resnum).strip() == "":
            raise ValueError("Protonation state records require a non-empty 'resnum'")
        records.append({
            "chain": str(chain).strip(),
            "resnum": str(resnum).strip(),
            "icode": str(icode or "").strip(),
            "state": _canonical_protonation_state(state),
        })

    if isinstance(protonation_states, dict):
        for key, state in protonation_states.items():
            chain, resnum, icode = _parse_protonation_site_key(str(key))
            add_record(chain, resnum, state, icode)
    elif isinstance(protonation_states, list):
        for entry in protonation_states:
            if not isinstance(entry, dict):
                raise ValueError("Each protonation state entry must be a dict")
            add_record(
                entry.get("chain"),
                entry.get("resnum", entry.get("residue_number")),
                entry.get("state", entry.get("protonation_state")),
                entry.get("icode", entry.get("insertion_code", "")),
            )
    elif protonation_states is not None:
        raise ValueError("protonation_states must be a dict, list of dicts, or None")

    if histidine_states:
        for key, state in histidine_states.items():
            chain, resnum, icode = _parse_protonation_site_key(str(key))
            add_record(chain, resnum, state, icode)

    deduped: dict[tuple[str, str, str], dict[str, str]] = {}
    for record in records:
        key = (record["chain"], record["resnum"], record["icode"])
        prior = deduped.get(key)
        if prior and prior["state"] != record["state"]:
            raise ValueError(
                f"Conflicting protonation states for {record['chain']}:{record['resnum']}"
                f"{(':' + record['icode']) if record['icode'] else ''}: "
                f"{prior['state']} vs {record['state']}"
            )
        deduped[key] = record
    return list(deduped.values())


def _apply_protonation_states_with_modeller(
    pdb_file: Path,
    protonation_states: list[dict[str, str]],
    ph: float = 7.4,
) -> dict:
    """Rebuild user-specified residue protonation states with OpenMM Modeller.

    The input PDB is modified in place.  Residue names are canonicalized only
    transiently so ``Modeller.addHydrogens(variants=...)`` can apply the
    desired hydrogen pattern, then stamped back to the Amber variant name.
    """
    result: dict[str, Any] = {
        "success": False,
        "applied_states": [],
        "histidine_states": {},
        "errors": [],
        "warnings": [],
    }
    if not protonation_states:
        result["success"] = True
        return result

    try:
        from openmm.app import Modeller
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"OpenMM Modeller is required for protonation states: {exc}")
        return result

    try:
        pdb = PDBFile(str(pdb_file))
        modeller = Modeller(pdb.topology, pdb.positions)
        residues = list(modeller.topology.residues())

        residue_by_site: dict[tuple[str, str, str], Any] = {}
        for residue in residues:
            site = (
                str(residue.chain.id).strip(),
                str(residue.id).strip(),
                str(getattr(residue, "insertionCode", "") or "").strip(),
            )
            residue_by_site[site] = residue

        variants: list[Optional[str]] = [None] * len(residues)
        matched: dict[int, dict[str, str]] = {}

        for record in protonation_states:
            site = (record["chain"], record["resnum"], record.get("icode", ""))
            residue = residue_by_site.get(site)
            if residue is None:
                result["errors"].append(
                    f"Protonation target not found: {record['chain']}:{record['resnum']}"
                    f"{(':' + record.get('icode', '')) if record.get('icode') else ''}"
                )
                continue
            state = record["state"]
            spec = _PROTONATION_STATE_SPECS[state]
            current_name = str(residue.name).strip().upper()
            if current_name not in spec["input_names"]:
                result["errors"].append(
                    f"State {state} is incompatible with residue {current_name} at "
                    f"{record['chain']}:{record['resnum']}; expected one of "
                    f"{sorted(spec['input_names'])}"
                )
                continue

            residue.name = spec["base"]
            variants[residue.index] = spec["modeller_variant"]
            matched[residue.index] = record

        if result["errors"]:
            return result

        actual_variants = modeller.addHydrogens(pH=ph, variants=variants)
        rebuilt_residues = list(modeller.topology.residues())

        for residue_index, record in matched.items():
            state = record["state"]
            residue = rebuilt_residues[residue_index]
            residue.name = state
            atoms = {atom.name for atom in residue.atoms()}
            spec = _PROTONATION_STATE_SPECS[state]
            missing = sorted(spec["present"] - atoms)
            forbidden = sorted(spec["absent"] & atoms)
            if missing or forbidden:
                result["errors"].append(
                    f"Protonation validation failed for {record['chain']}:{record['resnum']} "
                    f"as {state}: missing={missing}, forbidden_present={forbidden}"
                )
                continue
            applied = {
                "chain": record["chain"],
                "resnum": record["resnum"],
                "icode": record.get("icode", ""),
                "state": state,
                "modeller_variant": str(actual_variants[residue_index] or ""),
            }
            result["applied_states"].append(applied)
            if state in {"HID", "HIE", "HIP"}:
                key = f"{record['chain']}:{record['resnum']}"
                if record.get("icode"):
                    key += f":{record['icode']}"
                result["histidine_states"][key] = state

        if result["errors"]:
            return result

        tmp_file = pdb_file.with_suffix(pdb_file.suffix + ".protonation.tmp")
        with tmp_file.open("w") as fh:
            PDBFile.writeFile(modeller.topology, modeller.positions, fh, keepIds=True)
        variant_names = set(_PROTONATION_STATE_SPECS)
        normalized_lines = []
        for line in tmp_file.read_text().splitlines(keepends=True):
            if line.startswith("HETATM") and line[17:20].strip().upper() in variant_names:
                line = "ATOM  " + line[6:]
            normalized_lines.append(line)
        tmp_file.write_text("".join(normalized_lines))
        tmp_file.replace(pdb_file)
        result["success"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"OpenMM Modeller protonation rebuild failed: {type(exc).__name__}: {exc}")
        return result


# Define standard amino acids and water (module-level constants for reuse)
AMINO_ACIDS = {
    'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS', 
    'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP', 
    'TYR', 'VAL', 'SEC', 'PYL'
}
AMBER_PROTEIN_RESIDUES = {
    "HID", "HIE", "HIP", "HSD", "HSE", "HSP", "CYX", "CYM",
    "ASH", "GLH", "LYN", "ACE", "NME",
}
WATER_NAMES = {'HOH', 'WAT', 'H2O', 'DOD', 'D2O'}


def _inspect_molecules_impl(structure_file: str) -> dict:
    """Inspect an mmCIF or PDB structure file and return detailed molecular information.
    
    This tool examines a structure file without modifying it, returning comprehensive
    information about each chain/molecule including its type (protein, ligand, water, etc.),
    residue composition, identifiers, and metadata from the file header (when available).
    
    Use this tool to:
    - Understand the composition of a structure before splitting
    - Identify which chains are proteins vs ligands vs water vs ions
    - Get molecular names and descriptions from the header
    - Get chain IDs for selective extraction with split_molecules
    
    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb/.ent) file to inspect.
    
    Returns:
        Dict with:
            - success: bool - True if inspection completed successfully
            - source_file: str - Original input file path
            - file_format: str - Detected file format ('cif' or 'pdb')
            - header: dict - Header information (if available):
                - pdb_id: str - PDB identifier
                - title: str - Structure title
                - deposition_date: str - Date of deposition
                - resolution: float - Resolution in Angstroms (for X-ray)
                - experiment_method: str - Experimental method (X-RAY, NMR, etc.)
            - entities: list[dict] - Entity information from header:
                - entity_id: str - Entity identifier
                - name: str - Entity name/description (e.g., "ADENYLATE KINASE")
                - entity_type: str - Type (polymer, non-polymer, water)
                - polymer_type: str - For polymers (polypeptide(L), polyribonucleotide, etc.)
                - chain_ids: list[str] - Chain IDs belonging to this entity
            - num_models: int - Number of models in the structure
            - chains: list[dict] - Detailed information for each chain:
                - chain_id: str - Unique chain identifier (label_asym_id for mmCIF)
                - author_chain: str - Original author chain ID (auth_asym_id)
                - entity_id: str - Entity ID this chain belongs to
                - entity_name: str - Name of the entity (from header)
                - chain_type: str - Classification ('protein', 'ligand', 'water', 'ion')
                - is_protein: bool - True if chain contains protein residues
                - is_water: bool - True if chain is water molecules
                - num_residues: int - Number of residues in the chain
                - num_atoms: int - Number of atoms in the chain
                - residue_names: list[str] - Unique residue names in the chain
                - sequence: str - One-letter sequence (for proteins only)
            - summary: dict - Quick overview. Exposes BOTH chain-ID
              systems so callers can pick the right one for
              ``select_chains``:
                - num_protein_chains: int
                - num_ligand_chains: int
                - num_water_chains: int
                - num_ion_chains: int
                - total_chains: int
                # Pass these to ``select_chains`` (label_asym_id, the
                # simple PDB-style ID used by RCSB / SabDab):
                - protein_label_ids: list[str]
                - ligand_label_ids: list[str]
                - water_label_ids: list[str]
                - ion_label_ids: list[str]
                # Author IDs (auth_asym_id) — for display and provenance,
                # kept under the historical field names for backward
                # compatibility. split_molecules will still accept these
                # via its author_chain fallback:
                - protein_chain_ids: list[str]
                - ligand_chain_ids: list[str]
                - water_chain_ids: list[str]
                - ion_chain_ids: list[str]
                # Explicit mapping, useful when label and author disagree
                # (e.g. 7QVK label "B" ↔ auth "BBB"; 7NMU label "C" ↔
                # auth "DDD" — the mapping can even be reordered):
                - chain_id_map: dict[str, str]  # label -> author
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Inspecting molecules in: {structure_file}")
    
    # Initialize result structure
    result = {
        "success": False,
        "source_file": str(structure_file),
        "file_format": None,
        "header": {},
        "entities": [],
        "num_models": 0,
        "chains": [],
        "summary": {
            "num_protein_chains": 0,
            "num_nucleic_chains": 0,
            "num_glycan_chains": 0,
            "num_ligand_chains": 0,
            "num_water_chains": 0,
            "num_ion_chains": 0,
            "total_chains": 0,
            "protein_chain_ids": [],
            "nucleic_chain_ids": [],
            "glycan_chain_ids": [],
            "ligand_chain_ids": [],
            "water_chain_ids": [],
            "ion_chain_ids": [],
            "modified_nucleic_support_status": "not_detected",
            "modified_nucleic_support": modified_nucleic_support_report([]),
            "unsupported_modified_nucleic_residues": [],
        },
        "errors": [],
        "warnings": []
    }
    
    # Check for gemmi dependency
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        result["errors"].append("Hint: Install with: pip install gemmi")
        logger.error("gemmi not installed")
        return result
    
    # Validate input file
    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        logger.error(f"Structure file not found: {structure_file}")
        return result
    
    suffix = structure_path.suffix.lower()
    if suffix not in ['.cif', '.pdb', '.ent']:
        result["errors"].append(f"Unsupported file format: {suffix}")
        result["errors"].append("Hint: Supported formats are .cif, .pdb, and .ent")
        logger.error(f"Unsupported file format: {suffix}")
        return result
    
    result["file_format"] = "cif" if suffix == ".cif" else "pdb"
    
    try:
        # Read structure with gemmi
        logger.info(f"Reading structure with gemmi ({suffix})...")
        if suffix == '.cif':
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            structure = gemmi.make_structure_from_block(block)
        else:  # .pdb or .ent
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()
        
        result["num_models"] = len(structure)
        
        # Extract header information
        header_info = {}
        if structure.name:
            header_info["pdb_id"] = structure.name
        if hasattr(structure, 'info') and structure.info:
            # For mmCIF files, info contains metadata
            if '_struct.title' in structure.info:
                header_info["title"] = structure.info['_struct.title']
        # Try to get resolution
        if structure.resolution > 0:
            header_info["resolution"] = round(structure.resolution, 2)
        # Get spacegroup (indicates X-ray)
        if structure.spacegroup_hm:
            header_info["spacegroup"] = structure.spacegroup_hm
            header_info["experiment_method"] = "X-RAY DIFFRACTION"
        # Check for NMR models
        elif len(structure) > 1:
            header_info["experiment_method"] = "SOLUTION NMR"
        
        result["header"] = header_info
        
        # Extract entity information from structure
        entities_info = []
        entity_name_map = {}  # entity_id -> name mapping
        entity_subchains = {}  # entity_id -> list of chain_ids
        
        for entity in structure.entities:
            entity_id = entity.name if entity.name else str(len(entities_info) + 1)
            
            # Get entity type as string
            entity_type_str = str(entity.entity_type).replace("EntityType.", "").lower()
            
            # Get polymer type if applicable
            polymer_type_str = None
            if entity.polymer_type != gemmi.PolymerType.Unknown:
                polymer_type_str = str(entity.polymer_type).replace("PolymerType.", "")
            
            # Get chain IDs (subchains) for this entity
            chain_ids = list(entity.subchains)
            entity_subchains[entity_id] = chain_ids
            
            # Try to get entity description/name
            entity_name = None
            # For mmCIF, try to get from full_name or description
            if hasattr(entity, 'full_name') and entity.full_name:
                entity_name = entity.full_name
            
            # Store for later use
            for cid in chain_ids:
                entity_name_map[cid] = {
                    "entity_id": entity_id,
                    "name": entity_name,
                    "entity_type": entity_type_str,
                    "polymer_type": polymer_type_str
                }
            
            entity_info = {
                "entity_id": entity_id,
                "name": entity_name,
                "entity_type": entity_type_str,
                "polymer_type": polymer_type_str,
                "chain_ids": chain_ids
            }
            entities_info.append(entity_info)
        
        result["entities"] = entities_info
        
        # Common ions for classification
        COMMON_IONS = {'NA', 'CL', 'K', 'MG', 'CA', 'ZN', 'FE', 'MN', 'CU', 'CO', 'NI', 'CD', 'HG'}
        
        # One-letter amino acid code mapping
        AA_CODE = {
            'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C',
            'GLN': 'Q', 'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I',
            'LEU': 'L', 'LYS': 'K', 'MET': 'M', 'PHE': 'F', 'PRO': 'P',
            'SER': 'S', 'THR': 'T', 'TRP': 'W', 'TYR': 'Y', 'VAL': 'V',
            'SEC': 'U', 'PYL': 'O'
        }
        
        # Use first model for analysis
        model = structure[0]
        
        chains_info = []
        protein_chain_ids = []
        nucleic_chain_ids = []
        ligand_chain_ids = []
        glycan_chain_ids = []
        water_chain_ids = []
        ion_chain_ids = []
        nucleic_subtypes = {}
        modified_nucleic_residues = []
        glycan_residues = []
        
        for subchain in model.subchains():
            chain_id = subchain.subchain_id()  # label_asym_id - unique identifier
            res_list = list(subchain)
            if not res_list:
                continue
            
            # Collect residue information
            residue_names = set()
            num_atoms = 0
            sequence_parts = []
            
            has_protein = False
            has_water = False
            has_ion = False
            
            for res in res_list:
                res_name = res.name.strip()
                residue_names.add(res_name)
                num_atoms += len(list(res))
                
                if res_name in AMINO_ACIDS:
                    has_protein = True
                    sequence_parts.append(AA_CODE.get(res_name, 'X'))
                elif res_name in WATER_NAMES:
                    has_water = True
                elif res_name in COMMON_IONS:
                    has_ion = True
            
            # Get the author chain name (auth_asym_id) from the parent chain
            author_chain = None
            for chain in model:
                for chain_subchain in chain.subchains():
                    if chain_subchain.subchain_id() == chain_id:
                        author_chain = chain.name
                        break
                if author_chain:
                    break
            
            if author_chain is None:
                author_chain = chain_id  # Fallback

            # Get entity information before classification so polymer_type can
            # distinguish real nucleic polymers from one-letter ligand names.
            entity_info = entity_name_map.get(chain_id, {})
            nucleic_info = classify_nucleic_residues(
                residue_names,
                entity_info.get("polymer_type"),
            )
            glycan_info = classify_glycan_residues(
                residue_names,
                entity_info.get("entity_type"),
                entity_info.get("polymer_type"),
                entity_info.get("name"),
            )
            
            # Classify chain type.
            # The *_chain_ids lists store author_chain (auth_asym_id); they
            # are kept for display and backward compatibility. The
            # corresponding *_label_ids lists (emitted below in summary)
            # are built from chain_id (label_asym_id) and are what
            # split_molecules / prepare_complex expect in select_chains.
            if has_protein:
                chain_type = "protein"
                if author_chain not in protein_chain_ids:
                    protein_chain_ids.append(author_chain)
            elif nucleic_info["is_nucleic"]:
                chain_type = "nucleic"
                if author_chain not in nucleic_chain_ids:
                    nucleic_chain_ids.append(author_chain)
                if nucleic_info["subtype"]:
                    nucleic_subtypes[chain_id] = nucleic_info["subtype"]
                modified_names = set(nucleic_info["modified_residue_names"])
                for res in res_list:
                    res_name = res.name.strip()
                    if res_name not in modified_names:
                        continue
                    modified_nucleic_residues.append({
                        "chain": author_chain,
                        "author_chain": author_chain,
                        "label_chain": chain_id,
                        "resnum": res.seqid.num,
                        "icode": str(res.seqid.icode or ""),
                        "resname": res_name,
                        "source_resname": res_name,
                        "coordinate_frame": "source",
                    })
            elif glycan_info["is_glycan"]:
                chain_type = "glycan"
                if author_chain not in glycan_chain_ids:
                    glycan_chain_ids.append(author_chain)
                for res_name in glycan_info["residue_names"]:
                    glycan_residues.append({
                        "chain": author_chain,
                        "resname": res_name,
                    })
            elif has_water:
                chain_type = "water"
                if author_chain not in water_chain_ids:
                    water_chain_ids.append(author_chain)
            elif has_ion:
                chain_type = "ion"
                if author_chain not in ion_chain_ids:
                    ion_chain_ids.append(author_chain)
            else:
                chain_type = "ligand"
                if author_chain not in ligand_chain_ids:
                    ligand_chain_ids.append(author_chain)
            
            # Token optimization: Truncate residue_names and replace sequence with length
            unique_residues = sorted(list(residue_names))
            truncated_residues = unique_residues[:10] if len(unique_residues) > 10 else unique_residues
            residue_summary = {
                "unique_residues": truncated_residues,
                "total_unique_count": len(unique_residues),
                "truncated": len(unique_residues) > 10
            }

            # Get residue number for unique identification (first residue)
            first_res = res_list[0]
            first_resnum = first_res.seqid.num
            first_resname = first_res.name.strip()

            # Create unique ID for ligands/ions (format: chain:resname:resnum)
            # For proteins, use chain:PROTEIN:start-end format
            if chain_type in ("ligand", "ion"):
                unique_id = f"{author_chain}:{first_resname}:{first_resnum}"
            else:
                unique_id = None  # Proteins use chain ID only

            chain_info = {
                "chain_id": chain_id,
                "author_chain": author_chain,
                "entity_id": entity_info.get("entity_id"),
                "entity_name": entity_info.get("name"),
                "chain_type": chain_type,
                "is_protein": has_protein,
                "is_nucleic": chain_type == "nucleic",
                "nucleic_subtype": nucleic_info["subtype"] if chain_type == "nucleic" else None,
                "modified_nucleic_residue_names": (
                    nucleic_info["modified_residue_names"] if chain_type == "nucleic" else []
                ),
                "is_glycan": chain_type == "glycan",
                "glycan_residue_names": (
                    glycan_info["residue_names"] if chain_type == "glycan" else []
                ),
                "is_water": has_water,
                "num_residues": len(res_list),
                "num_atoms": num_atoms,
                "residue_names": residue_summary,
                "sequence_length": len(sequence_parts) if has_protein else 0,
                "resnum": first_resnum,
                "unique_id": unique_id,
            }
            chains_info.append(chain_info)
        
        result["chains"] = chains_info
        # Summary exposes BOTH ID systems so the caller can pick the right
        # one for `select_chains`. The contract is:
        #   - `protein_label_ids` / `ligand_label_ids` / ... = chain_id
        #     (label_asym_id) — **this is what you pass to select_chains**.
        #   - `protein_chain_ids` / `ligand_chain_ids` / ... =
        #     author_chain (auth_asym_id) — for display / provenance.
        #     Kept under the historical name for backward compatibility.
        #   - `chain_id_map` = {chain_id -> author_chain} so surprising
        #     pairings (e.g. 7NMU has label C ↔ auth DDD) are visible.
        protein_label_ids = [c["chain_id"] for c in chains_info if c["chain_type"] == "protein"]
        nucleic_label_ids = [c["chain_id"] for c in chains_info if c["chain_type"] == "nucleic"]
        glycan_label_ids  = [c["chain_id"] for c in chains_info if c["chain_type"] == "glycan"]
        ligand_label_ids  = [c["chain_id"] for c in chains_info if c["chain_type"] == "ligand"]
        water_label_ids   = [c["chain_id"] for c in chains_info if c["chain_type"] == "water"]
        ion_label_ids     = [c["chain_id"] for c in chains_info if c["chain_type"] == "ion"]
        chain_id_map = {c["chain_id"]: c["author_chain"] for c in chains_info}
        result["summary"] = {
            "num_protein_chains": len(protein_chain_ids),
            "num_nucleic_chains": len(nucleic_chain_ids),
            "num_glycan_chains": len(glycan_chain_ids),
            "num_ligand_chains": len(ligand_chain_ids),
            "num_water_chains": len(water_chain_ids),
            "num_ion_chains": len(ion_chain_ids),
            "total_chains": len(chains_info),
            # author_chain (auth_asym_id) lists — for display.
            "protein_chain_ids": protein_chain_ids,
            "nucleic_chain_ids": nucleic_chain_ids,
            "glycan_chain_ids": glycan_chain_ids,
            "ligand_chain_ids": ligand_chain_ids,
            "water_chain_ids": water_chain_ids,
            "ion_chain_ids": ion_chain_ids,
            # chain_id (label_asym_id) lists — pass these to select_chains.
            "protein_label_ids": protein_label_ids,
            "nucleic_label_ids": nucleic_label_ids,
            "glycan_label_ids": glycan_label_ids,
            "ligand_label_ids": ligand_label_ids,
            "water_label_ids": water_label_ids,
            "ion_label_ids": ion_label_ids,
            "chain_id_map": chain_id_map,
            "nucleic_subtypes": nucleic_subtypes,
            "modified_nucleic_residues": modified_nucleic_residues,
            "glycan_residues": glycan_residues,
        }
        modified_support = modified_nucleic_support_report(modified_nucleic_residues)
        result["summary"]["modified_nucleic_support_status"] = modified_support["status"]
        result["summary"]["modified_nucleic_support"] = modified_support
        result["summary"]["unsupported_modified_nucleic_residues"] = (
            modified_nucleic_residues if modified_support["detected"] else []
        )
        if modified_support["detected"]:
            result["warnings"].append(MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE)
        
        # Check if any chains were found
        if not chains_info:
            result["warnings"].append("No chains found in structure file")
            result["warnings"].append("Hint: The file may be empty or contain only header information")
        
        result["success"] = True
        logger.info(f"Successfully inspected structure: {len(chains_info)} chains found")
        logger.info(
            f"  Proteins: {len(protein_chain_ids)}, Nucleics: {len(nucleic_chain_ids)}, "
            f"Ligands: {len(ligand_chain_ids)}, Waters: {len(water_chain_ids)}, "
            f"Ions: {len(ion_chain_ids)}"
        )
        
    except Exception as e:
        error_msg = f"Error during structure inspection: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")
    
    return result


def split_molecules(
    structure_file: str,
    output_dir: Optional[str] = None,
    select_chains: Optional[List[str]] = None,
    include_types: Optional[List[str]] = None,
    include_ligand_ids: Optional[List[str]] = None,
    exclude_ligand_ids: Optional[List[str]] = None,
    keep_crystal_waters: bool = False,
) -> dict:
    """Split an mmCIF or PDB structure file into separate chain files.

    This tool splits a structure into chain files by molecular type:
    protein, nucleic, ligand, ion, and water. Output files are always in PDB
    format. Files are named as protein_1.pdb, nucleic_1.pdb, ligand_1.pdb,
    ion_1.pdb, water_1.pdb, etc.

    Chain Selection — label_asym_id vs auth_asym_id:
        **Rule of thumb: pass the short chain ID exactly as it appears
        in your input file.**

        - For **mmCIF** inputs pass ``chain_id`` (= label_asym_id), the
          short per-entity ID used by RCSB / SabDab (e.g. ``B`` for
          7QVK). The accompanying ``author_chain`` (auth_asym_id) is
          the depositor's original label and may be multi-letter
          (``AAA``, ``BBB``, ``AbA``) or reordered from the label
          (7NMU: label ``C`` ↔ auth ``DDD``).
        - For **PDB** inputs pass ``author_chain`` (= the 1-character
          value in column 22). gemmi's ``chain_id`` for PDB files is
          an auto-generated subchain ID like ``Axp`` / ``Ax1`` / ``Axw``
          — that's an internal artifact, not something users write.

        Implementation: the tool tries ``chain_id`` (label) first, then
        ``author_chain`` (auth). This single rule handles both formats
        correctly — mmCIF hits on the primary path, PDB hits on the
        fallback — and the fallback warning is suppressed for PDB
        inputs since author-match IS the natural path there. Errors
        list both systems and the ``label -> author`` mapping so the
        caller can disambiguate.

        Type-aware rescue: when the primary label match lands on chains
        that are all outside ``include_types`` (e.g. a SabDab
        ``Hchain='F'`` that matches the water/ligand subchain at label
        F while the nanobody lives at a different label), the tool
        retries with author_chain matching — including a
        first-character-of-author shortcut (``'F' -> auth 'FFF'``).
        This covers multi-chain RCSB mmCIF entries whose PDB-format
        export truncated long auth IDs to single letters. The rescue
        emits a warning naming the actual label chosen.

        Internally, gemmi's subchain iteration is keyed on label_asym_id
        (that's a gemmi API constraint, not a design choice). For
        chain-level bookkeeping (disulfide pairs, merged-PDB chain
        column) we prefer auth_asym_id because it tracks the biological
        chain rather than mmCIF's entity-level split.

    Type Filtering:
        Use include_types to filter by molecular type. By default (None), all
        types except water are included. Valid types: "protein", "nucleic",
        "glycan", "ligand", "ion", "water".

    Tip: Use inspect_molecules first to understand the structure and identify
    which chains you want to extract. It shows both chain_id (label_asym_id)
    and author_chain (auth_asym_id) for each chain, plus a
    ``chain_id_map`` in the summary.

    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb) file to split.
        output_dir: Output directory (auto-generated if None).
        select_chains: List of chain IDs to extract. Matches ``chain_id``
                       (label_asym_id) first and falls back to
                       ``author_chain`` (auth_asym_id) for any unresolved
                       entries. Use inspect_molecules to find available
                       IDs. If None, extracts all chains.
        include_types: List of molecular types to include. Valid values:
                       "protein", "nucleic", "glycan", "ligand", "ion", "water".
                       If None (default), includes ["protein", "nucleic", "glycan", "ligand", "ion"].
        keep_crystal_waters: If True, retain crystal waters when "water" is in include_types.
                            Default is False (crystal waters are excluded even if "water"
                            is in include_types). For most MD simulations, crystal waters
                            should be excluded and bulk solvent added via solvate_structure.
        include_ligand_ids: List of ligand unique IDs to include (format:
                           "author_chain:resname:resnum", e.g.,
                           ["A:ACP:501"]). If specified, only these ligands
                           are extracted. If select_chains omitted a requested
                           ligand's label chain, the chain is auto-included
                           with a ``ligand_chain_auto_included`` adjustment.
                           Use inspect_molecules to get available ligand unique IDs.
        exclude_ligand_ids: List of ligand unique IDs to exclude (format: "chain:resname:resnum",
                           e.g., ["A:ACT:401", "A:ACT:402"]). If specified, these ligands are
                           skipped. Takes precedence if a ligand is in both include and exclude.

    Returns:
        Dict with:
            - success: bool - True if splitting completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Directory containing output files
            - source_file: str - Original input file path
            - file_format: str - Output format (always 'pdb')
            - protein_files: list[str] - Paths to protein chain files (protein_*.pdb)
            - nucleic_files: list[str] - Paths to nucleic chain files (nucleic_*.pdb)
            - glycan_files: list[str] - Paths to glycan chain files (glycan_*.pdb)
            - ligand_files: list[str] - Paths to ligand chain files (ligand_*.pdb)
            - ion_files: list[str] - Paths to ion chain files (ion_*.pdb)
            - water_files: list[str] - Paths to water chain files (water_*.pdb)
            - all_chains: list[dict] - Metadata for all chains found (from inspect_molecules)
            - chain_file_info: list[dict] - Mapping of chains to output files
            - include_types: list[str] - Types that were included
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Splitting structure: {structure_file}")
    
    # Set default include_types (exclude water by default)
    if include_types is None:
        include_types = ["protein", "nucleic", "glycan", "ligand", "ion"]

    # Validate include_types
    valid_types = {"protein", "nucleic", "glycan", "ligand", "ion", "water"}
    invalid_types = set(include_types) - valid_types
    if invalid_types:
        logger.warning(f"Invalid include_types ignored: {invalid_types}. Valid: {valid_types}")
        include_types = [t for t in include_types if t in valid_types]

    # Remove crystal waters by default (can be overridden with keep_crystal_waters=True)
    # Crystal waters are typically removed for both implicit and explicit solvent simulations
    # (explicit solvent will add bulk water later via solvate_structure)
    if "water" in include_types and not keep_crystal_waters:
        logger.info("Crystal waters excluded (default behavior, use keep_crystal_waters=True to retain)")
        include_types = [t for t in include_types if t != "water"]
    
    # Initialize result structure for LLM error handling
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "source_file": str(structure_file),
        "file_format": "pdb",
        "protein_files": [],
        "nucleic_files": [],
        "glycan_files": [],
        "ligand_files": [],
        "ion_files": [],
        "water_files": [],
        "all_chains": [],
        "chain_file_info": [],
        "include_types": include_types,
        "selection_adjustments": [],
        "errors": [],
        "warnings": []
    }
    
    # First, analyze the structure
    analysis = _inspect_molecules_impl(structure_file)
    
    if not analysis["success"]:
        result["errors"] = analysis["errors"]
        result["warnings"] = analysis["warnings"]
        return result
    
    result["all_chains"] = analysis["chains"]
    
    # Check for gemmi dependency (should be available if analysis succeeded)
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        return result
    
    # Validate input file
    structure_path = Path(structure_file)
    suffix = structure_path.suffix.lower()
    
    # Setup output directory
    base_dir = Path(output_dir) if output_dir else WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "split")
    result["output_dir"] = str(out_dir)

    try:
        # Read structure with gemmi
        logger.info(f"Reading structure with gemmi ({suffix})...")
        if suffix == '.cif':
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            structure = gemmi.make_structure_from_block(block)
        else:  # .pdb or .ent
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()
        
        model = structure[0]  # Use first model
        
        # Build chain info lookup from analysis results
        chain_info = {c["chain_id"]: c for c in analysis["chains"]}
        
        # Determine which chains to select.
        # Option 2 contract: select_chains is label_asym_id (chain_id) first,
        # with a safety fallback to author_chain for unresolved IDs. We always
        # end up with a set of label IDs in selected_chain_ids, which is what
        # the downstream gemmi subchain loop keys on.
        available_chain_ids = [c["chain_id"] for c in analysis["chains"]]
        available_author_chains = sorted({c["author_chain"] for c in analysis["chains"]})
        label_to_author_map = {c["chain_id"]: c["author_chain"] for c in analysis["chains"]}

        if select_chains is not None:
            selected_chain_ids: set[str] = set()
            missing_chains: list[str] = []
            fallback_used: list[tuple[str, list[str]]] = []
            for ch in select_chains:
                # Primary: label_asym_id exact match
                label_hits = {c["chain_id"] for c in analysis["chains"] if c["chain_id"] == ch}
                if label_hits:
                    selected_chain_ids |= label_hits
                    continue
                # Fallback: author_chain match (all label IDs under that author)
                author_hits = {c["chain_id"] for c in analysis["chains"] if c["author_chain"] == ch}
                if author_hits:
                    selected_chain_ids |= author_hits
                    fallback_used.append((ch, sorted(author_hits)))
                    continue
                missing_chains.append(ch)

            if missing_chains:
                result["errors"].append(f"Chain(s) not found: {missing_chains}")
                result["errors"].append(
                    f"Hint: Available chain_id (label_asym_id) values: {available_chain_ids}"
                )
                result["errors"].append(
                    f"Hint: Available author_chain (auth_asym_id) values: {available_author_chains}"
                )
                result["errors"].append(
                    f"Hint: label -> author mapping: {label_to_author_map}"
                )
                logger.error(
                    f"Requested chains not found: {missing_chains} "
                    f"(available labels={available_chain_ids}; authors={available_author_chains})"
                )
                return result

            # Suppress fallback warnings when the input is PDB format. For
            # PDB files the "label_asym_id" that gemmi surfaces is actually
            # an auto-generated subchain_id (e.g. ``Axp``, ``Ax1``, ``Axw``
            # for the protein / 1st ligand / water of author chain A), not
            # something the user would reasonably type. The PDB chain
            # column is the author_chain, so author-fallback IS the natural
            # resolution path and need not be flagged.
            _suffix = structure_path.suffix.lower()
            _is_pdb_input = _suffix in (".pdb", ".ent")
            if fallback_used and not _is_pdb_input:
                for ch, labels in fallback_used:
                    result["warnings"].append(
                        f"Chain '{ch}' resolved via author_chain fallback -> label(s) {labels}. "
                        f"Pass the label_asym_id directly to avoid this fallback."
                    )

            # Second fallback: if the primary label selection yielded chains
            # but NONE of them are in include_types, try author-chain match
            # (including the first-character-of-author shortcut that SabDab
            # uses — Hchain='F' in a multi-chain mmCIF with auth 'FFF'
            # corresponds to the chain under auth 'FFF', which is the label
            # that carries the protein, not label 'F' which in those entries
            # is a ligand or water subchain).
            selected_infos = [c for c in analysis["chains"] if c["chain_id"] in selected_chain_ids]
            if selected_chain_ids and not any(c["chain_type"] in include_types for c in selected_infos):
                rescue_label_ids: set[str] = set()
                rescue_hits: list[tuple[str, list[str]]] = []
                for ch in select_chains:
                    # Pattern A: exact author_chain match against include_types chains
                    matches = [
                        c for c in analysis["chains"]
                        if c["author_chain"] == ch and c["chain_type"] in include_types
                    ]
                    # Pattern B: first-character-of-author match against
                    # include_types chains (e.g. user passes 'F' and the auth
                    # 'FFF' owns the protein subchain). Only used when
                    # single-character input and no exact auth hit.
                    if not matches and len(ch) == 1:
                        matches = [
                            c for c in analysis["chains"]
                            if c["chain_type"] in include_types
                            and c["author_chain"]
                            and c["author_chain"][0] == ch
                            and c["author_chain"] != ch
                        ]
                    if matches:
                        rescue_label_ids |= {c["chain_id"] for c in matches}
                        rescue_hits.append((ch, sorted({c["chain_id"] for c in matches})))
                if rescue_label_ids:
                    selected_chain_ids = rescue_label_ids
                    for ch, labels in rescue_hits:
                        result["warnings"].append(
                            f"Chain '{ch}' rescued via author-chain fallback (primary "
                            f"label match had no chains in include_types={include_types}). "
                            f"Resolved to label(s) {labels}. Common for SabDab 1-char IDs "
                            f"on mmCIF entries with multi-letter author IDs (e.g. 'F' -> "
                            f"auth 'FFF')."
                        )
            if include_ligand_ids is not None and "ligand" in include_types:
                requested = set(include_ligand_ids)
                matching_ligands = [
                    c for c in analysis["chains"]
                    if c.get("chain_type") == "ligand"
                    and c.get("unique_id") in requested
                ]
                auto_added = sorted(
                    c["chain_id"]
                    for c in matching_ligands
                    if c.get("chain_id") not in selected_chain_ids
                )
                if auto_added:
                    selected_chain_ids |= set(auto_added)
                    adjustment = {
                        "code": "ligand_chain_auto_included",
                        "message": (
                            "Requested ligand(s) were outside select_chains; "
                            "their label chain(s) were added automatically."
                        ),
                        "added_chain_ids": auto_added,
                        "requested_ligand_ids": sorted(requested),
                    }
                    result["selection_adjustments"].append(adjustment)
                    result["warnings"].append(
                        "ligand_chain_auto_included: requested ligand(s) "
                        f"{sorted(requested)} require ligand label chain(s) "
                        f"{auto_added}, which were added to select_chains."
                    )
        else:
            # Default: select all chains (type filtering happens later)
            selected_chain_ids = set(c["chain_id"] for c in analysis["chains"])
        
        logger.info(f"Chains to export: {sorted(selected_chain_ids)}")
        
        # Write each chain to a separate PDB file
        protein_files = []
        nucleic_files = []
        glycan_files = []
        ligand_files = []
        ion_files = []
        water_files = []
        protein_idx = 1
        nucleic_idx = 1
        glycan_idx = 1
        ligand_idx = 1
        ion_idx = 1
        water_idx = 1
        chain_file_info = []
        
        for subchain in model.subchains():
            chain_id = subchain.subchain_id()  # label_asym_id
            if chain_id not in selected_chain_ids:
                continue
            
            info = chain_info.get(chain_id, {})
            chain_type = info.get("chain_type", "ligand")

            # Skip if chain_type not in include_types
            if chain_type not in include_types:
                continue

            # Apply ligand-specific filtering by unique_id
            if chain_type == "ligand":
                unique_id = info.get("unique_id")
                if unique_id:
                    # Check exclude filter first (takes precedence)
                    if exclude_ligand_ids is not None and unique_id in exclude_ligand_ids:
                        logger.info(f"Excluding ligand {unique_id} (in exclude_ligand_ids)")
                        continue
                    # Check include filter
                    if include_ligand_ids is not None and unique_id not in include_ligand_ids:
                        logger.info(f"Skipping ligand {unique_id} (not in include_ligand_ids)")
                        continue
            
            # Build new structure with this chain's residues
            new_structure = gemmi.Structure()
            new_model = gemmi.Model("1")
            # Use author_chain (single letter) for PDB compatibility
            # label_asym_id can be too long for PDB format (e.g., "Axp" from OPM)
            author_chain = info.get("author_chain", chain_id)
            # Ensure chain name is max 1 character for PDB format
            pdb_chain_name = author_chain[0] if len(author_chain) > 1 else (author_chain if author_chain else "A")
            new_chain = gemmi.Chain(pdb_chain_name)
            residue_count = 0
            
            waters_skipped = 0
            for residue in subchain:
                res_name = residue.name.strip()
                # Skip water residues unless explicitly keeping them
                # This ensures crystal waters are removed regardless of chain type
                if res_name in WATER_NAMES and not keep_crystal_waters:
                    waters_skipped += 1
                    continue
                
                new_residue = gemmi.Residue()
                new_residue.name = residue.name
                new_residue.seqid = residue.seqid
                new_residue.subchain = residue.subchain
                seen_atom_names = set()
                for atom in residue:
                    altloc_char = atom.altloc
                    if altloc_char in ('\x00', '', 'A', ' '):
                        if atom.name not in seen_atom_names:
                            new_atom = gemmi.Atom()
                            new_atom.name = atom.name
                            new_atom.pos = atom.pos
                            new_atom.occ = atom.occ
                            new_atom.b_iso = atom.b_iso
                            new_atom.element = atom.element
                            new_residue.add_atom(new_atom)
                            seen_atom_names.add(atom.name)
                if len(list(new_residue)) > 0:
                    new_chain.add_residue(new_residue)
                    residue_count += 1

            if waters_skipped > 0:
                logger.info(f"Skipped {waters_skipped} water residue(s) in chain {chain_id}")

            if len(list(new_chain)):
                new_model.add_chain(new_chain)
                new_structure.add_model(new_model)
                
                # Determine output file based on chain type
                author_chain_id = info.get("author_chain", chain_id)
                resnum = info.get("resnum")
                # residue_names is a dict with "unique_residues" list
                residue_names_data = info.get("residue_names", {})
                if isinstance(residue_names_data, dict):
                    unique_res = residue_names_data.get("unique_residues", ["UNK"])
                    res_name = unique_res[0] if unique_res else "UNK"
                elif isinstance(residue_names_data, list):
                    res_name = residue_names_data[0] if residue_names_data else "UNK"
                else:
                    res_name = "UNK"

                if chain_type == "protein":
                    out_file = out_dir / f"protein_{protein_idx}.pdb"
                    protein_files.append(str(out_file))
                    protein_idx += 1
                elif chain_type == "nucleic":
                    out_file = out_dir / f"nucleic_{nucleic_idx}.pdb"
                    nucleic_files.append(str(out_file))
                    nucleic_idx += 1
                elif chain_type == "glycan":
                    if resnum is not None:
                        out_file = out_dir / f"glycan_{res_name}_{author_chain_id}{resnum}.pdb"
                    else:
                        out_file = out_dir / f"glycan_{glycan_idx}.pdb"
                    glycan_files.append(str(out_file))
                    glycan_idx += 1
                elif chain_type == "ligand":
                    # Use descriptive naming: ligand_{resname}_{chain}{resnum}.pdb
                    if resnum is not None:
                        out_file = out_dir / f"ligand_{res_name}_{author_chain_id}{resnum}.pdb"
                    else:
                        out_file = out_dir / f"ligand_{res_name}_{ligand_idx}.pdb"
                    ligand_files.append(str(out_file))
                    ligand_idx += 1
                elif chain_type == "ion":
                    # Use descriptive naming: ion_{resname}_{chain}{resnum}.pdb
                    if resnum is not None:
                        out_file = out_dir / f"ion_{res_name}_{author_chain_id}{resnum}.pdb"
                    else:
                        out_file = out_dir / f"ion_{res_name}_{ion_idx}.pdb"
                    ion_files.append(str(out_file))
                    ion_idx += 1
                elif chain_type == "water":
                    out_file = out_dir / f"water_{water_idx}.pdb"
                    water_files.append(str(out_file))
                    water_idx += 1
                else:
                    # Fallback for unknown types
                    out_file = out_dir / f"ligand_{ligand_idx}.pdb"
                    ligand_files.append(str(out_file))
                    ligand_idx += 1
                
                new_structure.write_pdb(str(out_file))
                logger.info(f"Wrote {chain_type}: {out_file}")
                chain_file_info.append({
                    "chain_id": chain_id,
                    "author_chain": info.get("author_chain", chain_id),
                    "chain_type": chain_type,
                    "nucleic_subtype": info.get("nucleic_subtype"),
                    "resnum": resnum,
                    "unique_id": info.get("unique_id"),
                    "file": str(out_file),
                    "residue_count": residue_count
                })
        
        result["protein_files"] = protein_files
        result["nucleic_files"] = nucleic_files
        result["glycan_files"] = glycan_files
        result["ligand_files"] = ligand_files
        result["ion_files"] = ion_files
        result["water_files"] = water_files
        result["chain_file_info"] = chain_file_info
        
        # Warn if no files were generated
        total_files = (
            len(protein_files)
            + len(nucleic_files)
            + len(glycan_files)
            + len(ligand_files)
            + len(ion_files)
            + len(water_files)
        )
        if total_files == 0:
            result["warnings"].append("No output files were generated")
            result["warnings"].append("Hint: All chains may have been filtered out by selection or water exclusion")
        
        # Write metadata file
        metadata_file = out_dir / "split_metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        result["success"] = True
        logger.info(
            f"Successfully split structure: {len(protein_files)} protein, "
            f"{len(nucleic_files)} nucleic, {len(glycan_files)} glycan, "
            f"{len(ligand_files)} ligand, "
            f"{len(ion_files)} ion, {len(water_files)} water files"
        )
        
    except Exception as e:
        error_msg = f"Error during structure splitting: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")
        elif "memory" in str(e).lower():
            result["errors"].append("Hint: The structure file may be too large. Try splitting manually first.")
    
    return result


def clean_protein(
    pdb_file: str,
    ignore_terminal_missing_residues: bool = True,
    cap_termini: bool = False,
    n_terminal_cap: str | None = None,
    c_terminal_cap: str | None = None,
    terminal_cap_forcefield: str | None = None,
    replace_nonstandard_residues: bool = True,
    remove_heterogens: bool = True,
    keep_water: bool = False,
    add_missing_atoms: bool = True,
    add_hydrogens: bool = True,
    ph: float = 7.4,
    disulfide_pairs: list[dict] | None = None,
    histidine_states: dict[str, str] | None = None,
    protonation_states: Optional[Dict[str, Any]] = None,
) -> dict:
    """Clean a monomer protein PDB/mmCIF file for MD simulation using PDBFixer.

    This tool processes a single-chain protein structure (from split_molecules output)
    and prepares it for MD simulation by fixing missing residues, atoms, and adding
    proper protonation.

    Args:
        pdb_file: Input protein PDB or mmCIF file path (single chain from split_molecules)
        ignore_terminal_missing_residues: Ignore missing residues at chain termini
                                          instead of modeling them (default: True)
        cap_termini: Backward-compatible shortcut for adding ACE at the
                     N terminus and NME at the C terminus (default: False).
        n_terminal_cap: Optional one-sided N-terminal cap. Currently supports
                        ``"ACE"`` or an explicit none-like value.
        c_terminal_cap: Optional one-sided C-terminal cap. Currently supports
                        ``"NME"`` or an explicit none-like value.
        terminal_cap_forcefield: Protein force field used only for OpenMM
                                 Modeller cap-hydrogen completion. Defaults
                                 to ff19SB; pass the planned topology protein
                                 force field when it differs.
        replace_nonstandard_residues: Replace non-standard residues with standard ones (default: True)
        remove_heterogens: Remove heteroatoms (ligands, ions, etc.) (default: True)
        keep_water: Keep water molecules when removing heterogens (default: False)
        add_missing_atoms: Add missing heavy atoms (default: True)
        add_hydrogens: Add hydrogen atoms at specified pH (default: True)
        ph: pH for protonation state assignment (default: 7.4)
        disulfide_pairs: Pre-defined disulfide bond pairs from Phase 1 analysis.
                        List of dicts with chain1, resnum1, chain2, resnum2, form_bond.
                        If provided, skips auto-detection and uses these pairs instead.
        histidine_states: Pre-defined histidine protonation states from Phase 1 analysis.
                         Dict mapping "chain:resnum" to state ("HID", "HIE", "HIP").
                         If provided, skips propka and applies these states directly.
        protonation_states: User-specified residue protonation states. Accepts
                         either a dict mapping "chain:resnum" to Amber variant
                         names, or a list of dicts with chain, resnum, state,
                         and optional icode. Supports ASP/ASH, GLU/GLH,
                         HID/HIE/HIP, LYS/LYN, and CYS/CYX/CYM.

    Returns:
        Dict with:
            - success: bool - True if cleaning completed without critical errors
            - output_file: str - Path to the cleaned PDB file (*.clean.pdb)
            - input_file: str - Original input file path
            - cap_termini_required: bool - True if ACE/NME caps still need to be
              added before openmmforcefields can build the System (PDBFixer cannot
              add caps directly).
            - n_terminal_cap: str | None - Applied/requested N-terminal cap.
            - c_terminal_cap: str | None - Applied/requested C-terminal cap.
            - terminal_cap_hydrogen_completion: dict - OpenMM Modeller cap-H
              completion report when ACE/NME caps are present.
            - operations: list[dict] - Details of each operation performed
            - warnings: list[str] - Non-critical issues encountered
            - errors: list[str] - Critical errors (empty if success=True)
            - statistics: dict - Summary counts (chains, residues, atoms, etc.)
            - disulfide_bonds: list[dict] - Detected disulfide bonds with residue info
              (CYS residues renamed to CYX for Amber compatibility)
    """
    logger.info(f"Cleaning protein structure: {pdb_file}")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "output_file": None,
        "input_file": str(pdb_file),
        "cap_termini_required": False,
        "n_terminal_cap": None,
        "c_terminal_cap": None,
        "terminal_caps": {},
        "terminal_cap_forcefield": terminal_cap_forcefield or DEFAULT_TERMINAL_CAP_FORCEFIELD,
        "terminal_cap_hydrogen_completion": None,
        "operations": [],
        "warnings": [],
        "errors": [],
        "statistics": {},
        "disulfide_bonds": [],
        "component_disposition": _component_disposition_payload([]),
        "component_disposition_summary": _component_disposition_payload([])["summary"],
    }

    try:
        requested_protonation_states = _normalize_protonation_state_overrides(
            protonation_states=protonation_states,
            histidine_states=histidine_states,
        )
        resolved_n_terminal_cap, resolved_c_terminal_cap = _resolve_terminal_cap_settings(
            cap_termini=cap_termini,
            n_terminal_cap=n_terminal_cap,
            c_terminal_cap=c_terminal_cap,
        )
    except ValueError as exc:
        result["errors"].append(str(exc))
        result["code"] = (
            "invalid_terminal_cap"
            if "terminal cap" in str(exc)
            else "invalid_protonation_state"
        )
        return result
    result["n_terminal_cap"] = resolved_n_terminal_cap
    result["c_terminal_cap"] = resolved_c_terminal_cap
    result["terminal_caps"] = {
        "n_terminal": resolved_n_terminal_cap,
        "c_terminal": resolved_c_terminal_cap,
    }
    
    # Validate input file
    input_path = Path(pdb_file)
    if not input_path.is_file():
        result["errors"].append(f"Input file not found: {pdb_file}")
        logger.error(f"Input file not found: {pdb_file}")
        return result
    
    # Generate output filename: protein_1.pdb -> protein_1.clean.pdb
    stem = input_path.stem
    output_file = input_path.parent / f"{stem}.clean.pdb"
    result["output_file"] = str(output_file)
    
    try:
        cleaning_input_path = input_path
        deuterium_stripped_file = input_path.parent / f"{stem}.deuterium_stripped.pdb"
        component_disposition = _exclude_deuterium_atoms_from_pdb(
            input_path,
            deuterium_stripped_file,
        )
        result["component_disposition"] = component_disposition
        result["component_disposition_summary"] = component_disposition["summary"]
        excluded_deuterium_count = component_disposition["summary"][
            "experimental_isotope_atoms_excluded"
        ]
        if excluded_deuterium_count:
            cleaning_input_path = deuterium_stripped_file
            result["deuterium_stripped_input_file"] = str(deuterium_stripped_file)
            result["operations"].append({
                "step": "component_disposition",
                "status": "excluded",
                "details": (
                    f"Excluded {excluded_deuterium_count} experimental deuterium atom(s); "
                    "standard hydrogens will be rebuilt downstream"
                ),
            })
            result["warnings"].append(
                f"Excluded {excluded_deuterium_count} experimental deuterium atom(s) "
                "from MD preparation input; standard hydrogens will be rebuilt"
            )

        # Load structure
        logger.info("Loading structure with PDBFixer")
        fixer = PDBFixer(filename=str(cleaning_input_path))
        
        # Get initial statistics
        initial_chains = list(fixer.topology.chains())
        initial_residues = list(fixer.topology.residues())
        result["statistics"]["initial_chains"] = len(initial_chains)
        result["statistics"]["initial_residues"] = len(initial_residues)
        
        result["operations"].append({
            "step": "load_structure",
            "status": "success",
            "details": f"Loaded {len(initial_chains)} chain(s), {len(initial_residues)} residue(s)"
        })
        
        # Step 1: Handle missing residues and terminal caps
        logger.info("Finding missing residues")
        fixer.findMissingResidues()
        num_missing_residues = len(fixer.missingResidues)
        
        # Get chain information for terminal handling
        chains = list(fixer.topology.chains())
        
        # Step 1a: Handle terminal missing residues
        terminal_caps_requested = bool(resolved_n_terminal_cap or resolved_c_terminal_cap)
        if ignore_terminal_missing_residues and not terminal_caps_requested:
            # Remove terminal missing residues from the dictionary
            keys_to_remove = []
            for key in list(fixer.missingResidues.keys()):
                chain_idx, res_idx = key
                chain = chains[chain_idx]
                chain_length = len(list(chain.residues()))
                if res_idx == 0 or res_idx == chain_length:
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del fixer.missingResidues[key]
            
            if keys_to_remove:
                result["operations"].append({
                    "step": "missing_residues",
                    "status": "modified",
                    "details": f"Found {num_missing_residues} missing residue(s), ignored {len(keys_to_remove)} terminal missing residue(s)"
                })
                result["warnings"].append(f"Ignored {len(keys_to_remove)} terminal missing residue(s)")
        
        # Step 1b: Add requested terminal caps. ``cap_termini=True`` resolves
        # to the historical ACE+NME pair; explicit one-sided cap arguments
        # can request only one terminus.
        if terminal_caps_requested:
            capped_chains = []
            for chain_idx, chain in enumerate(chains):
                chain_length = len(list(chain.residues()))
                if resolved_n_terminal_cap:
                    fixer.missingResidues[chain_idx, 0] = [resolved_n_terminal_cap]
                if resolved_c_terminal_cap:
                    fixer.missingResidues[chain_idx, chain_length] = [resolved_c_terminal_cap]
                capped_chains.append(chain.id)
            
            result["operations"].append({
                "step": "terminal_caps",
                "status": "added_to_missing",
                "n_terminal_cap": resolved_n_terminal_cap,
                "c_terminal_cap": resolved_c_terminal_cap,
                "details": (
                    "Added requested terminal caps as missing residues for "
                    f"{len(capped_chains)} chain(s): {capped_chains}"
                ),
            })
            logger.info(
                "Added terminal caps to missingResidues for chains %s: N=%s C=%s",
                capped_chains,
                resolved_n_terminal_cap,
                resolved_c_terminal_cap,
            )
        
        # Report remaining missing residues (excluding caps)
        internal_missing = []
        for (chain_idx, res_idx), residues in fixer.missingResidues.items():
            if residues not in [['ACE'], ['NME']]:
                internal_missing.append(f"Chain {chain_idx}, position {res_idx}: {residues}")
        
        if internal_missing:
            result["operations"].append({
                "step": "missing_residues",
                "status": "will_model",
                "count": len(internal_missing),
                "residues": internal_missing,
                "details": f"Found {len(internal_missing)} internal missing residue(s) to be modeled"
            })
        elif num_missing_residues == 0 and not terminal_caps_requested:
            result["operations"].append({
                "step": "missing_residues",
                "status": "none_found",
                "details": "No missing residues found"
            })
        
        # Step 2: Handle non-standard residues
        logger.info("Finding non-standard residues")
        fixer.findNonstandardResidues()
        num_nonstandard = len(fixer.nonstandardResidues)
        
        if num_nonstandard > 0:
            # PDBFixer's nonstandardResidues is a list of (Residue, replacement_name)
            # tuples, not a list of Residue objects — unpacking is mandatory.
            nonstandard_info = [
                f"{res.name}->{repl} (chain {res.chain.id}, pos {res.index})"
                for res, repl in fixer.nonstandardResidues
            ]
            
            if replace_nonstandard_residues:
                fixer.replaceNonstandardResidues()
                result["operations"].append({
                    "step": "nonstandard_residues",
                    "status": "replaced",
                    "details": f"Replaced {num_nonstandard} non-standard residue(s): {nonstandard_info}"
                })
                logger.info(f"Replaced {num_nonstandard} non-standard residues")
            else:
                result["operations"].append({
                    "step": "nonstandard_residues",
                    "status": "kept",
                    "details": f"Kept {num_nonstandard} non-standard residue(s): {nonstandard_info}"
                })
                result["warnings"].append(f"Non-standard residues kept: {nonstandard_info}")
        else:
            result["operations"].append({
                "step": "nonstandard_residues",
                "status": "none_found",
                "details": "No non-standard residues found"
            })
        
        # Step 3: Remove heterogens
        if remove_heterogens:
            logger.info(f"Removing heterogens (keep_water={keep_water})")
            fixer.removeHeterogens(keepWater=keep_water)
            water_status = "kept" if keep_water else "removed"
            result["operations"].append({
                "step": "remove_heterogens",
                "status": "success",
                "details": f"Removed heterogens, water {water_status}"
            })
        else:
            result["operations"].append({
                "step": "remove_heterogens",
                "status": "skipped",
                "details": "Heterogen removal skipped"
            })
            result["warnings"].append("Heterogens not removed - may cause issues in MD simulation")
        
        # Step 4: Add missing atoms and residues (including ACE/NME caps)
        if add_missing_atoms:
            logger.info("Finding and adding missing atoms")
            fixer.findMissingAtoms()
            
            num_missing_atoms = sum(len(atoms) for atoms in fixer.missingAtoms.values())
            num_missing_terminals = sum(len(atoms) for atoms in fixer.missingTerminals.values())
            num_missing_residues = len(fixer.missingResidues)
            
            # Always call addMissingAtoms if there are missing atoms OR missing residues (caps)
            if num_missing_atoms > 0 or num_missing_terminals > 0 or num_missing_residues > 0:
                fixer.addMissingAtoms()
                details_parts = []
                if num_missing_atoms > 0:
                    details_parts.append(f"{num_missing_atoms} missing atom(s)")
                if num_missing_terminals > 0:
                    details_parts.append(f"{num_missing_terminals} terminal atom(s)")
                if num_missing_residues > 0:
                    details_parts.append(f"{num_missing_residues} missing residue(s)")
                result["operations"].append({
                    "step": "missing_atoms",
                    "status": "added",
                    "details": f"Added {', '.join(details_parts)}"
                })
                logger.info(f"Added missing atoms/residues: {', '.join(details_parts)}")
            else:
                result["operations"].append({
                    "step": "missing_atoms",
                    "status": "none_found",
                    "details": "No missing atoms or residues found"
                })
        else:
            result["operations"].append({
                "step": "missing_atoms",
                "status": "skipped",
                "details": "Missing atom addition skipped"
            })
            result["warnings"].append("Missing atoms not added - structure may be incomplete")
        
        # Step 5: Detect and handle disulfide bonds
        logger.info("Detecting disulfide bonds")
        try:
            # Collect CYS residues before creating disulfide bonds
            cys_residues = set()
            cys_by_chain_resnum = {}  # Map (chain, resnum) -> residue for pre-defined pairs
            for residue in fixer.topology.residues():
                if residue.name == 'CYS':
                    cys_residues.add(residue)
                    try:
                        residue_number = int(residue.id)
                    except (TypeError, ValueError):
                        residue_number = residue.index
                    cys_by_chain_resnum[(residue.chain.id, residue_number)] = residue

            disulfide_info = []
            cyx_residues = set()  # Track residues to rename

            if disulfide_pairs is not None:
                # Use pre-defined disulfide pairs from Phase 1 analysis
                logger.info(f"Using {len(disulfide_pairs)} pre-defined disulfide pair(s) from Phase 1")
                for pair in disulfide_pairs:
                    # Skip pairs marked as "don't form bond"
                    if not pair.get("form_bond", True):
                        logger.info(f"Skipping user-excluded disulfide: {pair}")
                        continue

                    chain1 = pair.get("chain1")
                    resnum1 = pair.get("resnum1")
                    chain2 = pair.get("chain2")
                    resnum2 = pair.get("resnum2")

                    # Find the residues by chain and resnum
                    res1 = cys_by_chain_resnum.get((chain1, resnum1))
                    res2 = cys_by_chain_resnum.get((chain2, resnum2))

                    if res1 and res2:
                        bond_info = {
                            "residue1": {
                                "name": res1.name,
                                "chain": res1.chain.id,
                                "index": res1.index
                            },
                            "residue2": {
                                "name": res2.name,
                                "chain": res2.chain.id,
                                "index": res2.index
                            },
                            "source": "user_specified"
                        }
                        disulfide_info.append(bond_info)
                        cyx_residues.add(res1)
                        cyx_residues.add(res2)
                    else:
                        result["warnings"].append(
                            f"Could not find CYS pair: {chain1}:{resnum1} - {chain2}:{resnum2}"
                        )

                result["operations"].append({
                    "step": "disulfide_bonds",
                    "status": "user_specified",
                    "details": f"Applied {len(disulfide_info)} user-specified disulfide bond(s)"
                })
            else:
                # Auto-detect disulfide bonds using PDBFixer
                # createDisulfideBonds() modifies topology in place and returns None
                # It adds bonds between SG atoms of CYS residues that are close enough
                fixer.topology.createDisulfideBonds(fixer.positions)

                # Find disulfide bonds by scanning topology bonds for S-S bonds between CYS
                for bond in fixer.topology.bonds():
                    atom1, atom2 = bond
                    # Check if this is an S-S bond between two CYS residues
                    if (atom1.element.symbol == 'S' and atom2.element.symbol == 'S' and
                        atom1.residue in cys_residues and atom2.residue in cys_residues):

                        res1 = atom1.residue
                        res2 = atom2.residue

                        # Avoid duplicate entries (bond may be listed once)
                        bond_key = tuple(sorted([res1.index, res2.index]))
                        if any(tuple(sorted([d["residue1"]["index"], d["residue2"]["index"]])) == bond_key
                               for d in disulfide_info):
                            continue

                        # Record bond information before renaming
                        bond_info = {
                            "residue1": {
                                "name": res1.name,
                                "chain": res1.chain.id,
                                "index": res1.index
                            },
                            "residue2": {
                                "name": res2.name,
                                "chain": res2.chain.id,
                                "index": res2.index
                            },
                            "source": "auto_detected"
                        }
                        disulfide_info.append(bond_info)
                        cyx_residues.add(res1)
                        cyx_residues.add(res2)

                if disulfide_info:
                    result["operations"].append({
                        "step": "disulfide_bonds",
                        "status": "detected",
                        "details": f"Auto-detected {len(disulfide_info)} disulfide bond(s)"
                    })
                else:
                    result["operations"].append({
                        "step": "disulfide_bonds",
                        "status": "none_found",
                        "details": "No disulfide bonds detected"
                    })

            # Rename CYS -> CYX for Amber compatibility
            for res in cyx_residues:
                res.name = 'CYX'

            if disulfide_info:
                result["disulfide_bonds"] = disulfide_info
                logger.info(f"Applied {len(disulfide_info)} disulfide bonds, renamed {len(cyx_residues)} residues to CYX")
            else:
                logger.info("No disulfide bonds to apply")

        except Exception as e:
            result["warnings"].append(f"Disulfide bond detection failed: {str(e)}")
            result["operations"].append({
                "step": "disulfide_bonds",
                "status": "error",
                "details": f"Detection failed: {str(e)}"
            })
            logger.warning(f"Disulfide bond detection failed: {e}")
        
        # Step 6: Add hydrogens (protonation)
        # NOTE: We skip PDBFixer hydrogen addition here and let pdb2pqr + propka
        # handle it instead (with pdb4amber --reduce as fallback). This prevents
        # duplicate/conflicting hydrogens, especially at N-termini of internal
        # chain breaks (e.g., NALA, NVAL, NGLN) which can fail Amber residue
        # template matching at openmmforcefields build time.
        if add_hydrogens:
            logger.info(f"Skipping PDBFixer hydrogen addition (pH {ph}) - pdb2pqr/propka will handle it")
            result["operations"].append({
                "step": "protonation",
                "status": "deferred",
                "details": f"Hydrogen addition deferred to pdb2pqr+propka (pH {ph} requested)"
            })
            # Store pH for potential future use
            result["requested_ph"] = ph
        else:
            result["operations"].append({
                "step": "protonation",
                "status": "skipped",
                "details": "Hydrogen addition skipped"
            })
            result["warnings"].append("Hydrogens not added - required for most MD simulations")
        
        # Step 7: Record if terminal caps were requested
        result["cap_termini_required"] = terminal_caps_requested
        
        # Step 8: Write output file
        logger.info(f"Writing cleaned structure to {output_file}")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w') as f:
            PDBFile.writeFile(fixer.topology, fixer.positions, f, keepIds=True)
        
        # Get final statistics
        final_residues = list(fixer.topology.residues())
        final_atoms = list(fixer.topology.atoms())
        result["statistics"]["final_residues"] = len(final_residues)
        result["statistics"]["final_atoms"] = len(final_atoms)
        
        result["operations"].append({
            "step": "write_output",
            "status": "success",
            "details": f"Wrote {len(final_atoms)} atoms to {output_file}"
        })
        
        # Step 9: pH-dependent protonation + Amber naming conversion
        # Primary: pdb2pqr + propka (pH-aware, proper Amber naming)
        # Fallback: pdb4amber --reduce (pH ignored, geometry-based)
        # If site-specific protonation states are provided, pdb2pqr/pdb4amber
        # first creates an Amber-compatible protein PDB, then OpenMM Modeller
        # applies the requested residue variants and validates the H pattern.
        logger.info(f"Applying pH-dependent protonation (pH {ph})")
        amber_output_file = input_path.parent / f"{stem}.amber.pdb"
        pdb2pqr_success = False
        user_protonation_applied: list[dict[str, str]] = []

        try:
            # Primary method: pdb2pqr + propka (pH-aware protonation with Amber naming).
            # We always run propka so that pdb2pqr produces correct Amber
            # terminal variant naming (NHID/CHIE/etc.) and matching atom
            # lists. User-supplied histidine_states are applied *on top*
            # of the propka result by renaming residues after pdb2pqr
            # finishes — skipping propka leaves terminal HIS residues
            # without correct N-terminal H1/H2 (or C-terminal OXT) atom
            # naming, which fails residue template matching when
            # openmmforcefields applies the Amber HIS variant template.
            if pdb2pqr_wrapper.is_available() and add_hydrogens:
                logger.info(f"Using pdb2pqr with propka for pH {ph}")
                pqr_output = input_path.parent / f"{stem}.pqr"

                pdb2pqr_args = [
                    str(output_file),
                    str(pqr_output),
                    "--ff", "AMBER",
                    "--ffout", "AMBER",
                    "--titration-state-method", "propka",
                    "--with-ph", str(ph),
                    "--pdb-output", str(amber_output_file),
                    "--keep-chain",
                    "--drop-water",
                ]

                try:
                    pdb2pqr_wrapper.run(pdb2pqr_args)

                    if amber_output_file.exists():
                        if requested_protonation_states:
                            protonation_result = _apply_protonation_states_with_modeller(
                                amber_output_file,
                                requested_protonation_states,
                                ph=ph,
                            )
                            if not protonation_result["success"]:
                                result["errors"].extend(protonation_result["errors"])
                                result["warnings"].extend(protonation_result["warnings"])
                                result["code"] = "protonation_state_override_failed"
                                return result
                            user_protonation_applied = protonation_result["applied_states"]
                            his_states = _extract_histidine_states(amber_output_file)
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+openmm_modeller_user_states",
                                "ph": ph,
                                "histidine_states": his_states,
                                "protonation_states": user_protonation_applied,
                            })
                            result["protonation_method"] = "pdb2pqr+openmm_modeller_user_states"
                            result["protonation_states"] = user_protonation_applied
                            logger.info(
                                f"Applied {len(user_protonation_applied)} user-specified "
                                "residue protonation state(s)"
                            )
                        else:
                            his_states = _extract_histidine_states(amber_output_file)
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+propka",
                                "ph": ph,
                                "histidine_states": his_states,
                            })
                            result["protonation_method"] = "pdb2pqr+propka"
                            logger.info(f"pH-aware protonation complete: {len(his_states)} histidine states determined")

                        result["output_file"] = str(amber_output_file)
                        result["pdbfixer_output"] = str(output_file)
                        result["histidine_states"] = his_states
                        pdb2pqr_success = True
                        if his_states:
                            logger.info(f"Histidine states: {his_states}")
                    else:
                        raise RuntimeError("pdb2pqr did not create output PDB file")

                except Exception as pdb2pqr_error:
                    logger.warning(f"pdb2pqr failed: {pdb2pqr_error}, falling back to pdb4amber")
                    result["warnings"].append(f"pdb2pqr failed: {pdb2pqr_error}")

            # Fallback method: pdb4amber --reduce (pH ignored, geometry-based)
            if not pdb2pqr_success:
                if add_hydrogens:
                    logger.warning(f"Using pdb4amber --reduce (pH {ph} will be ignored)")
                    result["warnings"].append(
                        f"pH {ph} protonation not applied: using geometry-based hydrogen assignment"
                    )
                    reduce_flag = ["--reduce"]
                else:
                    reduce_flag = []

                if not pdb4amber_wrapper.is_available():
                    raise RuntimeError("Neither pdb2pqr nor pdb4amber available for Amber conversion")

                pdb4amber_wrapper.run([
                    "-i", str(output_file),
                    "-o", str(amber_output_file),
                    *reduce_flag,
                    "-l", str(input_path.parent / f"{stem}.pdb4amber.log")
                ])

                if amber_output_file.exists():
                    op = {
                        "step": "protonation",
                        "status": "success",
                        "method": "pdb4amber+reduce",
                        "details": "Geometry-based hydrogen assignment (pH ignored)",
                    }
                    if requested_protonation_states:
                        protonation_result = _apply_protonation_states_with_modeller(
                            amber_output_file,
                            requested_protonation_states,
                            ph=ph,
                        )
                        if not protonation_result["success"]:
                            result["errors"].extend(protonation_result["errors"])
                            result["warnings"].extend(protonation_result["warnings"])
                            result["code"] = "protonation_state_override_failed"
                            return result
                        user_protonation_applied = protonation_result["applied_states"]
                        his_states = _extract_histidine_states(amber_output_file)
                        op.update({
                            "method": "pdb4amber+openmm_modeller_user_states",
                            "ph": ph,
                            "histidine_states": his_states,
                            "protonation_states": user_protonation_applied,
                        })
                        result["protonation_method"] = "pdb4amber+openmm_modeller_user_states"
                        result["protonation_states"] = user_protonation_applied
                        result["histidine_states"] = his_states
                    result["operations"].append(op)
                    result["output_file"] = str(amber_output_file)
                    result["pdbfixer_output"] = str(output_file)
                    if not requested_protonation_states:
                        result["protonation_method"] = "pdb4amber+reduce"
                    logger.info(f"pdb4amber conversion successful: {amber_output_file}")
                else:
                    raise RuntimeError("pdb4amber did not create output file")

        except Exception as e:
            error_msg = f"Amber conversion failed: {str(e)}"
            result["warnings"].append(error_msg)
            result["operations"].append({
                "step": "protonation",
                "status": "error",
                "details": error_msg
            })
            logger.warning(error_msg)
            # Keep the PDBFixer output as the final output if conversion fails
            result["warnings"].append("Using PDBFixer output without Amber naming convention conversion")

        # Step 10: Complete terminal-cap hydrogens, scoped to ACE/NME caps.
        # Topology generation intentionally does no generic H repair; capped
        # peptides must be hydrogen-complete before they leave prep.
        output_for_cap_completion = result.get("output_file")
        if output_for_cap_completion:
            expected_caps = {
                cap
                for cap in (resolved_n_terminal_cap, resolved_c_terminal_cap)
                if cap
            }
            cap_residues_present = (
                _pdb_residue_names(output_for_cap_completion) & TERMINAL_CAP_RESIDUES
            )
            if expected_caps or cap_residues_present:
                cap_h_result = _complete_terminal_cap_hydrogens_with_modeller(
                    output_for_cap_completion,
                    expected_caps=expected_caps,
                    forcefield_name=terminal_cap_forcefield,
                    ph=ph,
                )
                result["terminal_cap_hydrogen_completion"] = cap_h_result
                result["warnings"].extend(cap_h_result.get("warnings", []))
                if not cap_h_result["success"]:
                    result["errors"].extend(cap_h_result.get("errors", []))
                    result["code"] = cap_h_result.get(
                        "code",
                        "terminal_cap_hydrogen_completion_failed",
                    )
                    return result
                if not cap_h_result.get("skipped"):
                    result["output_file"] = cap_h_result["output_file"]
                    result["terminal_cap_forcefield"] = cap_h_result.get("forcefield")
                    result["operations"].append({
                        "step": "terminal_cap_hydrogen_completion",
                        "status": "success",
                        "method": "openmm_modeller",
                        "forcefield": cap_h_result.get("forcefield"),
                        "forcefield_xml": cap_h_result.get("forcefield_xml"),
                        "n_terminal_cap": resolved_n_terminal_cap,
                        "c_terminal_cap": resolved_c_terminal_cap,
                        "cap_residues_present": cap_h_result.get("cap_residues_present", []),
                        "cap_hydrogens_added": cap_h_result.get("cap_hydrogens_added", 0),
                    })

        # Build structured provenance summary at top level
        # (operations[] is kept for full detail, summary for quick access)
        operations = result.get("operations", [])
        provenance = {}
        for op in operations:
            step = op.get("step", "")
            if step == "missing_residues" and op.get("status") == "will_model":
                provenance["missing_residues_modeled"] = op.get("residues", [])
                provenance["missing_residues_count"] = op.get("count", 0)
            elif step == "nonstandard_residues" and op.get("status") == "replaced":
                provenance["nonstandard_residues_replaced"] = op.get("details", "")
            elif step == "protonation" and op.get("status") == "success":
                provenance["protonation_method"] = op.get("method", "")
                provenance["protonation_ph"] = op.get("ph")
                if op.get("histidine_states"):
                    provenance["histidine_states"] = op["histidine_states"]
                if op.get("protonation_states"):
                    provenance["protonation_states"] = op["protonation_states"]
            elif step == "disulfide_bonds":
                if op.get("status") in ("success", "modified"):
                    provenance["disulfide_bonds_applied"] = True
                    provenance["disulfide_bonds_details"] = op.get("details", "")
                elif op.get("status") == "none_found":
                    provenance["disulfide_bonds_applied"] = False
            elif step == "component_disposition" and op.get("status") == "excluded":
                provenance["component_disposition_recorded"] = True
                provenance["experimental_isotopes_excluded"] = True
                provenance["component_disposition_details"] = op.get("details", "")
            elif step == "terminal_caps" and op.get("status") == "added_to_missing":
                provenance["n_terminal_cap"] = op.get("n_terminal_cap")
                provenance["c_terminal_cap"] = op.get("c_terminal_cap")
                provenance["terminal_capping_recorded"] = True
            elif step == "terminal_cap_hydrogen_completion" and op.get("status") == "success":
                provenance["terminal_cap_hydrogen_completion_method"] = op.get("method")
                provenance["terminal_cap_forcefield"] = op.get("forcefield")
                provenance["terminal_cap_forcefield_xml"] = op.get("forcefield_xml")
                provenance["terminal_cap_hydrogens_added"] = op.get("cap_hydrogens_added", 0)
        result["provenance"] = provenance

        result["success"] = True
        logger.info(f"Successfully cleaned protein structure: {result['output_file']}")
        
    except Exception as e:
        error_msg = f"Error during protein cleaning: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        # Try to provide helpful context for common errors
        if "topology" in str(e).lower():
            result["errors"].append("Hint: The input file may have structural issues. Try using split_molecules first.")
        elif "residue" in str(e).lower():
            result["errors"].append("Hint: There may be unusual residues in the structure. Check for modified amino acids.")
        elif "atom" in str(e).lower():
            result["errors"].append("Hint: Atom naming or connectivity issues detected. Verify the input structure.")
    
    return result


def estimate_net_charge(
    ligand_file: str,
    ph: float = 7.4
) -> dict:
    """Estimate net charge of ligand using RDKit.
    
    Used as chemistry provenance for topology-time ligand handling.
    
    Args:
        ligand_file: Path to ligand structure file (PDB, MOL2, SDF)
        ph: Target pH for protonation state estimation
    
    Returns:
        Dict with:
            - success: bool - True if estimation completed successfully
            - ligand_file: str - Input ligand file path
            - formal_charge: int - Formal charge from molecule structure
            - estimated_charge_at_ph: int - Estimated charge at target pH
            - target_ph: float - Target pH used for estimation
            - ionizable_groups: list[dict] - Detected ionizable functional groups
            - confidence: str - Confidence level ('high', 'medium', 'low')
            - confidence_notes: list[str] - Reasons for confidence level
            - molecular_formula: str - Molecular formula
            - num_atoms: int - Total number of atoms
            - num_heavy_atoms: int - Number of heavy atoms
            - smiles: str - SMILES representation
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Estimating net charge for: {ligand_file}")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "ligand_file": str(ligand_file),
        "formal_charge": None,
        "estimated_charge_at_ph": None,
        "target_ph": ph,
        "ionizable_groups": [],
        "confidence": None,
        "confidence_notes": [],
        "molecular_formula": None,
        "num_atoms": 0,
        "num_heavy_atoms": 0,
        "smiles": None,
        "errors": [],
        "warnings": []
    }
    
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
    except ImportError:
        result["errors"].append("RDKit not installed. Install via conda.")
        logger.error("RDKit not installed")
        return result
    
    ligand_path = Path(ligand_file)
    if not ligand_path.exists():
        result["errors"].append(f"Ligand file not found: {ligand_file}")
        logger.error(f"Ligand file not found: {ligand_file}")
        return result
    
    # Load molecule based on file format
    suffix = ligand_path.suffix.lower()
    mol = None
    
    try:
        if suffix == '.pdb':
            mol = Chem.MolFromPDBFile(str(ligand_path), removeHs=False)
        elif suffix == '.mol2':
            mol = Chem.MolFromMol2File(str(ligand_path), removeHs=False)
        elif suffix in ['.sdf', '.mol']:
            mol = Chem.MolFromMolFile(str(ligand_path), removeHs=False)
        else:
            result["errors"].append(f"Unsupported file format: {suffix}")
            result["errors"].append("Hint: Supported formats are .pdb, .mol2, .sdf, .mol")
            logger.error(f"Unsupported file format: {suffix}")
            return result
        
        if mol is None:
            result["errors"].append(f"Could not parse ligand file: {ligand_file}")
            result["errors"].append("Hint: The file may be corrupted or contain invalid molecular data")
            logger.error(f"Could not parse ligand file: {ligand_file}")
            return result
        
        # Get charge estimation
        charge_info = _estimate_charge_rdkit(mol)
        
        # Estimate physiological charge
        estimated_charge = _estimate_physiological_charge(charge_info, ph)
        
        # Calculate confidence
        confidence = "high"
        confidence_notes = []
        
        if len(charge_info["ionizable_groups"]) > 2:
            confidence = "medium"
            confidence_notes.append("Multiple ionizable groups detected")
        
        # Check for unusual structures
        num_atoms = mol.GetNumAtoms()
        if num_atoms > 100:
            confidence = "medium"
            confidence_notes.append("Large molecule - charge estimation may be less reliable")
        
        # Check for metals
        metals = ["Fe", "Cu", "Zn", "Mg", "Ca", "Mn"]
        for atom in mol.GetAtoms():
            if atom.GetSymbol() in metals:
                confidence = "low"
                confidence_notes.append(f"Metal atom ({atom.GetSymbol()}) detected - manual charge verification recommended")
                break
        
        result["formal_charge"] = charge_info["formal_charge"]
        result["estimated_charge_at_ph"] = estimated_charge
        result["ionizable_groups"] = charge_info["ionizable_groups"]
        result["confidence"] = confidence
        result["confidence_notes"] = confidence_notes
        result["molecular_formula"] = rdMolDescriptors.CalcMolFormula(mol)
        result["num_atoms"] = num_atoms
        result["num_heavy_atoms"] = mol.GetNumHeavyAtoms()
        result["smiles"] = Chem.MolToSmiles(mol)
        result["success"] = True
        
        logger.info(f"Estimated charge: {estimated_charge} (formal: {charge_info['formal_charge']}, confidence: {confidence})")
        
    except Exception as e:
        error_msg = f"Error during charge estimation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def clean_ligand(
    ligand_pdb: str,
    ligand_id: str,
    smiles: Optional[str] = None,
    output_dir: Optional[str] = None,
    optimize: bool = True,
    max_opt_iters: int = 200,
    fetch_smiles: bool = True,
    target_ph: float = 7.4,
    manual_charge: Optional[int] = None
) -> dict:
    """Clean ligand chemistry using SMILES template matching.
    
    Workflow for robust ligand preparation:
    1. Get correct SMILES (user-provided > CCD API > known dictionary)
    2. Apply pH-dependent protonation using Dimorphite-DL
    3. Use AssignBondOrdersFromTemplate to assign correct bond orders
    4. Add hydrogens with correct geometry
    5. Optionally optimize with MMFF94
    6. Calculate net charge from protonated molecule
    7. Output SDF format (preserves bond orders) and a matching PDB for merge
    
    This approach eliminates bond order ambiguity and ensures correct protonation
    state for the target pH.
    
    Args:
        ligand_pdb: Path to ligand PDB file (from split_molecules)
        ligand_id: 3-letter ligand residue name (e.g., 'ATP', 'SAH')
        smiles: User-provided SMILES (highest priority, bypasses API lookup)
        output_dir: Output directory (uses ligand dir if None)
        optimize: Whether to run MMFF94 optimization
        max_opt_iters: Maximum optimization iterations
        fetch_smiles: Whether to fetch SMILES from PDB CCD API
        target_ph: Target pH for protonation state (default: 7.4)
        manual_charge: Override calculated net charge (for complex cases)
    
    Returns:
        Dict with:
            - success: bool - True if preparation completed successfully
            - ligand_pdb: str - Input ligand PDB path
            - ligand_id: str - Ligand identifier
            - sdf_file: str - Path to prepared SDF file
            - pdb_file: str - Path to prepared PDB file
            - net_charge: int - Calculated net charge at target pH
            - charge_source: str - Source of charge value ('dimorphite', 'manual')
            - mol_formal_charge: int - Formal charge from molecule
            - smiles_used: str - SMILES that was used (protonated form)
            - smiles_original: str - Original SMILES before protonation
            - smiles_source: str - Where SMILES came from ('user', 'ccd', 'dictionary')
            - target_ph: float - Target pH used for protonation
            - num_atoms: int - Total number of atoms
            - num_heavy_atoms: int - Number of heavy atoms
            - optimized: bool - Whether optimization was performed
            - optimization_converged: bool - Whether optimization converged
            - output_dir: str - Output directory path
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = clean_ligand(
        ...     "ligand_ATP_chainA.pdb", 
        ...     "ATP",
        ...     target_ph=7.4,  # Physiological pH
        ...     optimize=True
        ... )
        >>> print(f"Charge at pH 7.4: {result['net_charge']}")
        >>> print(result['sdf_file'])  # Chemistry artifact for topology build
    """
    logger.info(f"Cleaning ligand: {ligand_pdb} (ID: {ligand_id})")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "ligand_pdb": str(ligand_pdb),
        "ligand_id": ligand_id,
        "sdf_file": None,
        "pdb_file": None,
        "net_charge": None,
        "charge_source": None,
        "mol_formal_charge": None,
        "smiles_used": None,
        "smiles_original": None,
        "smiles_source": None,
        "target_ph": target_ph,
        "num_atoms": 0,
        "num_heavy_atoms": 0,
        "optimized": optimize,
        "optimization_converged": False,
        "output_dir": None,
        "errors": [],
        "warnings": []
    }
    
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        result["errors"].append("RDKit not installed. Install via conda.")
        logger.error("RDKit not installed")
        return result
    
    ligand_path = Path(ligand_pdb).resolve()
    if not ligand_path.exists():
        result["errors"].append(f"Ligand PDB not found: {ligand_pdb}")
        logger.error(f"Ligand PDB not found: {ligand_pdb}")
        return result
    
    if output_dir is None:
        out_dir = ligand_path.parent
    else:
        out_dir = Path(output_dir).resolve()
    ensure_directory(out_dir)
    result["output_dir"] = str(out_dir)
    
    try:
        # Step 1: Get SMILES (source of truth for bond orders)
        smiles_source = None
        smiles_used = None
        
        if smiles:
            smiles_used = smiles
            smiles_source = "user"
            logger.info(f"Using user-provided SMILES for {ligand_id}")
        else:
            # Try to get SMILES from CCD or dictionary
            smiles_used = _get_ligand_smiles(ligand_id, user_smiles=None, fetch_from_ccd=fetch_smiles)
            if smiles_used:
                if fetch_smiles:
                    # Check if it came from CCD or dictionary
                    ccd_smiles = _fetch_smiles_from_ccd(ligand_id) if fetch_smiles else None
                    smiles_source = "ccd" if ccd_smiles == smiles_used else "dictionary"
                else:
                    smiles_source = "dictionary"
        
        if not smiles_used:
            result["errors"].append(f"No SMILES found for ligand {ligand_id}")
            result["errors"].append("Hint: Provide SMILES manually via the 'smiles' parameter, "
                                   "or add it to KNOWN_LIGAND_SMILES dictionary")
            logger.error(f"No SMILES found for ligand {ligand_id}")
            return result
        
        logger.info(f"Using SMILES from {smiles_source}: {smiles_used[:50]}...")
        
        # Store original SMILES before protonation
        smiles_original = smiles_used
        result["smiles_original"] = smiles_original
        result["smiles_source"] = smiles_source
        
        # Step 2: Apply pH-dependent protonation using Dimorphite-DL
        # This converts neutral CCD SMILES to correct protonation state
        protonated_smiles, calculated_charge = _apply_ph_protonation(smiles_used, target_ph)
        
        # Use protonated SMILES for template matching
        smiles_used = protonated_smiles
        result["smiles_used"] = smiles_used
        
        logger.info(f"Protonated SMILES at pH {target_ph}: {smiles_used[:50]}...")
        logger.info(f"Calculated net charge: {calculated_charge}")
        
        # Step 3: Read PDB (without sanitization to avoid bond order issues)
        pdb_mol = Chem.MolFromPDBFile(str(ligand_path), removeHs=False, sanitize=False)
        if pdb_mol is None:
            result["errors"].append(f"Failed to read PDB file: {ligand_pdb}")
            result["errors"].append("Hint: The PDB file may be corrupted or contain invalid atom data")
            logger.error(f"Failed to read PDB file: {ligand_pdb}")
            return result
        
        logger.info(f"Read PDB: {pdb_mol.GetNumAtoms()} atoms")
        
        # Step 4: Assign bond orders from SMILES template
        try:
            mol_with_bonds = _assign_bond_orders_from_smiles(pdb_mol, smiles_used)
        except ValueError as e:
            # If template matching fails, try without hydrogens
            logger.warning(f"Template matching failed, trying with hydrogen removal: {e}")
            result["warnings"].append(f"Template matching with H failed: {str(e)}, trying without H")
            pdb_mol_no_h = Chem.RemoveHs(pdb_mol)
            template = Chem.MolFromSmiles(smiles_used)
            if template:
                try:
                    mol_with_bonds = AllChem.AssignBondOrdersFromTemplate(template, pdb_mol_no_h)
                    Chem.SanitizeMol(mol_with_bonds)
                except Exception as e2:
                    result["errors"].append(f"Template matching failed even after H removal: {str(e2)}")
                    result["errors"].append("Hint: The PDB structure may not match the SMILES. "
                                           "Try providing a correct SMILES manually.")
                    logger.error(f"Template matching failed: {e2}")
                    return result
            else:
                result["errors"].append(f"Invalid SMILES template: {smiles_used}")
                logger.error(f"Invalid SMILES template: {smiles_used}")
                return result
        
        # Step 5: Add hydrogens with 3D coordinates
        mol_with_h = Chem.AddHs(mol_with_bonds, addCoords=True)
        logger.info(f"Added hydrogens: {mol_with_h.GetNumAtoms()} total atoms")
        
        # Step 6: Optional MMFF94 optimization
        optimization_converged = False
        if optimize:
            logger.info(f"Running MMFF94 optimization (max {max_opt_iters} iters)...")
            mol_with_h, optimization_converged = _optimize_ligand_rdkit(
                mol_with_h, max_iters=max_opt_iters, force_field="MMFF94"
            )
            result["optimization_converged"] = optimization_converged
        
        # Step 7: Determine net charge
        # Priority: manual_charge > Dimorphite-DL calculated_charge > GetFormalCharge
        mol_formal_charge = Chem.GetFormalCharge(mol_with_h)
        result["mol_formal_charge"] = mol_formal_charge
        
        if manual_charge is not None:
            net_charge = manual_charge
            charge_source = "manual"
            logger.info(f"Using manual override charge: {net_charge}")
        else:
            # Use Dimorphite-DL calculated charge
            net_charge = calculated_charge
            charge_source = "dimorphite"
            
            # Log any discrepancy
            if mol_formal_charge != calculated_charge:
                result["warnings"].append(
                    f"Charge discrepancy: mol formal={mol_formal_charge}, "
                    f"Dimorphite={calculated_charge}. Using Dimorphite result."
                )
                logger.warning(
                    f"Charge discrepancy: mol formal={mol_formal_charge}, "
                    f"Dimorphite={calculated_charge}. Using Dimorphite result."
                )
        
        result["net_charge"] = net_charge
        result["charge_source"] = charge_source
        logger.info(f"Final net charge: {net_charge} (source: {charge_source})")
        
        # Step 8: Write chemistry and coordinate artifacts. The SDF is the
        # chemistry source for topology; the PDB lets prepare_complex merge the
        # same hydrogenated ligand coordinates into the prepared complex.
        # Force 3D flag on conformer for downstream tool compatibility.
        if mol_with_h.GetNumConformers() > 0:
            mol_with_h.GetConformer().Set3D(True)

        output_sdf = out_dir / f"{ligand_path.stem}_prepared.sdf"
        output_pdb = out_dir / f"{ligand_path.stem}_prepared.pdb"

        writer = Chem.SDWriter(str(output_sdf))
        writer.SetForceV3000(False)
        writer.write(mol_with_h)
        writer.close()

        # Keep residue identity stable in the PDB emitted from RDKit. Existing
        # heavy-atom PDB residue info usually survives template matching; new
        # hydrogens need explicit names/residue metadata.
        first_info = None
        for atom in mol_with_h.GetAtoms():
            info = atom.GetPDBResidueInfo()
            if info is not None:
                first_info = info
                break
        chain_id = first_info.GetChainId().strip() if first_info else ""
        residue_number = first_info.GetResidueNumber() if first_info else 1
        residue_name = ligand_id[:3].upper()
        for idx, atom in enumerate(mol_with_h.GetAtoms(), start=1):
            info = atom.GetPDBResidueInfo()
            if info is None:
                symbol = atom.GetSymbol().upper()
                atom_name = f"{symbol}{idx % 1000:>3}"[-4:]
                info = Chem.AtomPDBResidueInfo()
                info.SetName(atom_name)
                info.SetChainId(chain_id or " ")
                info.SetResidueNumber(int(residue_number) if isinstance(residue_number, int) else 1)
            info.SetResidueName(residue_name)
            atom.SetMonomerInfo(info)
        Chem.MolToPDBFile(mol_with_h, str(output_pdb))
        
        logger.info(f"Wrote prepared ligand: {output_sdf}")
        
        # Verify output
        if not output_sdf.exists():
            result["errors"].append(f"Failed to create output SDF: {output_sdf}")
            logger.error(f"Failed to create output SDF: {output_sdf}")
            return result
        if not output_pdb.exists():
            result["errors"].append(f"Failed to create output PDB: {output_pdb}")
            logger.error(f"Failed to create output PDB: {output_pdb}")
            return result

        result["sdf_file"] = str(output_sdf)
        result["pdb_file"] = str(output_pdb)
        result["num_atoms"] = mol_with_h.GetNumAtoms()
        result["num_heavy_atoms"] = mol_with_h.GetNumHeavyAtoms()
        result["success"] = True
        
        logger.info(f"Successfully cleaned ligand: {output_sdf}")
        
    except Exception as e:
        error_msg = f"Error during ligand cleaning: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "template" in str(e).lower():
            result["errors"].append("Hint: Template matching issue - verify SMILES matches the PDB structure")
        elif "sanitize" in str(e).lower():
            result["errors"].append("Hint: Chemical validation failed - check for unusual atoms or bonds")
    
    return result


def _pdb_atom_count(pdb_file: str | Path) -> int:
    """Count atom records in a PDB file."""
    return sum(
        1
        for line in Path(pdb_file).read_text().splitlines()
        if line.startswith(("ATOM  ", "HETATM"))
    )


def _pdb_hydrogen_count(pdb_file: str | Path) -> int:
    """Count hydrogen-like atom records in a PDB file."""
    count = 0
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip().upper()
        if element in {"H", "D"} or atom_name.startswith(("H", "D")):
            count += 1
    return count


def _normalize_terminal_cap_choice(
    value: str | None,
    *,
    terminus: str,
) -> str | None:
    """Normalize a user-facing terminal cap choice.

    The current Amber/OpenMM path only supports the standard ACE/NME pair.
    ``None`` and common explicit "no cap" spellings mean uncapped.
    """
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in {"", "NONE", "NO", "FALSE", "UNCAPPED", "OFF"}:
        return None
    allowed = (
        SUPPORTED_N_TERMINAL_CAPS
        if terminus == "n"
        else SUPPORTED_C_TERMINAL_CAPS
    )
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(
            f"Unsupported {terminus.upper()}-terminal cap {value!r}; "
            f"supported values are: {allowed_text}, or none"
        )
    return normalized


def _resolve_terminal_cap_settings(
    *,
    cap_termini: bool,
    n_terminal_cap: str | None,
    c_terminal_cap: str | None,
) -> tuple[str | None, str | None]:
    """Resolve legacy ``cap_termini`` and explicit one-sided cap settings."""
    n_cap = _normalize_terminal_cap_choice(n_terminal_cap, terminus="n")
    c_cap = _normalize_terminal_cap_choice(c_terminal_cap, terminus="c")
    if cap_termini:
        if n_terminal_cap is None:
            n_cap = "ACE"
        if c_terminal_cap is None:
            c_cap = "NME"
    return n_cap, c_cap


def _pdb_residue_names(pdb_file: str | Path) -> set[str]:
    """Return residue names present in a PDB file."""
    names: set[str] = set()
    for line in Path(pdb_file).read_text().splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 20:
            names.add(line[17:20].strip().upper())
    return names


def _pdb_hydrogen_counts_by_resname(
    pdb_file: str | Path,
    residue_names: set[str],
) -> dict[str, int]:
    """Count hydrogen-like atom records grouped by residue name."""
    counts = {name: 0 for name in residue_names}
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname not in counts:
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip().upper()
        if element in {"H", "D"} or atom_name.startswith(("H", "D")):
            counts[resname] += 1
    return counts


def _pdb_noncap_protein_hydrogen_signature(
    pdb_file: str | Path,
) -> dict[str, tuple[str, ...]]:
    """Return non-cap protein H atom-name sets keyed by residue identity."""
    hydrogens: dict[str, list[str]] = {}
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith("ATOM  "):
            continue
        resname = line[17:20].strip().upper()
        if resname in TERMINAL_CAP_RESIDUES:
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip().upper()
        if element not in {"H", "D"} and not atom_name.startswith(("H", "D")):
            continue
        chain = line[21:22].strip()
        resseq = line[22:26].strip()
        icode = line[26:27].strip()
        key = f"{chain}:{resseq}:{icode}:{resname}"
        hydrogens.setdefault(key, []).append(atom_name)
    return {
        key: tuple(sorted(names))
        for key, names in hydrogens.items()
    }


def _terminal_cap_forcefield_xml(forcefield_name: str | None) -> tuple[str | None, str | None]:
    """Resolve a protein force field name to the XML used for cap H completion."""
    from mdclaw import forcefield_catalog as _ff_catalog

    requested = forcefield_name or DEFAULT_TERMINAL_CAP_FORCEFIELD
    canonical = _ff_catalog.normalize_protein(requested)
    if not canonical or canonical not in _ff_catalog.PROTEIN_FORCEFIELDS:
        return None, requested
    entry = _ff_catalog.PROTEIN_FORCEFIELDS[canonical]
    if not entry.openmm_xml:
        return None, canonical
    return entry.openmm_xml[0], canonical


def _complete_terminal_cap_hydrogens_with_modeller(
    pdb_file: str | Path,
    *,
    expected_caps: set[str] | None = None,
    forcefield_name: str | None = None,
    ph: float = 7.4,
) -> dict:
    """Complete ACE/NME cap hydrogens with OpenMM Modeller during prep.

    This is deliberately a prep-only, cap-scoped helper. Topology generation
    still validates atom/H completeness and does not perform generic repair.
    """
    input_path = Path(pdb_file).resolve()
    output_file = input_path.with_name(f"{input_path.stem}.cap_h.pdb")
    expected_caps = {str(c).upper() for c in (expected_caps or set()) if c}
    result: dict[str, Any] = {
        "success": False,
        "input_file": str(input_path),
        "output_file": str(output_file),
        "method": "openmm_modeller",
        "forcefield": forcefield_name or DEFAULT_TERMINAL_CAP_FORCEFIELD,
        "forcefield_xml": None,
        "cap_residues_present": [],
        "expected_caps": sorted(expected_caps),
        "hydrogens_added": 0,
        "cap_hydrogens_added": 0,
        "cap_hydrogen_count_before": {},
        "cap_hydrogen_count_after": {},
        "noncap_hydrogen_signature_preserved": None,
        "noncap_hydrogen_signature_changed_residues": [],
        "warnings": [],
        "errors": [],
        "operations": [],
    }

    if not input_path.exists():
        result["code"] = "terminal_cap_hydrogen_completion_failed"
        result["errors"].append(f"Input PDB not found: {input_path}")
        return result

    present_caps = _pdb_residue_names(input_path) & TERMINAL_CAP_RESIDUES
    result["cap_residues_present"] = sorted(present_caps)
    missing_expected = sorted(expected_caps - present_caps)
    if missing_expected:
        result["code"] = "terminal_cap_missing"
        result["errors"].append(
            "Requested terminal cap residue(s) are absent after cleaning: "
            f"{missing_expected}"
        )
        return result
    if not present_caps:
        result["success"] = True
        result["skipped"] = True
        result["operations"].append({
            "step": "terminal_cap_hydrogen_completion",
            "status": "skipped",
            "details": "No ACE/NME terminal cap residues present",
        })
        return result

    forcefield_xml, canonical_forcefield = _terminal_cap_forcefield_xml(forcefield_name)
    result["forcefield"] = canonical_forcefield or result["forcefield"]
    result["forcefield_xml"] = forcefield_xml
    if not forcefield_xml:
        result["code"] = "terminal_cap_hydrogen_completion_unavailable"
        result["errors"].append(
            "Could not resolve an OpenMM protein force-field XML for terminal "
            f"cap hydrogen completion: {forcefield_name!r}"
        )
        return result

    residues_before = _read_pdb_unique_residues(input_path)
    cap_h_before = _pdb_hydrogen_counts_by_resname(input_path, present_caps)
    noncap_h_signature_before = _pdb_noncap_protein_hydrogen_signature(input_path)
    total_h_before = _pdb_hydrogen_count(input_path)
    result["cap_hydrogen_count_before"] = cap_h_before

    try:
        from openmm.app import ForceField, Modeller

        pdb = PDBFile(str(input_path))
        forcefield = ForceField(forcefield_xml)
        modeller = Modeller(pdb.topology, pdb.positions)
        modeller.addHydrogens(forcefield, pH=ph)
        with output_file.open("w") as handle:
            PDBFile.writeFile(
                modeller.topology,
                modeller.positions,
                handle,
                keepIds=True,
            )
    except Exception as exc:  # noqa: BLE001
        result["code"] = "terminal_cap_hydrogen_completion_failed"
        result["errors"].append(
            f"Terminal cap hydrogen completion failed: {type(exc).__name__}: {exc}"
        )
        return result

    residues_after = _read_pdb_unique_residues(output_file)
    if residues_after != residues_before:
        result["code"] = "terminal_cap_hydrogen_completion_failed"
        result["errors"].append(
            "Terminal cap hydrogen completion changed residue identity/order."
        )
        return result

    noncap_h_signature_after = _pdb_noncap_protein_hydrogen_signature(output_file)
    if noncap_h_signature_after != noncap_h_signature_before:
        changed = sorted(
            key
            for key in (
                set(noncap_h_signature_before)
                | set(noncap_h_signature_after)
            )
            if noncap_h_signature_before.get(key)
            != noncap_h_signature_after.get(key)
        )
        result["code"] = "terminal_cap_hydrogen_completion_changed_noncap_hydrogens"
        result["noncap_hydrogen_signature_preserved"] = False
        result["noncap_hydrogen_signature_changed_residues"] = changed
        preview = ", ".join(changed[:5])
        if len(changed) > 5:
            preview += f", ... (+{len(changed) - 5} more)"
        result["errors"].append(
            "Terminal cap hydrogen completion changed non-cap protein "
            f"hydrogens: {preview}"
        )
        return result
    result["noncap_hydrogen_signature_preserved"] = True

    cap_h_after = _pdb_hydrogen_counts_by_resname(output_file, present_caps)
    total_h_after = _pdb_hydrogen_count(output_file)
    cap_added = sum(cap_h_after.values()) - sum(cap_h_before.values())
    result["cap_hydrogen_count_after"] = cap_h_after
    result["hydrogens_added"] = max(0, total_h_after - total_h_before)
    result["cap_hydrogens_added"] = max(0, cap_added)
    if result["cap_hydrogens_added"] == 0:
        result["warnings"].append(
            "OpenMM Modeller completed but did not add cap hydrogens; "
            "the cap residues may already have been hydrogen-complete."
        )

    result["operations"].append({
        "step": "terminal_cap_hydrogen_completion",
        "status": "success",
        "method": "openmm_modeller",
        "forcefield": result["forcefield"],
        "forcefield_xml": forcefield_xml,
        "ph": ph,
        "cap_residues_present": sorted(present_caps),
        "cap_hydrogens_added": result["cap_hydrogens_added"],
    })
    result["success"] = True
    return result


def _prepare_standard_nucleic(
    nucleic_file: str,
    *,
    nucleic_subtype: str | None,
    ph: float,
) -> dict:
    """Rebuild hydrogens for a standard DNA/RNA chain with OpenMM Modeller."""
    input_path = Path(nucleic_file).resolve()
    output_file = input_path.with_name(f"{input_path.stem}.nucleic_h.pdb")
    result: dict[str, Any] = {
        "success": False,
        "input_file": str(input_path),
        "output_file": str(output_file),
        "nucleic_subtype": nucleic_subtype,
        "hydrogen_rebuild_method": "openmm_modeller",
        "nucleic_forcefield_xml": None,
        "hydrogens_added": 0,
        "atom_count_before": 0,
        "atom_count_after": 0,
        "hydrogen_count_before": 0,
        "hydrogen_count_after": 0,
        "warnings": [],
        "errors": [],
        "operations": [],
    }

    residues_before = _read_pdb_unique_residues(input_path)
    residue_names = {str(r["resname"]).upper() for r in residues_before}
    nucleic_info = classify_nucleic_residues(residue_names)
    subtype = (nucleic_subtype or nucleic_info.get("subtype") or "").lower()
    result["nucleic_subtype"] = subtype or nucleic_subtype

    if nucleic_info.get("modified_residue_names") or subtype not in {"dna", "rna"}:
        result["code"] = "unsupported_modified_nucleic_residue"
        result["errors"].append(
            f"{MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE} "
            f"Residues={sorted(residue_names)} subtype={subtype or 'unknown'}."
        )
        return result

    if subtype == "dna":
        forcefield_xml = "amber/DNA.OL15.xml"
    else:
        forcefield_xml = "amber/RNA.OL3.xml"
    result["nucleic_forcefield_xml"] = forcefield_xml

    try:
        from openmm.app import ForceField, Modeller
    except Exception as exc:  # noqa: BLE001
        result["code"] = "nucleic_hydrogen_rebuild_unavailable"
        result["errors"].append(
            f"OpenMM Modeller/ForceField is required for standard nucleic "
            f"hydrogen rebuild: {type(exc).__name__}: {exc}"
        )
        return result

    try:
        result["atom_count_before"] = _pdb_atom_count(input_path)
        result["hydrogen_count_before"] = _pdb_hydrogen_count(input_path)
        pdb = PDBFile(str(input_path))
        forcefield = ForceField(forcefield_xml)
        modeller = Modeller(pdb.topology, pdb.positions)
        variants = modeller.addHydrogens(forcefield, pH=ph)
        with output_file.open("w") as handle:
            PDBFile.writeFile(
                modeller.topology,
                modeller.positions,
                handle,
                keepIds=True,
            )
    except ValueError as exc:
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            f"Standard nucleic hydrogen rebuild failed: {type(exc).__name__}: {exc}"
        )
        return result
    except Exception as exc:  # noqa: BLE001
        code = (
            "nucleic_hydrogen_rebuild_unavailable"
            if "Could not locate file" in str(exc)
            else "nucleic_hydrogen_rebuild_failed"
        )
        result["code"] = code
        result["errors"].append(
            f"Standard nucleic hydrogen rebuild failed: {type(exc).__name__}: {exc}"
        )
        return result

    result["atom_count_after"] = _pdb_atom_count(output_file)
    result["hydrogen_count_after"] = _pdb_hydrogen_count(output_file)
    result["hydrogens_added"] = max(
        0,
        result["hydrogen_count_after"] - result["hydrogen_count_before"],
    )
    result["variants"] = [
        str(v) if v is not None else None
        for v in variants
    ]

    residues_after = _read_pdb_unique_residues(output_file)
    if residues_after != residues_before:
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            "Nucleic hydrogen rebuild changed residue identity/order."
        )
        return result
    if result["atom_count_after"] < result["atom_count_before"]:
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            "Nucleic hydrogen rebuild removed atom records unexpectedly."
        )
        return result
    if (
        result["hydrogen_count_before"] == 0
        and result["hydrogen_count_after"] == 0
    ):
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            "Nucleic hydrogen rebuild completed without adding hydrogens."
        )
        return result

    result["operations"].append({
        "step": "nucleic_hydrogen_rebuild",
        "status": "success",
        "method": "openmm_modeller",
        "forcefield_xml": forcefield_xml,
        "ph": ph,
        "hydrogens_added": result["hydrogens_added"],
    })
    result["success"] = True
    return result


def _fix_amino_acid_hetatm_records(pdb_file: Path) -> None:
    """Convert HETATM to ATOM for residues with amino acid backbone.

    gemmi doesn't recognize Amber residue naming (HIE, NALA, etc.) and
    writes them as HETATM. Detect amino acids by backbone atoms (N, CA, C)
    instead of maintaining a residue name list.

    Also removes HET header records for amino acid residues, which confuse
    external tools like MEMEMBED (used by packmol-memgen for membrane embedding).
    """
    import gemmi

    # Read structure to identify amino acid residues
    st = gemmi.read_pdb(str(pdb_file))
    amino_acid_residues = set()  # (chain_id, resnum, resname)
    amino_acid_resnames = set()  # Just resnames for HET record filtering

    for model in st:
        for chain in model:
            for res in chain:
                atom_names = {a.name for a in res}
                # Check for backbone atoms (N, CA, C)
                if {"N", "CA", "C"}.issubset(atom_names):
                    amino_acid_residues.add((chain.name, res.seqid.num, res.name))
                    amino_acid_resnames.add(res.name)

    if not amino_acid_residues:
        return  # No amino acids to fix

    # Fix HETATM records and remove HET header records for amino acids
    with open(pdb_file) as f:
        lines = f.readlines()

    fixed_hetatm_count = 0
    removed_het_count = 0
    fixed_lines = []
    for line in lines:
        # Remove HET header records for amino acid residues
        # Format: HET    resname chain resnum  natoms
        if line.startswith("HET ") or line.startswith("HET\t"):
            try:
                parts = line.split()
                if len(parts) >= 2:
                    het_resname = parts[1].strip()
                    if het_resname in amino_acid_resnames:
                        removed_het_count += 1
                        continue  # Skip this HET record
            except (IndexError, ValueError):
                pass
        # Convert HETATM to ATOM for amino acid residues
        elif line.startswith("HETATM"):
            chain_id = line[21].strip() or line[21]
            try:
                resnum = int(line[22:26])
                resname = line[17:20].strip()
                if (chain_id, resnum, resname) in amino_acid_residues:
                    line = "ATOM  " + line[6:]
                    fixed_hetatm_count += 1
            except ValueError:
                pass
        fixed_lines.append(line)

    with open(pdb_file, 'w') as f:
        f.writelines(fixed_lines)

    if fixed_hetatm_count > 0:
        logger.info(f"Fixed {fixed_hetatm_count} HETATM records to ATOM for amino acid residues")
    if removed_het_count > 0:
        logger.info(f"Removed {removed_het_count} HET header records for amino acid residues")


def _iter_unique_conect_bonds(conect_map: dict) -> list[tuple[int, int, int]]:
    """Return unique PDB CONECT bonds as ``(serial1, serial2, order)``.

    Gemmi stores CONECT as a low-level serial-number map.  Some writers emit
    both directions, and bond order is represented by repeating the partner
    serial.  Collapse those records into one unordered bond while preserving
    the maximum directional repeat count as the order.
    """
    directional_counts: Dict[Tuple[int, int], int] = {}
    for serial1, partners in (conect_map or {}).items():
        try:
            s1 = int(serial1)
        except (TypeError, ValueError):
            continue
        for partner in partners or []:
            try:
                s2 = int(partner)
            except (TypeError, ValueError):
                continue
            if s1 <= 0 or s2 <= 0 or s1 == s2:
                continue
            directional_counts[(s1, s2)] = directional_counts.get((s1, s2), 0) + 1

    pair_orders: Dict[Tuple[int, int], int] = {}
    for (s1, s2), count in directional_counts.items():
        key = (s1, s2) if s1 < s2 else (s2, s1)
        pair_orders[key] = max(pair_orders.get(key, 0), count)

    return [
        (s1, s2, order)
        for (s1, s2), order in sorted(pair_orders.items())
    ]


def merge_structures(
    pdb_files: List[str],
    output_dir: Optional[str] = None,
    output_name: str = "merged"
) -> dict:
    """Merge multiple PDB files into a single structure file.
    
    This tool combines multiple protein and ligand PDB files into one unified
    structure suitable for solvation or membrane embedding with packmol-memgen.
    
    PDB chain IDs are automatically assigned as short compatibility labels:
    - First 26 chains: A-Z
    - Next 26 chains: a-z
    - Next 10 chains: 0-9
    - Additional chains reuse the same pool.

    The one-character PDB chain ID is not a canonical identity.  For systems
    with many chains, use the returned ``chain_identity_map`` entries
    (component_id + topology_chain_index + atom/residue index ranges) to track
    source components unambiguously.
    
    Atom names are preserved exactly as they appear in the input files,
    which helps subsequent openmmforcefields builds preserve component identity.
    
    Args:
        pdb_files: List of PDB file paths to merge. Accepts:
                   - *.amber.pdb from clean_protein
                   - *_prepared.pdb from clean_ligand
                   - *.pdb for standard force-field components
        output_dir: Output directory (auto-generated if None)
        output_name: Base name for output file (default: "merged")
    
    Returns:
        Dict with:
            - success: bool - True if merge completed successfully
            - job_id: str - Unique identifier for this operation
            - output_file: str - Path to the merged PDB file
            - output_dir: str - Output directory path
            - input_files: list[str] - List of input file paths
            - chain_mapping: dict - Mapping of {input_file: {original_chain: new_chain}}
            - chain_identity_map: dict - Component-level source-to-merged chain
              identity map; required when PDB chain IDs are reused
            - chain_identity_map_file: str - JSON file storing chain_identity_map
            - statistics: dict - Summary statistics (total_atoms, total_residues, etc.)
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = merge_structures([
        ...     "output/job1/protein_1.amber.pdb",
        ...     "output/job1/ligand_1.amber.pdb"
        ... ])
        >>> print(result["output_file"])
        'output/abc123/merged.pdb'
    """
    logger.info(f"Merging {len(pdb_files)} structure files")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_file": None,
        "output_dir": None,
        "input_files": pdb_files,
        "chain_mapping": {},
        "chain_mapping_entries": [],
        "chain_identity_map": {
            "schema_version": "mdclaw.chain_identity_map.v1",
            "identity_contract": (
                "PDB chain IDs are MD compatibility labels and may be reused; "
                "component_id plus topology_chain_index and atom/residue ranges "
                "are the canonical identities."
            ),
            "pdb_chain_id_policy": "cycle_A-Z_a-z_0-9",
            "pdb_chain_id_pool_size": len(PDB_CHAIN_ID_POOL),
            "pdb_chain_ids_may_repeat": True,
            "components": [],
        },
        "chain_identity_map_file": None,
        "statistics": {
            "total_atoms": 0,
            "total_residues": 0,
            "total_chains": 0,
            "unique_pdb_chain_ids": 0,
            "input_file_count": len(pdb_files),
            "conect_bond_count": 0,
            "conect_skipped_bond_count": 0,
        },
        "errors": [],
        "warnings": []
    }
    
    # Validate input
    if not pdb_files:
        result["errors"].append("No PDB files provided")
        logger.error("No PDB files provided")
        return result
    
    # Check for gemmi
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        result["errors"].append("Hint: Install with: pip install gemmi")
        logger.error("gemmi not installed")
        return result
    
    # Validate all input files exist
    missing_files = []
    for pdb_file in pdb_files:
        if not Path(pdb_file).exists():
            missing_files.append(pdb_file)
    
    if missing_files:
        result["errors"].append(f"Files not found: {missing_files}")
        logger.error(f"Files not found: {missing_files}")
        return result

    # Setup output directory
    base_dir = Path(output_dir) if output_dir else WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "merge")
    result["output_dir"] = str(out_dir)

    try:
        # Create new structure to hold merged result
        merged_structure = gemmi.Structure()
        merged_structure.name = output_name
        merged_model = gemmi.Model("1")
        
        total_atoms = 0
        total_residues = 0
        topology_chain_index = 0
        assigned_chain_ids: list[str] = []
        pending_conect_sources: list[dict[str, Any]] = []
        
        for pdb_file in pdb_files:
            pdb_path = Path(pdb_file)
            logger.info(f"Processing: {pdb_path.name}")
            
            # Read input structure
            try:
                input_structure = gemmi.read_pdb(str(pdb_path))
            except Exception as e:
                result["warnings"].append(f"Failed to read {pdb_path.name}: {str(e)}")
                logger.warning(f"Failed to read {pdb_path.name}: {e}")
                continue
            
            if len(input_structure) == 0:
                result["warnings"].append(f"No models in {pdb_path.name}")
                continue
            
            input_model = input_structure[0]
            source_conect_bonds = _iter_unique_conect_bonds(input_structure.conect_map)
            source_serial_to_merged_atom_index: dict[int, int] = {}
            file_chain_mapping = {}
            source_chain_index = 0
            
            for chain in input_model:
                original_chain_id = chain.name

                # Assign a short PDB-compatible label.  It may repeat after
                # the finite PDB chain-ID pool is exhausted; the identity map
                # below is the authoritative source component key.
                new_chain_id = _pdb_chain_id_for_index(topology_chain_index)
                
                file_chain_mapping[original_chain_id] = new_chain_id
                result["chain_mapping_entries"].append({
                    "source_file": str(pdb_path),
                    "source_chain_id": original_chain_id,
                    "source_chain_index": source_chain_index,
                    "topology_chain_index": topology_chain_index,
                    "md_chain_id": new_chain_id,
                })
                
                # Create new chain with new ID
                new_chain = gemmi.Chain(new_chain_id)
                atom_start = total_atoms
                residue_start = total_residues
                component_atom_count = 0
                component_residue_count = 0
                
                # Copy residues and atoms (preserving atom names exactly)
                for residue in chain:
                    new_residue = gemmi.Residue()
                    new_residue.name = residue.name
                    new_residue.seqid = residue.seqid
                    new_residue.subchain = new_chain_id
                    
                    for atom in residue:
                        new_atom = gemmi.Atom()
                        new_atom.name = atom.name  # Preserve atom name exactly
                        new_atom.pos = atom.pos
                        new_atom.occ = atom.occ
                        new_atom.b_iso = atom.b_iso
                        new_atom.element = atom.element
                        new_residue.add_atom(new_atom)
                        if atom.serial > 0:
                            source_serial_to_merged_atom_index[int(atom.serial)] = total_atoms
                        total_atoms += 1
                        component_atom_count += 1
                    
                    if len(list(new_residue)) > 0:
                        new_chain.add_residue(new_residue)
                        total_residues += 1
                        component_residue_count += 1
                
                if len(list(new_chain)) > 0:
                    merged_model.add_chain(new_chain)
                    assigned_chain_ids.append(new_chain_id)
                    component_id = f"component_{topology_chain_index + 1:06d}"
                    result["chain_identity_map"]["components"].append({
                        "component_id": component_id,
                        "source_file": str(pdb_path),
                        "source_chain_id": original_chain_id,
                        "source_chain_index": source_chain_index,
                        "topology_chain_index": topology_chain_index,
                        "md_chain_id": new_chain_id,
                        "pdb_chain_id": new_chain_id,
                        "atom_index_start": atom_start,
                        "atom_index_end_exclusive": total_atoms,
                        "atom_count": component_atom_count,
                        "residue_index_start": residue_start,
                        "residue_index_end_exclusive": total_residues,
                        "residue_count": component_residue_count,
                    })
                    logger.info(
                        f"  Chain {original_chain_id} -> {new_chain_id} "
                        f"({component_id})"
                    )
                    topology_chain_index += 1
                source_chain_index += 1
            
            result["chain_mapping"][str(pdb_path)] = file_chain_mapping
            if source_conect_bonds:
                pending_conect_sources.append({
                    "source_file": str(pdb_path),
                    "bonds": source_conect_bonds,
                    "serial_to_atom_index": source_serial_to_merged_atom_index,
                })
        
        # Add model to structure
        merged_structure.add_model(merged_model)

        # Rebuild input CONECT records against final merged atom serials.
        # Gemmi's CONECT map is serial-number based and does not track atom
        # edits, so original records cannot be copied directly after chain
        # relabeling / merging.  Assign final serials first, then add mapped
        # bonds and write with conect_records=True below.
        merged_structure.assign_serial_numbers(numbered_ter=True)
        merged_structure.clear_conect()
        merged_atoms = [
            atom
            for model in merged_structure
            for chain in model
            for residue in chain
            for atom in residue
        ]
        conect_bond_count = 0
        conect_skipped_bond_count = 0
        for source in pending_conect_sources:
            serial_to_atom_index = source["serial_to_atom_index"]
            for serial1, serial2, order in source["bonds"]:
                atom_index1 = serial_to_atom_index.get(serial1)
                atom_index2 = serial_to_atom_index.get(serial2)
                if atom_index1 is None or atom_index2 is None:
                    conect_skipped_bond_count += 1
                    continue
                try:
                    merged_serial1 = int(merged_atoms[atom_index1].serial)
                    merged_serial2 = int(merged_atoms[atom_index2].serial)
                except (IndexError, TypeError, ValueError):
                    conect_skipped_bond_count += 1
                    continue
                if merged_serial1 <= 0 or merged_serial2 <= 0:
                    conect_skipped_bond_count += 1
                    continue
                merged_structure.add_conect(
                    merged_serial1,
                    merged_serial2,
                    max(1, int(order)),
                )
                conect_bond_count += 1
        if conect_skipped_bond_count:
            result["warnings"].append(
                f"Skipped {conect_skipped_bond_count} CONECT bond(s) whose "
                f"source atom serials could not be mapped after merge."
            )
        
        # Write output
        output_file = out_dir / f"{output_name}.pdb"
        write_options = gemmi.PdbWriteOptions(
            preserve_serial=True,
            conect_records=True,
        )
        merged_structure.write_pdb(str(output_file), write_options)

        # Fix HETATM records for amino acid residues
        # gemmi doesn't recognize Amber naming conventions (HIE, NALA, etc.)
        _fix_amino_acid_hetatm_records(output_file)

        chain_identity_map_file = out_dir / f"{output_name}.chain_identity_map.json"
        with open(chain_identity_map_file, "w") as f:
            json.dump(result["chain_identity_map"], f, indent=2)

        result["output_file"] = str(output_file)
        result["chain_identity_map_file"] = str(chain_identity_map_file)
        result["statistics"]["total_atoms"] = total_atoms
        result["statistics"]["total_residues"] = total_residues
        result["statistics"]["total_chains"] = topology_chain_index
        result["statistics"]["unique_pdb_chain_ids"] = len(set(assigned_chain_ids))
        result["statistics"]["pdb_chain_id_reuse_count"] = (
            topology_chain_index - len(set(assigned_chain_ids))
        )
        result["statistics"]["conect_bond_count"] = conect_bond_count
        result["statistics"]["conect_skipped_bond_count"] = conect_skipped_bond_count
        result["success"] = True
        
        logger.info(f"Successfully merged {len(pdb_files)} files into {output_file}")
        logger.info(
            f"  Total: {total_atoms} atoms, {total_residues} residues, "
            f"{topology_chain_index} chains"
        )
        
    except Exception as e:
        error_msg = f"Error during structure merging: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def _read_pdb_unique_residues(pdb_file: str | Path) -> list[dict]:
    """Read unique residue records from a PDB file without changing order."""
    residues = []
    seen = set()
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 27:
            continue
        chain = line[21].strip() or "A"
        resnum = line[22:26].strip()
        icode = line[26].strip()
        resname = line[17:20].strip()
        key = (chain, resnum, icode, resname)
        if key in seen:
            continue
        seen.add(key)
        residues.append({
            "chain": chain,
            "resnum": int(resnum) if resnum.lstrip("-").isdigit() else resnum,
            "resnum_str": resnum,
            "icode": icode,
            "resname": resname,
        })
    return residues


def _build_nucleic_residue_mapping(
    split_result: dict,
    merge_result: dict,
) -> list[dict]:
    """Map source nucleic residues from split files onto merged.pdb residue IDs."""
    mapping = []
    merge_chain_mapping = merge_result.get("chain_mapping", {})
    for info in split_result.get("chain_file_info", []):
        if info.get("chain_type") != "nucleic":
            continue
        chain_file = info.get("file")
        if not chain_file:
            continue
        file_map = (
            merge_chain_mapping.get(chain_file)
            or merge_chain_mapping.get(str(Path(chain_file)))
            or merge_chain_mapping.get(str(Path(chain_file).resolve()))
            or {}
        )
        residues = _read_pdb_unique_residues(chain_file)
        for residue in residues:
            split_chain = residue["chain"]
            merged_chain = file_map.get(split_chain, split_chain)
            mapping.append({
                "source_chain": info.get("author_chain", split_chain),
                "source_label_chain": info.get("chain_id"),
                "source_resnum": residue["resnum"],
                "source_icode": residue["icode"],
                "source_resname": residue["resname"],
                "merged_chain": merged_chain,
                "merged_resnum": residue["resnum"],
                "merged_icode": residue["icode"],
                "merged_resname": residue["resname"],
                "chain_file": chain_file,
            })
    return mapping


def _build_residue_mapping_for_type(
    split_result: dict,
    merge_result: dict,
    chain_type: str,
) -> list[dict]:
    """Map source residues of one split chain type onto merged.pdb residue IDs."""
    mapping = []
    merge_chain_mapping = merge_result.get("chain_mapping", {})
    for info in split_result.get("chain_file_info", []):
        if info.get("chain_type") != chain_type:
            continue
        chain_file = info.get("file")
        if not chain_file:
            continue
        file_map = (
            merge_chain_mapping.get(chain_file)
            or merge_chain_mapping.get(str(Path(chain_file)))
            or merge_chain_mapping.get(str(Path(chain_file).resolve()))
            or {}
        )
        for residue in _read_pdb_unique_residues(chain_file):
            split_chain = residue["chain"]
            merged_chain = file_map.get(split_chain, split_chain)
            mapping.append({
                "source_chain": info.get("author_chain", split_chain),
                "source_label_chain": info.get("chain_id"),
                "source_resnum": residue["resnum"],
                "source_icode": residue["icode"],
                "source_resname": residue["resname"],
                "merged_chain": merged_chain,
                "merged_resnum": residue["resnum"],
                "merged_icode": residue["icode"],
                "merged_resname": residue["resname"],
                "chain_file": chain_file,
            })
    return mapping


def _index_prepared_component_sources(
    *,
    chain_info_map: dict,
    proteins: list[dict],
    nucleics: list[dict],
    glycans: list[dict],
    ligands: list[dict],
    ion_files: list[str],
) -> dict[str, dict]:
    """Build a path-keyed lookup from prepared fragments to source identity."""
    index: dict[str, dict] = {}

    def add(path: str | Path | None, metadata: dict) -> None:
        for key in _path_lookup_keys(path):
            index[key] = metadata

    def source_metadata(chain_id: str | None, chain_type: str) -> dict:
        info = chain_info_map.get(chain_id, {}) if chain_id is not None else {}
        return {
            "source_label_asym_id": chain_id,
            "source_auth_asym_id": info.get("author_chain", chain_id),
            "source_chain_type": chain_type,
            "source_unique_id": info.get("unique_id"),
            "source_resnum": info.get("resnum"),
            "source_nucleic_subtype": info.get("nucleic_subtype"),
        }

    for protein in proteins or []:
        if protein.get("success") and protein.get("output_file"):
            add(
                protein.get("output_file"),
                {
                    **source_metadata(protein.get("chain_id"), "protein"),
                    "prepared_fragment_role": "protein",
                    "prepared_input_file": protein.get("input_file"),
                },
            )

    for nucleic in nucleics or []:
        if nucleic.get("success") and nucleic.get("output_file"):
            add(
                nucleic.get("output_file"),
                {
                    **source_metadata(nucleic.get("chain_id"), "nucleic"),
                    "prepared_fragment_role": "nucleic",
                    "prepared_input_file": nucleic.get("input_file"),
                },
            )

    for glycan in glycans or []:
        if glycan.get("success") and glycan.get("output_file"):
            add(
                glycan.get("output_file"),
                {
                    **source_metadata(glycan.get("chain_id"), "glycan"),
                    "prepared_fragment_role": "glycan",
                    "prepared_input_file": glycan.get("input_file"),
                    "source_residue_names": glycan.get("residue_names", []),
                },
            )

    for ligand in ligands or []:
        if ligand.get("success"):
            ligand_path = ligand.get("pdb_file") or ligand.get("input_file")
            if ligand_path:
                add(
                    ligand_path,
                    {
                        **source_metadata(ligand.get("chain_id"), "ligand"),
                        "prepared_fragment_role": "ligand",
                        "prepared_input_file": ligand.get("input_file"),
                        "ligand_instance_id": ligand.get("ligand_instance_id"),
                        "ligand_id": ligand.get("ligand_id"),
                        "source_residue_name": ligand.get("residue_name"),
                    },
                )

    for ion_file in ion_files or []:
        ion_info = None
        for info in chain_info_map.values():
            if info.get("file") == ion_file:
                ion_info = info
                break
        chain_id = ion_info.get("chain_id") if ion_info else None
        add(
            ion_file,
            {
                **source_metadata(chain_id, "ion"),
                "prepared_fragment_role": "ion",
                "prepared_input_file": ion_file,
            },
        )

    return index


def _enrich_chain_identity_map(
    chain_identity_map: dict,
    prepared_source_index: dict[str, dict],
) -> dict:
    """Attach source label/auth identifiers to merge-level component records."""
    enriched = dict(chain_identity_map or {})
    components = []
    for component in (chain_identity_map or {}).get("components", []):
        enriched_component = dict(component)
        metadata = None
        for key in _path_lookup_keys(component.get("source_file")):
            metadata = prepared_source_index.get(key)
            if metadata:
                break
        if metadata:
            enriched_component.update(metadata)
        components.append(enriched_component)
    enriched["components"] = components
    return enriched


def _as_linkage_resnum(value: object) -> object:
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else text


def _glycan_linkage_key(linkage: dict) -> tuple:
    protein = linkage.get("protein") or {}
    glycan = linkage.get("glycan") or {}
    return (
        str(protein.get("chain", "")),
        str(protein.get("resnum", "")),
        str(protein.get("icode", "") or ""),
        str(protein.get("resname", "")).upper(),
        str(protein.get("atom", "")),
        str(glycan.get("chain", "")),
        str(glycan.get("resnum", "")),
        str(glycan.get("icode", "") or ""),
        str(glycan.get("resname", "")).upper(),
        str(glycan.get("atom", "")),
    )


def _endpoint_metadata(
    *,
    atom: str,
    resname: str,
    chain: str,
    resnum: object,
    icode: str,
    source: str,
    connection_id: str | None,
    reported_distance: float | None,
) -> dict:
    return {
        "atom": atom,
        "resname": resname,
        "chain": chain,
        "resnum": _as_linkage_resnum(resnum),
        "icode": icode,
        "source": source,
        "connection_id": connection_id,
        "reported_distance": reported_distance,
    }


def _parse_pdb_glycan_link_records(structure_path: Path) -> list[dict]:
    """Parse PDB LINK records that connect a protein residue to a glycan."""
    linkages: list[dict] = []
    try:
        lines = structure_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return linkages

    protein_resnames = AMINO_ACIDS | AMBER_PROTEIN_RESIDUES
    for line in lines:
        if not line.startswith("LINK"):
            continue
        atom1 = line[12:16].strip()
        res1 = line[17:20].strip()
        chain1 = line[21].strip() or "A"
        resnum1 = line[22:26].strip()
        icode1 = line[26].strip()
        atom2 = line[42:46].strip()
        res2 = line[47:50].strip()
        chain2 = line[51].strip() or "A"
        resnum2 = line[52:56].strip()
        icode2 = line[56].strip()

        side1_is_glycan = is_glycan_residue_name(res1)
        side2_is_glycan = is_glycan_residue_name(res2)
        side1_is_protein = res1 in protein_resnames
        side2_is_protein = res2 in protein_resnames
        if side1_is_protein and side2_is_glycan:
            protein = (atom1, res1, chain1, resnum1, icode1)
            glycan = (atom2, res2, chain2, resnum2, icode2)
        elif side2_is_protein and side1_is_glycan:
            protein = (atom2, res2, chain2, resnum2, icode2)
            glycan = (atom1, res1, chain1, resnum1, icode1)
        else:
            continue

        linkages.append({
            "source": "pdb_link",
            "connection_id": None,
            "reported_distance": None,
            "protein": _endpoint_metadata(
                atom=protein[0],
                resname=protein[1],
                chain=protein[2],
                resnum=protein[3],
                icode=protein[4],
                source="pdb_link",
                connection_id=None,
                reported_distance=None,
            ),
            "glycan": _endpoint_metadata(
                atom=glycan[0],
                resname=glycan[1],
                chain=glycan[2],
                resnum=glycan[3],
                icode=glycan[4],
                source="pdb_link",
                connection_id=None,
                reported_distance=None,
            ),
        })
    return linkages


def _parse_gemmi_glycan_link_records(structure_path: Path) -> list[dict]:
    """Parse mmCIF/PDB covalent connections between protein and glycans."""
    try:
        import gemmi
    except ImportError:
        return []

    try:
        suffix = structure_path.suffix.lower()
        if suffix in {".cif", ".mmcif"}:
            doc = gemmi.cif.read(str(structure_path))
            structure = gemmi.make_structure_from_block(doc[0])
            source = "mmcif_struct_conn"
        else:
            structure = gemmi.read_pdb(str(structure_path))
            source = "pdb_struct_conn"
    except Exception:
        return []

    protein_resnames = AMINO_ACIDS | AMBER_PROTEIN_RESIDUES
    covalent_type = getattr(gemmi.ConnectionType, "Covale", None)
    linkages: list[dict] = []

    def _is_covalent(conn: object) -> bool:
        conn_type = getattr(conn, "type", None)
        if covalent_type is not None and conn_type == covalent_type:
            return True
        return "coval" in str(conn_type).lower()

    def _partner_tuple(partner: object) -> tuple[str, str, str, object, str]:
        res_id = getattr(partner, "res_id", None)
        seqid = getattr(res_id, "seqid", None)
        return (
            str(getattr(partner, "atom_name", "") or "").strip(),
            str(getattr(res_id, "name", "") or "").strip().upper(),
            str(getattr(partner, "chain_name", "") or "").strip() or "A",
            getattr(seqid, "num", "") if seqid is not None else "",
            str(getattr(seqid, "icode", "") or "").strip(),
        )

    for conn in getattr(structure, "connections", []):
        if not _is_covalent(conn):
            continue
        p1 = _partner_tuple(conn.partner1)
        p2 = _partner_tuple(conn.partner2)
        side1_is_glycan = is_glycan_residue_name(p1[1])
        side2_is_glycan = is_glycan_residue_name(p2[1])
        side1_is_protein = p1[1] in protein_resnames
        side2_is_protein = p2[1] in protein_resnames
        if side1_is_protein and side2_is_glycan:
            protein, glycan = p1, p2
        elif side2_is_protein and side1_is_glycan:
            protein, glycan = p2, p1
        else:
            continue

        reported_distance = getattr(conn, "reported_distance", None)
        try:
            reported_distance = float(reported_distance) if reported_distance is not None else None
        except (TypeError, ValueError):
            reported_distance = None
        connection_id = str(getattr(conn, "name", "") or "").strip() or None
        linkages.append({
            "source": source,
            "connection_id": connection_id,
            "reported_distance": reported_distance,
            "protein": _endpoint_metadata(
                atom=protein[0],
                resname=protein[1],
                chain=protein[2],
                resnum=protein[3],
                icode=protein[4],
                source=source,
                connection_id=connection_id,
                reported_distance=reported_distance,
            ),
            "glycan": _endpoint_metadata(
                atom=glycan[0],
                resname=glycan[1],
                chain=glycan[2],
                resnum=glycan[3],
                icode=glycan[4],
                source=source,
                connection_id=connection_id,
                reported_distance=reported_distance,
            ),
        })
    return linkages


def _parse_glycan_link_records(structure_path: Path) -> list[dict]:
    """Parse protein-glycan covalent linkages from mmCIF/PDB metadata."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for linkage in _parse_gemmi_glycan_link_records(structure_path):
        key = _glycan_linkage_key(linkage)
        if key not in seen:
            out.append(linkage)
            seen.add(key)
    for linkage in _parse_pdb_glycan_link_records(structure_path):
        key = _glycan_linkage_key(linkage)
        if key not in seen:
            out.append(linkage)
            seen.add(key)
    return out


def _remap_glycan_linkages(
    linkages: list[dict],
    protein_chain_map: dict,
    glycan_residue_mapping: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Resolve source LINK endpoints onto merged.pdb chain/residue IDs."""
    glycan_by_source = {
        (
            str(r["source_chain"]),
            str(r["source_resnum"]),
            str(r.get("source_icode", "") or ""),
            str(r["source_resname"]).upper(),
        ): r
        for r in glycan_residue_mapping or []
    }
    remapped: list[dict] = []
    dropped: list[dict] = []
    for link in linkages or []:
        protein = dict(link.get("protein") or {})
        glycan = dict(link.get("glycan") or {})
        protein_chain = protein_chain_map.get(protein.get("chain"))
        glycan_key = (
            str(glycan.get("chain")),
            str(glycan.get("resnum")),
            str(glycan.get("icode", "") or ""),
            str(glycan.get("resname", "")).upper(),
        )
        glycan_map = glycan_by_source.get(glycan_key)
        if protein_chain is None or glycan_map is None:
            dropped.append(dict(link))
            continue
        protein.update({
            "original_chain": protein.get("chain"),
            "chain": protein_chain,
            "merged_chain": protein_chain,
            "merged_resnum": protein.get("resnum"),
        })
        glycan.update({
            "original_chain": glycan.get("chain"),
            "chain": glycan_map["merged_chain"],
            "merged_chain": glycan_map["merged_chain"],
            "merged_resnum": glycan_map["merged_resnum"],
            "merged_icode": glycan_map.get("merged_icode", ""),
        })
        remapped.append({
            **link,
            "protein": protein,
            "glycan": glycan,
            "status": "remapped",
        })
    return remapped, dropped


def _resolve_prepare_node_structure_file(
    job_dir: Optional[str],
    node_id: Optional[str],
    structure_file: Optional[str],
    source_selection: Optional[dict] = None,
) -> dict:
    """Resolve prep input structure from the DAG when not provided explicitly."""
    if not (job_dir and node_id) or structure_file:
        return {"structure_file": structure_file}
    from mdclaw._node import resolve_node_inputs

    inputs = resolve_node_inputs(job_dir, node_id, "prep")
    if inputs.get("source_bundle_file"):
        try:
            from mdclaw.source_bundle import materialize_source_selection

            selected = materialize_source_selection(
                bundle_file=inputs["source_bundle_file"],
                selection=source_selection,
                prep_artifacts_dir=Path(job_dir) / "nodes" / node_id / "artifacts",
            )
            return {
                "structure_file": selected.get("structure_file"),
                "input_resolution_error": inputs.get("input_resolution_error"),
                "input_resolution_errors": inputs.get("input_resolution_errors", []),
                "source_bundle_file": selected.get("source_bundle_file"),
                "source_selection_file": selected.get("source_selection_file"),
                "source_selection": selected.get("source_selection"),
                "source_structure_id": selected.get("selected_structure", {}).get("structure_id"),
                "source_structure": selected.get("selected_structure"),
                "source_selection_materialized": selected.get("materialized"),
            }
        except Exception as exc:
            return {
                "structure_file": None,
                "input_resolution_error": str(exc),
                "input_resolution_errors": inputs.get("input_resolution_errors", []) + [str(exc)],
                "source_bundle_file": inputs.get("source_bundle_file"),
                "source_selection": source_selection,
                "source_structure_id": inputs.get("source_structure_id"),
            }
    return {
        "structure_file": inputs.get("structure_file", structure_file),
        "input_resolution_error": inputs.get("input_resolution_error"),
        "input_resolution_errors": inputs.get("input_resolution_errors", []),
        "source_bundle_file": inputs.get("source_bundle_file"),
        "source_selection": source_selection,
        "source_structure_id": inputs.get("source_structure_id"),
        "source_structure": inputs.get("source_structure"),
    }


def _validate_prepare_node_context(
    *,
    job_dir: str,
    node_id: str,
    select_chains: Optional[List[str]],
    ph: float,
    cap_termini: bool,
    n_terminal_cap: str | None,
    c_terminal_cap: str | None,
    terminal_cap_forcefield: str | None,
    process_proteins: bool,
    process_ligands: bool,
    include_types: Optional[List[str]],
    include_ligand_ids: Optional[List[str]],
    exclude_ligand_ids: Optional[List[str]],
    keep_crystal_waters: bool,
    source_structure_id: Optional[str] = None,
    source_candidate_id: Optional[str] = None,
    source_model_index: Optional[int] = None,
    source_model_id: Optional[str] = None,
) -> dict:
    """Validate declared prep-node conditions against runtime parameters."""
    from mdclaw._node import validate_node_execution_context

    return validate_node_execution_context(
        job_dir,
        node_id,
        "prep",
        actual_conditions={
            "select_chains": select_chains,
            "ph": ph,
            "cap_termini": cap_termini,
            "n_terminal_cap": n_terminal_cap,
            "c_terminal_cap": c_terminal_cap,
            "terminal_cap_forcefield": terminal_cap_forcefield,
            "process_proteins": process_proteins,
            "process_ligands": process_ligands,
            "include_types": include_types,
            "include_ligand_ids": include_ligand_ids,
            "exclude_ligand_ids": exclude_ligand_ids,
            "keep_crystal_waters": keep_crystal_waters,
            "source_structure_id": source_structure_id,
            "source_candidate_id": source_candidate_id,
            "source_model_index": source_model_index,
            "source_model_id": source_model_id,
        },
    )


def _prepare_complex_initial_result(job_id: str, structure_file: Optional[str]) -> dict:
    return {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "source_file": str(structure_file) if structure_file else None,
        "source_bundle_file": None,
        "source_selection_file": None,
        "source_selection": None,
        "source_structure_id": None,
        "inspection": None,
        "split": None,
        "proteins": [],
        "nucleics": [],
        "glycans": [],
        "ligands": [],
        "errors": [],
        "warnings": [],
        "component_disposition": _component_disposition_payload([]),
        "component_disposition_summary": _component_disposition_payload([])["summary"],
        "component_disposition_file": None,
        "excluded_components_file": None,
    }


def prepare_complex(
    structure_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    select_chains: Optional[List[str]] = None,
    ph: float = 7.4,
    cap_termini: bool = False,
    n_terminal_cap: str | None = None,
    c_terminal_cap: str | None = None,
    terminal_cap_forcefield: str | None = None,
    process_proteins: bool = True,
    process_ligands: bool = True,
    ligand_smiles: Optional[Dict[str, str]] = None,
    include_types: Optional[List[str]] = None,
    include_ligand_ids: Optional[List[str]] = None,
    exclude_ligand_ids: Optional[List[str]] = None,
    optimize_ligands: bool = False,
    structure_analysis: Optional[dict] = None,
    disulfide_pairs: Optional[List[Dict[str, Any]]] = None,
    histidine_states: Optional[Dict[str, str]] = None,
    protonation_states: Optional[Dict[str, Any]] = None,
    keep_crystal_waters: bool = False,
    source_structure_id: Optional[str] = None,
    source_candidate_id: Optional[str] = None,
    source_model_index: Optional[int] = None,
    source_model_id: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Prepare a protein-ligand complex for MD simulation (complete workflow).

    This tool combines multiple steps into a single workflow:
    1. Inspect the structure to identify chains
    2. Split the structure into individual chain files
    3. Clean protein chains (PDBFixer + pdb4amber)
    4. Rebuild hydrogens on standard DNA/RNA chains with OpenMM Modeller
    5. Clean ligand chains (SMILES template matching)
    6. Record ligand chemistry artifacts for topology-time ligand FF resolution
    7. Merge all prepared structures into a single PDB file

    This is the recommended one-step workflow for preparing structures from
    PDB or Boltz-2 predictions for MD simulation. The output merged_pdb can be
    directly passed to solvate_structure or build_amber_system.

    Args:
        structure_file: Path to mmCIF (.cif) or PDB (.pdb/.ent) file
        output_dir: Output directory (auto-generated if None)
        select_chains: List of chain IDs to process. **Pass the short
                       chain ID exactly as it appears in your input file:**
                       ``chain_id`` (label_asym_id) for mmCIF,
                       ``author_chain`` (auth_asym_id) for PDB. One unified
                       matching rule handles both formats — see
                       ``split_molecules`` for the full chain-ID contract.
                       Gotcha: for mmCIF, ``author_chain`` can be
                       multi-letter (e.g. 7QVK ``AAA BBB AbA``) and
                       occasionally reordered (7NMU ``label C ↔ auth
                       DDD``) — stick to ``chain_id`` there. For PDB,
                       gemmi's internal ``chain_id`` is an auto-generated
                       subchain label like ``Axp`` / ``Ax1`` / ``Axw`` and
                       is not user-facing. None = all chains.
        ph: pH for protonation state (default: 7.4)
        cap_termini: Backward-compatible shortcut to add ACE at the
                     N terminus and NME at the C terminus (default: False).
        n_terminal_cap: Optional one-sided N-terminal cap. Currently supports
                        ``"ACE"`` or an explicit none-like value.
        c_terminal_cap: Optional one-sided C-terminal cap. Currently supports
                        ``"NME"`` or an explicit none-like value.
        terminal_cap_forcefield: Protein force field used only for prep-stage
                                 cap hydrogen completion. Use the planned
                                 topology force field when specified; default
                                 is ff19SB.
        process_proteins: Whether to clean protein chains (default: True)
        process_ligands: Whether to clean ligands and record topology-time
                         chemistry inputs (default: True)
        ligand_smiles: Dict mapping ligand_id to SMILES (e.g., {"SAH": "Nc1ncnc..."})
                       If not provided, SMILES will be fetched from PDB CCD
        include_types: List of molecular types to include: "protein", "nucleic", "glycan", "ligand", "ion", "water".
                       Default (None) includes ["protein", "nucleic", "glycan", "ligand", "ion"].
        keep_crystal_waters: If True, retain crystal waters when "water" is in include_types.
                            Default is False (crystal waters excluded for MD simulations).
        source_structure_id: Candidate ID from the source bundle to prepare,
                             e.g. ``candidate_002``. Used only in node mode
                             when the source bundle contains multiple candidates.
        source_candidate_id: Alias for ``source_structure_id``.
        source_model_index: Model index/rank selector for NMR-style inputs.
                            Accepts the user-facing one-based model rank.
        source_model_id: Model identifier selector when present in the source
                         bundle provenance.
        include_ligand_ids: List of ligand unique IDs to include (format:
                           "author_chain:resname:resnum", e.g.,
                           ["A:ACP:501"]). If specified, only these ligands
                           are processed. Requested ligand label chains are
                           auto-included when select_chains would otherwise
                           omit them.
        exclude_ligand_ids: List of ligand unique IDs to exclude (format: "chain:resname:resnum",
                           e.g., ["A:ACT:401", "A:ACT:402"]). These ligands are skipped.
        optimize_ligands: Run MMFF94 optimization on ligands (default: False).
                          Bound-ligand heavy-atom coordinates are preserved unless
                          this is explicitly enabled.
        structure_analysis: Pre-computed structure analysis from Phase 1. Contains
                           user-approved settings for disulfide bonds, histidine states,
                           missing residue handling, and ligand processing. If provided,
                           these settings are used instead of auto-detection.
        disulfide_pairs: Explicit disulfide bond list — complete replacement of
                        auto-detection. Each pair is ``{"cys1": {"chain", "resnum"},
                        "cys2": {"chain", "resnum"}}``. Pass ``[]`` to disable
                        disulfides entirely. Wins over ``structure_analysis``
                        when both are provided.
        histidine_states: Explicit histidine protonation state overrides. Dict
                         mapping ``"<chain>:<resnum>"`` to ``"HID"`` / ``"HIE"``
                         / ``"HIP"``. Only the keys present are overridden; the
                         rest keep their propka-derived state. Wins over
                         ``structure_analysis`` when both are provided.
        protonation_states: Explicit residue protonation state overrides. Accepts
                         either ``{"A:57": "HIP", "A:25": "ASH"}`` or a list of
                         ``{"chain": "A", "resnum": 57, "state": "HIP"}``
                         records. Supports ASP/ASH, GLU/GLH, HID/HIE/HIP,
                         LYS/LYN, and CYS/CYX/CYM. Wins over
                         ``structure_analysis`` when provided.

    Returns:
        Dict with:
            - success: bool - True if workflow completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Directory containing all output files
            - source_file: str - Original input file path
            - inspection: dict - Results from inspect_molecules
            - split: dict - Results from split_molecules
            - proteins: list[dict] - Results for each protein chain:
                - chain_id: str
                - input_file: str
                - output_file: str (cleaned .amber.pdb)
                - success: bool
                - statistics: dict
            - nucleics: list[dict] - Standard DNA/RNA chains prepared for topology:
                - chain_id: str
                - input_file: str
                - output_file: str (hydrogen-complete nucleic PDB)
                - nucleic_subtype: str
                - hydrogens_added: int
                - nucleic_forcefield_xml: str
                - success: bool
            - glycans: list[dict] - Glycan chains passed through for GLYCAM:
                - chain_id: str
                - input_file: str
                - output_file: str
                - residue_names: list[str]
                - success: bool
            - ligands: list[dict] - Results for each ligand chain:
                - chain_id: str
                - ligand_id: str (residue name)
                - input_file: str
                - sdf_file: str (cleaned SDF)
                - pdb_file: str (cleaned ligand PDB)
                - net_charge: int
                - success: bool
            - merged_pdb: str - Path to merged PDB file (protein + ligands combined)
            - merge_result: dict - Results from merge_structures
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = prepare_complex(
        ...     "boltz_prediction.cif",
        ...     ph=7.4,
        ...     cap_termini=False,
        ...     ligand_smiles={"SAH": "Nc1ncnc2c1ncn2[C@@H]1O[C@H]..."}
        ... )
        >>> print(f"Proteins: {len(result['proteins'])}")
        >>> print(f"Ligands: {len(result['ligands'])}")
        >>> for lig in result['ligands']:
        ...     print(f"  {lig['ligand_id']}: {lig['sdf_file']}")
    """
    from mdclaw.source_bundle import source_selection_from_values

    _source_selection = source_selection_from_values(
        source_structure_id=source_structure_id,
        source_candidate_id=source_candidate_id,
        source_model_index=source_model_index,
        source_model_id=source_model_id,
    )
    _resolved_structure = _resolve_prepare_node_structure_file(
        job_dir, node_id, structure_file, _source_selection
    )
    structure_file = _resolved_structure["structure_file"]

    logger.info(f"Preparing complex: {structure_file}")

    # Initialize result structure
    job_id = generate_job_id()
    result = _prepare_complex_initial_result(job_id, structure_file)
    result["source_bundle_file"] = _resolved_structure.get("source_bundle_file")
    result["source_selection_file"] = _resolved_structure.get("source_selection_file")
    result["source_selection"] = _resolved_structure.get("source_selection")
    result["source_structure_id"] = _resolved_structure.get("source_structure_id")

    if _resolved_structure.get("input_resolution_error"):
        blocked = {
            **result,
            **create_validation_error(
                "job_dir/node_id",
                _resolved_structure["input_resolution_error"],
                expected="Completed source ancestor with structure_file artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _resolved_structure.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex input resolution blocked",
            )
        return blocked

    if job_dir and node_id:
        _ctx = _validate_prepare_node_context(
            job_dir=job_dir,
            node_id=node_id,
            select_chains=select_chains,
            ph=ph,
            cap_termini=cap_termini,
            n_terminal_cap=n_terminal_cap,
            c_terminal_cap=c_terminal_cap,
            terminal_cap_forcefield=terminal_cap_forcefield,
            process_proteins=process_proteins,
            process_ligands=process_ligands,
            include_types=include_types,
            include_ligand_ids=include_ligand_ids,
            exclude_ligand_ids=exclude_ligand_ids,
            keep_crystal_waters=keep_crystal_waters,
            source_structure_id=_resolved_structure.get("source_structure_id"),
            source_candidate_id=source_candidate_id,
            source_model_index=source_model_index,
            source_model_id=source_model_id,
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex node execution context invalid",
            )

    if not structure_file:
        blocked = {
            **result,
            **create_validation_error(
                "structure_file",
                "structure_file is required",
                expected="Explicit structure path, or --job-dir/--node-id with a source ancestor",
                actual=structure_file,
                hints=["Run fetch_structure first or execute in node mode from a prep node."],
                code="missing_structure_file",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex missing structure_file",
            )
        return blocked

    # Validate input file
    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        logger.error(f"Structure file not found: {structure_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="prepare_complex structure file not found",
            )
        return result

    # Setup output directory.
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        base_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR / f"job_{job_id}"
    ensure_directory(base_dir)
    out_dir = base_dir
    result["output_dir"] = str(base_dir)

    try:
        # Step 1: Inspect structure
        logger.info("Step 1: Inspecting structure...")
        inspection = _inspect_molecules_impl(str(structure_file))

        # PTM detection (SEP / TPO / PTR). Recorded *before* cleaning, since
        # PDBFixer's replaceNonstandardResidues will swap them for SER/THR/TYR
        # in the output merged.pdb. The list lets a follow-up
        # `phosphorylate_residues --restore-from-detection` re-introduce them
        # on a branched prep node, and lets `build_amber_system` decide
        # whether to add the matching ``amber/phosaa*.xml`` to the
        # ``openmmforcefields.SystemGenerator`` bundle.
        from mdclaw.research_server import detect_ptm_sites
        detected_ptm_residues = detect_ptm_sites(str(structure_file))
        detected_glycan_linkages = _parse_glycan_link_records(Path(structure_file))

        # Token optimization: Store only essential inspection info (not full chains/entities)
        result["inspection"] = {
            "success": inspection["success"],
            "summary": inspection.get("summary", {}),
            "header": inspection.get("header", {}),
            "errors": inspection.get("errors", []),
            "warnings": inspection.get("warnings", [])
        }

        if not inspection["success"]:
            result["errors"].append(f"Inspection failed: {inspection['errors']}")
            return result

        summary = inspection["summary"]
        logger.info(f"Found: {summary['num_protein_chains']} proteins, "
                   f"{summary.get('num_nucleic_chains', 0)} nucleics, "
                   f"{summary.get('num_glycan_chains', 0)} glycans, "
                   f"{summary['num_ligand_chains']} ligands, "
                   f"{summary['num_ion_chains']} ions")

        # Step 1.5: Aggregate disulfide bonds. When the caller supplies
        # ``disulfide_pairs`` explicitly, it is a **complete replacement** of
        # auto-detection — empty list means "no disulfides at all". Otherwise
        # we merge explicit PDB SSBOND / mmCIF _struct_conn entries with
        # distance-based candidates so downstream tools have a single
        # chain-filtered source of truth. Detection runs on the ORIGINAL
        # structure file (before splitting) so inter-chain pairs survive.
        if disulfide_pairs is not None:
            disulfide_bonds = list(disulfide_pairs)
            for b in disulfide_bonds:
                b.setdefault("source", "user_override")
            logger.info(
                f"Disulfide pairs overridden by caller: {len(disulfide_bonds)} pair(s)"
            )
            disulfide_source = "user_override"
        else:
            from mdclaw.research_server import (
                _parse_ssbond_records,
                _detect_disulfide_candidates,
            )
            ssbond_pairs = _parse_ssbond_records(structure_path)
            distance_pairs = _detect_disulfide_candidates(structure_path)
            # Translate select_chains (label_asym_id per Fix B) into the
            # author_chain values that SSBOND / _struct_conn records
            # actually carry — gemmi's ``chain.name`` / ``partner.chain_name``
            # return auth_asym_id for both PDB and mmCIF. Without this
            # mapping, ``_merge_disulfide_pairs``'s filter silently drops
            # pairs whose auth_asym_id differs from the user-supplied label
            # (e.g. 7QVK label "B" ↔ auth "BBB").
            ss_select_chains: Optional[List[str]] = None
            if select_chains is not None:
                chain_id_map = inspection.get("summary", {}).get("chain_id_map", {})
                author_chains_set = set(chain_id_map.values())
                ss_select_chains = []
                for ch in select_chains:
                    if ch in chain_id_map:
                        ss_select_chains.append(chain_id_map[ch])   # label -> auth
                    elif ch in author_chains_set:
                        ss_select_chains.append(ch)                 # already auth (fallback path)
                    # else: unknown ID — let split_molecules raise the error.
            disulfide_bonds = _merge_disulfide_pairs(
                ssbond_pairs, distance_pairs, select_chains=ss_select_chains
            )
            if disulfide_bonds:
                logger.info(
                    f"Disulfide pairs detected: {len(disulfide_bonds)} "
                    f"(ssbond={len(ssbond_pairs)}, distance={len(distance_pairs)})"
                )
            disulfide_source = "auto_detected"
        result["disulfide_bonds"] = disulfide_bonds
        result["disulfide_source"] = disulfide_source

        try:
            resolved_n_terminal_cap, resolved_c_terminal_cap = _resolve_terminal_cap_settings(
                cap_termini=cap_termini,
                n_terminal_cap=n_terminal_cap,
                c_terminal_cap=c_terminal_cap,
            )
        except ValueError as exc:
            result["errors"].append(str(exc))
            result["code"] = "invalid_terminal_cap"
            result["overall_status"] = "failed"
            return result
        terminal_caps_requested = bool(resolved_n_terminal_cap or resolved_c_terminal_cap)

        # Step 2: Split structure
        logger.info("Step 2: Splitting structure...")
        split_result = split_molecules(
            str(structure_file),
            output_dir=str(base_dir),
            select_chains=select_chains,
            include_types=include_types,
            include_ligand_ids=include_ligand_ids,
            exclude_ligand_ids=exclude_ligand_ids,
            keep_crystal_waters=keep_crystal_waters,
        )

        # Adopt split_molecules output dir as our working dir
        if split_result["success"]:
            result["output_dir"] = split_result["output_dir"]
            out_dir = Path(split_result["output_dir"])
        
        result["split"] = {
            "success": split_result["success"],
            "protein_files": split_result.get("protein_files", []),
            "nucleic_files": split_result.get("nucleic_files", []),
            "glycan_files": split_result.get("glycan_files", []),
            "ligand_files": split_result.get("ligand_files", []),
            "ion_files": split_result.get("ion_files", []),
            "water_files": split_result.get("water_files", []),
            "chain_file_info": split_result.get("chain_file_info", [])
        }
        
        if not split_result["success"]:
            result["errors"].append(f"Split failed: {split_result['errors']}")
            return result
        
        # Build lookup for chain info
        # Create a lookup from chain_id to all_chain_data (match by chain_id, not index)
        all_chains_lookup = {c["chain_id"]: c for c in split_result.get("all_chains", [])}
        
        chain_info_map = {}
        for info in split_result.get("chain_file_info", []):
            chain_id = info["chain_id"]
            chain_info_map[chain_id] = {
                **info,
                "all_chain_data": all_chains_lookup.get(chain_id, {})
            }
        
        # Step 3: Process proteins
        if process_proteins and split_result.get("protein_files"):
            logger.info(f"Step 3: Processing {len(split_result['protein_files'])} protein(s)...")

            # Assemble the disulfide + histidine override lists that the
            # per-chain clean_protein step actually consumes. Precedence is
            # direct CLI args > structure_analysis > auto-detect.
            sa_disulfide_pairs = None
            sa_histidine_states = None
            sa_protonation_states = None

            if disulfide_pairs is not None:
                # Convert user's nested cys1/cys2 schema into the flat shape
                # clean_protein expects. An empty list intentionally disables
                # disulfide formation in cleaning too.
                sa_disulfide_pairs = [
                    {
                        "chain1": p.get("cys1", {}).get("chain"),
                        "resnum1": p.get("cys1", {}).get("resnum"),
                        "chain2": p.get("cys2", {}).get("chain"),
                        "resnum2": p.get("cys2", {}).get("resnum"),
                        "form_bond": True,
                    }
                    for p in disulfide_pairs
                ]
                logger.info(f"User override: {len(sa_disulfide_pairs)} disulfide pair(s)")

            if histidine_states:
                sa_histidine_states = dict(histidine_states)
                logger.info(f"User override: {len(sa_histidine_states)} histidine state(s)")

            if protonation_states:
                try:
                    sa_protonation_states = _normalize_protonation_state_overrides(
                        protonation_states=protonation_states,
                        histidine_states=sa_histidine_states,
                    )
                    # The generic protonation list subsumes legacy HIS overrides.
                    sa_histidine_states = None
                except ValueError as exc:
                    result["errors"].append(str(exc))
                    result["code"] = "invalid_protonation_state"
                    result["overall_status"] = "failed"
                    return result
                logger.info(
                    f"User override: {len(sa_protonation_states)} residue protonation state(s)"
                )

            if structure_analysis:
                # Extract disulfide bonds only if no direct override
                if sa_disulfide_pairs is None:
                    sa_disulfide_bonds = structure_analysis.get("disulfide_bonds", [])
                    if sa_disulfide_bonds:
                        # Filter to only form_bond=True and convert to expected format
                        sa_disulfide_pairs = [
                            {
                                "chain1": bond.get("chain1"),
                                "resnum1": bond.get("resnum1"),
                                "chain2": bond.get("chain2"),
                                "resnum2": bond.get("resnum2"),
                                "form_bond": bond.get("form_bond", True),
                            }
                            for bond in sa_disulfide_bonds
                            if bond.get("form_bond", True)
                        ]
                        logger.info(f"Using {len(sa_disulfide_pairs)} pre-defined disulfide pair(s)")

                # Extract generic protonation states if no direct override.
                if (
                    sa_protonation_states is None
                    and sa_histidine_states is None
                    and structure_analysis.get("protonation_states")
                ):
                    try:
                        sa_protonation_states = _normalize_protonation_state_overrides(
                            protonation_states=structure_analysis.get("protonation_states"),
                        )
                    except ValueError as exc:
                        result["errors"].append(str(exc))
                        result["code"] = "invalid_protonation_state"
                        result["overall_status"] = "failed"
                        return result
                    logger.info(
                        f"Using {len(sa_protonation_states)} pre-defined residue protonation state(s)"
                    )

                # Extract histidine states only if no generic/direct override
                if sa_histidine_states is None and sa_protonation_states is None:
                    sa_histidine_list = structure_analysis.get("histidine_states", [])
                    if sa_histidine_list:
                        sa_histidine_states = {}
                        for his in sa_histidine_list:
                            chain = his.get("chain")
                            resnum = his.get("resnum")
                            state = his.get("state")
                            if chain and resnum and state:
                                sa_histidine_states[f"{chain}:{resnum}"] = state
                        logger.info(f"Using {len(sa_histidine_states)} pre-defined histidine state(s)")

            for protein_file in split_result["protein_files"]:
                # Find chain info for this file
                chain_id = None
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == protein_file:
                        chain_id = cid
                        break

                protein_result = {
                    "chain_id": chain_id,
                    "input_file": protein_file,
                    "output_file": None,
                    "success": False,
                    "statistics": {},
                    "errors": []
                }

                try:
                    clean_result = clean_protein(
                        pdb_file=protein_file,
                        ph=ph,
                        cap_termini=cap_termini,
                        n_terminal_cap=n_terminal_cap,
                        c_terminal_cap=c_terminal_cap,
                        terminal_cap_forcefield=terminal_cap_forcefield,
                        ignore_terminal_missing_residues=not terminal_caps_requested,
                        disulfide_pairs=sa_disulfide_pairs,
                        histidine_states=sa_histidine_states,
                        protonation_states=sa_protonation_states,
                    )

                    if clean_result["success"]:
                        protein_result["output_file"] = clean_result["output_file"]
                        protein_result["statistics"] = clean_result.get("statistics", {})
                        protein_result["provenance"] = clean_result.get("provenance", {})
                        protein_result["n_terminal_cap"] = clean_result.get("n_terminal_cap")
                        protein_result["c_terminal_cap"] = clean_result.get("c_terminal_cap")
                        protein_result["terminal_caps"] = clean_result.get("terminal_caps", {})
                        protein_result["terminal_cap_forcefield"] = clean_result.get(
                            "terminal_cap_forcefield"
                        )
                        protein_result["terminal_cap_hydrogen_completion"] = clean_result.get(
                            "terminal_cap_hydrogen_completion"
                        )
                        protein_result["component_disposition_summary"] = clean_result.get(
                            "component_disposition_summary",
                            _component_disposition_payload([])["summary"],
                        )
                        protein_result["component_disposition"] = clean_result.get(
                            "component_disposition",
                            _component_disposition_payload([]),
                        )
                        protein_result["success"] = True
                        logger.info(f"  ✓ Protein {chain_id}: {clean_result['output_file']}")
                    else:
                        protein_result["errors"] = clean_result.get("errors", [])
                        result["warnings"].append(f"Protein {chain_id} cleaning failed: {clean_result['errors']}")
                        logger.warning(f"  ✗ Protein {chain_id} failed: {clean_result['errors']}")

                except Exception as e:
                    protein_result["errors"].append(str(e))
                    result["warnings"].append(f"Protein {chain_id} error: {str(e)}")
                    logger.error(f"  ✗ Protein {chain_id} error: {e}")

                result["proteins"].append(protein_result)

        # Step 4: Rebuild standard DNA/RNA hydrogens during prep. Topology
        # generation deliberately validates atom/H completeness without doing
        # generic repair.
        if split_result.get("nucleic_files"):
            logger.info(f"Step 4: Preparing {len(split_result['nucleic_files'])} nucleic chain(s)...")
            for nucleic_file in split_result["nucleic_files"]:
                chain_id = None
                cinfo_for_nucleic = {}
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == nucleic_file:
                        chain_id = cid
                        cinfo_for_nucleic = cinfo
                        break
                nucleic_result = _prepare_standard_nucleic(
                    nucleic_file,
                    nucleic_subtype=cinfo_for_nucleic.get("nucleic_subtype"),
                    ph=ph,
                )
                nucleic_result.update({
                    "chain_id": chain_id,
                    "author_chain": cinfo_for_nucleic.get("author_chain", chain_id),
                    "input_file": nucleic_file,
                })
                if nucleic_result["success"]:
                    logger.info(
                        f"  ✓ Nucleic {chain_id}: {nucleic_result['output_file']} "
                        f"({nucleic_result['hydrogens_added']} H added)"
                    )
                else:
                    result["errors"].extend(nucleic_result.get("errors", []))
                    result["warnings"].append(
                        f"Nucleic {chain_id} preparation failed: "
                        f"{nucleic_result.get('errors', [])}"
                    )
                    logger.warning(
                        f"  ✗ Nucleic {chain_id} failed: "
                        f"{nucleic_result.get('errors', [])}"
                    )
                result["nucleics"].append(nucleic_result)

        # Step 4.5: Pass glycan chains through unchanged. GLYCAM support is
        # applied during build_amber_system; these should not be parameterized
        # as GAFF ligands.
        if split_result.get("glycan_files"):
            logger.info(f"Step 4.5: Passing through {len(split_result['glycan_files'])} glycan chain(s)...")
            for glycan_file in split_result["glycan_files"]:
                chain_id = None
                cinfo_for_glycan = {}
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == glycan_file:
                        chain_id = cid
                        cinfo_for_glycan = cinfo
                        break
                chain_data = cinfo_for_glycan.get("all_chain_data", {})
                residue_names = chain_data.get("glycan_residue_names") or chain_data.get("residue_names", [])
                if isinstance(residue_names, dict):
                    residue_names = residue_names.get("unique_residues", [])
                result["glycans"].append({
                    "chain_id": chain_id,
                    "author_chain": cinfo_for_glycan.get("author_chain", chain_id),
                    "input_file": glycan_file,
                    "output_file": glycan_file,
                    "residue_names": sorted(set(residue_names or [])),
                    "success": True,
                    "warnings": [],
                })
                logger.info(f"  ✓ Glycan {chain_id}: {glycan_file}")

        # Step 5: Process ligands
        if process_ligands and split_result.get("ligand_files"):
            logger.info(f"Step 5: Processing {len(split_result['ligand_files'])} ligand(s)...")
            
            for ligand_file in split_result["ligand_files"]:
                # Find chain info for this file
                chain_id = None
                ligand_id = None
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == ligand_file:
                        chain_id = cid
                        # Get ligand residue name
                        chain_data = cinfo.get("all_chain_data", {})
                        residue_names = chain_data.get("residue_names", {})
                        if residue_names:
                            unique_residues = residue_names.get("unique_residues", [])
                            if unique_residues:
                                ligand_id = unique_residues[0]  # First residue name
                        break
                
                # If ligand_id not found in chain_info_map, read directly from PDB file
                if not ligand_id:
                    try:
                        with open(ligand_file, 'r') as f:
                            for line in f:
                                if line.startswith('HETATM') or line.startswith('ATOM'):
                                    # Residue name is at columns 17-20 (0-indexed: 17:20)
                                    ligand_id = line[17:20].strip()
                                    if ligand_id:
                                        break
                    except Exception as e:
                        logger.warning(f"Could not read ligand ID from {ligand_file}: {e}")
                
                if not ligand_id:
                    result["warnings"].append(f"Could not determine ligand ID for {ligand_file}")
                    continue
                
                cinfo_for_ligand = chain_info_map.get(chain_id, {}) if chain_id else {}
                ligand_instance_id = cinfo_for_ligand.get("unique_id")
                author_chain = cinfo_for_ligand.get("author_chain", chain_id)
                resnum = cinfo_for_ligand.get("resnum")
                if not ligand_instance_id:
                    ligand_instance_id = f"{author_chain or chain_id}:{ligand_id}:{resnum or 'UNK'}"

                ligand_result = {
                    "ligand_instance_id": ligand_instance_id,
                    "chain_id": chain_id,
                    "author_chain": author_chain,
                    "ligand_id": ligand_id,
                    "residue_name": ligand_id[:3].upper(),
                    "resnum": resnum,
                    "input_file": ligand_file,
                    "sdf_file": None,
                    "prepared_pdb_file": None,
                    "pdb_file": None,
                    "net_charge": None,
                    "charge_source": None,
                    "mol_formal_charge": None,
                    "roundtrip_validation": None,
                    "success": False,
                    "errors": []
                }

                # Check if this ligand is excluded in structure_analysis
                sa_ligand_spec = None
                if structure_analysis:
                    sa_ligands = structure_analysis.get("ligands", [])
                    for lig_spec in sa_ligands:
                        if lig_spec.get("resname") == ligand_id or lig_spec.get("chain") == chain_id:
                            sa_ligand_spec = lig_spec
                            break

                    if sa_ligand_spec and not sa_ligand_spec.get("include", True):
                        logger.info(f"  Skipping ligand {ligand_id} (user excluded)")
                        continue

                try:
                    # Get SMILES (user-provided or from structure_analysis or fetch)
                    user_smiles = None
                    user_charge = None

                    # First check direct ligand_smiles parameter
                    if ligand_smiles and ligand_id in ligand_smiles:
                        user_smiles = ligand_smiles[ligand_id]

                    # Then check structure_analysis for overrides
                    if sa_ligand_spec:
                        if sa_ligand_spec.get("smiles"):
                            user_smiles = sa_ligand_spec["smiles"]
                        if sa_ligand_spec.get("net_charge") is not None:
                            user_charge = sa_ligand_spec["net_charge"]
                            logger.info(f"  Using user-specified charge {user_charge} for {ligand_id}")

                    # Clean ligand
                    clean_result = clean_ligand(
                        ligand_pdb=ligand_file,
                        ligand_id=ligand_id,
                        smiles=user_smiles,
                        target_ph=ph,
                        optimize=optimize_ligands
                    )

                    # Override charge if user specified
                    if user_charge is not None and clean_result["success"]:
                        clean_result["net_charge"] = user_charge
                    
                    if clean_result["success"]:
                        ligand_result["sdf_file"] = clean_result["sdf_file"]
                        ligand_result["prepared_pdb_file"] = clean_result.get("pdb_file")
                        ligand_result["pdb_file"] = clean_result.get("pdb_file")
                        ligand_result["net_charge"] = clean_result["net_charge"]
                        ligand_result["charge_source"] = clean_result.get("charge_source")
                        ligand_result["mol_formal_charge"] = clean_result.get("mol_formal_charge")
                        ligand_result["smiles_used"] = clean_result.get("smiles_used")
                        ligand_result["smiles_original"] = clean_result.get("smiles_original")
                        ligand_result["smiles_source"] = clean_result.get("smiles_source")
                        logger.info(f"  ✓ Ligand {ligand_id} ({chain_id}): cleaned, charge={clean_result['net_charge']}")
                        ligand_result["success"] = True
                    else:
                        ligand_result["errors"] = clean_result.get("errors", [])
                        ligand_result["failure_class"] = "ligand_chemistry_failed"
                        ligand_result["recommended_next_action"] = (
                            "provide_smiles_or_exclude_ligand"
                        )
                        result["warnings"].append(f"Ligand {ligand_id} cleaning failed: {clean_result['errors']}")
                        logger.warning(f"  ✗ Ligand {ligand_id} failed: {clean_result['errors']}")
                        
                except Exception as e:
                    ligand_result["errors"].append(str(e))
                    result["warnings"].append(f"Ligand {ligand_id} error: {str(e)}")
                    logger.error(f"  ✗ Ligand {ligand_id} error: {e}")
                
                result["ligands"].append(ligand_result)
        
        # Determine overall success
        # Success if requested protein/ligand processing succeeded; nucleic
        # chains are pass-through inputs and only fail if split omitted them.
        proteins_ok = any(p["success"] for p in result["proteins"]) if result["proteins"] else True
        nucleics_ok = all(nuc["success"] for nuc in result["nucleics"]) if result["nucleics"] else True
        glycans_ok = all(gly["success"] for gly in result["glycans"]) if result["glycans"] else True
        ligands_ok = any(lig["success"] for lig in result["ligands"]) if result["ligands"] else True
        
        if process_proteins and not result["proteins"]:
            proteins_ok = not split_result.get("protein_files")  # OK if no proteins to process
        if process_ligands and not result["ligands"]:
            ligands_ok = not split_result.get("ligand_files")  # OK if no ligands to process
        
        # Step 6: Merge structures if we have successful outputs
        if proteins_ok or nucleics_ok or glycans_ok or ligands_ok:
            logger.info("Step 6: Merging structures...")
            pdb_files_to_merge = []
            
            # Add protein files
            for p in result["proteins"]:
                if p["success"] and p.get("output_file"):
                    pdb_files_to_merge.append(p["output_file"])

            # Add hydrogen-complete standard DNA/RNA files.
            for nuc in result["nucleics"]:
                if nuc["success"] and nuc.get("output_file"):
                    pdb_files_to_merge.append(nuc["output_file"])

            # Add glycan files as-is; topology generation loads GLYCAM and
            # applies recorded protein-glycan linkages.
            for gly in result["glycans"]:
                if gly["success"] and gly.get("output_file"):
                    pdb_files_to_merge.append(gly["output_file"])
            
            # Add ligand files. The standard path uses the cleaned ligand PDB
            # emitted alongside the SDF.
            for lig in result["ligands"]:
                if lig["success"]:
                    # Prefer the explicit ligand PDB recorded on the ligand.
                    ligand_pdb = lig.get("pdb_file")
                    if ligand_pdb and Path(ligand_pdb).exists():
                        pdb_files_to_merge.append(ligand_pdb)
                    else:
                        result["warnings"].append(
                            f"No cleaned PDB found for ligand {lig.get('ligand_id')}"
                        )

            # Add ion files as-is (no cleaning needed). Multivalent metals
            # must land in merged.pdb so parameterize_metal_ion can locate
            # them; otherwise the metal would silently disappear between
            # split and merge even though "ion" was in include_types.
            for ion_pdb in split_result.get("ion_files", []):
                if ion_pdb and Path(ion_pdb).exists():
                    pdb_files_to_merge.append(ion_pdb)

            if pdb_files_to_merge:
                merge_result = merge_structures(
                    pdb_files=pdb_files_to_merge,
                    output_dir=str(out_dir.parent),  # Will create subdirectory
                    output_name="merged"
                )
                
                if merge_result["success"]:
                    result["merged_pdb"] = merge_result["output_file"]
                    result["merge_result"] = {
                        "success": True,
                        "output_file": merge_result["output_file"],
                        "statistics": merge_result.get("statistics", {}),
                        "chain_mapping": merge_result.get("chain_mapping", {}),
                        "chain_mapping_entries": merge_result.get("chain_mapping_entries", []),
                        "chain_identity_map_file": merge_result.get("chain_identity_map_file"),
                    }
                    prepared_source_index = _index_prepared_component_sources(
                        chain_info_map=chain_info_map,
                        proteins=result.get("proteins", []),
                        nucleics=result.get("nucleics", []),
                        glycans=result.get("glycans", []),
                        ligands=result.get("ligands", []),
                        ion_files=split_result.get("ion_files", []),
                    )
                    chain_identity_map = _enrich_chain_identity_map(
                        merge_result.get("chain_identity_map", {}),
                        prepared_source_index,
                    )
                    result["chain_identity_map"] = chain_identity_map
                    chain_identity_map_json = base_dir / "chain_identity_map.json"
                    with open(chain_identity_map_json, "w") as f:
                        json.dump(chain_identity_map, f, indent=2)
                    result["chain_identity_map_file"] = str(chain_identity_map_json)
                    logger.info(f"  ✓ Merged: {merge_result['output_file']}")
                    logger.info(
                        "  ↳ Wrote chain_identity_map.json "
                        f"({len(chain_identity_map.get('components', []))} components)"
                    )

                    residue_mapping = _build_nucleic_residue_mapping(
                        split_result=split_result,
                        merge_result=merge_result,
                    )
                    result["residue_mapping"] = residue_mapping
                    if residue_mapping:
                        residue_mapping_json = base_dir / "residue_mapping.json"
                        with open(residue_mapping_json, "w") as f:
                            json.dump(residue_mapping, f, indent=2)
                        result["residue_mapping_file"] = str(residue_mapping_json)
                        logger.info(
                            f"  ↳ Wrote residue_mapping.json ({len(residue_mapping)} nucleic residues)"
                        )

                    glycan_residue_mapping = _build_residue_mapping_for_type(
                        split_result=split_result,
                        merge_result=merge_result,
                        chain_type="glycan",
                    )
                    result["glycan_residue_mapping"] = glycan_residue_mapping
                    glycan_metadata = {
                        "glycans": result.get("glycans", []),
                        "residue_mapping": glycan_residue_mapping,
                    }
                    if glycan_residue_mapping:
                        glycan_metadata_json = base_dir / "glycan_metadata.json"
                        with open(glycan_metadata_json, "w") as f:
                            json.dump(glycan_metadata, f, indent=2)
                        result["glycan_metadata_file"] = str(glycan_metadata_json)
                        logger.info(
                            f"  ↳ Wrote glycan_metadata.json ({len(glycan_residue_mapping)} glycan residues)"
                        )

                    if result.get("glycans"):
                        protein_chain_map_for_glycans = _build_source_to_merged_chain_map(
                            chain_file_info=split_result.get("chain_file_info", []),
                            proteins=result.get("proteins", []),
                            merge_chain_mapping=merge_result.get("chain_mapping", {}),
                        )
                        remapped_links, dropped_links = _remap_glycan_linkages(
                            detected_glycan_linkages,
                            protein_chain_map_for_glycans,
                            glycan_residue_mapping,
                        )
                        result["glycan_linkages"] = remapped_links
                        if dropped_links:
                            result["warnings"].append(
                                "Glycan LINK record(s) could not be mapped onto merged.pdb "
                                "and will not be wired into the OpenMM topology by "
                                f"build_amber_system: {dropped_links}"
                            )
                            result["unmapped_glycan_linkages"] = dropped_links
                        glycan_linkages_json = base_dir / "glycan_linkages.json"
                        with open(glycan_linkages_json, "w") as f:
                            json.dump(remapped_links, f, indent=2)
                        result["glycan_linkages_file"] = str(glycan_linkages_json)
                        logger.info(
                            f"  ↳ Wrote glycan_linkages.json ({len(remapped_links)} linkages)"
                        )

                    # Apply merge_structures' chain reassignment to the PTM
                    # detection list captured before cleaning. Without this,
                    # `phosphorylate_residues --restore-from-detection` would
                    # look up the source chain id against merged.pdb's
                    # reassigned ids and either miss (multi-letter mmCIF
                    # author chain truncated by split_molecules + reassigned
                    # by merge_structures) or — worse — silently hit the
                    # wrong residue.
                    if detected_ptm_residues:
                        composite_map = _build_source_to_merged_chain_map(
                            chain_file_info=split_result.get("chain_file_info", []),
                            proteins=result.get("proteins", []),
                            merge_chain_mapping=merge_result.get("chain_mapping", {}),
                        )
                        remapped, dropped = _remap_detected_ptm_chains(
                            detected_ptm_residues,
                            composite_map,
                        )
                        if dropped:
                            result["warnings"].append(
                                "PTM residue(s) on source chains that did not "
                                "make it into the merged PDB (likely excluded "
                                f"by select_chains): {dropped}. They will not "
                                "be available for `phosphorylate_residues "
                                "--restore-from-detection`."
                            )
                        detected_ptm_residues = remapped

                    # Reconcile CYS/CYX residue names against the
                    # authoritative disulfide_bonds list. pdb2pqr does its
                    # own geometric SS detection and may have left CYX
                    # residues that our list does not include (or vice
                    # versa). Without this step, `disulfide_pairs=[]`
                    # would yield CYX residues in merged.pdb without a
                    # corresponding SG-SG topology bond, leaving SG
                    # atoms unprotonated.
                    try:
                        reconcile = _reconcile_cyx_cys_in_pdb(
                            merge_result["output_file"],
                            result.get("disulfide_bonds", []),
                        )
                        if (
                            reconcile["renamed_to_cys"]
                            or reconcile["renamed_to_cyx"]
                            or reconcile.get("stripped_hg_from_cyx", 0)
                        ):
                            logger.info(
                                f"  ↳ CYS/CYX reconciliation: "
                                f"{reconcile['renamed_to_cyx']} promoted, "
                                f"{reconcile['renamed_to_cys']} demoted, "
                                f"{reconcile.get('stripped_hg_from_cyx', 0)} HG atoms stripped"
                            )
                            result["cys_cyx_reconciliation"] = reconcile
                    except Exception as e:
                        result["warnings"].append(
                            f"CYS/CYX reconciliation skipped: {type(e).__name__}: {e}"
                        )
                else:
                    result["warnings"].append(f"Merge failed: {merge_result.get('errors', [])}")
                    result["merge_result"] = {"success": False, "errors": merge_result.get("errors", [])}
                    logger.warning(f"  ✗ Merge failed: {merge_result.get('errors', [])}")
            else:
                result["warnings"].append("No files available to merge")
        
        result["success"] = proteins_ok and nucleics_ok and glycans_ok and ligands_ok
        result["protein_preparation_success"] = proteins_ok
        result["nucleic_preparation_success"] = nucleics_ok
        result["glycan_preparation_success"] = glycans_ok
        result["ligand_preparation_success"] = ligands_ok

        # -- overall_status: workflow-level status for skill dispatch ------
        failed_ligands = [
            lig for lig in result.get("ligands", []) if not lig.get("success")
        ]
        if proteins_ok and nucleics_ok and glycans_ok and ligands_ok:
            result["overall_status"] = "success"
        elif proteins_ok and not ligands_ok:
            result["overall_status"] = "completed_with_blocking_ligand_failure"
        else:
            result["overall_status"] = "failed"

        # -- workflow_recommendation: deterministic options for the caller --
        if failed_ligands and proteins_ok:
            blocking = []
            for lig in failed_ligands:
                blocking.append({
                    "ligand_id": lig.get("ligand_id"),
                    "failure_class": lig.get("failure_class"),
                    "ligand_class": lig.get("ligand_class"),
                    "recommended_next_action": lig.get("recommended_next_action"),
                })
            # Collect unique next-actions
            actions = {b["recommended_next_action"] for b in blocking
                       if b["recommended_next_action"]}
            options = []
            if "provide_smiles_or_exclude_ligand" in actions:
                options.append("provide_ligand_chemistry_and_rerun")
            options.append("exclude_ligands_and_continue_protein_only")
            if "hard_fail" in actions:
                options.append("stop")
            else:
                options.append("stop")
            result["workflow_recommendation"] = {
                "blocking_ligands": blocking,
                "options": options,
            }

        component_entries: list[dict[str, Any]] = []
        for protein in result.get("proteins", []):
            component_payload = protein.get("component_disposition") or {}
            for entry in component_payload.get("entries", []) or []:
                recorded_entry = dict(entry)
                recorded_entry.setdefault("source_file", protein.get("input_file"))
                recorded_entry.setdefault("chain_id", protein.get("chain_id"))
                component_entries.append(recorded_entry)
        component_disposition = _component_disposition_payload(component_entries)
        excluded_components = {
            "schema_version": component_disposition["schema_version"],
            "summary": component_disposition["summary"],
            "entries": [
                entry for entry in component_entries
                if entry.get("action_taken") == "excluded"
            ],
        }
        component_disposition_json = base_dir / "component_disposition.json"
        excluded_components_json = base_dir / "excluded_components.json"
        with open(component_disposition_json, "w") as f:
            json.dump(component_disposition, f, indent=2)
        with open(excluded_components_json, "w") as f:
            json.dump(excluded_components, f, indent=2)
        result["component_disposition"] = component_disposition
        result["component_disposition_summary"] = component_disposition["summary"]
        result["component_disposition_file"] = str(component_disposition_json)
        result["excluded_components_file"] = str(excluded_components_json)

        # Aggregate provenance from all proteins into top-level summary
        # so LLM can read it directly without digging into proteins[]
        preparation_summary = {}
        if result.get("source_structure_id"):
            preparation_summary["source_structure_id"] = result["source_structure_id"]
            preparation_summary["source_selection"] = result.get("source_selection") or {}
        preparation_summary["disulfide_pairs"] = result.get("disulfide_bonds", [])
        preparation_summary["disulfide_detection_recorded"] = bool(
            result.get("disulfide_source")
        )
        for p in result.get("proteins", []):
            prov = p.get("provenance", {})
            if prov:
                for key, val in prov.items():
                    if key == "histidine_states" and isinstance(val, dict):
                        preparation_summary.setdefault("histidine_states", {}).update(val)
                    elif key == "protonation_states" and isinstance(val, list):
                        preparation_summary.setdefault("protonation_states", []).extend(val)
                    elif key == "missing_residues_modeled" and val:
                        preparation_summary.setdefault("missing_residues_modeled", []).extend(val)
                    elif key == "missing_residues_count" and val:
                        preparation_summary["missing_residues_count"] = \
                            preparation_summary.get("missing_residues_count", 0) + val
                    elif key not in preparation_summary:
                        preparation_summary[key] = val
        successful_proteins = [p for p in result.get("proteins", []) if p.get("success")]
        n_caps = sorted({
            p.get("n_terminal_cap")
            for p in successful_proteins
            if p.get("n_terminal_cap")
        })
        c_caps = sorted({
            p.get("c_terminal_cap")
            for p in successful_proteins
            if p.get("c_terminal_cap")
        })
        cap_h_reports = [
            p.get("terminal_cap_hydrogen_completion") or {}
            for p in successful_proteins
        ]
        cap_h_reports = [r for r in cap_h_reports if r.get("success") and not r.get("skipped")]
        if n_caps or c_caps or cap_h_reports:
            preparation_summary["terminal_capping_recorded"] = True
            if n_caps:
                preparation_summary["n_terminal_cap"] = n_caps[0] if len(n_caps) == 1 else n_caps
            if c_caps:
                preparation_summary["c_terminal_cap"] = c_caps[0] if len(c_caps) == 1 else c_caps
            preparation_summary["terminal_cap_hydrogen_completion_method"] = "openmm_modeller"
            preparation_summary["terminal_cap_hydrogens_added"] = sum(
                int(r.get("cap_hydrogens_added") or 0)
                for r in cap_h_reports
            )
            cap_forcefields = sorted({
                str(r.get("forcefield"))
                for r in cap_h_reports
                if r.get("forcefield")
            })
            cap_forcefield_xml = sorted({
                str(r.get("forcefield_xml"))
                for r in cap_h_reports
                if r.get("forcefield_xml")
            })
            if cap_forcefields:
                preparation_summary["terminal_cap_forcefield"] = (
                    cap_forcefields[0] if len(cap_forcefields) == 1 else cap_forcefields
                )
            if cap_forcefield_xml:
                preparation_summary["terminal_cap_forcefield_xml"] = cap_forcefield_xml
        if result.get("nucleics"):
            successful_nucleics = [n for n in result["nucleics"] if n.get("success")]
            preparation_summary["has_nucleic"] = bool(successful_nucleics)
            preparation_summary["nucleic_subtypes"] = sorted({
                n.get("nucleic_subtype")
                for n in successful_nucleics
                if n.get("nucleic_subtype")
            })
            preparation_summary["nucleic_chains"] = [
                {
                    "chain_id": n.get("chain_id"),
                    "author_chain": n.get("author_chain"),
                    "nucleic_subtype": n.get("nucleic_subtype"),
                    "hydrogen_rebuild_method": n.get("hydrogen_rebuild_method"),
                    "nucleic_forcefield_xml": n.get("nucleic_forcefield_xml"),
                    "hydrogens_added": n.get("hydrogens_added", 0),
                }
                for n in successful_nucleics
            ]
            preparation_summary["nucleic_hydrogen_rebuild_method"] = "openmm_modeller"
            preparation_summary["nucleic_hydrogens_added"] = sum(
                int(n.get("hydrogens_added") or 0)
                for n in successful_nucleics
            )
            preparation_summary["nucleic_forcefield_xml"] = sorted({
                n.get("nucleic_forcefield_xml")
                for n in successful_nucleics
                if n.get("nucleic_forcefield_xml")
            })
            residue_names = set()
            for info in split_result.get("chain_file_info", []):
                if info.get("chain_type") == "nucleic":
                    chain_data = chain_info_map.get(info.get("chain_id"), {}).get("all_chain_data", {})
                    res_data = chain_data.get("residue_names", {})
                    if isinstance(res_data, dict):
                        residue_names.update(res_data.get("unique_residues", []))
                    elif isinstance(res_data, list):
                        residue_names.update(res_data)
            preparation_summary["nucleic_residue_names"] = sorted(residue_names)
        if result.get("glycans"):
            successful_glycans = [g for g in result["glycans"] if g.get("success")]
            preparation_summary["has_glycan"] = bool(successful_glycans)
            preparation_summary["glycan_chains"] = [
                {
                    "chain_id": g.get("chain_id"),
                    "author_chain": g.get("author_chain"),
                    "residue_names": g.get("residue_names", []),
                }
                for g in successful_glycans
            ]
            glycan_residue_names = set()
            for gly in successful_glycans:
                glycan_residue_names.update(gly.get("residue_names", []))
            preparation_summary["glycan_residue_names"] = sorted(glycan_residue_names)
            preparation_summary["glycan_linkage_count"] = len(result.get("glycan_linkages", []))
        if detected_ptm_residues:
            preparation_summary["detected_ptm_residues"] = detected_ptm_residues
        if result.get("chain_identity_map"):
            chain_identity_components = result["chain_identity_map"].get("components", [])
            preparation_summary["chain_identity_map"] = {
                "component_count": len(chain_identity_components),
                "pdb_chain_ids_may_repeat": result["chain_identity_map"].get(
                    "pdb_chain_ids_may_repeat", True
                ),
                "artifact": "chain_identity_map.json",
            }
        component_summary = result.get("component_disposition_summary") or {}
        preparation_summary["component_disposition_recorded"] = bool(
            result.get("component_disposition_file")
        )
        preparation_summary["experimental_isotope_atoms_excluded"] = int(
            component_summary.get("experimental_isotope_atoms_excluded", 0)
        )
        preparation_summary["experimental_isotopes_excluded"] = (
            preparation_summary["experimental_isotope_atoms_excluded"] > 0
        )
        result["preparation_summary"] = preparation_summary

        # -- confirmation_needed: structured block for HITL review --------
        # Surface disulfide bonds and residue protonation states in one
        # place so the skill can show them to the user before proceeding
        # to solvation. Each block carries a `source` field so the skill
        # can tell "auto_detected" from "user_override" and avoid prompting
        # again when the caller already supplied an explicit value.
        applied_his = preparation_summary.get("histidine_states", {}) or {}
        applied_protonation = preparation_summary.get("protonation_states", []) or []
        protonation_source = (
            "user_override"
            if (histidine_states or protonation_states)
            else "auto_detected"
        )
        confirmation_items = {
            "disulfide_bonds": {
                "source": result.get("disulfide_source", "auto_detected"),
                "pairs": result.get("disulfide_bonds", []),
            },
            "histidine_states": {
                "source": protonation_source,
                "states": applied_his,
            },
            "protonation_states": {
                "source": protonation_source,
                "states": applied_protonation,
            },
        }
        if confirmation_items["disulfide_bonds"]["pairs"] or applied_his or applied_protonation:
            confirmation_items["policy"] = (
                "In human_in_the_loop mode, present these values to the user "
                "and confirm before invoking solvate_structure. In autonomous "
                "mode, log and continue. When `source == user_override` the "
                "skill may skip the prompt — the caller already made the "
                "decision. To change auto-detected values, re-run "
                "prepare_complex with --disulfide-pairs, --histidine-states, "
                "or --protonation-states."
            )
            result["confirmation_needed"] = confirmation_items

        # Write ligand_chemistry.json to the job root for auto-detection by
        # build_amber_system. Prep owns ligand identity, coordinates,
        # protonation, charge, and chemistry provenance; topology owns
        # geostd/GAFF force-field resolution.
        ligand_chemistry = []
        if result.get("ligands"):
            ligand_chemistry = [
                {
                    "sdf": lig.get("sdf_file"),
                    "sdf_file": lig.get("sdf_file"),
                    "pdb": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "coordinate_file": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "ligand_instance_id": lig.get("ligand_instance_id"),
                    "chain_id": lig.get("chain_id"),
                    "author_chain": lig.get("author_chain"),
                    "ligand_id": lig.get("ligand_id"),
                    "residue_name": lig.get("residue_name", lig.get("ligand_id", "LIG")[:3].upper()),
                    "resnum": lig.get("resnum"),
                    "net_charge": lig.get("net_charge"),
                    "charge_source": lig.get("charge_source"),
                    "mol_formal_charge": lig.get("mol_formal_charge"),
                    "smiles": lig.get("smiles_used"),
                    "smiles_original": lig.get("smiles_original"),
                    "smiles_source": lig.get("smiles_source"),
                    "target_ph": ph,
                }
                for lig in result["ligands"]
                if lig.get("success") and lig.get("sdf_file")
            ]
            if ligand_chemistry:
                ligand_chemistry_json = base_dir / "ligand_chemistry.json"
                with open(ligand_chemistry_json, 'w') as f:
                    json.dump(ligand_chemistry, f, indent=2)
                result["ligand_chemistry"] = ligand_chemistry
                logger.info(
                    f"Wrote ligand_chemistry.json ({len(ligand_chemistry)} ligands) "
                    f"to {ligand_chemistry_json}"
                )

        # Persist merged disulfide bond pairs (written even when empty so
        # downstream consumers can distinguish "none" from "not recorded").
        disulfide_json = base_dir / "disulfide_bonds.json"
        with open(disulfide_json, 'w') as f:
            json.dump(result.get("disulfide_bonds", []), f, indent=2)

        # Save workflow summary
        summary_file = out_dir / "prepare_complex_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(result, f, indent=2, default=str)

        n_prot = sum(1 for p in result['proteins'] if p['success'])
        n_nuc = sum(1 for nuc in result['nucleics'] if nuc['success'])
        n_gly = sum(1 for gly in result['glycans'] if gly['success'])
        n_lig = sum(1 for lig in result['ligands'] if lig['success'])
        logger.info(f"Complex preparation: overall_status={result['overall_status']}")
        logger.info(f"  Proteins: {n_prot}/{len(result['proteins'])}")
        logger.info(f"  Nucleics: {n_nuc}/{len(result['nucleics'])}")
        logger.info(f"  Glycans: {n_gly}/{len(result['glycans'])}")
        logger.info(f"  Ligands: {n_lig}/{len(result['ligands'])}")
        if result.get("merged_pdb"):
            logger.info(f"  Merged PDB: {result['merged_pdb']}")

    except Exception as e:
        error_msg = f"Error during complex preparation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        result["overall_status"] = "failed"
        logger.error(error_msg)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success") or result.get("overall_status") == "success":
            proteins = result.get("proteins", [])
            nucleics = result.get("nucleics", [])
            glycans = result.get("glycans", [])
            ligands = result.get("ligands", [])
            artifacts = {}
            if result.get("merged_pdb"):
                artifacts["merged_pdb"] = "artifacts/merge/merged.pdb"
            lig_chemistry = [
                {
                    "sdf": lig.get("sdf_file"),
                    "sdf_file": lig.get("sdf_file"),
                    "pdb": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "coordinate_file": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "ligand_instance_id": lig.get("ligand_instance_id"),
                    "chain_id": lig.get("chain_id"),
                    "author_chain": lig.get("author_chain"),
                    "ligand_id": lig.get("ligand_id"),
                    "residue_name": lig.get("residue_name", lig.get("ligand_id", "LIG")[:3].upper()),
                    "resnum": lig.get("resnum"),
                    "net_charge": lig.get("net_charge"),
                    "charge_source": lig.get("charge_source"),
                    "mol_formal_charge": lig.get("mol_formal_charge"),
                    "smiles": lig.get("smiles_used"),
                    "smiles_original": lig.get("smiles_original"),
                    "smiles_source": lig.get("smiles_source"),
                    "target_ph": ph,
                }
                for lig in ligands if lig.get("success") and lig.get("sdf_file")
            ]
            if lig_chemistry:
                artifacts["ligand_chemistry"] = lig_chemistry
            # disulfide_bonds.json is always written (see above); register
            # it as a node artifact so build_amber_system / analysis tools
            # can auto-resolve it from the prep ancestor.
            if (base_dir / "disulfide_bonds.json").exists():
                artifacts["disulfide_bonds"] = "artifacts/disulfide_bonds.json"
            if (base_dir / "component_disposition.json").exists():
                artifacts["component_disposition"] = "artifacts/component_disposition.json"
            if (base_dir / "excluded_components.json").exists():
                artifacts["excluded_components"] = "artifacts/excluded_components.json"
            if (base_dir / "residue_mapping.json").exists():
                artifacts["residue_mapping"] = "artifacts/residue_mapping.json"
            if (base_dir / "chain_identity_map.json").exists():
                artifacts["chain_identity_map"] = "artifacts/chain_identity_map.json"
            if (base_dir / "glycan_metadata.json").exists():
                artifacts["glycan_metadata"] = "artifacts/glycan_metadata.json"
            if (base_dir / "glycan_linkages.json").exists():
                artifacts["glycan_linkages"] = "artifacts/glycan_linkages.json"
            if result.get("source_selection_file"):
                artifacts["source_selection"] = result["source_selection_file"]
            complete_node(job_dir, node_id,
                artifacts=artifacts,
                metadata=result.get("preparation_summary", {}),
                warnings=result.get("warnings", []))
            update_job_summaries(job_dir,
                system={
                    "pdb_id": result.get("pdb_id"),
                    "chains": [
                        p.get("chain_id", "") for p in proteins
                    ] + [
                        n.get("chain_id", "") for n in nucleics
                    ] + [
                        g.get("chain_id", "") for g in glycans
                    ],
                    "num_residues": sum(
                        p.get("statistics", {}).get("final_residues", 0)
                        for p in proteins if p.get("success")),
                    "nucleics": [
                        {
                            "chain_id": n.get("chain_id"),
                            "nucleic_subtype": n.get("nucleic_subtype"),
                        }
                        for n in nucleics if n.get("success")
                    ],
                    "glycans": [
                        {
                            "chain_id": g.get("chain_id"),
                            "residue_names": g.get("residue_names", []),
                        }
                        for g in glycans if g.get("success")
                    ],
                    "ligands": [lig["ligand_id"] for lig in ligands if lig.get("success")],
                },
                preparation=result.get("preparation_summary", {}))
        else:
            fail_node(job_dir, node_id,
                      errors=result.get("errors", []),
                      warnings=result.get("warnings", []))

    return result


def create_mutated_structure(
    pdb_file: Optional[str] = None,
    mutations: Optional[List[str]] = None,
    sequence: Optional[str] = None,
    seq_file: Optional[str] = None,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    repack_radius_angstrom: float = 8.0,
    refinement_iterations: int = 5,
) -> dict:
    """Apply point/multi-mutations to a *cleaned* structure via HPacker.

    Mutation is a post-prep transformation: it expects a structure that has
    already been cleaned by ``prepare_complex`` (PDBFixer + pdb4amber +
    protonation + merge). HPacker changes the requested mutation residues and
    repacks nearby side chains.

    DAG placement::

        source_001 -> prep_001 (prepare_complex) -> prep_002 (this tool)
                                                   -> solv_001 -> ...

    In node mode (``job_dir`` + ``node_id`` with ``node_type=prep``), the
    input PDB is auto-resolved from the **nearest prep ancestor's
    ``merged_pdb`` artifact** (i.e., the cleaned output of
    ``prepare_complex``). The mutated PDB is registered under both
    ``merged_pdb`` and ``mutated_pdb`` keys so the downstream ``solv``
    resolver picks it up automatically without extra wiring.

    Args:
        pdb_file: Cleaned PDB. Required unless running in node mode with a
                  resolvable prep ancestor.
        mutations: Preferred mutation specs in ``L99A`` or ``A:L99A`` notation.
        sequence: Legacy mixed-case one-letter sequence input. Lowercase means
                  keep; uppercase means mutate to that residue. Mutually
                  exclusive with ``mutations`` and ``seq_file``.
        seq_file: Path to a legacy mixed-case sequence text file. Mutually
                  exclusive with ``mutations`` and ``sequence``.
        name: Optional name prefix for output files (e.g. "k27a").
        output_dir: Output directory (ignored in node mode — artifacts go
                    to the node directory).
        job_dir: DAG job directory (node mode).
        node_id: Node ID inside ``job_dir``; expected ``node_type=prep``
                 with a prep ancestor as parent.
        repack_radius_angstrom: Nearby side chains within this HPacker
                                proximity cutoff are repacked.
        refinement_iterations: HPacker refinement iterations.

    Returns:
        Dict with:
            - success: bool
            - output_dir: str
            - output_path: str — path to mutated PDB
            - errors: list[str]
            - warnings: list[str]
    """
    result = {
        "success": False,
        "output_dir": None,
        "output_path": None,
        "mutation_specs": [],
        "mutation_count": 0,
        "mutation_backend": "hpacker",
        "sidechain_method": "hpacker",
        "repack_radius_angstrom": repack_radius_angstrom,
        "refinement_iterations": refinement_iterations,
        "hpacker_version": None,
        "code": None,
        "errors": [],
        "warnings": [],
    }

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "prep",
            actual_conditions={
                "mutations": mutations,
                "sequence": sequence,
                "seq_file": seq_file,
                "name": name,
                "repack_radius_angstrom": repack_radius_angstrom,
                "refinement_iterations": refinement_iterations,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="create_mutated_structure node execution context invalid",
            )

    # Auto-resolve input from nearest prep ancestor (the cleaned merged.pdb,
    # not the raw source structure — mutation runs AFTER prepare_complex).
    if job_dir and node_id and not pdb_file:
        from mdclaw._node import find_ancestor_artifact
        v = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb")
        if v:
            pdb_file = v

    mutation_inputs = sum([
        bool(mutations),
        sequence is not None,
        seq_file is not None,
    ])
    if mutation_inputs != 1:
        result["errors"].append(
            "Provide exactly one of `mutations`, `sequence`, or `seq_file`."
        )
        result["code"] = "mutation_input_invalid"
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="create_mutated_structure sequence input invalid",
            )
        return result

    if not pdb_file:
        result["errors"].append(
            "pdb_file is required (or pass --job-dir/--node-id with a prep "
            "ancestor that provides a merged_pdb artifact)."
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="create_mutated_structure missing pdb_file",
            )
        return result

    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.is_file():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="create_mutated_structure input PDB file not found",
            )
        return result

    # Resolve output base_dir + begin_node
    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node
        base_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = create_unique_subdir(WORKING_DIR, "hpacker")
    ensure_directory(base_dir)

    pref = f"{name}_" if name else ""

    seq_path = None
    if sequence is not None:
        seq_path = (base_dir / f"{pref}legacy_sequence.txt").resolve()
        seq_path.write_text(sequence)
    elif seq_file is not None:
        seq_path = Path(seq_file).resolve()
        if not seq_path.is_file():
            result["errors"].append(f"sequence file not found: {seq_file}")
            result["code"] = "mutation_input_invalid"
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=result["errors"])
            return result

    output_path = (base_dir / f"{pref}mutated.pdb").resolve()

    from mdclaw.sidechain_packer import run_hpacker_mutation

    logger.info("Running HPacker mutation: %s -> %s", pdb_path, output_path)
    hpacker_result = run_hpacker_mutation(
        pdb_path,
        output_path,
        mutations=mutations,
        sequence=sequence,
        seq_file=seq_path if seq_file is not None else None,
        repack_radius_angstrom=repack_radius_angstrom,
        refinement_iterations=refinement_iterations,
    )
    result["warnings"].extend(hpacker_result.warnings)
    result["errors"].extend(hpacker_result.errors)
    result["code"] = hpacker_result.code
    result["mutation_specs"] = hpacker_result.mutation_specs
    result["mutation_count"] = len(hpacker_result.mutation_specs)
    result["hpacker_version"] = hpacker_result.hpacker_version

    if hpacker_result.success and output_path.is_file():
        result["success"] = True
        result["output_dir"] = str(base_dir)
        result["output_path"] = str(output_path)
        logger.info("HPacker successfully generated mutant structure")
    elif not result["errors"]:
        result["errors"].append("HPacker produced no PDB output")
        result["code"] = "hpacker_no_output"

    if _node_mode:
        from mdclaw._node import complete_node, fail_node
        if result["success"]:
            rel_out = f"artifacts/{output_path.name}"
            complete_node(
                job_dir, node_id,
                artifacts={
                    "merged_pdb": rel_out,
                    "mutated_pdb": rel_out,
                },
                metadata={
                    "name": name,
                    "mutation_source_pdb": str(pdb_path),
                    "mutation_backend": "hpacker",
                    "sidechain_method": "hpacker",
                    "mutation_specs": hpacker_result.mutation_specs,
                    "mutation_count": len(hpacker_result.mutation_specs),
                    "sequence_file": str(seq_path) if seq_path else None,
                    "repack_radius_angstrom": repack_radius_angstrom,
                    "refinement_iterations": refinement_iterations,
                    "hpacker_version": hpacker_result.hpacker_version,
                },
                warnings=result.get("warnings", []),
            )
        else:
            fail_node(
                job_dir, node_id,
                errors=result["errors"],
                warnings=result.get("warnings", []),
            )

    return result


# =============================================================================
# Phosphorylation
# =============================================================================

# Map of phospho residue → its plain (post-PDBFixer) counterpart and the
# hydroxyl hydrogen atom name we must strip so the openmmforcefields
# phosaa XML residue template (``amber/phosaa19SB.xml`` / ``phosaa14SB.xml``
# / ``phosaa10.xml`` / ``phosfb18.xml``) can rebuild the phosphate atoms
# against the existing OG / OG1 / OH oxygen when SystemGenerator builds
# the System. (The XML route only assigns parameters to existing atoms,
# unlike the legacy tleap path which also added missing atoms — so this
# tool also has to write ``P`` and ``O1P``/``O2P``/``O3P`` with sensible
# tetrahedral coordinates.)
_PHOSPHO_TARGETS = {
    "SEP": {"source": "SER", "hydroxyl_h": "HG", "ester_o": "OG", "parent_c": "CB"},
    "TPO": {"source": "THR", "hydroxyl_h": "HG1", "ester_o": "OG1", "parent_c": "CB"},
    "PTR": {"source": "TYR", "hydroxyl_h": "HH", "ester_o": "OH", "parent_c": "CZ"},
}


def _compute_phospho_atom_coords(
    parent_c_xyz: tuple[float, float, float],
    ester_o_xyz: tuple[float, float, float],
    *,
    p_o_ester_bond: float = 1.60,
    p_o_terminal_bond: float = 1.50,
    o_h_bond: float = 0.97,
) -> dict[str, tuple[float, float, float]]:
    """Place the phosphate atoms on a tetrahedral phosphorus.

    Geometry (Amber dianion convention; SEP / TPO / PTR all share the
    same skeleton):

    - ``P`` sits along the parent_C → ester_O direction, extended by
      ``p_o_ester_bond`` past the ester oxygen.
    - The three terminal oxygens (``O1P`` / ``O2P`` / ``O3P``) ring P
      tetrahedrally so each forms a ~109.5° angle with the P-OG / P-OG1
      / P-OH bond. They are evenly spaced 120° around the C-O axis with
      arbitrary phase (downstream eq/min relaxes the orientation).
    - ``HOP2`` / ``HOP3`` are written as protons on ``O2P`` / ``O3P``
      with the H pointing radially outward from P. Pablo's CCD entries
      for SEP / TPO / PTR ship the *protonated* (singly-anion or
      neutral) form and refuse to match unless these hydrogens are
      present; the topology builder strips them again with
      ``Modeller.delete`` after Pablo loads so Amber's dianion phosaa
      templates apply.
    """
    import math

    cx, cy, cz = parent_c_xyz
    ox, oy, oz = ester_o_xyz
    vx, vy, vz = ox - cx, oy - cy, oz - cz
    norm = math.sqrt(vx * vx + vy * vy + vz * vz) or 1.0
    ux, uy, uz = vx / norm, vy / norm, vz / norm

    px = ox + p_o_ester_bond * ux
    py = oy + p_o_ester_bond * uy
    pz = oz + p_o_ester_bond * uz

    if abs(ux) < 0.9:
        rx, ry, rz = 1.0, 0.0, 0.0
    else:
        rx, ry, rz = 0.0, 1.0, 0.0
    dot = rx * ux + ry * uy + rz * uz
    e1x = rx - dot * ux
    e1y = ry - dot * uy
    e1z = rz - dot * uz
    n = math.sqrt(e1x * e1x + e1y * e1y + e1z * e1z) or 1.0
    e1x, e1y, e1z = e1x / n, e1y / n, e1z / n
    e2x = uy * e1z - uz * e1y
    e2y = uz * e1x - ux * e1z
    e2z = ux * e1y - uy * e1x

    cos_t = 1.0 / 3.0
    sin_t = math.sqrt(8.0) / 3.0

    out: dict[str, tuple[float, float, float]] = {}
    op_data: list[tuple[str, tuple[float, float, float], tuple[float, float, float]]] = []
    for label, phi in (("O1P", 0.0), ("O2P", 2 * math.pi / 3), ("O3P", 4 * math.pi / 3)):
        c, s = math.cos(phi), math.sin(phi)
        dx = cos_t * ux + sin_t * (c * e1x + s * e2x)
        dy = cos_t * uy + sin_t * (c * e1y + s * e2y)
        dz = cos_t * uz + sin_t * (c * e1z + s * e2z)
        op_xyz = (
            px + p_o_terminal_bond * dx,
            py + p_o_terminal_bond * dy,
            pz + p_o_terminal_bond * dz,
        )
        out[label] = op_xyz
        op_data.append((label, op_xyz, (dx, dy, dz)))
    out["P"] = (px, py, pz)
    # Protons placed along the P→O direction, extended by ``o_h_bond`` past
    # each terminal oxygen. Direction-only — bond / angle relax in eq/min.
    for label, (ox_p, oy_p, oz_p), (dx, dy, dz) in op_data:
        # ``O2P`` → ``HOP2`` etc. The Pablo CCD entries name the proton
        # ``HOP{n}`` rather than ``HO{n}P``.
        h_label = "HOP" + label[1]
        out[h_label] = (
            ox_p + o_h_bond * dx,
            oy_p + o_h_bond * dy,
            oz_p + o_h_bond * dz,
        )
    return out


def _build_source_to_merged_chain_map(
    chain_file_info: list[dict],
    proteins: list[dict],
    merge_chain_mapping: dict,
) -> dict:
    """Build the ``source_author_chain -> merged_chain`` composite map.

    Three pieces are joined on file path:

    - ``chain_file_info`` (from ``split_molecules``) gives ``chain_id`` (the
      label_asym_id used internally) plus the **full** ``author_chain``
      (auth_asym_id, possibly multi-letter on mmCIF inputs like ``"BBB"``).
    - ``proteins`` (from ``prepare_complex``) maps ``chain_id`` to the
      cleaned ``output_file`` that ``merge_structures`` actually consumed.
    - ``merge_chain_mapping`` (from ``merge_structures``) maps
      ``cleaned_file_path -> {1char_in_split: merged_chain}``. The 1-char
      key is what ``split_molecules`` wrote into the PDB (PDB format only
      has a 1-character chain column, so multi-letter authors get
      truncated); we don't use the key directly because ``split_molecules``
      emits one chain per file so the dict has exactly one entry whose
      value is what we want.

    The result is keyed by the **full** source author chain so that PTMs
    coming out of ``detect_ptm_sites`` (which records the source chain as
    gemmi sees it on the source structure — multi-letter for mmCIF) line
    up directly with the merged chain id without a brittle truncate-and-
    pray step.
    """
    chain_id_to_author: dict[str, str] = {}
    for info in chain_file_info or []:
        cid = info.get("chain_id")
        if cid is not None:
            chain_id_to_author[cid] = info.get("author_chain", cid)

    composite: dict[str, str] = {}
    for p in proteins or []:
        if not p.get("success"):
            continue
        cleaned_file = p.get("output_file")
        cid = p.get("chain_id")
        if not cleaned_file or cid is None:
            continue
        author = chain_id_to_author.get(cid, cid)
        per_file = (merge_chain_mapping or {}).get(cleaned_file) or {}
        if not per_file:
            continue
        # split_molecules emits one chain per cleaned file, so the per-file
        # mapping has exactly one entry. Take its value (the merged id).
        merged_id = next(iter(per_file.values()))
        composite[author] = merged_id
    return composite


def _remap_detected_ptm_chains(
    detected_ptm_residues: list[dict],
    composite_chain_map: dict,
) -> tuple[list[dict], list[dict]]:
    """Apply a pre-built ``source_author_chain -> merged_chain`` map to PTM
    detection results.

    The composite map is built by :func:`_build_source_to_merged_chain_map`
    inside ``prepare_complex`` because the join needs three sources of
    information (split's chain_file_info, prepare_complex's proteins[],
    and merge's chain_mapping). Splitting the helpers keeps this one
    trivially testable in isolation.

    Args:
        detected_ptm_residues: list of ``{"chain","resnum","name"}`` from
            ``detect_ptm_sites`` — ``chain`` is the **source author chain**
            (full, possibly multi-letter on mmCIF inputs).
        composite_chain_map: ``{source_author_chain: merged_chain}``.

    Returns:
        ``(remapped, dropped)``. Each remapped entry carries:
            - ``chain``: the merged.pdb chain id (what
              ``phosphorylate_residues`` actually looks up).
            - ``original_chain``: the source author chain (provenance).
            - ``resnum`` / ``name``: unchanged.
        ``dropped`` collects entries whose source chain has no entry in
        the composite map (typically excluded by ``select_chains``).
    """
    remapped: list[dict] = []
    dropped: list[dict] = []
    for ptm in detected_ptm_residues or []:
        original = ptm["chain"]
        merged_chain = (composite_chain_map or {}).get(original)
        if merged_chain is None:
            dropped.append(dict(ptm))
            continue
        remapped.append({
            "chain": merged_chain,
            "original_chain": original,
            "resnum": ptm["resnum"],
            "name": ptm["name"],
        })
    return remapped, dropped


def _parse_sites_str(sites_str: str) -> list[dict]:
    """Parse "A:65:SEP,A:178:TPO" into a list of site dicts."""
    out: list[dict] = []
    for chunk in sites_str.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid site entry '{chunk}': expected 'CHAIN:RESNUM:TARGET'"
            )
        chain, resnum_s, target = parts
        try:
            resnum = int(resnum_s)
        except ValueError as e:
            raise ValueError(
                f"Invalid resnum in site '{chunk}': '{resnum_s}'"
            ) from e
        out.append({"chain": chain.strip(), "resnum": resnum, "target": target.strip().upper()})
    return out


def _apply_phosphorylation_to_pdb(
    in_path: Path,
    out_path: Path,
    sites: list[dict],
) -> dict:
    """Rename target residues to SEP/TPO/PTR and strip hydroxyl hydrogens.

    Operates on standard PDB format (cols 18-20 = resName, col 22 = chainID,
    cols 23-26 = resSeq, cols 13-16 = atom name). `clean_protein` always
    emits standard PDB so single-character chain IDs are guaranteed.

    Returns a dict with:
        applied: list of fully-applied sites (chain, resnum, target, source)
        mismatch: list of sites whose current residue did not match the
                  expected source residue for the requested target
        not_found: list of sites whose (chain, resnum) was not found in the PDB
    """
    site_map: dict[tuple, str] = {}
    for s in sites:
        site_map[(s["chain"], int(s["resnum"]))] = s["target"]

    # First pass: gather parent_C and ester_O coordinates per target site so
    # we can synthesise phosphate-atom positions before the residue closes.
    site_geometry: dict[tuple, dict[str, tuple[float, float, float]]] = {}
    with in_path.open() as fin:
        for line in fin:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            resname = line[17:20].strip()
            chain = line[21:22].strip()
            try:
                resnum = int(line[22:26].strip())
            except ValueError:
                continue
            key = (chain, resnum)
            if key not in site_map:
                continue
            spec = _PHOSPHO_TARGETS.get(site_map[key])
            if spec is None:
                continue
            atom_name = line[12:16].strip()
            if atom_name not in (spec["parent_c"], spec["ester_o"]):
                continue
            try:
                xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError:
                continue
            entry = site_geometry.setdefault(key, {})
            if atom_name == spec["parent_c"]:
                entry["parent_c"] = xyz
            else:
                entry["ester_o"] = xyz

    seen: dict[tuple, str] = {}
    mismatch: list[dict] = []
    last_serial = 0
    pending_phospho_lines: list[str] = []
    current_residue_key: Optional[tuple] = None

    def _emit_phospho_atoms(
        key: tuple, target: str, last_template_line: str
    ) -> list[str]:
        """Build P / O1P / O2P / O3P / HOP2 / HOP3 ATOM records.

        Pablo's CCD ships SEP / TPO / PTR in the protonated form; we
        emit ``HOP2`` and ``HOP3`` (placed by ``_compute_phospho_atom_coords``)
        so Pablo's residue match succeeds. ``build_amber_system`` strips
        these protons after Pablo loads so the dianion phosaa templates
        used by ``protein.ff*.xml`` apply.
        """
        nonlocal last_serial
        geom = site_geometry.get(key, {})
        parent_c = geom.get("parent_c")
        ester_o = geom.get("ester_o")
        if not parent_c or not ester_o:
            return []
        coords = _compute_phospho_atom_coords(parent_c, ester_o)
        chain_field = last_template_line[21:22]
        resnum_field = last_template_line[22:26]
        icode_field = last_template_line[26:27]
        out_lines: list[str] = []
        for atom_name in ("P", "O1P", "O2P", "O3P", "HOP2", "HOP3"):
            x, y, z = coords[atom_name]
            element = "H" if atom_name.startswith("H") else atom_name[0]
            atom_field = f"{atom_name:>4}" if len(atom_name) < 4 else atom_name[:4]
            last_serial += 1
            out_lines.append(
                f"ATOM  {last_serial:>5} {atom_field} {target:>3} {chain_field}"
                f"{resnum_field}{icode_field}   "
                f"{x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00  0.00          {element:>2}\n"
            )
        return out_lines

    with in_path.open() as fin, out_path.open("w") as fout:
        for line in fin:
            if line.startswith(("ATOM  ", "HETATM")):
                resname = line[17:20].strip()
                chain = line[21:22].strip()
                resnum_field = line[22:26].strip()
                try:
                    resnum = int(resnum_field)
                except ValueError:
                    if pending_phospho_lines:
                        for pl in pending_phospho_lines:
                            fout.write(pl)
                        pending_phospho_lines = []
                    fout.write(line)
                    continue
                try:
                    last_serial = max(last_serial, int(line[6:11].strip()))
                except ValueError:
                    pass
                atom_name = line[12:16].strip()
                key = (chain, resnum)

                if current_residue_key is not None and current_residue_key != key:
                    for pl in pending_phospho_lines:
                        fout.write(pl)
                    pending_phospho_lines = []
                current_residue_key = key

                if key in site_map:
                    target = site_map[key]
                    spec = _PHOSPHO_TARGETS.get(target)
                    if spec is None:
                        fout.write(line)
                        continue
                    expected_source = spec["source"]
                    if resname != expected_source:
                        if key not in seen:
                            mismatch.append({
                                "chain": chain,
                                "resnum": resnum,
                                "expected": expected_source,
                                "actual": resname,
                                "target": target,
                            })
                            seen[key] = "mismatch"
                        fout.write(line)
                        continue
                    if seen.get(key) != target:
                        seen[key] = target
                        # Queue phospho atoms to flush right after the
                        # last source atom — keeps the residue contiguous
                        # so PDBFile / Pablo treat them as one residue.
                        pending_phospho_lines = _emit_phospho_atoms(key, target, line)
                    if atom_name == spec["hydroxyl_h"]:
                        # Drop the original hydroxyl hydrogen — Amber's
                        # phosaa XMLs assume the dianion form (no H on the
                        # phosphate oxygens). The phosphate atoms we
                        # synthesised replace it.
                        continue
                    new_line = line[:17] + f"{target:>3}" + line[20:]
                    fout.write(new_line)
                    continue
            else:
                if pending_phospho_lines:
                    for pl in pending_phospho_lines:
                        fout.write(pl)
                    pending_phospho_lines = []
                current_residue_key = None
            fout.write(line)
        if pending_phospho_lines:
            for pl in pending_phospho_lines:
                fout.write(pl)

    applied = [
        {
            "chain": chain,
            "resnum": resnum,
            "target": target,
            "source": _PHOSPHO_TARGETS[target]["source"],
        }
        for (chain, resnum), target in seen.items()
        if target != "mismatch"
    ]
    not_found = [
        {"chain": chain, "resnum": resnum, "target": tgt}
        for (chain, resnum), tgt in site_map.items()
        if (chain, resnum) not in seen
    ]
    return {"applied": applied, "mismatch": mismatch, "not_found": not_found}


def phosphorylate_residues(
    pdb_file: Optional[str] = None,
    sites: Optional[List[Dict[str, Any]]] = None,
    sites_str: Optional[str] = None,
    restore_from_detection: bool = False,
    allow_partial: bool = False,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Apply phosphorylation (SER→SEP / THR→TPO / TYR→PTR) to a *cleaned* PDB.

    Phosphorylation is a post-prep transformation that runs on a branched
    ``prep`` node (parallels ``create_mutated_structure``). The DAG shape::

        source_001 → prep_001 (prepare_complex) → prep_002 (this tool)
                                                 → solv_001 → ...

    Three input modes (mutually exclusive):

    - ``restore_from_detection=True`` — reads
      ``metadata.detected_ptm_residues`` from the nearest prep ancestor
      (recorded by ``prepare_complex``) and re-introduces the same set of
      sites. Use when the source PDB carried PTMs and you want them back
      after PDBFixer's standard nonstandard-residue replacement.
    - ``sites=[{"chain":"A","resnum":65,"target":"SEP"}, ...]`` — explicit list.
    - ``sites_str="A:65:SEP,A:178:TPO"`` — CLI sugar for the same.

    Each site's *current* residue (in ``merged_pdb``) must be the standard
    counterpart of the requested target (``SEP`` requires ``SER`` etc.).
    The tool renames the residue and strips the hydroxyl hydrogen
    (``HG`` / ``HG1`` / ``HH``); ``OG`` / ``OG1`` / ``OH`` is kept as the
    phosphate linkage atom. ``build_amber_system`` then routes the matching
    openmmforcefields phosaa XML — ``amber/phosaa19SB.xml`` (ff19SB),
    ``amber/phosaa14SB.xml`` (ff14SB), ``amber/phosaa10.xml`` (ff03 /
    ff99SB legacy), ``amber/phosfb18.xml`` (fb15) — into the
    ``SystemGenerator`` ForceField bundle so the phosphate atoms get
    rebuilt by the OpenMM ForceField residue template (no tleap
    source step is involved).

    Args:
        pdb_file: Cleaned PDB (output of ``prepare_complex``). Required
                  unless running in node mode with a resolvable prep ancestor.
        sites: Explicit site list. See docstring head.
        sites_str: CLI sugar. See docstring head.
        restore_from_detection: Use sites recorded by ``prepare_complex``.
        allow_partial: When ``False`` (the default), any requested site that
                  is not located in the input PDB makes the call fail. This
                  catches typos in ``--sites-str`` and chain-remap drift in
                  ``--restore-from-detection``. Set ``True`` only if you
                  knowingly want to apply whichever subset is present.
        name: Optional name prefix for output files (e.g. "p_a65_a178").
        output_dir: Output directory (ignored in node mode).
        job_dir: DAG job directory (node mode).
        node_id: Node ID; expected ``node_type=prep`` with a prep parent.

    Returns:
        Dict with success / output_path / applied_sites / errors / warnings.
    """
    result = {
        "success": False,
        "output_dir": None,
        "output_path": None,
        "applied_sites": [],
        "errors": [],
        "warnings": [],
    }

    # Mutual exclusivity check
    explicit_modes = sum(
        1 for v in (sites, sites_str, restore_from_detection) if v
    )
    if explicit_modes != 1:
        result["errors"].append(
            "Provide exactly one of: --sites (JSON list), --sites-str "
            "('CHAIN:RESNUM:TARGET,...'), or --restore-from-detection."
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="phosphorylate_residues site mode invalid",
            )
        return result

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "prep",
            actual_conditions={
                "restore_from_detection": restore_from_detection,
                "explicit_sites": bool(sites or sites_str),
                "name": name,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="phosphorylate_residues node execution context invalid",
            )

    # Resolve site list
    resolved_sites: list[dict] = []
    if restore_from_detection:
        if not (job_dir and node_id):
            result["errors"].append(
                "--restore-from-detection requires --job-dir and --node-id."
            )
            return result
        from mdclaw._node import find_ancestor_metadata
        detected = find_ancestor_metadata(
            job_dir, node_id, "prep", "detected_ptm_residues"
        )
        if not detected:
            result["errors"].append(
                "No detected_ptm_residues metadata on any prep ancestor. "
                "Was prepare_complex run on a structure with SEP/TPO/PTR?"
            )
            return result
        for d in detected:
            resolved_sites.append({
                "chain": d["chain"],
                "resnum": int(d["resnum"]),
                "target": d["name"],
            })
    elif sites_str:
        try:
            resolved_sites = _parse_sites_str(sites_str)
        except ValueError as e:
            result["errors"].append(str(e))
            return result
    else:
        for s in sites or []:
            try:
                resolved_sites.append({
                    "chain": s["chain"],
                    "resnum": int(s["resnum"]),
                    "target": s.get("target", s.get("name", "")).upper(),
                })
            except (KeyError, TypeError, ValueError) as e:
                result["errors"].append(
                    f"Invalid site entry {s!r}: {type(e).__name__}: {e}"
                )
                return result

    if not resolved_sites:
        result["errors"].append("Resolved site list is empty.")
        return result

    invalid = [s for s in resolved_sites if s["target"] not in _PHOSPHO_TARGETS]
    if invalid:
        result["errors"].append(
            f"Unsupported target residue(s): {invalid}. "
            f"Supported: {sorted(_PHOSPHO_TARGETS)}."
        )
        return result

    # Auto-resolve input from nearest prep ancestor
    if job_dir and node_id and not pdb_file:
        from mdclaw._node import find_ancestor_artifact
        v = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb")
        if v:
            pdb_file = v

    if not pdb_file:
        result["errors"].append(
            "pdb_file is required (or pass --job-dir/--node-id with a prep "
            "ancestor that provides a merged_pdb artifact)."
        )
        return result

    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.is_file():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        return result

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node
        base_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = create_unique_subdir(WORKING_DIR, "phospho")
    ensure_directory(base_dir)

    pref = f"{name}_" if name else ""
    output_path = (base_dir / f"{pref}phosphorylated.pdb").resolve()

    edit_result = _apply_phosphorylation_to_pdb(
        pdb_path, output_path, resolved_sites
    )

    if edit_result["mismatch"]:
        result["errors"].append(
            "Residue/target mismatch — refusing to write a partial result. "
            f"Details: {edit_result['mismatch']}"
        )
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        if _node_mode:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    if edit_result["not_found"]:
        message = (
            "The following sites were not located in the input PDB: "
            f"{edit_result['not_found']}. Common causes: a typo in "
            "--sites-str, or a chain-id mismatch between the cleaned merged "
            "PDB and the detection list (re-run prepare_complex if its "
            "chain remapping was missing)."
        )
        if allow_partial:
            result["warnings"].append(message + " Proceeding because allow_partial=True.")
        else:
            result["errors"].append(
                message + " Pass --allow-partial to apply the rest anyway."
            )
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=result["errors"])
            return result

    if not edit_result["applied"]:
        result["errors"].append(
            "No sites were applied (input PDB did not contain any of the "
            "requested residues)."
        )
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        if _node_mode:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    result["success"] = True
    result["output_dir"] = str(base_dir)
    result["output_path"] = str(output_path)
    result["applied_sites"] = edit_result["applied"]
    logger.info(
        "Phosphorylation applied: %s sites -> %s",
        len(edit_result["applied"]),
        output_path,
    )

    if _node_mode:
        from mdclaw._node import complete_node
        rel_out = f"artifacts/{output_path.name}"
        ptm_residues_meta = [
            {
                "chain": s["chain"],
                "resnum": s["resnum"],
                "name": s["target"],
                "source": "detected" if restore_from_detection else "introduced",
            }
            for s in edit_result["applied"]
        ]
        complete_node(
            job_dir, node_id,
            artifacts={
                "merged_pdb": rel_out,
                "phosphorylated_pdb": rel_out,
            },
            metadata={
                "name": name,
                "phosphorylation_source_pdb": str(pdb_path),
                "ptm_residues": ptm_residues_meta,
                "restore_from_detection": restore_from_detection,
            },
            warnings=result.get("warnings", []),
        )

    return result


def _source_candidates_from_mapping(residue_mapping: list[dict]) -> list[dict]:
    return [
        {
            "source_chain": m.get("source_chain"),
            "source_label_chain": m.get("source_label_chain"),
            "source_resnum": m.get("source_resnum"),
            "source_icode": m.get("source_icode", ""),
            "source_resname": m.get("source_resname"),
            "merged_chain": m.get("merged_chain"),
            "merged_resnum": m.get("merged_resnum"),
            "merged_resname": m.get("merged_resname"),
        }
        for m in residue_mapping
    ]


def _find_mapped_modxna_target(mod: dict, residue_mapping: list[dict]) -> dict | None:
    chain = str(mod.get("chain", ""))
    resnum = str(mod.get("resnum", ""))
    icode = str(mod.get("icode", mod.get("source_icode", "")) or "")
    source_resname = str(mod.get("source_resname", "")).upper()
    for entry in residue_mapping:
        chain_matches = chain in {
            str(entry.get("source_chain", "")),
            str(entry.get("source_label_chain", "")),
        }
        if not chain_matches:
            continue
        if str(entry.get("source_resnum", "")) != resnum:
            continue
        if str(entry.get("source_icode", "") or "") != icode:
            continue
        if source_resname and str(entry.get("source_resname", "")).upper() != source_resname:
            continue
        return dict(entry)
    return None


def _find_merged_modxna_target(mod: dict, merged_pdb: Path) -> dict | None:
    chain = str(mod.get("chain", ""))
    resnum = str(mod.get("resnum", ""))
    icode = str(mod.get("icode", "") or "")
    source_resname = str(mod.get("source_resname", "")).upper()
    for residue in _read_pdb_unique_residues(merged_pdb):
        if str(residue["chain"]) != chain:
            continue
        if str(residue["resnum"]) != resnum:
            continue
        if str(residue.get("icode", "") or "") != icode:
            continue
        if source_resname and str(residue["resname"]).upper() != source_resname:
            continue
        return {
            "source_chain": chain,
            "source_label_chain": chain,
            "source_resnum": residue["resnum"],
            "source_icode": residue.get("icode", ""),
            "source_resname": residue["resname"],
            "merged_chain": chain,
            "merged_resnum": residue["resnum"],
            "merged_icode": residue.get("icode", ""),
            "merged_resname": residue["resname"],
            "chain_file": None,
        }
    return None


def _merged_residue_candidates(merged_pdb: Path) -> list[dict]:
    return [
        {
            "chain": r["chain"],
            "resnum": r["resnum"],
            "icode": r.get("icode", ""),
            "resname": r["resname"],
        }
        for r in _read_pdb_unique_residues(merged_pdb)
    ]


MODXNA_FRAGMENT_PRESETS: dict[str, dict[str, str]] = {
    # 5-methylcytidine: default non-terminal deoxy-cytidine backbone used by
    # the existing 6JV5 integration path. Unknown modifications still require
    # explicit user-provided fragment IDs.
    "5CM": {"backbone": "DPO", "sugar": "DC2", "base": "M5C"},
}


def _apply_modxna_fragment_preset(mod: dict) -> tuple[dict, dict | None]:
    updated = dict(mod)
    missing = [field for field in ("backbone", "sugar", "base") if not updated.get(field)]
    if not missing:
        return updated, None
    source_resname = str(updated.get("source_resname") or updated.get("resname") or "").upper()
    preset = MODXNA_FRAGMENT_PRESETS.get(source_resname)
    if not preset:
        return updated, None
    for field in missing:
        updated[field] = preset[field]
    return updated, {
        "source_resname": source_resname,
        "fragments": dict(preset),
        "filled_fields": missing,
    }


def _read_modxna_library_residue_name(lib_path: Path) -> str:
    """Read the LEaP residue code from a modXNA library, falling back to stem."""
    text = lib_path.read_text(encoding="utf-8", errors="ignore")
    quoted = re.findall(r'"([A-Za-z0-9]{1,4})"', text)
    if quoted:
        return quoted[0].upper()[:3]
    return lib_path.stem.upper()[:3]


def _terminal_modxna_targets(merged_pdb: Path, resolved_targets: list[dict]) -> list[dict]:
    residues_by_chain: dict[str, list[dict]] = {}
    for residue in _read_pdb_unique_residues(merged_pdb):
        residues_by_chain.setdefault(str(residue["chain"]), []).append(residue)

    terminal = []
    for target in resolved_targets:
        chain = str(target["merged_chain"])
        residues = residues_by_chain.get(chain, [])
        if len(residues) < 3:
            terminal_target = dict(target)
            terminal_target["terminal_position"] = "short_chain"
            terminal_target["chain_residue_count"] = len(residues)
            terminal.append(terminal_target)
            continue
        first = residues[0]
        last = residues[-1]
        key = (str(target["merged_resnum"]), str(target.get("merged_icode", "") or ""))
        first_key = (str(first["resnum"]), str(first.get("icode", "") or ""))
        last_key = (str(last["resnum"]), str(last.get("icode", "") or ""))
        if key in {first_key, last_key}:
            terminal_target = dict(target)
            terminal_target["terminal_position"] = "5prime" if key == first_key else "3prime"
            terminal_target["chain_residue_count"] = len(residues)
            terminal.append(terminal_target)
    return terminal


def _rename_pdb_residues(
    input_pdb: Path,
    output_pdb: Path,
    rename_map: dict[tuple[str, str, str], str],
) -> dict:
    atom_count = 0
    residue_keys = set()
    renamed = 0
    out_lines = []
    for line in input_pdb.read_text().splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 27:
            atom_count += 1
            chain = line[21].strip() or "A"
            resnum = line[22:26].strip()
            icode = line[26].strip()
            residue_keys.add((chain, resnum, icode))
            new_name = rename_map.get((chain, resnum, icode))
            if new_name:
                line = line[:17] + new_name.rjust(3)[:3] + line[20:]
                renamed += 1
        out_lines.append(line)
    output_pdb.write_text("\n".join(out_lines) + "\n")
    return {
        "atom_count": atom_count,
        "residue_count": len(residue_keys),
        "renamed_atom_count": renamed,
    }


def prepare_modified_nucleic(
    modifications: Optional[List[Dict[str, Any]]] = None,
    modxna_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Legacy modXNA branch.

    This can generate modXNA files, but the standard MDClaw OpenMM topology
    path does not consume them as MD-ready parameters.
    """
    result = {
        "success": False,
        "errors": [],
        "warnings": [
            MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE
            + " prepare_modified_nucleic is a legacy/experimental helper and "
            "does not make modified DNA/RNA supported by the standard topology path."
        ],
        "modxna_params": [],
        "resolved_modifications": [],
        "modified_nucleic_support": modified_nucleic_support_report([{"source": "user_requested"}]),
    }

    if not (job_dir and node_id):
        return {
            **result,
            "error_type": "ValidationError",
            "code": "node_mode_required",
            "errors": ["prepare_modified_nucleic requires job_dir and node_id."],
        }
    if not modifications:
        return {
            **result,
            "error_type": "ValidationError",
            "code": "modxna_modifications_required",
            "errors": ["modifications must be a non-empty list."],
        }

    from mdclaw._node import (
        begin_node,
        complete_node,
        fail_node,
        find_ancestor_artifact,
        validate_node_execution_context,
    )

    ctx = validate_node_execution_context(
        job_dir,
        node_id,
        "prep",
        actual_conditions={"modifications": modifications},
    )
    if not ctx["success"]:
        blocked = {"success": False, "error_type": "ValidationError", **ctx}
        from mdclaw._node import fail_node_from_result
        return fail_node_from_result(
            job_dir,
            node_id,
            blocked,
            default_error="prepare_modified_nucleic node execution context invalid",
        )

    merged_pdb = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb")
    residue_mapping_path = find_ancestor_artifact(job_dir, node_id, "prep", "residue_mapping")
    if not merged_pdb:
        result.update({
            "error_type": "ValidationError",
            "code": "modxna_missing_parent_merged_pdb",
            "errors": ["No merged_pdb artifact found on a completed prep ancestor."],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result
    if not residue_mapping_path:
        result.update({
            "error_type": "ValidationError",
            "code": "modxna_missing_residue_mapping",
            "errors": ["No residue_mapping artifact found on a completed prep ancestor."],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result

    merged_pdb_path = Path(merged_pdb).resolve()
    try:
        residue_mapping = json.loads(Path(residue_mapping_path).read_text())
    except (json.JSONDecodeError, OSError) as e:
        result.update({
            "error_type": "ValidationError",
            "code": "modxna_missing_residue_mapping",
            "errors": [f"Could not read residue_mapping artifact: {e}"],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result

    resolved_targets = []
    for mod in modifications:
        mod, preset_info = _apply_modxna_fragment_preset(mod)
        frame = str(mod.get("coordinate_frame", "source")).lower()
        if frame == "source":
            target = _find_mapped_modxna_target(mod, residue_mapping)
            if not target:
                result.update({
                    "error_type": "ValidationError",
                    "code": "modxna_target_residue_not_found",
                    "source_candidates": _source_candidates_from_mapping(residue_mapping),
                })
                result["errors"].append(f"Requested source residue not found in residue_mapping: {mod}")
                fail_node(job_dir, node_id, errors=result["errors"])
                return result
        elif frame == "merged":
            target = _find_merged_modxna_target(mod, merged_pdb_path)
            if not target:
                result.update({
                    "error_type": "ValidationError",
                    "code": "modxna_residue_mapping_stale",
                    "merged_candidates": _merged_residue_candidates(merged_pdb_path),
                })
                result["errors"].append(f"Requested merged residue not found in merged_pdb: {mod}")
                fail_node(job_dir, node_id, errors=result["errors"])
                return result
        else:
            result.update({
                "error_type": "ValidationError",
                "code": "invalid_coordinate_frame",
                "errors": [f"coordinate_frame must be 'source' or 'merged': {frame}"],
            })
            fail_node(job_dir, node_id, errors=result["errors"])
            return result

        for field in ("backbone", "sugar", "base"):
            if not mod.get(field):
                result.update({
                    "error_type": "ValidationError",
                    "code": "invalid_modxna_fragment_spec",
                    "errors": [f"modification is missing required fragment field '{field}': {mod}"],
                    "required_fields": ["backbone", "sugar", "base"],
                    "known_presets": sorted(MODXNA_FRAGMENT_PRESETS),
                })
                fail_node(job_dir, node_id, errors=result["errors"])
                return result
        target["fragments"] = {
            "backbone": str(mod["backbone"]),
            "sugar": str(mod["sugar"]),
            "base": str(mod["base"]),
        }
        target["coordinate_frame"] = frame
        if preset_info:
            target["fragment_preset"] = preset_info
        resolved_targets.append(target)

    stale = []
    merged_residue_keys = {
        (str(r["chain"]), str(r["resnum"]), str(r.get("icode", "") or ""), str(r["resname"]).upper())
        for r in _read_pdb_unique_residues(merged_pdb_path)
    }
    for target in resolved_targets:
        key = (
            str(target["merged_chain"]),
            str(target["merged_resnum"]),
            str(target.get("merged_icode", "") or ""),
            str(target["merged_resname"]).upper(),
        )
        if key not in merged_residue_keys:
            stale.append(target)
    if stale:
        result.update({
            "error_type": "ValidationError",
            "code": "modxna_residue_mapping_stale",
            "merged_candidates": _merged_residue_candidates(merged_pdb_path),
            "errors": [f"Resolved residue(s) are missing from merged_pdb: {stale}"],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result

    terminal_targets = _terminal_modxna_targets(merged_pdb_path, resolved_targets)
    if terminal_targets:
        result.update({
            "error_type": "ValidationError",
            "code": "modxna_terminal_residue_unsupported",
            "errors": [f"Terminal modified nucleic residues are not supported yet: {terminal_targets}"],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result

    modxna_root = Path(modxna_dir or os.environ.get("MDCLAW_MODXNA_DIR", "")).expanduser()
    modxna_sh = modxna_root / "modxna.sh"
    modxna_frcmod = modxna_root / "dat" / "frcmod.modxna"
    if not modxna_root or not modxna_sh.is_file() or not modxna_frcmod.is_file():
        result.update({
            "error_type": "ToolUnavailableError",
            "code": "modxna_tool_unavailable",
            "errors": [
                "modxna.sh and dat/frcmod.modxna are required. "
                "Pass modxna_dir or set MDCLAW_MODXNA_DIR."
            ],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result

    out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    begin_node(job_dir, node_id)

    in_modxna = out_dir / "in.modxna"
    in_lines = ["# Generated by MDClaw prepare_modified_nucleic"]
    unique_fragment_keys: list[tuple[str, str, str]] = []
    fragment_libs: dict[tuple[str, str, str], dict] = {}
    for target in resolved_targets:
        fragments = target["fragments"]
        key = (fragments["backbone"], fragments["sugar"], fragments["base"])
        in_lines.append(" ".join(key))
        if key not in unique_fragment_keys:
            unique_fragment_keys.append(key)
    in_modxna.write_text("\n".join(in_lines) + "\n")

    stdout_parts = []
    stderr_parts = []
    for index, key in enumerate(unique_fragment_keys, start=1):
        run_dir = out_dir / f"modxna_{index:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_input = run_dir / "in.modxna"
        run_input.write_text(
            "# Generated by MDClaw prepare_modified_nucleic\n"
            + " ".join(key)
            + "\n",
            encoding="utf-8",
        )
        before_libs = set(run_dir.glob("*.lib")) | set(run_dir.glob("*.off"))
        try:
            completed = subprocess.run(
                [str(modxna_sh), "-i", str(run_input)],
                cwd=str(run_dir),
                check=False,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            result.update({
                "error_type": "ToolUnavailableError",
                "code": "modxna_tool_unavailable",
                "errors": [f"modXNA execution failed: {type(e).__name__}: {e}"],
            })
            fail_node(job_dir, node_id, errors=result["errors"])
            return result

        stdout_parts.append(completed.stdout)
        stderr_parts.append(completed.stderr)
        if completed.returncode != 0:
            result.update({
                "error_type": "ToolExecutionError",
                "code": "modxna_execution_failed",
                "errors": [f"modXNA exited with code {completed.returncode}", completed.stderr.strip()],
            })
            fail_node(job_dir, node_id, errors=result["errors"])
            return result

        generated_libs = sorted((set(run_dir.glob("*.lib")) | set(run_dir.glob("*.off"))) - before_libs)
        if len(generated_libs) != 1:
            result.update({
                "error_type": "ValidationError",
                "code": "invalid_modxna_parameters",
                "generated_libraries": [str(path) for path in generated_libs],
                "errors": [
                    "modXNA must generate exactly one library file per unique "
                    f"fragment specification {key}; generated {len(generated_libs)}."
                ],
            })
            fail_node(job_dir, node_id, errors=result["errors"])
            return result
        lib_path = generated_libs[0]
        fragment_libs[key] = {
            "lib": lib_path,
            "residue_name": _read_modxna_library_residue_name(lib_path),
            "in_modxna": run_input,
        }

    result["modxna_stdout"] = "".join(stdout_parts)
    result["modxna_stderr"] = "".join(stderr_parts)

    local_frcmod = out_dir / "frcmod.modxna"
    shutil.copy2(modxna_frcmod, local_frcmod)

    rename_map = {}
    modxna_params = []
    updated_mapping = [dict(m) for m in residue_mapping]
    seen_param_keys = set()
    for target in resolved_targets:
        fragments = target["fragments"]
        fragment_key = (fragments["backbone"], fragments["sugar"], fragments["base"])
        lib_record = fragment_libs[fragment_key]
        lib_path = lib_record["lib"]
        residue_name = lib_record["residue_name"]
        chain = str(target["merged_chain"])
        resnum = str(target["merged_resnum"])
        icode = str(target.get("merged_icode", "") or "")
        rename_map[(chain, resnum, icode)] = residue_name
        target = dict(target)
        target["modxna_residue_name"] = residue_name
        target["modxna_library"] = str(lib_path.resolve())
        result["resolved_modifications"].append(target)
        param_key = (residue_name, str(lib_path.resolve()), str(local_frcmod.resolve()))
        if param_key not in seen_param_keys:
            seen_param_keys.add(param_key)
            modxna_params.append({
                "residue_name": residue_name,
                "lib": str(lib_path.resolve()),
                "frcmod": str(local_frcmod.resolve()),
                "source_resname": target.get("source_resname"),
                "chain": target.get("source_chain"),
                "resnum": target.get("source_resnum"),
                "merged_chain": chain,
                "merged_resnum": target.get("merged_resnum"),
                "backbone": fragments["backbone"],
                "sugar": fragments["sugar"],
                "base": fragments["base"],
                "target_count": sum(
                    1 for other in resolved_targets
                    if (
                        other["fragments"]["backbone"],
                        other["fragments"]["sugar"],
                        other["fragments"]["base"],
                    ) == fragment_key
                ),
            })
        for entry in updated_mapping:
            if (
                str(entry.get("merged_chain")) == chain
                and str(entry.get("merged_resnum")) == resnum
                and str(entry.get("merged_icode", "") or "") == icode
            ):
                entry["merged_resname"] = residue_name
                entry["modxna_residue_name"] = residue_name

    output_pdb = out_dir / "modified_nucleic.pdb"
    before_counts = {
        "atom_count": sum(1 for line in merged_pdb_path.read_text().splitlines() if line.startswith(("ATOM", "HETATM"))),
        "residue_count": len(_read_pdb_unique_residues(merged_pdb_path)),
    }
    rename_stats = _rename_pdb_residues(merged_pdb_path, output_pdb, rename_map)
    after_counts = {
        "atom_count": sum(1 for line in output_pdb.read_text().splitlines() if line.startswith(("ATOM", "HETATM"))),
        "residue_count": len(_read_pdb_unique_residues(output_pdb)),
    }
    if before_counts != after_counts:
        result.update({
            "error_type": "ValidationError",
            "code": "modxna_pdb_rename_changed_structure",
            "errors": [f"Residue rename changed atom/residue counts: before={before_counts}, after={after_counts}"],
        })
        fail_node(job_dir, node_id, errors=result["errors"])
        return result

    params_json = out_dir / "modxna_params.json"
    params_json.write_text(json.dumps(modxna_params, indent=2), encoding="utf-8")
    mapping_json = out_dir / "residue_mapping.json"
    mapping_json.write_text(json.dumps(updated_mapping, indent=2), encoding="utf-8")

    result.update({
        "success": True,
        "merged_pdb": str(output_pdb),
        "modxna_params": modxna_params,
        "residue_mapping": str(mapping_json),
        "in_modxna": str(in_modxna),
        "rename_stats": rename_stats,
    })
    complete_node(
        job_dir,
        node_id,
        artifacts={
            "merged_pdb": "artifacts/modified_nucleic.pdb",
            "modxna_params": "artifacts/modxna_params.json",
            "residue_mapping": "artifacts/residue_mapping.json",
        },
        metadata={
            "has_modified_nucleic": True,
            "modxna_residue_names": [p["residue_name"] for p in modxna_params],
            "modxna_modifications": result["resolved_modifications"],
        },
        warnings=result["warnings"],
    )
    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "split_molecules": split_molecules,
    "clean_protein": clean_protein,
    "clean_ligand": clean_ligand,
    "merge_structures": merge_structures,
    "prepare_complex": prepare_complex,
    "create_mutated_structure": create_mutated_structure,
    "phosphorylate_residues": phosphorylate_residues,
    "prepare_modified_nucleic": prepare_modified_nucleic,
}
