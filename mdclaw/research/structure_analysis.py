"""
Research Server - External database retrieval and structure inspection tools.

This server integrates with external MCP servers (PDB-MCP-Server, AlphaFold-MCP-Server,
UniProt-MCP-Server) from Augmented-Nature by implementing the same REST API calls.

Provides tools for:
- PDB structure retrieval and search (mirrors PDB-MCP-Server)
- AlphaFold structure retrieval (mirrors AlphaFold-MCP-Server)
- UniProt protein search and info (mirrors UniProt-MCP-Server)
- Structure file inspection (mdclaw-specific gemmi-based analysis)
"""

import os
import re
import sys
from pathlib import Path


# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import (  # noqa: E402
    ensure_directory,
    setup_logger,
)

from mdclaw.chemistry_constants import (  # noqa: E402
    COMMON_IONS,
    GAFF_SUPPORTED_ELEMENTS,
    METAL_ELEMENTS,
    PROTEIN_RESNAMES,
    WATER_NAMES,
)

logger = setup_logger(__name__)

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)


def _detect_disulfide_candidates(structure_path: Path) -> list[dict]:
    """Detect potential disulfide bonds by measuring CYS-CYS S-S distances.

    This is a read-only analysis that doesn't modify the structure.
    """
    try:
        import gemmi
    except ImportError:
        return []

    candidates = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        # Find all CYS residues with SG atoms
        cys_residues = []
        for chain in model:
            for res in chain:
                if res.name in ("CYS", "CYX"):
                    sg_atom = res.find_atom("SG", "*")
                    if sg_atom:
                        cys_residues.append({
                            "chain": chain.name,
                            "resnum": res.seqid.num,
                            "resname": res.name,
                            "sg_pos": sg_atom.pos,
                        })

        # Check all pairs for S-S distance
        for i, cys1 in enumerate(cys_residues):
            for cys2 in cys_residues[i + 1:]:
                # Calculate S-S distance
                dx = cys1["sg_pos"].x - cys2["sg_pos"].x
                dy = cys1["sg_pos"].y - cys2["sg_pos"].y
                dz = cys1["sg_pos"].z - cys2["sg_pos"].z
                distance = (dx * dx + dy * dy + dz * dz) ** 0.5

                # Typical S-S distance is ~2.03Å, consider up to 3.0Å as candidates
                if distance < 3.0:
                    confidence = "high" if distance < 2.5 else "medium"
                    candidates.append({
                        "cys1": {
                            "chain": cys1["chain"],
                            "resnum": cys1["resnum"],
                            "resname": cys1["resname"],
                        },
                        "cys2": {
                            "chain": cys2["chain"],
                            "resnum": cys2["resnum"],
                            "resname": cys2["resname"],
                        },
                        "distance_angstrom": round(distance, 2),
                        "confidence": confidence,
                        "recommendation": "form_bond" if confidence == "high" else "review",
                        "source": "distance",
                    })
    except Exception as e:
        logger.warning(f"Error detecting disulfide candidates: {e}")

    return candidates


