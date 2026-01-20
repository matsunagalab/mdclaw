"""Clarification utilities for MDZen.

This module provides data structures and helper functions for
clarification question generation. These are used by:
- ADK agents (via clarification_agent.py)
- Colab Forms (optional graphical interface)

Note: Structure analysis functions were removed. Use MCP tools
(get_structure_info, download_structure, inspect_molecules) via
ADK McpToolset instead.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StructureAnalysis:
    """Result of structure analysis from PDB/UniProt."""

    pdb_id: str
    title: str = ""
    experimental_method: str = ""
    resolution: Optional[float] = None

    # Polymer entities with UniProt info
    polymer_entities: list[dict] = field(default_factory=list)

    # Ligand information
    ligands: list[dict] = field(default_factory=list)

    # UniProt biological info
    uniprot_info: dict = field(default_factory=dict)
    subunit_composition: str = ""  # "Monomer", "Homodimer", etc.

    # Structure inspection results
    chains: list[dict] = field(default_factory=list)
    protein_chain_ids: list[str] = field(default_factory=list)
    ligand_chain_ids: list[str] = field(default_factory=list)

    # Downloaded file path
    structure_file: Optional[str] = None

    # Analysis flags
    has_multiple_chains: bool = False
    chains_are_crystallographic: bool = False  # True if UniProt says monomer but PDB has multiple chains
    has_ligands: bool = False

    # Variant/wild-type information (from PDB title analysis)
    is_likely_variant: bool = False  # True if title suggests mutant/variant
    variant_indicators: list[str] = field(default_factory=list)  # Detected mutations, e.g., ["K127A", "mutant"]
    is_wild_type: bool = False  # True if title explicitly mentions wild-type

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "pdb_id": self.pdb_id,
            "title": self.title,
            "experimental_method": self.experimental_method,
            "resolution": self.resolution,
            "polymer_entities": self.polymer_entities,
            "ligands": self.ligands,
            "uniprot_info": self.uniprot_info,
            "subunit_composition": self.subunit_composition,
            "chains": self.chains,
            "protein_chain_ids": self.protein_chain_ids,
            "ligand_chain_ids": self.ligand_chain_ids,
            "structure_file": self.structure_file,
            "has_multiple_chains": self.has_multiple_chains,
            "chains_are_crystallographic": self.chains_are_crystallographic,
            "has_ligands": self.has_ligands,
            "is_likely_variant": self.is_likely_variant,
            "variant_indicators": self.variant_indicators,
            "is_wild_type": self.is_wild_type,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StructureAnalysis":
        """Create StructureAnalysis from dictionary."""
        return cls(
            pdb_id=data.get("pdb_id", ""),
            title=data.get("title", ""),
            experimental_method=data.get("experimental_method", ""),
            resolution=data.get("resolution"),
            polymer_entities=data.get("polymer_entities", []),
            ligands=data.get("ligands", []),
            uniprot_info=data.get("uniprot_info", {}),
            subunit_composition=data.get("subunit_composition", ""),
            chains=data.get("chains", []),
            protein_chain_ids=data.get("protein_chain_ids", []),
            ligand_chain_ids=data.get("ligand_chain_ids", []),
            structure_file=data.get("structure_file"),
            has_multiple_chains=data.get("has_multiple_chains", False),
            chains_are_crystallographic=data.get("chains_are_crystallographic", False),
            has_ligands=data.get("has_ligands", False),
            is_likely_variant=data.get("is_likely_variant", False),
            variant_indicators=data.get("variant_indicators", []),
            is_wild_type=data.get("is_wild_type", False),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
        )


@dataclass
class ClarificationQuestion:
    """A clarification question with options."""

    id: str  # e.g., "chain_selection", "ligand_handling"
    question: str
    options: list[str]
    default: int = 0
    context: str = ""  # Why this question matters


def generate_clarification_questions(
    analysis: StructureAnalysis,
    user_request: str = "",
) -> list[ClarificationQuestion]:
    """Generate clarification questions based on structure analysis.

    Args:
        analysis: StructureAnalysis from MCP tool results
        user_request: Original user request text

    Returns:
        List of ClarificationQuestion objects
    """
    questions = []
    user_lower = user_request.lower()

    # Parse any chain specification from user request
    chain_pattern = re.search(r'chain[s]?\s*[:\s]?\s*([A-Za-z,\s]+)', user_lower)
    user_specified_chains = None
    if chain_pattern:
        user_specified_chains = [c.strip().upper() for c in chain_pattern.group(1).split(",")]

    # Question 1: Chain selection (if multiple protein chains and not already specified)
    if analysis.has_multiple_chains and not user_specified_chains:
        chain_options = []

        # Build chain options
        for chain in analysis.chains:
            if chain.get("chain_type") == "protein":
                pass  # Just for counting

        # First option: single chain (recommended for monomers)
        if analysis.protein_chain_ids:
            first_chain = analysis.protein_chain_ids[0]
            if analysis.chains_are_crystallographic:
                chain_options.append(
                    f"Chain {first_chain} only (recommended - biological monomer)"
                )
            else:
                chain_options.append(f"Chain {first_chain} only")

        # Option for all chains
        all_chains = ",".join(analysis.protein_chain_ids)
        if analysis.chains_are_crystallographic:
            chain_options.append(
                f"All chains ({all_chains}) - crystallographic copies"
            )
        else:
            chain_options.append(f"All chains ({all_chains})")

        # Option for specific selection
        if len(analysis.protein_chain_ids) > 2:
            chain_options.append("Custom selection (specify in notes)")

        context = ""
        if analysis.subunit_composition:
            context = f"UniProt indicates this protein is a {analysis.subunit_composition}. "
        if analysis.chains_are_crystallographic:
            context += "The multiple chains in the crystal structure appear to be crystallographic copies, not a biological oligomer."

        questions.append(ClarificationQuestion(
            id="chain_selection",
            question="Which chains would you like to simulate?",
            options=chain_options,
            default=0,
            context=context,
        ))

    # Question 2: Ligand handling (if ligands present and not specified)
    if analysis.has_ligands:
        ligand_names = [f"{lig.get('comp_id', 'UNK')} ({lig.get('name', 'Unknown')})"
                        for lig in analysis.ligands]
        ligand_list = ", ".join(ligand_names)

        # Check if user mentioned ligand handling
        ligand_mentioned = any(word in user_lower for word in ["ligand", "inhibitor", "substrate", "apo", "holo"])

        if not ligand_mentioned:
            ligand_options = [
                f"Keep ligands ({ligand_list})",
                "Remove all ligands (apo form)",
            ]
            if len(analysis.ligands) > 1:
                ligand_options.append("Keep some ligands (specify in notes)")

            questions.append(ClarificationQuestion(
                id="ligand_handling",
                question="How should ligands be handled?",
                options=ligand_options,
                default=0,
                context=f"Structure contains: {ligand_list}",
            ))

    # Question 3: Water model (if not specified)
    if "water" not in user_lower or "model" not in user_lower:
        questions.append(ClarificationQuestion(
            id="water_model",
            question="Which water model would you like to use?",
            options=[
                "tip3p (fastest, standard choice)",
                "opc (more accurate for proteins)",
                "spce (widely tested)",
                "tip4pew (4-site, most accurate but slower)",
            ],
            default=0,
            context="TIP3P is recommended for most simulations. OPC provides better accuracy for protein dynamics.",
        ))

    # Question 4: Force field
    if "force" not in user_lower and "ff" not in user_lower:
        questions.append(ClarificationQuestion(
            id="force_field",
            question="Which protein force field?",
            options=[
                "ff19SB (latest Amber, recommended)",
                "ff14SB (well-tested, widely used)",
            ],
            default=0,
            context="ff19SB is the latest Amber protein force field with improved backbone parameters.",
        ))

    # Question 5: Simulation time (if not specified)
    time_pattern = re.search(r'(\d+(?:\.\d+)?)\s*(?:ns|nanosecond)', user_lower)
    if not time_pattern:
        questions.append(ClarificationQuestion(
            id="simulation_time",
            question="How long should the simulation run?",
            options=[
                "0.1 ns (quick test, ~1 min)",
                "1 ns (short equilibration, ~10 min)",
                "10 ns (production quality, ~1-2 hours)",
                "100 ns (extended sampling)",
            ],
            default=1,
            context="Longer simulations provide better sampling but take more time.",
        ))

    return questions


def generate_brief_from_answers(
    analysis: StructureAnalysis,
    questions: list[ClarificationQuestion],
    answers: list[int],
    user_request: str = "",
) -> dict:
    """Generate SimulationBrief from structure analysis and user answers.

    Args:
        analysis: StructureAnalysis from MCP tool results
        questions: List of ClarificationQuestion objects
        answers: List of answer indices (0-based)
        user_request: Original user request

    Returns:
        SimulationBrief as dictionary
    """
    # Parse basic parameters from user request
    user_lower = user_request.lower()

    # Extract temperature
    temp_match = re.search(r'(\d+)\s*k(?:elvin)?', user_lower)
    temperature = int(temp_match.group(1)) if temp_match else 300

    # Build answer map
    answer_map = {}
    for i, q in enumerate(questions):
        if i < len(answers):
            answer_idx = answers[i]
            if answer_idx < len(q.options):
                answer_map[q.id] = q.options[answer_idx]

    # Determine chain selection
    select_chains = None
    chain_answer = answer_map.get("chain_selection", "")
    if "Chain " in chain_answer:
        # Extract chain ID(s) from answer
        chain_match = re.search(r'Chain\s+([A-Z])(?:\s+only)?', chain_answer)
        if chain_match:
            select_chains = [chain_match.group(1)]
        elif "All chains" in chain_answer:
            select_chains = analysis.protein_chain_ids

    # Determine ligand handling
    include_ligands = True
    ligand_answer = answer_map.get("ligand_handling", "")
    if "Remove" in ligand_answer or "apo" in ligand_answer.lower():
        include_ligands = False

    # Determine water model
    water_model = "tip3p"
    water_answer = answer_map.get("water_model", "")
    for model in ["tip3p", "opc", "spce", "tip4pew"]:
        if model in water_answer.lower():
            water_model = model
            break

    # Determine force field
    force_field = "ff19SB"
    ff_answer = answer_map.get("force_field", "")
    if "ff14SB" in ff_answer:
        force_field = "ff14SB"

    # Determine simulation time
    simulation_time_ns = 1.0
    time_answer = answer_map.get("simulation_time", "")
    time_match = re.search(r'(\d+(?:\.\d+)?)\s*ns', time_answer)
    if time_match:
        simulation_time_ns = float(time_match.group(1))

    # Build include_types
    include_types = ["protein", "ion"]
    if include_ligands and analysis.has_ligands:
        include_types.append("ligand")

    brief = {
        "pdb_id": analysis.pdb_id,
        "select_chains": select_chains,
        "include_types": include_types,
        "temperature": temperature,
        "simulation_time_ns": simulation_time_ns,
        "water_model": water_model,
        "force_field": force_field,
        "box_padding": 12.0,
        "salt_concentration": 0.15,
        "pressure_bar": 1.0,
        "ensemble": "NPT",
        "cubic_box": True,
        "ph": 7.4,
        # Metadata
        "structure_file": analysis.structure_file,
        "title": analysis.title,
        "subunit_composition": analysis.subunit_composition,
    }

    return brief
