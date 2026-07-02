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
from mdclaw._tool_meta import node_tool  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    create_unique_subdir,
    ensure_directory,
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
SUPPORTED_PREP_SOLVENT_TYPES = {"explicit", "implicit", "vacuum"}

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")


@node_tool
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