def _parse_ssbond_records(structure_path: Path) -> list[dict]:
    """Parse explicit disulfide bond records from PDB SSBOND or mmCIF _struct_conn.

    Uses gemmi's unified ``Structure.connections`` which exposes both PDB
    SSBOND lines and mmCIF ``_struct_conn`` entries with
    ``conn_type_id="disulf"``. The returned entries use the same schema as
    ``_detect_disulfide_candidates`` so the two sources can be merged
    downstream, with the additional field ``source="pdb_ssbond"``.

    The ``distance_angstrom`` is recomputed from the actual SG atom
    coordinates — the SSBOND ``Length`` column (74-78) is optional and
    only meaningful when both symmetry operators are 1555, so the
    measured value is preferred.
    """
    try:
        import gemmi
    except ImportError:
        return []

    out: list[dict] = []
    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        if len(st) == 0:
            return []
        model = st[0]

        def _find_sg_atom(addr):
            """Locate the SG atom described by a gemmi AtomAddress, if any."""
            try:
                chain = model.find_chain(addr.chain_name)
                if chain is None:
                    return None
                # Prefer exact seqid match; fallback to iterating residues.
                for res in chain:
                    if res.seqid.num == addr.res_id.seqid.num and res.name == addr.res_id.name:
                        return res.find_atom(addr.atom_name or "SG", "*")
                return None
            except Exception:
                return None

        for conn in st.connections:
            if conn.type != gemmi.ConnectionType.Disulf:
                continue
            p1, p2 = conn.partner1, conn.partner2
            entry = {
                "cys1": {
                    "chain": p1.chain_name,
                    "resnum": p1.res_id.seqid.num,
                    "resname": p1.res_id.name,
                },
                "cys2": {
                    "chain": p2.chain_name,
                    "resnum": p2.res_id.seqid.num,
                    "resname": p2.res_id.name,
                },
                "distance_angstrom": None,
                "confidence": "high",
                "recommendation": "form_bond",
                "source": "pdb_ssbond",
            }

            a1 = _find_sg_atom(p1)
            a2 = _find_sg_atom(p2)
            if a1 is not None and a2 is not None:
                dx = a1.pos.x - a2.pos.x
                dy = a1.pos.y - a2.pos.y
                dz = a1.pos.z - a2.pos.z
                entry["distance_angstrom"] = round((dx * dx + dy * dy + dz * dz) ** 0.5, 2)

            out.append(entry)
    except Exception as e:
        logger.warning(f"Error parsing SSBOND records: {e}")

    return out


def _find_histidines(structure_path: Path) -> list[dict]:
    """Find all histidine residues in the structure."""
    try:
        import gemmi
    except ImportError:
        return []

    histidines = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        for chain in model:
            for res in chain:
                if res.name in ("HIS", "HID", "HIE", "HIP"):
                    histidines.append({
                        "chain": chain.name,
                        "resnum": res.seqid.num,
                        "current_name": res.name,
                    })
    except Exception as e:
        logger.warning(f"Error finding histidines: {e}")

    return histidines


def _estimate_histidine_pka(pdb_file: Path, histidines: list[dict], ph: float = 7.4) -> list[dict]:
    """Estimate pKa values for histidines using propka.

    Returns histidine analysis with recommended protonation states.
    """
    results = []
    pka_values = {}

    # Try to run propka for pKa estimation
    try:
        import propka.run as propka_run
        import io
        import sys

        # propka writes to stdout and stderr, capture/suppress them
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        try:
            # write_pka=False to avoid writing .pka file
            mol = propka_run.single(str(pdb_file), write_pka=False)

            # Extract HIS pKa values from conformations
            # propka API: mol.conformations is a dict of ConformationContainer objects
            if mol and hasattr(mol, "conformations") and mol.conformations:
                # Use first conformation (usually "1A" or main chain)
                for conf_name, conformation in mol.conformations.items():
                    if conf_name == "AVR":  # Skip average conformation
                        continue
                    for group in conformation.groups:
                        # Check if this is a HIS group
                        if hasattr(group, "residue_type") and group.residue_type == "HIS":
                            # Access chain_id and res_num via group.atom
                            if hasattr(group, "atom") and group.atom:
                                chain_id = getattr(group.atom, "chain_id", "")
                                res_num = getattr(group.atom, "res_num", 0)
                                pka_value = getattr(group, "pka_value", None)
                                if chain_id and res_num and pka_value is not None:
                                    key = f"{chain_id}:{res_num}"
                                    pka_values[key] = pka_value
                    break  # Only process first valid conformation
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    except ImportError:
        logger.info("propka not available, using default histidine assignments")
    except Exception as e:
        logger.warning(f"propka error: {e}")

    # Build results with pKa-based recommendations
    for his in histidines:
        key = f"{his['chain']}:{his['resnum']}"
        pka = pka_values.get(key)

        if pka is not None:
            # Determine protonation state based on pKa vs pH
            if pka < ph - 1.0:
                # Well below pH: neutral, prefer HIE (epsilon-protonated)
                recommended = "HIE"
                reason = f"pKa ({pka:.1f}) < pH ({ph}): neutral, ε-protonated"
            elif pka > ph + 1.0:
                # Well above pH: protonated (positively charged)
                recommended = "HIP"
                reason = f"pKa ({pka:.1f}) > pH ({ph}): positively charged"
            else:
                # Near pH: check environment (default to HIE)
                recommended = "HIE"
                reason = f"pKa ({pka:.1f}) ≈ pH ({ph}): borderline, default to HIE"
        else:
            # No pKa available: use default
            recommended = "HIE"
            reason = "No pKa estimate available, using default HIE"
            pka = None

        results.append({
            "chain": his["chain"],
            "resnum": his["resnum"],
            "current_name": his["current_name"],
            "estimated_pka": round(pka, 1) if pka is not None else None,
            "recommended_state": recommended,
            "reason": reason,
            "alternatives": ["HID", "HIE", "HIP"],
        })

    return results


