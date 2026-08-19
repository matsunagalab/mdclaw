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
    "system_box",
    "topology_check",
}

# A molecular surface over every water in a solvated membrane box is minutes of
# work for a picture whose point is the envelope. Past this many solvent atoms
# the preview falls back to dots and says so.
_SOLVENT_SURFACE_MAX_ATOMS = 400000

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


def _event_registered_artifacts(job_dir: str, node_id: str) -> dict:
    """Artifacts attached to a sealed node through preview_registered events.

    Terminal node.json records are immutable, so post-hoc preview and review
    attachments live in the append-only event log; later events win.
    """
    events_dir = Path(job_dir) / "events"
    if not events_dir.is_dir():
        return {}
    merged: dict = {}
    for path in sorted(events_dir.glob(f"*_{node_id}_preview_registered_*.json")):
        try:
            event = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if event.get("node_id") != node_id:
            continue
        artifacts = (event.get("details") or {}).get("artifacts")
        if isinstance(artifacts, dict):
            merged.update(artifacts)
    return merged


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
    orthogonal_png: Optional[Path] = None,
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
cmd.set("depth_cue", 0)
# Orthographic: a box drawn in perspective has no two edges the same length, so
# the viewer cannot read distances or judge whether anything crosses a face.
cmd.set("orthoscopic", 1)
cmd.set("ray_trace_mode", 0)
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
solvent_representation = "dots"
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
# The periodic cell, when the structure carries one. It is the only thing in the
# picture that shows whether the system actually fits in its box.
#
# Drawn around the system's own centre rather than with cmd.show("cell"), which
# puts it at the crystallographic origin: an MD box is written with its solute
# centred, so a cell hanging off one corner tells the viewer nothing about
# whether anything fits inside it.
def draw_periodic_cell():
    try:
        symmetry = cmd.get_symmetry("structure")
    except Exception:
        return "no symmetry recorded"
    if not symmetry:
        return "no symmetry recorded"
    a, b, c = (float(v) for v in symmetry[:3])
    alpha, beta, gamma = (float(v) for v in symmetry[3:6])
    if not all(v > 0.0 for v in (a, b, c)):
        return "cell lengths are not positive"
    # Only orthorhombic cells are drawn. Reading a, b, c and ignoring the
    # angles would render a triclinic box as a rectangular one whose faces are
    # in the wrong places — a picture that answers "does this fit" incorrectly
    # is worse than no picture.
    if max(abs(alpha - 90.0), abs(beta - 90.0), abs(gamma - 90.0)) > 0.5:
        print(
            "periodic cell not drawn: angles "
            f"{{alpha:.1f}}/{{beta:.1f}}/{{gamma:.1f}} are not orthorhombic"
        )
        return f"omitted (triclinic {{alpha:.1f}}/{{beta:.1f}}/{{gamma:.1f}})"
    # Centre the cell on the material that fills it — the solvent and lipids —
    # not on everything. Centring on the whole system lets a protein reaching
    # out of the box drag the box after it, so the drawn cell no longer lines up
    # with the slab that defines it and the picture stops meaning anything.
    bulk = "solvent_sel or lipid_sel"
    reference = bulk if cmd.count_atoms(bulk) > 0 else "structure"
    extent = cmd.get_extent(reference)
    cx = 0.5 * (extent[0][0] + extent[1][0])
    cy = 0.5 * (extent[0][1] + extent[1][1])
    cz = 0.5 * (extent[0][2] + extent[1][2])
    xs = (cx - a / 2.0, cx + a / 2.0)
    ys = (cy - b / 2.0, cy + b / 2.0)
    zs = (cz - c / 2.0, cz + c / 2.0)
    corners = [(x, y, z) for x in xs for y in ys for z in zs]
    edges = [
        (0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3),
        (2, 6), (3, 7), (4, 5), (4, 6), (5, 7), (6, 7),
    ]
    obj = []
    for i, j in edges:
        obj += [2.0, 1.0]                 # BEGIN, LINES
        obj += [6.0, 0.10, 0.60, 0.10]    # COLOR
        obj += [4.0, *corners[i]]         # VERTEX
        obj += [4.0, *corners[j]]
        obj += [3.0]                      # END
    cmd.load_cgo(obj, "periodic_cell")
    cmd.set("cgo_line_width", 2.0, "periodic_cell")
    return f"drawn ({{a:.1f}} x {{b:.1f}} x {{c:.1f}} A)"

