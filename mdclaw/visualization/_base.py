"""Structure preview rendering tools.

This module keeps visualization optional at import time.  PyMOL is only
required when a rendering tool is actually executed, so CLI discovery and unit
tests still work in lightweight environments.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    setup_logger,
)

logger = setup_logger(__name__)

_SUPPORTED_STRUCTURE_SUFFIXES = {".pdb", ".cif", ".mmcif", ".ent"}

_STRUCTURE_ARTIFACT_PRIORITY_BY_NODE_TYPE = {
    "source": ("structure_file", "pdb_file", "cif_file"),
    "prep": (
        "merged_pdb",
        "mutated_pdb",
        "phosphorylated_pdb",
        "modified_nucleic_pdb",
        "prepared_pdb",
        "pdb_file",
    ),
    "solv": ("solvated_pdb", "pdb_file"),
    "topo": ("topology_pdb", "pdb_file"),
    "eq": ("equilibrated_pdb", "final_structure_pdb", "final_structure", "topology_pdb"),
    "prod": ("final_structure_pdb", "final_structure", "topology_pdb", "reference_pdb"),
    "analyze": ("reference_pdb", "topology_pdb"),
}

_COMMON_STRUCTURE_ARTIFACT_KEYS = (
    "structure_file",
    "merged_pdb",
    "solvated_pdb",
    "topology_pdb",
    "equilibrated_pdb",
    "final_structure_pdb",
    "final_structure",
    "reference_pdb",
    "pdb_file",
)

_LIPID_RESNAMES = (
    "POPC", "POPE", "POPG", "POPS", "PIP2", "PIP3", "DOPC", "DOPE", "DOPS",
    "DPPC", "DLPC", "DMPC", "CHOL", "PA", "PC", "PE", "PG", "PS", "CL",
)

_ION_RESNAMES = (
    "LI", "NA", "K", "RB", "CS", "MG", "CA", "SR", "BA", "ZN", "CU", "FE",
    "MN", "CO", "NI", "CD", "CL", "BR", "IOD", "F",
)

_STYLE_CHOICES = {
    "overview",
    "publication",
    "ligand_site",
    "membrane",
    "solvent_ions",
    "topology_check",
}

_CAMERA_CHOICES = {"auto", "overview", "ligand_site", "membrane", "topology_check"}

_VISUAL_REVIEWER_TYPES = {
    "multimodal_llm",
    "human",
    "not_available",
    "unknown",
}

_VISUAL_REVIEW_SEVERITIES = {
    "none",
    "low",
    "medium",
    "high",
    "not_reviewed",
}

_VISUAL_REVIEW_RECOMMENDATIONS = {
    "continue",
    "user_confirm",
    "manual_review",
    "rerender_preview",
    "rerun_previous_step",
    "blocked",
}

_VISUAL_REVIEW_DEFAULT_CHECKS = {
    "image_framing": "Major structure is visible and not cut off.",
    "expected_components": "Expected protein/nucleic/ligand/lipid/water/ion components are visible.",
    "ligand_position": "Ligands or cofactors are not obviously far from their expected binding site.",
    "membrane_orientation": "For membrane systems, protein and membrane placement is not obviously broken.",
    "solvent_ion_distribution": "Water, ions, or lipids do not form obvious impossible clumps or isolated artifacts.",
    "limitations_stated": "The review states what cannot be judged from the image alone.",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    return cleaned.strip("._") or "structure"


def _is_supported_structure_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _SUPPORTED_STRUCTURE_SUFFIXES


def _artifact_to_path(job_dir: str, node_id: str, value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(job_dir) / "nodes" / node_id / path
    return path.resolve(strict=False)


def _read_node_if_present(job_dir: str, node_id: str) -> Optional[dict]:
    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    if not node_json.is_file():
        return None
    try:
        data = json.loads(node_json.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _candidate_node_ids(job_dir: str, node_id: str, source_node_id: Optional[str]) -> list[str]:
    if source_node_id:
        return [source_node_id]

    node = _read_node_if_present(job_dir, node_id)
    if not node:
        return [node_id]

    candidates: list[str] = []
    if node.get("node_type") != "analyze":
        candidates.append(node_id)
    for parent_id in node.get("parent_node_ids", []) or []:
        candidates.append(str(parent_id))

    try:
        from mdclaw._node import get_ancestors

        candidates.extend(str(nid) for nid in get_ancestors(job_dir, node_id))
    except Exception:  # noqa: BLE001 - best-effort discovery only
        pass

    if node_id not in candidates:
        candidates.append(node_id)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def _resolve_structure_from_node(
    job_dir: str,
    node_id: str,
    *,
    source_node_id: Optional[str] = None,
    structure_artifact_key: Optional[str] = None,
) -> tuple[Optional[Path], Optional[str], Optional[str], list[str]]:
    """Return ``(path, source_node_id, artifact_key, warnings)``."""
    warnings: list[str] = []
    for candidate_id in _candidate_node_ids(job_dir, node_id, source_node_id):
        node = _read_node_if_present(job_dir, candidate_id)
        if not node:
            warnings.append(f"node not found or unreadable while resolving preview input: {candidate_id}")
            continue
        artifacts = node.get("artifacts", {})
        if not isinstance(artifacts, dict):
            continue

        node_type = str(node.get("node_type") or "")
        if structure_artifact_key:
            keys = (structure_artifact_key,)
        else:
            keys = (
                *_STRUCTURE_ARTIFACT_PRIORITY_BY_NODE_TYPE.get(node_type, ()),
                *_COMMON_STRUCTURE_ARTIFACT_KEYS,
                *sorted(artifacts.keys()),
            )

        seen_keys: set[str] = set()
        for key in keys:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            value = artifacts.get(key)
            path = _artifact_to_path(job_dir, candidate_id, value)
            if path is None:
                continue
            if not _is_supported_structure_path(path):
                continue
            if path.is_file():
                return path, candidate_id, key, warnings
            warnings.append(f"candidate structure artifact missing on disk: {candidate_id}:{key}")

    return None, None, None, warnings


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))










def _pymol_selection_script(
    *,
    structure_file: Path,
    output_png: Path,
    view_json: Path,
    width: int,
    height: int,
    dpi: int,
    ray: bool,
    style: str,
    selection: Optional[str],
    background: str,
    show_solvent: bool,
    show_ions: bool,
    show_lipids: bool,
    highlight_ligands: bool,
    camera_preset: str,
    zoom_buffer: float,
) -> str:
    lipid_resn = "+".join(_LIPID_RESNAMES)
    ion_resn = "+".join(_ION_RESNAMES)
    return f"""from pymol import cmd