def _find_missing_residues(pdb_file: Path) -> tuple[list[dict], list[dict]]:
    """Find missing residues and atoms using PDBFixer (read-only).

    Returns (missing_residues, missing_atoms)
    """
    missing_residues = []
    missing_atoms = []

    try:
        from pdbfixer import PDBFixer

        fixer = PDBFixer(filename=str(pdb_file))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()

        # Process missing residues
        chains = list(fixer.topology.chains())
        for (chain_idx, res_idx), residue_names in fixer.missingResidues.items():
            chain = chains[chain_idx]
            chain_length = len(list(chain.residues()))

            # Determine location
            if res_idx == 0:
                location = "N-terminal"
                recommendation = "ignore"
                reason = "Terminal missing residues are common in crystal structures"
            elif res_idx >= chain_length:
                location = "C-terminal"
                recommendation = "ignore"
                reason = "Terminal missing residues are common in crystal structures"
            else:
                location = "internal"
                recommendation = "model"
                reason = "Internal missing residues should be modeled for MD"

            missing_residues.append({
                "chain": chain.id,
                "start_resnum": res_idx,
                "end_resnum": res_idx + len(residue_names) - 1,
                "residue_names": residue_names,
                "location": location,
                "recommendation": recommendation,
                "reason": reason,
            })

        # Process missing atoms
        for residue, atoms in fixer.missingAtoms.items():
            missing_atoms.append({
                "chain": residue.chain.id,
                "resnum": residue.index,
                "resname": residue.name,
                "missing_atoms": [atom.name for atom in atoms],
                "recommendation": "add",
                "reason": "Missing atoms will be added by PDBFixer",
            })

    except ImportError:
        logger.warning("PDBFixer not available for missing residue detection")
    except Exception as e:
        logger.warning(f"Error finding missing residues: {e}")

    return missing_residues, missing_atoms


def _find_nonstandard_residues(pdb_file: Path) -> list[dict]:
    """Find non-standard residues using PDBFixer (read-only)."""
    nonstandard = []

    # Common non-standard to standard mappings
    NONSTANDARD_MAP = {
        "MSE": "MET",  # Selenomethionine
        "SEP": "SER",  # Phosphoserine
        "TPO": "THR",  # Phosphothreonine
        "PTR": "TYR",  # Phosphotyrosine
        "HYP": "PRO",  # Hydroxyproline
        "MLY": "LYS",  # N-dimethyl-lysine
        "CSO": "CYS",  # S-hydroxycysteine
    }

    try:
        from pdbfixer import PDBFixer

        fixer = PDBFixer(filename=str(pdb_file))
        fixer.findNonstandardResidues()

        for residue in fixer.nonstandardResidues:
            standard = NONSTANDARD_MAP.get(residue.name)
            nonstandard.append({
                "chain": residue.chain.id,
                "resnum": residue.index,
                "resname": residue.name,
                "standard_equivalent": standard,
                "recommendation": "replace" if standard else "review",
                "reason": f"{residue.name} → {standard}" if standard else "Unknown modification",
            })

    except ImportError:
        logger.warning("PDBFixer not available for nonstandard residue detection")
    except Exception as e:
        logger.warning(f"Error finding nonstandard residues: {e}")

    return nonstandard