cell_state = draw_periodic_cell()
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
elif style == "system_box":
    # The assembled system as built: protein by chain, bilayer as sticks, water
    # as a transparent envelope, ions as spheres, and the periodic cell drawn
    # around all of it. The envelope is this style's alone — leaving it in the
    # generic block put a surface under every other style's dots.
    if has("solvent_sel") and {str(show_solvent)}:
        cmd.hide("dots", "solvent_sel")
        if cmd.count_atoms("solvent_sel") <= {_SOLVENT_SURFACE_MAX_ATOMS}:
            # PyMOL flags solvent "ignore", which excludes it from surfaces.
            cmd.flag("ignore", "solvent_sel", "clear")
            cmd.set("surface_quality", 0)
            cmd.set("transparency", 0.78, "solvent_sel")
            cmd.show("surface", "solvent_sel")
            solvent_representation = "transparent surface"
        else:
            cmd.show("dots", "solvent_sel")
            solvent_representation = "dots (too many atoms to surface)"
        cmd.color("skyblue", "solvent_sel")
    if has("lipid_sel") and {str(show_lipids)}:
        cmd.show("sticks", "lipid_sel")
        cmd.set("stick_transparency", 0.0, "lipid_sel")
    if has("ion_sel") and {str(show_ions)}:
        cmd.show("spheres", "ion_sel")
elif style == "topology_check":
    cmd.show("lines", "all")
    cmd.show("sticks", "not solvent_sel")
    cmd.set("stick_radius", 0.10)

# Two axis-aligned views rather than one oriented one. A view down x shows the
# bilayer edge-on — where a protein leaves the box, where the water layers are
# thin — and a view down z shows the patch from above, where a lateral gap or a
# protein too close to its own lateral image is visible instead.
# Axis-aligned views set by the rotation matrix rather than by turns: the rows
# are the camera's right, up and out-of-screen axes in world coordinates, so
# asking for "down x with z up" is one literal instead of a sequence of turns
# whose composition has to be rediscovered every time.
VIEW_ALONG_X = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
VIEW_ALONG_Z = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

def set_axis_view(rotation):
    cmd.reset()
    cmd.zoom("all", {zoom_buffer})
    view = list(cmd.get_view())
    view[:9] = rotation
    cmd.set_view(view)
    cmd.zoom("all", {zoom_buffer})

axis_views = {bool(orthogonal_png)} and style == "system_box"
if axis_views:
    set_axis_view(VIEW_ALONG_X)           # membrane normal (z) vertical
else:
    cmd.orient("visible")
    if user_selection and has("user_focus_sel"):
        cmd.center("user_focus_sel")
        cmd.zoom("user_focus_sel", {zoom_buffer})
    elif (camera == "ligand_site" or style == "ligand_site") and has("ligand_sel"):
        cmd.center("ligand_sel")
        cmd.zoom("ligand_sel", {zoom_buffer})
    elif style == "system_box":
        cmd.orient("protein_sel or nucleic_sel or lipid_sel")
        cmd.turn("x", 90)
        cmd.zoom("all", {zoom_buffer})
    elif (camera == "membrane" or style == "membrane") and has("lipid_sel"):
        cmd.orient("protein_sel or nucleic_sel or ligand_sel or lipid_sel")
        cmd.turn("x", 90)
        cmd.zoom("visible", {zoom_buffer})
    else:
        cmd.center("visible")
        cmd.zoom("visible", {zoom_buffer})

view = list(cmd.get_view())
effective = {{
    "solvent": (
        solvent_representation if has("solvent_sel") and {str(show_solvent)}
        else "hidden"
    ),
    "lipids": (
        "sticks" if has("lipid_sel") and {str(show_lipids)} else "hidden"
    ),
    "ions": "spheres" if has("ion_sel") and {str(show_ions)} else "hidden",
    "periodic_cell": cell_state,
}}
with open({json.dumps(str(view_json))}, "w") as fh:
    json.dump({{"view": view, "effective_representations": effective}}, fh, indent=2)

cmd.png(
    {json.dumps(str(output_png))},
    width={width},
    height={height},
    dpi={dpi},
    ray={1 if ray else 0},
)

# A second view down an orthogonal axis. One projection hides whatever lines up
# with it — a protein leaning out of the box, a gap on one face, lipids missing
# from one edge — and a membrane picture is exactly where that happens, because
# everything interesting is stacked along one axis.
orthogonal_png = {json.dumps(str(orthogonal_png) if orthogonal_png else "")}
if orthogonal_png and axis_views:
    set_axis_view(VIEW_ALONG_Z)           # looking down the membrane normal
    cmd.png(
        orthogonal_png,
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
    """A hand-editable starting point, not a reproduction of the render.

    It carries the generic representations and one oriented view; the rendered
    PNGs come from the generated Python next to it, which is what to read to
    reproduce a picture exactly.
    """
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

    # Terminal node.json records are sealed; the sanctioned channel for
    # post-hoc attachments is the append-only event log. The artifact files
    # themselves already live under the node's artifacts/ directory.
    from mdclaw._event import write_event

    write_event(
        job_dir,
        node_id,
        "preview_registered",
        success=True,
        details={
            "tool": str(metadata.get("tool") or "render_structure_preview"),
            "artifacts": artifacts,
            "metadata": metadata,
            "warnings": list(warnings or []),
        },
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