import json

cmd.reinitialize()
cmd.load({json.dumps(str(structure_file))}, "structure")
cmd.hide("everything", "all")
cmd.bg_color({json.dumps(background)})
cmd.set("ray_opaque_background", 1)
cmd.set("antialias", 2)
cmd.set("depth_cue", 1)
cmd.set("cartoon_fancy_helices", 1)
cmd.set("cartoon_side_chain_helper", 1)
cmd.set("stick_radius", 0.16)
cmd.set("sphere_scale", 0.35)

cmd.select("protein_sel", "polymer.protein")
cmd.select("nucleic_sel", "polymer.nucleic")
cmd.select("solvent_sel", "solvent")
cmd.select("lipid_sel", "resn {lipid_resn}")
cmd.select("ion_sel", "(inorganic and not solvent) or resn {ion_resn}")
cmd.select("ligand_sel", "organic and not polymer and not solvent and not lipid_sel")
user_selection = {selection!r}
if user_selection:
    cmd.select("user_focus_sel", user_selection)

def has(selection):
    return cmd.count_atoms(selection) > 0

if has("protein_sel"):
    cmd.show("cartoon", "protein_sel")
    cmd.spectrum("chain", "rainbow", "protein_sel")
if has("nucleic_sel"):
    cmd.show("cartoon", "nucleic_sel")
    cmd.color("orange", "nucleic_sel")
if has("ligand_sel") and {str(highlight_ligands)}:
    cmd.show("sticks", "ligand_sel")
    cmd.color("yelloworange", "ligand_sel")
    cmd.set("stick_radius", 0.22, "ligand_sel")
if has("ion_sel") and {str(show_ions)}:
    cmd.show("spheres", "ion_sel")
    cmd.color("tv_blue", "ion_sel")
if has("lipid_sel") and {str(show_lipids)}:
    cmd.show("sticks", "lipid_sel")
    cmd.color("gray70", "lipid_sel")
    cmd.set("stick_transparency", 0.25, "lipid_sel")
if has("solvent_sel") and {str(show_solvent)}:
    cmd.show("dots", "solvent_sel")
    cmd.color("lightblue", "solvent_sel")
    cmd.set("dot_width", 1.0, "solvent_sel")
if user_selection and has("user_focus_sel"):
    cmd.show("sticks", "user_focus_sel")
    cmd.color("hotpink", "user_focus_sel")