def _analyze_ligands(structure_path: Path) -> list[dict]:
    """Analyze ligands in the structure and record graph-derived metadata."""
    ligands = []

    try:
        import gemmi
    except ImportError:
        return []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        st.setup_entities()
        model = st[0]

        # Find ligand chains (non-protein, non-water, non-ion)
        for chain in model:
            for res in chain:
                resname = res.name.strip()

                # Skip protein residues (including Amber/protonation variants), water, and ions
                if resname in PROTEIN_RESNAMES:
                    continue
                if resname in WATER_NAMES:
                    continue
                if resname in COMMON_IONS:
                    continue

                # Count atoms and collect element information
                atoms = list(res)
                num_atoms = len(atoms)
                if num_atoms < 3:
                    continue  # Too small to be a meaningful ligand

                # Detect metal/unsupported elements
                ligand_elements = set()
                for atom in atoms:
                    elem = atom.element
                    if elem.name:
                        ligand_elements.add(elem.name)

                unsupported_elements = ligand_elements - GAFF_SUPPORTED_ELEMENTS
                contains_metal = bool(ligand_elements & METAL_ELEMENTS)
                is_gaff_compatible = len(unsupported_elements) == 0

                # Try to get a chemistry graph. Its formal charge is metadata
                # for validation; MDClaw does not pH-enumerate ligand charge
                # during inspection.
                smiles = None
                smiles_source = "not_found"
                net_charge = None

                try:
                    from rdkit import Chem
                    from mdclaw.structure.ligand_chemistry import (
                        KNOWN_LIGAND_SMILES,
                        _fetch_smiles_from_ccd,
                    )

                    known_smiles = KNOWN_LIGAND_SMILES.get(resname.upper())
                    if known_smiles and re.search(r"\[[^\]]*[+-]", known_smiles):
                        smiles = known_smiles
                        smiles_source = "curated_charged_dictionary"
                    else:
                        smiles = _fetch_smiles_from_ccd(resname, timeout=5)
                        if smiles:
                            smiles_source = "ccd"
                        elif known_smiles:
                            smiles = known_smiles
                            smiles_source = "known_dictionary"
                    if smiles:
                        mol = Chem.MolFromSmiles(smiles)
                        if mol is not None:
                            net_charge = int(Chem.GetFormalCharge(mol))
                except Exception:
                    pass

                # Get residue number for unique identification
                resnum = res.seqid.num
                unique_id = f"{chain.name}:{resname}:{resnum}"

                # Build recommendation based on GAFF compatibility
                recommendation = {
                    "include": is_gaff_compatible,  # Auto-exclude if not compatible
                }
                if not is_gaff_compatible:
                    recommendation["warning"] = (
                        f"Contains unsupported elements: {sorted(unsupported_elements)}. "
                        "Cannot build a GAFF ligand template from this chemistry."
                    )

                ligands.append({
                    "chain": chain.name,
                    "resname": resname,
                    "resnum": resnum,
                    "unique_id": unique_id,
                    "num_atoms": num_atoms,
                    "smiles_source": smiles_source,
                    "smiles": smiles,
                    "net_charge": net_charge,
                    "charge_source": "molecule_formal_charge" if net_charge is not None else None,
                    # Metal/element compatibility fields
                    "elements": sorted(ligand_elements),
                    "contains_metal": contains_metal,
                    "is_gaff_compatible": is_gaff_compatible,
                    "unsupported_elements": sorted(unsupported_elements),
                    "recommendation": recommendation,
                })

    except Exception as e:
        logger.warning(f"Error analyzing ligands: {e}")

    return ligands


