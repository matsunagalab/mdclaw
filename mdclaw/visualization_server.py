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
    create_file_not_found_error,
    create_validation_error,
    ensure_directory,
    get_timeout,
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


def _coerce_json_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return dict(value)


def _coerce_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        value = parsed
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    return [str(item) for item in value]


def _coerce_findings(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"findings must be a JSON list: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("findings must be a list")
    coerced: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if isinstance(item, dict):
            coerced.append(dict(item))
        else:
            coerced.append({"description": str(item), "index": idx})
    return coerced


def _resolve_preview_image_from_node(
    job_dir: str,
    node_id: str,
    *,
    source_node_id: Optional[str] = None,
) -> tuple[Optional[Path], Optional[str], Optional[str], list[str]]:
    warnings: list[str] = []
    for candidate_id in _candidate_node_ids(job_dir, node_id, source_node_id):
        node = _read_node_if_present(job_dir, candidate_id)
        if not node:
            continue
        artifacts = node.get("artifacts", {})
        if not isinstance(artifacts, dict):
            continue
        for key in ("structure_preview_png", "preview_png", "output_png"):
            path = _artifact_to_path(job_dir, candidate_id, artifacts.get(key))
            if path is None:
                continue
            if path.suffix.lower() != ".png":
                continue
            if path.is_file():
                return path, candidate_id, key, warnings
            warnings.append(f"candidate preview image missing on disk: {candidate_id}:{key}")
    return None, None, None, warnings


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

    A completed prep/solv/topo/prod node may request a post-hoc preview
    attachment. Rendering failures must not rewrite that scientific node's
    status to failed.
    """
    node = _read_node_if_present(job_dir, node_id)
    if not node:
        return
    if node.get("node_type") == "analyze" and node.get("status") != "completed":
        from mdclaw._node import fail_node

        fail_node(job_dir, node_id, errors=errors)


def render_structure_preview(
    structure_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: Optional[str] = None,
    style: str = "overview",
    width: int = 1600,
    height: int = 1200,
    dpi: int = 150,
    ray: bool = True,
    background: str = "white",
    selection: Optional[str] = None,
    show_solvent: bool = False,
    show_ions: bool = True,
    show_lipids: bool = True,
    highlight_ligands: bool = True,
    camera_preset: str = "auto",
    zoom_buffer: float = 8.0,
    structure_artifact_key: Optional[str] = None,
    source_node_id: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Render a human-review PNG preview for a PDB/mmCIF structure.

    In node mode, pass ``--job-dir`` and ``--node-id``. The tool resolves a
    representative structure artifact from the current node, its parent, or
    ancestors, writes a rendered PNG under ``artifacts/previews/``, and records
    the preview as node artifacts. Use an ``analyze`` node for standalone
    preview generation; completed non-analyze nodes can receive a preview as a
    post-hoc attachment.
    """
    result: dict[str, Any] = {
        "success": False,
        "structure_file": None,
        "output_png": None,
        "structure_preview_png": None,
        "structure_preview_manifest": None,
        "manifest": None,
        "pymol_script": None,
        "pymol_pml": None,
        "source_node_id": source_node_id,
        "source_artifact_key": structure_artifact_key,
        "style": style,
        "camera_preset": camera_preset,
        "warnings": [],
        "errors": [],
    }

    if style not in _STYLE_CHOICES:
        return create_validation_error(
            "style",
            f"Unsupported preview style: {style}",
            expected=", ".join(sorted(_STYLE_CHOICES)),
            actual=style,
            code="preview_style_unsupported",
        )
    if camera_preset not in _CAMERA_CHOICES:
        return create_validation_error(
            "camera_preset",
            f"Unsupported camera preset: {camera_preset}",
            expected=", ".join(sorted(_CAMERA_CHOICES)),
            actual=camera_preset,
            code="preview_camera_preset_unsupported",
        )

    node_mode = bool(job_dir and node_id)
    if bool(job_dir) != bool(node_id):
        return create_validation_error(
            "job_dir/node_id",
            "Pass both job_dir and node_id for node mode, or neither for direct mode.",
            code="preview_node_context_incomplete",
        )

    resolved_source_node_id = source_node_id
    resolved_artifact_key = structure_artifact_key
    if structure_file is None and node_mode:
        resolved, resolved_source_node_id, resolved_artifact_key, warnings = _resolve_structure_from_node(
            job_dir or "",
            node_id or "",
            source_node_id=source_node_id,
            structure_artifact_key=structure_artifact_key,
        )
        result["warnings"].extend(warnings)
        if resolved is not None:
            structure_path = resolved
        else:
            return create_validation_error(
                "structure_file",
                "No supported PDB/mmCIF structure artifact could be resolved from the node DAG.",
                expected="A node artifact ending in .pdb, .cif, .mmcif, or .ent",
                actual=structure_artifact_key,
                hints=[
                    "Pass --structure-file explicitly, or pass --structure-artifact-key for a known artifact.",
                    "For workflow use, create an analyze node parented to the prod/analyze node to preview.",
                ],
                warnings=result["warnings"],
                code="preview_structure_artifact_missing",
            )
    elif structure_file is None:
        return create_validation_error(
            "structure_file",
            "structure_file is required in direct mode.",
            code="preview_structure_file_required",
        )
    else:
        structure_path = Path(structure_file).expanduser().resolve(strict=False)

    if not structure_path.is_file():
        return create_file_not_found_error(str(structure_path), "structure file")
    if not _is_supported_structure_path(structure_path):
        return create_validation_error(
            "structure_file",
            "Unsupported structure format for PyMOL preview.",
            expected=", ".join(sorted(_SUPPORTED_STRUCTURE_SUFFIXES)),
            actual=structure_path.suffix,
            code="preview_structure_format_unsupported",
        )

    if output_dir:
        out_dir = ensure_directory(Path(output_dir).expanduser())
    elif node_mode:
        out_dir = ensure_directory(Path(job_dir or "") / "nodes" / (node_id or "") / "artifacts" / "previews")
    else:
        out_dir = ensure_directory(Path.cwd() / "structure_previews")

    base = _sanitize_name(output_name or f"{structure_path.stem}.{style}")
    output_png = out_dir / f"{base}.preview.png"
    pymol_py = out_dir / f"{base}.preview.py"
    pymol_pml = out_dir / f"{base}.preview.pml"
    view_json = out_dir / f"{base}.view.json"
    manifest_file = out_dir / f"{base}.preview_manifest.json"

    script = _pymol_selection_script(
        structure_file=structure_path,
        output_png=output_png,
        view_json=view_json,
        width=width,
        height=height,
        dpi=dpi,
        ray=ray,
        style=style,
        selection=selection,
        background=background,
        show_solvent=show_solvent,
        show_ions=show_ions,
        show_lipids=show_lipids,
        highlight_ligands=highlight_ligands,
        camera_preset=camera_preset,
        zoom_buffer=zoom_buffer,
    )
    pymol_py.write_text(script)
    pymol_pml.write_text(
        _pymol_pml_preview(
            structure_file=structure_path,
            output_png=output_png,
            style=style,
            camera_preset=camera_preset,
            zoom_buffer=zoom_buffer,
        )
    )

    try:
        completed = _run_pymol(pymol_py, get_timeout("visualization"))
    except FileNotFoundError:
        if node_mode:
            _fail_preview_node_if_mutable(
                job_dir or "",
                node_id or "",
                ["pymol executable not found in PATH"],
            )
        return create_validation_error(
            "pymol",
            "PyMOL executable not found in PATH.",
            expected="pymol command available, e.g. conda install -c conda-forge pymol-open-source",
            code="pymol_not_available",
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"PyMOL rendering timed out after {exc.timeout} seconds"
        if node_mode:
            _fail_preview_node_if_mutable(job_dir or "", node_id or "", [msg])
        return create_validation_error("pymol", msg, code="pymol_render_timeout")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        msg = f"PyMOL rendering failed: {stderr or exc}"
        if node_mode:
            _fail_preview_node_if_mutable(job_dir or "", node_id or "", [msg])
        return create_validation_error(
            "pymol",
            msg,
            code="pymol_render_failed",
            context_extra={"stderr": stderr, "stdout": (exc.stdout or "").strip()},
        )

    if not output_png.is_file():
        msg = "PyMOL completed but did not produce the expected PNG."
        if node_mode:
            _fail_preview_node_if_mutable(job_dir or "", node_id or "", [msg])
        return create_validation_error(
            "output_png",
            msg,
            expected=str(output_png),
            code="pymol_preview_missing_output",
        )

    view_data: dict[str, Any] = {}
    if view_json.is_file():
        try:
            view_data = json.loads(view_json.read_text())
        except json.JSONDecodeError:
            result["warnings"].append(f"could not parse PyMOL view JSON: {view_json}")

    manifest = {
        "created_at": _now_iso(),
        "tool": "render_structure_preview",
        "structure_file": str(structure_path),
        "source_node_id": resolved_source_node_id,
        "source_artifact_key": resolved_artifact_key,
        "output_png": str(output_png),
        "pymol_script": str(pymol_py),
        "pymol_pml": str(pymol_pml),
        "style": style,
        "camera_preset": camera_preset,
        "selection": selection,
        "rendering": {
            "width": width,
            "height": height,
            "dpi": dpi,
            "ray": ray,
            "background": background,
            "zoom_buffer": zoom_buffer,
        },
        "representations": {
            "protein": "cartoon",
            "nucleic": "cartoon",
            "ligand": "sticks" if highlight_ligands else "hidden",
            "water": "dots" if show_solvent else "hidden",
            "ions": "spheres" if show_ions else "hidden",
            "lipids": "sticks" if show_lipids else "hidden",
        },
        "pymol": {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        "view": view_data.get("view"),
        "warnings": result["warnings"],
    }
    _write_json(manifest_file, manifest)

    result.update({
        "success": True,
        "structure_file": str(structure_path),
        "output_png": str(output_png),
        "structure_preview_png": str(output_png),
        "structure_preview_manifest": str(manifest_file),
        "manifest": str(manifest_file),
        "pymol_script": str(pymol_py),
        "pymol_pml": str(pymol_pml),
        "source_node_id": resolved_source_node_id,
        "source_artifact_key": resolved_artifact_key,
    })

    if node_mode:
        node_dir = Path(job_dir or "") / "nodes" / (node_id or "")

        def rel(path: Path) -> str:
            return os.path.relpath(path.resolve(), node_dir.resolve())

        artifacts = {
            "structure_preview_png": rel(output_png),
            "structure_preview_manifest": rel(manifest_file),
            "structure_preview_pymol_script": rel(pymol_py),
            "structure_preview_pymol_pml": rel(pymol_pml),
        }
        metadata = {
            "tool": "render_structure_preview",
            "analysis_type": "structure_preview",
            "preview": {
                "source_node_id": resolved_source_node_id,
                "source_artifact_key": resolved_artifact_key,
                "style": style,
                "camera_preset": camera_preset,
                "output_png": rel(output_png),
            },
        }
        try:
            _register_preview_on_node(
                job_dir=job_dir or "",
                node_id=node_id or "",
                artifacts=artifacts,
                metadata=metadata,
                warnings=result["warnings"],
            )
        except Exception as exc:  # noqa: BLE001
            result["success"] = False
            result["errors"].append(f"failed to register preview artifacts: {type(exc).__name__}: {exc}")

            _fail_preview_node_if_mutable(job_dir or "", node_id or "", result["errors"])

    return result


def register_visual_review(
    image_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "visual_review",
    reviewer_type: str = "not_available",
    severity: str = "not_reviewed",
    recommendation: str = "manual_review",
    summary: Optional[str] = None,
    checks: Optional[dict[str, Any]] = None,
    findings: Optional[list[dict[str, Any]]] = None,
    limitations: Optional[list[str]] = None,
    source_node_id: Optional[str] = None,
    source_artifact_key: Optional[str] = None,
    reviewer_model: Optional[str] = None,
    review_prompt: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Register a best-effort visual QA review for a preview PNG.

    The tool does not perform image understanding. A multimodal LLM or human
    reviews the PNG first, then this function records the outcome as a
    ``visual_review_json`` artifact. The review is only a coarse accident check
    and must not be treated as scientific validation.
    """
    result: dict[str, Any] = {
        "success": False,
        "visual_review_json": None,
        "image_path": image_path,
        "reviewer_type": reviewer_type,
        "severity": severity,
        "recommendation": recommendation,
        "requires_user_confirmation": False,
        "warnings": [],
        "errors": [],
    }

    if reviewer_type not in _VISUAL_REVIEWER_TYPES:
        return create_validation_error(
            "reviewer_type",
            f"Unsupported visual reviewer type: {reviewer_type}",
            expected=", ".join(sorted(_VISUAL_REVIEWER_TYPES)),
            actual=reviewer_type,
            code="visual_review_reviewer_type_unsupported",
        )
    if severity not in _VISUAL_REVIEW_SEVERITIES:
        return create_validation_error(
            "severity",
            f"Unsupported visual review severity: {severity}",
            expected=", ".join(sorted(_VISUAL_REVIEW_SEVERITIES)),
            actual=severity,
            code="visual_review_severity_unsupported",
        )
    if recommendation not in _VISUAL_REVIEW_RECOMMENDATIONS:
        return create_validation_error(
            "recommendation",
            f"Unsupported visual review recommendation: {recommendation}",
            expected=", ".join(sorted(_VISUAL_REVIEW_RECOMMENDATIONS)),
            actual=recommendation,
            code="visual_review_recommendation_unsupported",
        )

    node_mode = bool(job_dir and node_id)
    if bool(job_dir) != bool(node_id):
        return create_validation_error(
            "job_dir/node_id",
            "Pass both job_dir and node_id for node mode, or neither for direct mode.",
            code="visual_review_node_context_incomplete",
        )

    resolved_source_node_id = source_node_id
    resolved_artifact_key = source_artifact_key
    if image_path is None and node_mode:
        resolved, resolved_source_node_id, resolved_artifact_key, warnings = (
            _resolve_preview_image_from_node(
                job_dir or "",
                node_id or "",
                source_node_id=source_node_id,
            )
        )
        result["warnings"].extend(warnings)
        if resolved is not None:
            image = resolved
        else:
            image = None
            result["warnings"].append(
                "No structure_preview_png artifact could be resolved; recording review without image."
            )
    elif image_path is None:
        image = None
    else:
        image = Path(image_path).expanduser().resolve(strict=False)
        if not image.is_file():
            return create_file_not_found_error(str(image), "preview image")

    try:
        merged_checks = {
            **_VISUAL_REVIEW_DEFAULT_CHECKS,
            **_coerce_json_object(checks, "checks"),
        }
        review_findings = _coerce_findings(findings)
        review_limitations = _coerce_string_list(limitations, "limitations")
    except ValueError as exc:
        return create_validation_error(
            "visual_review",
            str(exc),
            code="visual_review_payload_invalid",
        )

    if not review_limitations:
        review_limitations = [
            "Visual QA is a coarse image-based accident check, not scientific validation.",
            "The reviewer must not infer force-field, protonation, or parameter correctness from the image alone.",
        ]
    if reviewer_type == "not_available":
        review_limitations.append(
            "No image-capable reviewer was available; the preview path should be shown to a human."
        )

    requires_user_confirmation = severity == "high" or recommendation in {"user_confirm", "blocked"}
    if reviewer_type == "not_available" and severity != "not_reviewed":
        result["warnings"].append(
            "reviewer_type='not_available' usually pairs with severity='not_reviewed'."
        )

    if output_dir:
        out_dir = ensure_directory(Path(output_dir).expanduser())
    elif node_mode:
        out_dir = ensure_directory(Path(job_dir or "") / "nodes" / (node_id or "") / "artifacts" / "previews")
    else:
        out_dir = ensure_directory(Path.cwd() / "structure_previews")

    base = _sanitize_name(output_name or "visual_review")
    review_path = out_dir / f"{base}.visual_review.json"
    review = {
        "success": True,
        "created_at": _now_iso(),
        "tool": "register_visual_review",
        "reviewer_type": reviewer_type,
        "reviewer_model": reviewer_model,
        "image_path": str(image) if image is not None else None,
        "source_node_id": resolved_source_node_id,
        "source_artifact_key": resolved_artifact_key,
        "checks": merged_checks,
        "findings": review_findings,
        "severity": severity,
        "recommendation": recommendation,
        "requires_user_confirmation": requires_user_confirmation,
        "summary": summary,
        "limitations": review_limitations,
        "review_prompt": review_prompt,
        "warnings": result["warnings"],
    }
    _write_json(review_path, review)

    result.update({
        "success": True,
        "visual_review_json": str(review_path),
        "image_path": str(image) if image is not None else None,
        "source_node_id": resolved_source_node_id,
        "source_artifact_key": resolved_artifact_key,
        "requires_user_confirmation": requires_user_confirmation,
    })

    if node_mode:
        node_dir = Path(job_dir or "") / "nodes" / (node_id or "")

        def rel(path: Path) -> str:
            return os.path.relpath(path.resolve(), node_dir.resolve())

        image_metadata_path = None
        if image is not None:
            image_metadata_path = rel(image) if image.resolve().is_relative_to(node_dir.resolve()) else str(image)

        artifacts = {"visual_review_json": rel(review_path)}
        metadata = {
            "tool": "register_visual_review",
            "analysis_type": "visual_review",
            "visual_review": {
                "reviewer_type": reviewer_type,
                "severity": severity,
                "recommendation": recommendation,
                "requires_user_confirmation": requires_user_confirmation,
                "image_path": image_metadata_path,
                "visual_review_json": rel(review_path),
            },
        }
        try:
            _register_preview_on_node(
                job_dir=job_dir or "",
                node_id=node_id or "",
                artifacts=artifacts,
                metadata=metadata,
                warnings=result["warnings"],
            )
        except Exception as exc:  # noqa: BLE001
            result["success"] = False
            result["errors"].append(f"failed to register visual review: {type(exc).__name__}: {exc}")
            _fail_preview_node_if_mutable(job_dir or "", node_id or "", result["errors"])

    return result


TOOLS = {
    "render_structure_preview": render_structure_preview,
    "register_visual_review": register_visual_review,
}
