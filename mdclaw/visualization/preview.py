"""visualization.preview submodule (behavior-preserving split)."""

from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional
from mdclaw._common import (
    create_file_not_found_error,
    create_validation_error,
    ensure_directory,
    get_timeout,
)

from mdclaw.visualization._base import (
    _CAMERA_CHOICES,
    _STYLE_CHOICES,
    _SUPPORTED_STRUCTURE_SUFFIXES,
    _fail_preview_node_if_mutable,
    _is_supported_structure_path,
    _now_iso,
    _pymol_pml_preview,
    _pymol_selection_script,
    _register_preview_on_node,
    _resolve_structure_from_node,
    _run_pymol,
    _sanitize_name,
    _write_json,
)


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

