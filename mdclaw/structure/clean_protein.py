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
import shutil
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional, Dict, Any  # noqa: E402

from pdbfixer import PDBFixer  # noqa: E402
from openmm.app import PDBFile  # noqa: E402
from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    sha256_file,
)
from mdclaw.forcefield_templates import nucleic_residue_name_map  # noqa: E402
from mdclaw.research.nucleic import (  # noqa: E402
    MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE,
    classify_nucleic_residues,
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
PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES = 10
PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH = 5

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")

PDB_ATOM_RECORD_PREFIXES = ("ATOM  ", "HETATM")
_NUCLEIC_5P_TERMINAL_PHOSPHATE_ATOMS = {
    "P",
    "OP1",
    "OP2",
    "OP3",
    "O1P",
    "O2P",
    "O3P",
    "HOP1",
    "HOP2",
    "HOP3",
    "H1P",
    "H2P",
    "H3P",
}
_NUCLEIC_5P_TERMINAL_PHOSPHATE_OXYGENS = {
    "OP1",
    "OP2",
    "OP3",
    "O1P",
    "O2P",
    "O3P",
}

from mdclaw.structure.pdb_utils import _pdb_atom_count, _pdb_hydrogen_count, _pdb_residue_names, _read_pdb_unique_residues, restore_residue_numbering_from_reference  # noqa: E402
from mdclaw.structure.protonation import _apply_protonation_states_with_modeller, _extract_histidine_states, _extract_input_protonation_state_overrides, _extract_non_default_protonation_states, _merge_input_protonation_state_overrides, _merge_protonation_states, _normalize_protonation_state_overrides  # noqa: E402
from mdclaw.structure.terminal_caps import _complete_terminal_cap_hydrogens_with_modeller, _resolve_terminal_cap_settings  # noqa: E402


def _pdb_atom_name(line: str) -> str:
    return line[12:16].strip()


def _pdb_residue_name(line: str) -> str:
    return line[17:20].strip().upper()


def _pdb_chain_residue_key(line: str) -> tuple[str, str, str]:
    return (line[21:22], line[22:26], line[26:27])


def _normalize_nucleic_input_for_openmm(
    input_path: Path,
    forcefield_xml: str,
) -> tuple[Path, dict[str, Any]]:
    """Normalize standard nucleic termini to templates shipped with OpenMM.

    OpenMM's Amber DNA/RNA XML bundles provide unphosphorylated 5' terminal
    variants (A5/G5/...) and ordinary internal variants, but not a standalone
    5'-phosphorylated terminal residue whose P atom lacks a previous-residue
    O3' external bond. Many experimental PDB/mmCIF entries include that
    terminal phosphate. Remove only that unsupported terminal phosphate group,
    leaving residue identity/order intact, so Modeller can add hydrogens and
    downstream topology generation can use standard templates.
    """
    report: dict[str, Any] = {
        "applied": False,
        "normalized_file": None,
        "removed_atom_count": 0,
        "removed_atoms": [],
        "code": None,
    }
    lines = input_path.read_text(encoding="utf-8").splitlines()
    template_resnames = set(nucleic_residue_name_map(forcefield_xml))
    first_residue_keys: set[tuple[str, str, str]] = set()
    first_residue_names: dict[tuple[str, str, str], str] = {}
    first_residue_atoms: dict[tuple[str, str, str], set[str]] = {}
    segment_chain: str | None = None
    segment_first: tuple[str, str, str] | None = None

    for line in lines:
        if line.startswith("TER"):
            segment_chain = None
            segment_first = None
            continue
        if not line.startswith(PDB_ATOM_RECORD_PREFIXES):
            continue
        key = _pdb_chain_residue_key(line)
        chain_id = key[0]
        if segment_first is None or chain_id != segment_chain:
            segment_chain = chain_id
            segment_first = key
            first_residue_keys.add(key)
            first_residue_names[key] = _pdb_residue_name(line)
        if segment_first == key:
            first_residue_atoms.setdefault(key, set()).add(_pdb_atom_name(line).upper())

    residues_to_normalize = {
        key
        for key, atom_names in first_residue_atoms.items()
        if key in first_residue_keys
        and first_residue_names.get(key) in template_resnames
        and "P" in atom_names
        and bool(atom_names & _NUCLEIC_5P_TERMINAL_PHOSPHATE_OXYGENS)
    }
    if not residues_to_normalize:
        return input_path, report

    normalized_lines: list[str] = []
    removed_atoms: list[dict[str, str]] = []
    for line in lines:
        if line.startswith(PDB_ATOM_RECORD_PREFIXES):
            key = _pdb_chain_residue_key(line)
            atom_name = _pdb_atom_name(line).upper()
            if (
                key in residues_to_normalize
                and atom_name in _NUCLEIC_5P_TERMINAL_PHOSPHATE_ATOMS
            ):
                removed_atoms.append({
                    "chain": key[0].strip(),
                    "resnum": key[1].strip(),
                    "icode": key[2].strip(),
                    "resname": _pdb_residue_name(line),
                    "atom_name": atom_name,
                })
                continue
        normalized_lines.append(line)

    if not removed_atoms:
        return input_path, report

    normalized_path = input_path.with_name(f"{input_path.stem}.openmm_nucleic_input.pdb")
    normalized_path.write_text("\n".join(normalized_lines) + "\n", encoding="utf-8")
    report.update({
        "applied": True,
        "normalized_file": str(normalized_path),
        "removed_atom_count": len(removed_atoms),
        "removed_atoms": removed_atoms,
        "code": "removed_unsupported_5prime_terminal_phosphate",
    })
    return normalized_path, report


def _remove_heterogens_preserving_caps(fixer, keep_water: bool) -> dict:
    """Remove heterogens like ``PDBFixer.removeHeterogens`` but keep terminal
    caps (ACE/NME).

    ``PDBFixer.removeHeterogens`` keeps only standard protein/nucleic residues
    (+ water), so it deletes ACE/NME caps as heterogens — silently turning a
    capped peptide into a charged free terminus. This mirrors that logic with
    the terminal caps added to the keep set. Returns a summary with the number
    of removed residues and the preserved cap names.
    """
    from openmm import app as _app
    from pdbfixer.pdbfixer import dnaResidues, proteinResidues, rnaResidues

    keep = set(proteinResidues) | set(dnaResidues) | set(rnaResidues)
    keep |= {"N", "UNK"} | TERMINAL_CAP_RESIDUES
    if keep_water:
        keep.add("HOH")

    to_delete = [r for r in fixer.topology.residues() if r.name not in keep]
    preserved_caps = [
        r.name for r in fixer.topology.residues() if r.name in TERMINAL_CAP_RESIDUES
    ]
    modeller = _app.Modeller(fixer.topology, fixer.positions)
    modeller.delete(to_delete)
    fixer.topology = modeller.topology
    fixer.positions = modeller.positions
    return {"removed_count": len(to_delete), "preserved_caps": preserved_caps}


def _internal_missing_residue_records(
    missing_residues: dict,
    chains: list,
) -> list[dict]:
    records: list[dict] = []
    for (chain_idx, res_idx), residues in sorted(missing_residues.items()):
        residue_names = [str(residue) for residue in residues]
        if residue_names in (["ACE"], ["NME"]):
            continue
        chain = chains[chain_idx] if 0 <= chain_idx < len(chains) else None
        chain_id = str(getattr(chain, "id", chain_idx))
        records.append({
            "chain_index": chain_idx,
            "chain_id": chain_id,
            "position": res_idx,
            "residues": residue_names,
            "residue_count": len(residue_names),
        })
    return records


def _missing_residue_summary(records: list[dict]) -> dict:
    total_residues = sum(int(record.get("residue_count") or 0) for record in records)
    max_segment_length = max(
        (int(record.get("residue_count") or 0) for record in records),
        default=0,
    )
    return {
        "segment_count": len(records),
        "total_residues": total_residues,
        "max_segment_length": max_segment_length,
        "segments": records,
    }


MISSING_RESIDUE_METHODS = ("auto", "pdbfixer", "modeller")


# MODELLER loop generation is stochastic. A fixed seed keeps a re-run of the
# same node producing the same loops; without one, re-running a job quietly
# changes the structure the whole study rests on. MODELLER wants a negative
# seed in [-50000, -2].
MODELLER_REPAIR_RANDOM_SEED = -8123


def _probe_internal_missing_residue_summary(input_path: Path) -> dict:
    """Measure internal gaps without changing the structure."""
    probe = PDBFixer(filename=str(input_path))
    probe.findMissingResidues()
    chains = list(probe.topology.chains())
    internal = {}
    for (chain_idx, res_idx), residues in probe.missingResidues.items():
        if not 0 <= chain_idx < len(chains):
            continue
        chain_length = len(list(chains[chain_idx].residues()))
        if res_idx in (0, chain_length):
            continue
        internal[(chain_idx, res_idx)] = residues
    return _missing_residue_summary(
        _internal_missing_residue_records(internal, chains)
    )


def _modeller_repair_usability() -> dict:
    """Require both a MODELLER key and an importable package."""
    import subprocess
    import sys

    from mdclaw.genesis.modeller import _has_modeller_license_env

    license_env_present = _has_modeller_license_env()
    import_error = None
    modeller_importable = False
    if license_env_present:
        # MODELLER's installed config may contain a placeholder key. Probe in
        # an isolated interpreter, injecting the environment key the same way
        # the real runner does, so this checks an actual import without
        # contaminating this process's module cache.
        try:
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import importlib.util, os, re, sys, types\n"
                        "from pathlib import Path\n"
                        "key = next(v for k, v in os.environ.items() "
                        "if k.startswith('KEY_MODELLER') and v)\n"
                        "spec = importlib.util.find_spec('modeller')\n"
                        "if spec is None: raise ModuleNotFoundError('modeller')\n"
                        "locations = list(spec.submodule_search_locations or [])\n"
                        "install_dir = None\n"
                        "if locations:\n"
                        " p = Path(locations[0]) / 'config.py'\n"
                        " if p.exists():\n"
                        "  m = re.search(r\"install_dir\\s*=\\s*r?['\\\"]"
                        "([^'\\\"]+)['\\\"]\", p.read_text())\n"
                        "  install_dir = m.group(1) if m else None\n"
                        "cfg = types.ModuleType('modeller.config')\n"
                        "cfg.license = key\n"
                        "if install_dir: cfg.install_dir = install_dir\n"
                        "sys.modules['modeller.config'] = cfg\n"
                        "import modeller\n"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if probe.returncode == 0:
                modeller_importable = True
            else:
                import_error = (probe.stderr or probe.stdout).strip()[-1000:]
        except Exception as exc:  # noqa: BLE001 - returned as structured context
            import_error = f"{type(exc).__name__}: {exc}"
    return {
        "usable": license_env_present and modeller_importable,
        "license_env_present": license_env_present,
        "modeller_importable": modeller_importable,
        "import_error": import_error,
    }


def _missing_residue_auto_recommendation(
    summary: dict,
    usability: dict,
) -> dict:
    export_command = "export KEY_MODELLER10v8=<your license key>"
    return {
        "reason": "auto_missing_residue_repair_requires_usable_modeller",
        "recommended_next_action": "export_modeller_license_and_create_new_prep_node",
        "restart_stage": "prep",
        "next_commands": [
            export_command,
            (
                "mdclaw create_node --job-dir <job_dir> --node-type prep "
                "--parent-node-ids <completed_parent_node_id>"
            ),
            (
                "mdclaw --job-dir <job_dir> --node-id <new_prep_node_id> "
                "prepare_complex"
            ),
        ],
        "options": [
            {
                "option": "provide_modeller_license_and_create_new_prep_node",
                "next_skill": "skills/md-prepare/SKILL.md",
                "command": export_command,
                "when": (
                    "Allow the default auto method to rebuild the out-of-scope "
                    "gaps with MODELLER."
                ),
            },
            {
                "option": "pin_pdbfixer_strictly",
                "next_skill": "skills/md-prepare/SKILL.md",
                "flag": "--missing-residue-method pdbfixer",
                "when": (
                    "Predicted loop coordinates are deliberately prohibited; "
                    "prep will retain the strict out-of-scope failure."
                ),
            },
        ],
        "modeller_usability": usability,
        "missing_residue_summary": summary,
    }


def _validate_modeller_repair_model(
    template_path: Path,
    model_path: Path,
    target_sequence: str,
    template_frame: dict | None,
) -> dict:
    """Verify that an in-place MODELLER repair is a complete drop-in replacement."""
    import gemmi

    from mdclaw.genesis.modeller import _pdb_residue_order

    result = {
        "success": False,
        "errors": [],
        "expected_residue_count": len(target_sequence),
        "observed_residue_count": 0,
        "missing_observed_residues": [],
        "target_sequence_matches": False,
        "residues_renumbered": (
            int(template_frame.get("residues_renumbered") or 0)
            if isinstance(template_frame, dict)
            else 0
        ),
    }
    result["template_numbering_restored"] = (
        result["residues_renumbered"] == len(target_sequence)
    )
    if not result["template_numbering_restored"]:
        result["errors"].append(
            "MODELLER did not restore template author numbering for every target "
            f"residue: expected {len(target_sequence)}, restored "
            f"{result['residues_renumbered']}"
        )

    template_chains, template_residues, _ = _pdb_residue_order(Path(template_path))
    model_chains, model_residues, _ = _pdb_residue_order(Path(model_path))
    template_keys = {
        (chain, resnum, icode)
        for chain in template_chains
        for resnum, icode in template_residues[chain]
    }
    model_order = [
        (chain, resnum, icode)
        for chain in model_chains
        for resnum, icode in model_residues[chain]
    ]
    model_keys = set(model_order)
    missing_observed = sorted(template_keys - model_keys)
    result["missing_observed_residues"] = [
        {"chain": chain, "resnum": resnum, "icode": icode.strip()}
        for chain, resnum, icode in missing_observed
    ]
    if missing_observed:
        result["errors"].append(
            "MODELLER output lost observed template residue identities: "
            f"{result['missing_observed_residues']}"
        )

    result["observed_residue_count"] = len(model_order)
    if len(model_order) != len(target_sequence):
        result["errors"].append(
            "MODELLER output residue count does not match the requested target span: "
            f"expected {len(target_sequence)}, observed {len(model_order)}"
        )

    residue_names: dict[tuple[str, int, str], str] = {}
    for line in Path(model_path).read_text().splitlines():
        if line.startswith(("ATOM", "HETATM")):
            key = (line[21], int(line[22:26]), line[26])
            residue_names.setdefault(key, line[17:20].strip())
    model_sequence = gemmi.one_letter_code(
        [residue_names[key] for key in model_order if key in residue_names]
    ).upper()
    result["observed_sequence"] = model_sequence
    result["target_sequence_matches"] = model_sequence == target_sequence
    if not result["target_sequence_matches"]:
        result["errors"].append(
            "MODELLER output sequence does not match the requested target span: "
            f"expected {target_sequence}, observed {model_sequence}"
        )

    result["success"] = not result["errors"]
    return result


def _repair_missing_residues_with_modeller(
    input_path: Path,
    random_seed: int = MODELLER_REPAIR_RANDOM_SEED,
) -> dict:
    """Rebuild a chain's internal missing residues with MODELLER loop modeling.

    PDBFixer builds missing residues geometrically and is reliable only for
    short gaps; the caller asked for MODELLER instead. Everything MODELLER
    needs is already here: the template is this very structure, and the target
    sequence is its own SEQRES, which is also what told PDBFixer the residues
    were missing in the first place.

    Terminal tails are left alone, matching ``ignore_terminal_missing_residues``
    -- an unresolved terminus is disorder, not a gap to bridge.

    Returns ``{applied, success, model_file, summary, operation, errors,
    warnings, code}``. ``applied`` is False when there is nothing internal to
    fill, including when the structure carries no reference sequence: that is
    not an error, it just leaves the normal PDBFixer path in charge.
    """
    from pdbfixer import PDBFixer

    input_path = Path(input_path)
    outcome: dict = {
        "applied": False,
        "success": True,
        "model_file": None,
        "summary": None,
        "operation": None,
        "detection": None,
        "template": None,
        "validation": None,
        "random_seed": random_seed,
        "errors": [],
        "warnings": [],
        "code": None,
    }

    probe = PDBFixer(filename=str(input_path))
    probe.findMissingResidues()
    chains = list(probe.topology.chains())

    internal: dict = {}
    leading_terminal = 0
    trailing_terminal = 0
    for (chain_idx, res_idx), residues in probe.missingResidues.items():
        if not 0 <= chain_idx < len(chains):
            continue
        chain_length = len(list(chains[chain_idx].residues()))
        if res_idx == 0:
            leading_terminal += len(residues)
            continue
        if res_idx == chain_length:
            trailing_terminal += len(residues)
            continue
        internal[(chain_idx, res_idx)] = residues

    records = _internal_missing_residue_records(internal, chains)
    if not records:
        return outcome
    summary = _missing_residue_summary(records)
    outcome["summary"] = summary
    # Describe the structure as it was BEFORE repair. The model MODELLER writes
    # carries no SEQRES, so detection run on it afterwards would report
    # "not checked" directly under a line saying 40 residues were rebuilt.
    outcome["detection"] = {
        "reference_sequence_available": True,
        "reference_sequence_chains": len(getattr(probe, "sequences", None) or []),
        "modeled_residues": len(list(probe.topology.residues())),
        "status": "detected",
        "terminal_excluded": {
            "total_residues": leading_terminal + trailing_terminal,
            "n_terminal_residues": leading_terminal,
            "c_terminal_residues": trailing_terminal,
        },
    }

    sequences = list(getattr(probe, "sequences", None) or [])
    if len(sequences) != 1:
        outcome["success"] = False
        outcome["code"] = "modeller_repair_reference_sequence_unavailable"
        outcome["errors"].append(
            "MODELLER missing-residue repair needs exactly one reference "
            f"sequence for {input_path.name}, found {len(sequences)}"
        )
        return outcome

    import gemmi

    # Model only the span between the first and last observed residue. The
    # reference sequence covers the unresolved termini too, and handing it over
    # whole would have MODELLER grow long de-novo tails that nothing measured
    # -- the same disorder the terminal filter just decided to leave alone.
    reference_residues = list(sequences[0].residues)
    span = reference_residues[
        leading_terminal : len(reference_residues) - trailing_terminal or None
    ]
    if leading_terminal or trailing_terminal:
        outcome["warnings"].append(
            f"Left {leading_terminal + trailing_terminal} unresolved terminal "
            f"residue(s) out of the MODELLER repair ({leading_terminal} N, "
            f"{trailing_terminal} C); only the {len(span)}-residue observed span "
            "was rebuilt"
        )
    outcome["detection"]["reference_sequence_length"] = len(reference_residues)
    target_sequence = gemmi.one_letter_code(span).upper()
    if "X" in target_sequence:
        outcome["warnings"].append(
            f"{target_sequence.count('X')} residue(s) in the reference sequence "
            "have no one-letter code and were passed to MODELLER as X"
        )

    from mdclaw.genesis.modeller import modeller_from_alignment

    out_dir = input_path.parent / f"{input_path.stem}.modeller_repair"
    model_result = modeller_from_alignment(
        template_pdb=str(input_path),
        target_sequence=target_sequence,
        template_code=input_path.stem,
        target_code=f"{input_path.stem}_filled",
        num_models=1,
        loop_refinement=True,
        loop_models=2,
        # The default ceiling is 30; raise it so the largest gap present is
        # actually refined rather than silently left as built.
        loop_max_length=max(30, int(summary["max_segment_length"])),
        # The repaired chain has to stay superposed on the structure it came
        # from, or a membrane orientation or partner chain kept from the
        # original lands in the wrong place.
        template_frame=True,
        random_seed=random_seed,
        output_dir=str(out_dir),
    )
    outcome["warnings"].extend(model_result.get("warnings", []))

    if not model_result.get("success"):
        outcome["success"] = False
        outcome["errors"].extend(
            model_result.get("errors") or ["MODELLER missing-residue repair failed"]
        )
        outcome["code"] = model_result.get("code") or "modeller_missing_residue_repair_failed"
        return outcome

    model_file = (model_result.get("selected_model") or {}).get("path")
    if not model_file or not Path(model_file).is_file():
        outcome["success"] = False
        outcome["code"] = "modeller_missing_residue_repair_failed"
        outcome["errors"].append(
            "MODELLER reported success but produced no readable model file"
        )
        return outcome

    validation = _validate_modeller_repair_model(
        input_path,
        Path(model_file),
        target_sequence,
        (model_result.get("selected_model") or {}).get("template_frame"),
    )
    outcome["validation"] = validation
    if not validation["success"]:
        outcome["success"] = False
        outcome["code"] = "modeller_missing_residue_repair_validation_failed"
        outcome["errors"].extend(validation["errors"])
        return outcome

    outcome["applied"] = True
    outcome["model_file"] = model_file
    outcome["template"] = {
        # For a repair the template is the input structure itself, and that is
        # part of the scientific record of the model: it says which coordinates
        # the rebuilt loops were grown from.
        "file": str(input_path),
        "sha256": sha256_file(input_path),
        "role": "self_template_repair",
    }
    outcome["operation"] = {
        "step": "missing_residues",
        "status": "modeled_with_modeller",
        "method": "modeller",
        "count": len(records),
        "segment_count": summary["segment_count"],
        "total_residues": summary["total_residues"],
        "max_segment_length": summary["max_segment_length"],
        "segments": records,
        "model_file": model_file,
        "random_seed": random_seed,
        "template": outcome["template"],
        "reference_sequence_length": len(target_sequence),
        "validation": validation,
        "details": (
            f"Rebuilt {summary['total_residues']} internal missing residue(s) in "
            f"{summary['segment_count']} segment(s) with MODELLER loop modeling"
        ),
    }
    return outcome


def _missing_residue_regeneration_recommendation(summary: dict) -> dict:
    return {
        "reason": "internal_missing_residues_exceed_pdbfixer_scope",
        # The failed prep node is terminal and sealed. Repair the same source
        # structure in a new sibling prep node with the same completed parent.
        "recommended_next_action": "create_new_prep_node_with_modeller_missing_residue_method",
        "restart_stage": "prep",
        "next_commands": [
            (
                "mdclaw create_node --job-dir <job_dir> --node-type prep "
                "--parent-node-ids <completed_parent_node_id>"
            ),
            (
                "mdclaw --job-dir <job_dir> --node-id <new_prep_node_id> "
                "prepare_complex --missing-residue-method modeller"
            ),
        ],
        "options": [
            {
                "option": "repair_gaps_in_new_prep_node",
                "next_skill": "skills/md-prepare/SKILL.md",
                "tool": "prepare_complex",
                "flag": "--missing-residue-method modeller",
                "when": (
                    "The structure itself is the right starting point and only "
                    "its gaps need rebuilding. Create a new prep node with the "
                    "failed node's same completed parent; failed nodes are sealed."
                ),
                "required_inputs": [],
            },
            {
                "option": "use_modeller_template_modeling",
                "next_skill": "skills/modeller-predict/SKILL.md",
                "tool": "modeller_from_alignment",
                "when": "The target is a different sequence from the template, so a new source structure has to be modeled rather than repaired.",
                "required_inputs": [
                    "template_pdb",
                    "target_sequence or alignment_file",
                ],
            },
            {
                "option": "use_boltz2_structure_prediction",
                "next_skill": "skills/boltz-predict/SKILL.md",
                "tool": "boltz2_protein_from_seq",
                "when": "No reliable template/alignment is available, or missing segments are too extensive for template repair.",
                "required_inputs": ["amino_acid_sequence_list"],
            },
            {
                "option": "provide_more_complete_source_structure",
                "next_skill": "skills/md-prepare/SKILL.md",
                "tool": "fetch_structure or register_local_structure",
                "when": "A better experimental structure, biological assembly, or curated local model is available.",
            },
        ],
        "missing_residue_summary": summary,
    }


def _spell(windows) -> str:
    """``18-214 and 383-458`` -- the windows as a report says them."""
    written = [f"{low}-{high}" for low, high in windows]
    if len(written) < 2:
        return written[0] if written else "empty"
    return " and ".join([", ".join(written[:-1]), written[-1]])


def _restrict_missing_to_window(fixer, chains, window) -> dict:
    """Drop the parts of each missing-residue segment that fall outside a range.

    PDBFixer reports a segment as "these residues belong between residue i-1 and
    residue i", with no numbers of their own until they are built.  Their numbers
    follow from the chain: the leading segment counts backwards from the first
    resolved residue, a trailing segment counts forwards from the last, and an
    internal segment lies between two known ones.

    A chain may be cropped to more than one window -- a GPCR fusion construct is
    kept as 18-214 and 383-458, with the crystallisation partner between them
    left out.  An internal segment is then no longer safe by construction: the
    gap between the two windows has an anchor on each side, and building it back
    rebuilds exactly what the ranges removed.  Every segment is therefore
    bounded, and one spanning from one window into another is dropped whole even
    when its numbers cannot be derived.
    """
    windows = [window] if window and isinstance(window[0], int) else list(window)

    def _held(number):
        return any(low <= number <= high for low, high in windows)

    def _same_window(left, right):
        return any(low <= left <= high and low <= right <= high
                   for low, high in windows)

    trimmed = {}

    def _number(residue):
        """The residue's author number, or None when it carries an insertion code.

        A chain numbered 100, 100A, 100B has no arithmetic to do, so a segment
        anchored on one is left alone rather than trimmed on a wrong number.
        """
        try:
            return int(str(residue.id).strip())
        except (TypeError, ValueError):
            return None

    for key in list(fixer.missingResidues.keys()):
        chain_idx, res_idx = key
        residues = list(chains[chain_idx].residues())
        if not residues:
            continue
        segment = fixer.missingResidues[key]
        left = right = None
        if res_idx == 0:
            anchor = _number(residues[0])
            numbers = (None if anchor is None
                       else list(range(anchor - len(segment), anchor)))
        elif res_idx == len(residues):
            anchor = _number(residues[-1])
            numbers = (None if anchor is None
                       else list(range(anchor + 1, anchor + 1 + len(segment))))
        else:
            # Internal segments are bounded by both of their anchors, so they
            # cannot leave a window that holds the anchors -- but only while
            # cropping has already removed everything outside it, and only while
            # there is one window. Trimming them from the anchors as well means
            # the two do not have to agree.
            left, right = _number(residues[res_idx - 1]), _number(residues[res_idx])
            numbers = (None if left is None or right is None
                       or right - left - 1 != len(segment)
                       else list(range(left + 1, right)))
        if numbers is None:
            # Undeducible numbers are left alone -- unless the segment bridges
            # two windows, where the anchors alone say it is the piece the
            # ranges deleted and no arithmetic is needed to refuse it.
            spans = (0 < res_idx < len(residues)
                     and left is not None and right is not None
                     and not _same_window(left, right))
            trimmed[f"chain {chain_idx} position {res_idx}"] = {
                "requested_window": _spell(windows),
                "segment_residues": len(segment),
                "kept": 0 if spans else len(segment),
                "note": ("dropped: the segment lies between two requested ranges"
                         if spans else
                         "left alone: the anchoring residue numbers are not plain "
                         "integers, so the segment's numbers cannot be derived"),
            }
            if spans:
                del fixer.missingResidues[key]
            continue
        keep = [name for name, number in zip(segment, numbers) if _held(number)]
        if len(keep) == len(segment):
            continue
        trimmed[f"chain {chain_idx} position {res_idx}"] = {
            "requested_window": _spell(windows),
            "segment_residues": len(segment),
            "kept": len(keep),
        }
        if keep:
            fixer.missingResidues[key] = keep
        else:
            del fixer.missingResidues[key]
    return trimmed


def clean_protein(
    pdb_file: str,
    ignore_terminal_missing_residues: bool = True,
    build_terminal_missing_residues: bool = False,
    build_window: Optional[tuple] = None,
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
    missing_residue_method: str = "auto",
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
        missing_residue_method: How to rebuild internal missing residues.
                                ``"auto"`` (default) uses PDBFixer in scope and
                                escalates larger gaps to MODELLER when licensed;
                                ``"pdbfixer"`` never escalates; ``"modeller"``
                                always uses MODELLER.
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
    }

    try:
        explicit_protonation_states = _normalize_protonation_state_overrides(
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
    original_input_path = input_path
    
    # Generate output filenames:
    # - *.pdbfixer.pdb: intermediate heavy-atom PDBFixer output.
    # - *.clean.pdb: final agent-facing cleaned output, after Amber/protonation.
    stem = input_path.stem
    final_output_file = input_path.parent / f"{stem}.clean.pdb"
    output_file = input_path.parent / f"{stem}.pdbfixer.pdb"
    result["output_file"] = str(output_file)
    result["final_output_file"] = str(final_output_file)
    
    method = str(missing_residue_method or "auto").strip().lower()
    if method not in MISSING_RESIDUE_METHODS:
        result["errors"].append(
            f"missing_residue_method must be one of {list(MISSING_RESIDUE_METHODS)}, "
            f"got {missing_residue_method!r}"
        )
        result["code"] = "invalid_missing_residue_method"
        return result
    result["missing_residue_method"] = method
    result["missing_residue_method_requested"] = method

    try:
        effective_method = "pdbfixer" if method == "auto" else method
        escalated_to_modeller = False
        if method == "auto":
            auto_summary = _probe_internal_missing_residue_summary(input_path)
            auto_out_of_scope = (
                auto_summary["total_residues"]
                > PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES
                or auto_summary["max_segment_length"]
                > PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH
            )
            if auto_out_of_scope:
                usability = _modeller_repair_usability()
                if not usability["usable"]:
                    recommendation = _missing_residue_auto_recommendation(
                        auto_summary,
                        usability,
                    )
                    result["missing_residue_method_used"] = "pdbfixer"
                    result["missing_residue_method_escalated"] = False
                    result["missing_residue_repair"] = {
                        "method": "pdbfixer",
                        "method_requested": "auto",
                        "method_used": "pdbfixer",
                        "escalated": False,
                        "status": "out_of_scope",
                        **auto_summary,
                    }
                    result["workflow_recommendation"] = recommendation
                    result["recommended_next_action"] = recommendation[
                        "recommended_next_action"
                    ]
                    result["recommended_next_skills"] = [
                        "skills/md-prepare/SKILL.md",
                    ]
                    result["code"] = "missing_residues_require_modeller_license"
                    result["errors"].append(
                        "Internal missing residues exceed the PDBFixer repair "
                        f"scope ({auto_summary['total_residues']} residue(s), "
                        f"max segment {auto_summary['max_segment_length']}). "
                        "Automatic MODELLER escalation is unavailable; run "
                        "'export KEY_MODELLER10v8=<your license key>' in an "
                        "MDClaw runtime with the modeller package installed, "
                        "then create a new prep node with the same completed parent."
                    )
                    return result
                effective_method = "modeller"
                escalated_to_modeller = True
        result["missing_residue_method_used"] = effective_method
        result["missing_residue_method_escalated"] = escalated_to_modeller

        input_protonation_states = _extract_input_protonation_state_overrides(
            original_input_path
        )
        requested_protonation_states = _merge_input_protonation_state_overrides(
            input_protonation_states,
            explicit_protonation_states,
        )
        result["input_protonation_states_promoted"] = [
            state
            for state in requested_protonation_states
            if state.get("input_state_preserved")
        ]

        # Rebuild internal gaps with MODELLER first when asked, so the rest of
        # the cleaning runs on a chain that no longer has any. Doing it here
        # rather than mid-flow keeps the terminal-cap bookkeeping below working
        # on a single, final PDBFixer instance.
        if effective_method == "modeller":
            repair = _repair_missing_residues_with_modeller(input_path)
            result["warnings"].extend(repair["warnings"])
            if not repair["success"]:
                result["errors"].extend(repair["errors"])
                result["code"] = repair["code"]
                return result
            if repair["applied"]:
                if escalated_to_modeller:
                    escalation_warning = (
                        "Automatic missing-residue repair escalated from PDBFixer "
                        "to MODELLER because the internal gaps exceeded the "
                        "PDBFixer scope; the rebuilt coordinates are predicted."
                    )
                    result["warnings"].append(escalation_warning)
                    repair["operation"]["warning"] = escalation_warning
                repair["operation"].update({
                    "method_requested": method,
                    "method_used": "modeller",
                    "escalated": escalated_to_modeller,
                })
                result["operations"].append(repair["operation"])
                result["missing_residue_detection"] = repair["detection"]
                result["missing_residue_repair"] = {
                    "method": "modeller",
                    "method_requested": method,
                    "method_used": "modeller",
                    "escalated": escalated_to_modeller,
                    "status": "modeled",
                    "model_file": repair["model_file"],
                    "random_seed": repair["random_seed"],
                    "template": repair["template"],
                    "validation": repair["validation"],
                    **repair["summary"],
                }
                input_path = Path(repair["model_file"])
                logger.info("Continuing from the MODELLER-repaired chain %s", input_path)

        # Load structure
        logger.info("Loading structure with PDBFixer")
        fixer = PDBFixer(filename=str(input_path))
        
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
        
        # Record what the reference sequence made visible. Without SEQRES
        # PDBFixer has nothing to compare coordinates against and reports zero
        # missing residues — indistinguishable, in the result, from a complete
        # chain. Say which of the two it is.
        reference_sequences = list(getattr(fixer, "sequences", None) or [])
        reference_length = (
            len(list(reference_sequences[0].residues))
            if len(reference_sequences) == 1
            else None
        )
        result.setdefault("missing_residue_detection", {
            "reference_sequence_available": bool(reference_sequences),
            "reference_sequence_chains": len(reference_sequences),
            "reference_sequence_length": reference_length,
            "modeled_residues": len(initial_residues),
        })
        if not reference_sequences and result["missing_residue_detection"].get(
            "status"
        ) != "detected":
            result["missing_residue_detection"]["status"] = "not_detectable"
            result["warnings"].append(
                f"{input_path.name} carries no reference sequence (SEQRES), so "
                "missing residues cannot be detected: a report of zero gaps here "
                "means 'not checked', not 'none present'"
            )
            result["operations"].append({
                "step": "missing_residues",
                "status": "not_detectable",
                "details": (
                    "No reference sequence in the input; PDBFixer cannot tell "
                    "modeled residues from missing ones"
                ),
            })
        elif reference_sequences:
            result["missing_residue_detection"]["status"] = "detected"

        # The residue range, when one was requested, bounds what may be built:
        # asking for 4-315 of a chain whose SEQRES runs to 317 must add residue
        # 315 and not 316 or 317. Without the window the terminal switch rebuilds
        # the whole overhang -- measured on 6W9C, 1-317 instead of the 4-315 that
        # was asked for.
        #
        # Outside the terminal branch, because a chain cropped to several ranges
        # has an internal gap that must be refused whatever the terminal switch
        # says: the space between two ranges is the crystallisation partner the
        # ranges took out, it has an anchor on each side, and building it back is
        # what a fusion crop exists to prevent. Inside the branch this ran only
        # when a terminus was being built, which is off by default.
        if build_window is not None:
            _trimmed = _restrict_missing_to_window(fixer, chains, build_window)
            if _trimmed:
                result["missing_residue_detection"]["window_trimmed"] = _trimmed

        # Step 1a: Handle terminal missing residues
        terminal_caps_requested = bool(resolved_n_terminal_cap or resolved_c_terminal_cap)
        if not (ignore_terminal_missing_residues and not terminal_caps_requested):
            # Building them is the requested branch; say so and say how many.
            # Otherwise "the terminus was rebuilt" would have to be inferred
            # from the absence of the warning that says it was not.
            built = []
            for key in list(fixer.missingResidues.keys()):
                chain_idx, res_idx = key
                chain = chains[chain_idx]
                if res_idx == 0 or res_idx == len(list(chain.residues())):
                    built.append({
                        "chain_index": chain_idx,
                        "chain_id": str(getattr(chain, "id", chain_idx)),
                        "terminus": "N" if res_idx == 0 else "C",
                        "residue_count": len(fixer.missingResidues[key]),
                    })
            if built:
                total = sum(record["residue_count"] for record in built)
                result["missing_residue_detection"]["terminal_built"] = {
                    "segment_count": len(built),
                    "total_residues": total,
                    "segments": built,
                }
                result["warnings"].append(
                    f"Rebuilt {total} terminal residue(s) in {len(built)} "
                    "segment(s); a terminus has an anchor on one side only, so "
                    "these coordinates are predicted rather than measured")
        if ignore_terminal_missing_residues and not terminal_caps_requested:
            # Remove terminal missing residues from the dictionary
            keys_to_remove = []
            terminal_records = []
            for key in list(fixer.missingResidues.keys()):
                chain_idx, res_idx = key
                chain = chains[chain_idx]
                chain_length = len(list(chain.residues()))
                if res_idx == 0 or res_idx == chain_length:
                    keys_to_remove.append(key)
                    terminal_records.append({
                        "chain_index": chain_idx,
                        "chain_id": str(getattr(chain, "id", chain_idx)),
                        "terminus": "N" if res_idx == 0 else "C",
                        "residue_count": len(fixer.missingResidues[key]),
                    })
            
            for key in keys_to_remove:
                del fixer.missingResidues[key]
            
            if keys_to_remove:
                # Segments and residues are different numbers, and the second
                # is the one that matters: 2 unresolved termini can be 77
                # residues. Report both so an empty internal-gap list is not
                # read as "the structure was complete".
                terminal_residue_total = sum(
                    record["residue_count"] for record in terminal_records
                )
                result["missing_residue_detection"]["terminal_excluded"] = {
                    "segment_count": len(terminal_records),
                    "total_residues": terminal_residue_total,
                    "segments": terminal_records,
                }
                result["operations"].append({
                    "step": "missing_residues",
                    "status": "modified",
                    "segment_count": len(terminal_records),
                    "total_residues": terminal_residue_total,
                    "segments": terminal_records,
                    "details": (
                        f"Found {num_missing_residues} missing residue segment(s); "
                        f"left {terminal_residue_total} residue(s) in "
                        f"{len(terminal_records)} unresolved terminus/termini unmodeled"
                    ),
                })
                result["warnings"].append(
                    f"Left {terminal_residue_total} terminal residue(s) in "
                    f"{len(terminal_records)} segment(s) unmodeled; an unresolved "
                    "terminus is disorder, not a gap to bridge"
                )
        
        # Step 1b: Add requested terminal caps. ``cap_termini=True`` resolves
        # to the historical ACE+NME pair; explicit one-sided cap arguments
        # can request only one terminus.
        if terminal_caps_requested:
            capped_chains = []
            for chain_idx, chain in enumerate(chains):
                chain_length = len(list(chain.residues()))
                # A cap normally takes the terminal slot outright: asking for
                # caps is asking for ACE/NME at the end of what was resolved,
                # not for the unresolved tail to be modelled first. When the
                # tail was asked for in its own right the two are both honoured
                # instead, with the cap on the outside of the residues it caps
                # -- otherwise a request to build residue 315 and cap the
                # terminus would quietly build NME in its place.
                keep_tail = build_terminal_missing_residues
                if resolved_n_terminal_cap:
                    tail = fixer.missingResidues.get((chain_idx, 0), []) if keep_tail else []
                    fixer.missingResidues[chain_idx, 0] = [resolved_n_terminal_cap] + list(tail)
                if resolved_c_terminal_cap:
                    tail = (fixer.missingResidues.get((chain_idx, chain_length), [])
                            if keep_tail else [])
                    fixer.missingResidues[chain_idx, chain_length] = (
                        list(tail) + [resolved_c_terminal_cap])
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
        internal_missing_records = _internal_missing_residue_records(
            fixer.missingResidues,
            chains,
        )
        missing_summary = _missing_residue_summary(internal_missing_records)
        internal_missing = [
            f"Chain {record['chain_index']}, position {record['position']}: {record['residues']}"
            for record in internal_missing_records
        ]
        
        if internal_missing:
            result["operations"].append({
                "step": "missing_residues",
                "status": "will_model",
                "count": len(internal_missing),
                "segment_count": missing_summary["segment_count"],
                "total_residues": missing_summary["total_residues"],
                "max_segment_length": missing_summary["max_segment_length"],
                "residues": internal_missing,
                "segments": internal_missing_records,
                "details": f"Found {len(internal_missing)} internal missing residue(s) to be modeled"
            })
            result.setdefault("missing_residue_repair", {}).update({
                "method": "pdbfixer",
                "method_requested": method,
                "method_used": "pdbfixer",
                "escalated": False,
                "status": "within_scope",
                "max_internal_missing_residues": PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES,
                "max_missing_residue_segment_length": PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH,
                **missing_summary,
            })
            if (
                missing_summary["total_residues"] > PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES
                or missing_summary["max_segment_length"] > PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH
            ):
                recommendation = _missing_residue_regeneration_recommendation(missing_summary)
                result["missing_residue_repair"]["status"] = "out_of_scope"
                result["missing_residue_repair"]["reason"] = recommendation["reason"]
                result["workflow_recommendation"] = recommendation
                result["recommended_next_action"] = recommendation["recommended_next_action"]
                result["recommended_next_skills"] = [
                    "skills/md-prepare/SKILL.md",
                    "skills/modeller-predict/SKILL.md",
                    "skills/boltz-predict/SKILL.md",
                ]
                result["code"] = "pdbfixer_missing_residues_out_of_scope"
                result["errors"].append(
                    "Internal missing residues exceed the PDBFixer repair scope: "
                    f"{missing_summary['total_residues']} residue(s) total, "
                    f"max segment length {missing_summary['max_segment_length']}."
                )
                return result
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
        
        # Step 3: Remove heterogens (preserving ACE/NME terminal caps, which
        # PDBFixer.removeHeterogens would otherwise silently delete).
        if remove_heterogens:
            logger.info(f"Removing heterogens (keep_water={keep_water})")
            het_summary = _remove_heterogens_preserving_caps(fixer, keep_water)
            water_status = "kept" if keep_water else "removed"
            details = (
                f"Removed {het_summary['removed_count']} heterogen residue(s), "
                f"water {water_status}"
            )
            if het_summary["preserved_caps"]:
                details += (
                    f"; preserved terminal caps {het_summary['preserved_caps']}"
                )
                result["preserved_terminal_caps"] = het_summary["preserved_caps"]
            result["operations"].append({
                "step": "remove_heterogens",
                "status": "success",
                "details": details,
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
                            detected_protonation = _extract_non_default_protonation_states(
                                amber_output_file
                            )
                            reported_protonation = _merge_protonation_states(
                                detected_protonation,
                                user_protonation_applied,
                            )
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+openmm_modeller_user_states",
                                "ph": ph,
                                "histidine_states": his_states,
                                "protonation_states": reported_protonation,
                            })
                            result["protonation_method"] = "pdb2pqr+openmm_modeller_user_states"
                            result["protonation_states"] = reported_protonation
                            logger.info(
                                f"Applied {len(user_protonation_applied)} user-specified "
                                "residue protonation state(s)"
                            )
                        else:
                            his_states = _extract_histidine_states(amber_output_file)
                            detected_protonation = _extract_non_default_protonation_states(
                                amber_output_file
                            )
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+propka",
                                "ph": ph,
                                "histidine_states": his_states,
                                "protonation_states": detected_protonation,
                            })
                            result["protonation_method"] = "pdb2pqr+propka"
                            result["protonation_states"] = detected_protonation
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
                    # pdb4amber renumbers residues (it makes numbering continuous
                    # across chains, e.g. chain B 1-99 -> 215-430). That silently
                    # invalidates every site-keyed input (protonation_states /
                    # histidine_states keyed by chain:resnum, detected PTM resnum).
                    # The PDBFixer output (output_file) still carries the original
                    # numbering and the same residue order, so restore it before
                    # any site-keyed step runs. Atoms/coords/H are untouched.
                    restored = restore_residue_numbering_from_reference(
                        amber_output_file, output_file
                    )
                    if restored is None:
                        result["warnings"].append(
                            "Could not restore original residue numbering after "
                            "pdb4amber (residue count changed); site-keyed inputs "
                            "may not match."
                        )
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
                        detected_protonation = _extract_non_default_protonation_states(
                            amber_output_file
                        )
                        reported_protonation = _merge_protonation_states(
                            detected_protonation,
                            user_protonation_applied,
                        )
                        op.update({
                            "method": "pdb4amber+openmm_modeller_user_states",
                            "ph": ph,
                            "histidine_states": his_states,
                            "protonation_states": reported_protonation,
                        })
                        result["protonation_method"] = "pdb4amber+openmm_modeller_user_states"
                        result["protonation_states"] = reported_protonation
                        result["histidine_states"] = his_states
                    else:
                        detected_protonation = _extract_non_default_protonation_states(
                            amber_output_file
                        )
                        op["protonation_states"] = detected_protonation
                        result["protonation_states"] = detected_protonation
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

        current_output = Path(str(result.get("output_file") or ""))
        if current_output.is_file() and current_output.resolve() != final_output_file.resolve():
            shutil.copy2(current_output, final_output_file)
            result["published_from"] = str(current_output)
            result["output_file"] = str(final_output_file)
            result["statistics"]["final_atoms"] = _pdb_atom_count(final_output_file)
            result["statistics"]["final_hydrogens"] = _pdb_hydrogen_count(
                final_output_file
            )

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
        modeller_input_path, normalization_report = _normalize_nucleic_input_for_openmm(
            input_path,
            forcefield_xml,
        )
        result["input_normalization"] = normalization_report
        if normalization_report.get("applied"):
            result["operations"].append({
                "step": "nucleic_input_normalization",
                "status": "success",
                "method": "openmm_terminal_template_compatibility",
                "code": normalization_report.get("code"),
                "removed_atom_count": normalization_report.get("removed_atom_count", 0),
                "normalized_file": normalization_report.get("normalized_file"),
            })
            result["warnings"].append(
                "Removed unsupported 5' terminal phosphate atom(s) from standard "
                "nucleic input to match OpenMM Amber terminal templates."
            )
        pdb = PDBFile(str(modeller_input_path))
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
    removed_atom_count = int(
        (result.get("input_normalization") or {}).get("removed_atom_count") or 0
    )
    min_expected_atom_count = result["atom_count_before"] - removed_atom_count
    if result["atom_count_after"] < min_expected_atom_count:
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