def analyze_structure_details(
    structure_file: str,
    ph: float = 7.4,
    detect_disulfides: bool = True,
    estimate_protonation: bool = True,
    check_missing: bool = True,
    identify_ligands: bool = True,
) -> dict:
    """Perform detailed structural analysis (read-only, no modifications).

    This tool analyzes a protein structure file and returns detailed information
    about disulfide bonds, histidine protonation states, missing residues, and
    ligands. The results can be presented to the user for review and approval
    before proceeding with structure preparation.

    Use this in Phase 1 (Clarification) to:
    - Detect potential disulfide bonds by CYS-CYS S-S distance
    - Estimate histidine pKa values and recommend protonation states
    - Identify missing residues and atoms
    - Detect non-standard residues
    - Analyze ligands and record SMILES graph formal-charge metadata

    Args:
        structure_file: Path to structure file (PDB or mmCIF)
        ph: Target pH for protonation analysis (default: 7.4)
        detect_disulfides: Whether to detect disulfide bond candidates
        estimate_protonation: Whether to estimate histidine protonation states
        check_missing: Whether to check for missing residues/atoms
        identify_ligands: Whether to analyze ligands

    Returns:
        Dict with:
            - success: bool
            - structure_file: str
            - ph: float
            - disulfide_candidates: list - Potential disulfide bonds
            - histidine_analysis: list - Histidine pKa and state recommendations
            - missing_residues: list - Missing residue segments
            - missing_atoms: list - Missing heavy atoms
            - nonstandard_residues: list - Non-standard residue modifications
            - ligand_analysis: list - Ligand SMILES and graph-derived charge metadata
            - summary: dict - Quick overview for LLM
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Analyzing structure details: {structure_file} at pH {ph}")

    result = {
        "success": False,
        "structure_file": str(structure_file),
        "ph": ph,
        "disulfide_candidates": [],
        "histidine_analysis": [],
        "missing_residues": [],
        "missing_atoms": [],
        "nonstandard_residues": [],
        "ligand_analysis": [],
        "summary": {},
        "errors": [],
        "warnings": [],
    }

    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        return result

    suffix = structure_path.suffix.lower()
    if suffix not in [".cif", ".pdb", ".ent"]:
        result["errors"].append(f"Unsupported file format: {suffix}")
        return result

    try:
        # Detect disulfide bond candidates
        if detect_disulfides:
            logger.info("Detecting disulfide bond candidates")
            disulfide_candidates = _detect_disulfide_candidates(structure_path)
            result["disulfide_candidates"] = disulfide_candidates
            if disulfide_candidates:
                logger.info(f"Found {len(disulfide_candidates)} disulfide candidate(s)")

        # Analyze histidines
        if estimate_protonation:
            logger.info("Analyzing histidine protonation states")
            histidines = _find_histidines(structure_path)
            if histidines:
                his_analysis = _estimate_histidine_pka(structure_path, histidines, ph)
                result["histidine_analysis"] = his_analysis
                logger.info(f"Analyzed {len(his_analysis)} histidine(s)")

        # Check for missing residues and atoms
        if check_missing:
            logger.info("Checking for missing residues and atoms")
            missing_residues, missing_atoms = _find_missing_residues(structure_path)
            result["missing_residues"] = missing_residues
            result["missing_atoms"] = missing_atoms

            # Find non-standard residues
            nonstandard = _find_nonstandard_residues(structure_path)
            result["nonstandard_residues"] = nonstandard

            if missing_residues:
                logger.info(f"Found {len(missing_residues)} missing residue segment(s)")
            if nonstandard:
                logger.info(f"Found {len(nonstandard)} non-standard residue(s)")

        # Analyze ligands
        if identify_ligands:
            logger.info("Analyzing ligands")
            ligand_analysis = _analyze_ligands(structure_path)
            result["ligand_analysis"] = ligand_analysis
            if ligand_analysis:
                logger.info(f"Found {len(ligand_analysis)} ligand(s)")

        # Build summary
        requires_decision = []
        if result["histidine_analysis"]:
            requires_decision.append("histidine_states")
        if result["ligand_analysis"]:
            requires_decision.append("ligand_processing")
        if any(mr["recommendation"] == "review" for mr in result["missing_residues"]):
            requires_decision.append("missing_residues")

        result["summary"] = {
            "num_disulfide_candidates": len(result["disulfide_candidates"]),
            "num_histidines": len(result["histidine_analysis"]),
            "num_missing_residue_segments": len(result["missing_residues"]),
            "num_missing_atom_residues": len(result["missing_atoms"]),
            "num_nonstandard_residues": len(result["nonstandard_residues"]),
            "num_ligands": len(result["ligand_analysis"]),
            "requires_user_decision": requires_decision,
        }

        result["success"] = True
        logger.info(f"Structure analysis complete: {result['summary']}")

    except Exception as e:
        error_msg = f"Error during structure analysis: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)

    return result


# =============================================================================
# Tool Registry
# =============================================================================
