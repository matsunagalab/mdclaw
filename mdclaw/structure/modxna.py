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

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    tail_for_agent,
)
from mdclaw.research.nucleic import (  # noqa: E402
    MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE,
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
SUPPORTED_PREP_SOLVENT_TYPES = {"explicit", "implicit", "vacuum"}

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")

from mdclaw.structure.pdb_utils import _read_pdb_unique_residues, _rename_pdb_residues  # noqa: E402


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


@node_tool
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
            "hints": [
                "prepare_modified_nucleic is a workflow tool: create a prep "
                "node, then run it with both --job-dir and --node-id.",
            ],
            "errors": ["prepare_modified_nucleic requires job_dir and node_id."],
        }
    if not modifications:
        return {
            **result,
            "error_type": "ValidationError",
            "code": "modxna_modifications_required",
            "hints": [
                "Pass a non-empty modifications list, e.g. "
                '[{"chain": "A", "resnum": 5, "target": "OMC"}].',
            ],
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
            "hints": [
                "Run a prep node to completion first so it produces the "
                "merged_pdb artifact, then create this prep node from it.",
            ],
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
            stderr_log = run_dir / "modxna.stderr"
            stderr_log.write_text(completed.stderr or "")
            result.update({
                "error_type": "ToolExecutionError",
                "code": "modxna_execution_failed",
                "errors": [
                    f"modXNA exited with code {completed.returncode}",
                    tail_for_agent(completed.stderr, log_path=str(stderr_log)),
                ],
            })
            result.setdefault("context", {})["log_artifact"] = str(stderr_log)
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