style = {json.dumps(style)}
camera = {json.dumps(camera_preset)}
if style == "publication":
    cmd.set("cartoon_transparency", 0.05, "protein_sel")
    if has("solvent_sel"):
        cmd.hide("everything", "solvent_sel")
elif style == "ligand_site" and has("ligand_sel"):
    cmd.select("binding_site_sel", "polymer within 5.0 of ligand_sel")
    cmd.show("sticks", "binding_site_sel")
    cmd.color("gray85", "binding_site_sel")
    cmd.set("stick_radius", 0.14, "binding_site_sel")
elif style == "membrane":
    if has("solvent_sel"):
        cmd.hide("everything", "solvent_sel")
    if has("lipid_sel"):
        cmd.show("sticks", "lipid_sel")
elif style == "solvent_ions":
    if has("solvent_sel"):
        cmd.show("dots", "solvent_sel")
    if has("ion_sel"):
        cmd.show("spheres", "ion_sel")
elif style == "topology_check":
    cmd.show("lines", "all")
    cmd.show("sticks", "not solvent_sel")
    cmd.set("stick_radius", 0.10)

cmd.orient("visible")
if user_selection and has("user_focus_sel"):
    cmd.center("user_focus_sel")
    cmd.zoom("user_focus_sel", {zoom_buffer})
elif (camera == "ligand_site" or style == "ligand_site") and has("ligand_sel"):
    cmd.center("ligand_sel")
    cmd.zoom("ligand_sel", {zoom_buffer})
elif (camera == "membrane" or style == "membrane") and has("lipid_sel"):
    cmd.orient("protein_sel or nucleic_sel or ligand_sel or lipid_sel")
    cmd.turn("x", 90)
    cmd.zoom("visible", {zoom_buffer})
else:
    cmd.center("visible")
    cmd.zoom("visible", {zoom_buffer})

view = list(cmd.get_view())
with open({json.dumps(str(view_json))}, "w") as fh:
    json.dump({{"view": view}}, fh, indent=2)

cmd.png(
    {json.dumps(str(output_png))},
    width={width},
    height={height},
    dpi={dpi},
    ray={1 if ray else 0},
)
cmd.quit()
"""


def _pymol_pml_preview(
    *,
    structure_file: Path,
    output_png: Path,
    style: str,
    camera_preset: str,
    zoom_buffer: float,
) -> str:
    return "\n".join([
        f"load {structure_file}, structure",
        "hide everything, all",
        "show cartoon, polymer.protein or polymer.nucleic",
        "show sticks, organic and not polymer and not solvent",
        "show spheres, inorganic and not solvent",
        "orient visible",
        f"# style={style} camera_preset={camera_preset} zoom_buffer={zoom_buffer}",
        f"png {output_png}, ray=1",
        "",
    ])


def _run_pymol(script_file: Path, timeout: int) -> subprocess.CompletedProcess:
    pymol = shutil.which("pymol")
    if not pymol:
        raise FileNotFoundError("pymol executable not found in PATH")
    return subprocess.run(
        [pymol, "-cq", str(script_file)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def _register_preview_on_node(
    *,
    job_dir: str,
    node_id: str,
    artifacts: dict[str, str],
    metadata: dict[str, Any],
    warnings: list[str],
) -> None:
    from mdclaw._node import begin_node, complete_node, read_node

    node = read_node(job_dir, node_id)
    node_type = node.get("node_type")
    if node_type == "analyze" and node.get("status") != "completed":
        begin_node(job_dir, node_id)
        complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata, warnings=warnings or None)
        return

    if node.get("status") != "completed":
        raise ValueError(
            "render_structure_preview can attach previews to completed non-analyze nodes only; "
            "create an analyze node for in-progress workflow steps."
        )

    merged_artifacts = dict(node.get("artifacts") or {})
    merged_artifacts.update(artifacts)
    complete_node(
        job_dir,
        node_id,
        artifacts=merged_artifacts,
        metadata=metadata,
        warnings=warnings or None,
    )


def _fail_preview_node_if_mutable(job_dir: str, node_id: str, errors: list[str]) -> None:
    """Fail only preview/analyze nodes that this tool owns.

    A completed prep/solv/topo/min/eq/prod node may request a post-hoc preview
    attachment. Rendering failures must not rewrite that scientific node's
    status to failed.
    """
    node = _read_node_if_present(job_dir, node_id)
    if not node:
        return
    if node.get("node_type") == "analyze" and node.get("status") != "completed":
        from mdclaw._node import fail_node

        fail_node(job_dir, node_id, errors=errors)
