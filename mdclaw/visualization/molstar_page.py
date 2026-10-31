"""Self-contained Mol* playback page for clients that cannot open the MCP viewer.

Claude Code (terminal, Desktop Code tab, Remote Control) does not render MCP
Apps widgets, but it can publish an HTML page as a claude.ai Artifact. This
tool writes that page: the same DisplayData selection, wrap, sampling and
presentation as serve_molstar / molstar-mcp, with the retained display
coordinates embedded and Mol* loaded from jsDelivr pinned to the bundled build.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
from pathlib import Path

from mdclaw._common import create_validation_error
from mdclaw.visualization.molstar_display import _STAGES
from mdclaw.visualization.molstar_viewer import _ASSETS

# Artifacts allow 16 MB per page; leave room for HTML, CSS and the PDB.
_DEFAULT_MAX_BYTES = 8_000_000
_CDN = "https://cdn.jsdelivr.net/npm/molstar@{version}/build/viewer/molstar.js"


def _molstar_script() -> str:
    """Pinned jsDelivr tag whose SRI hash is computed from the bundled build."""
    provenance = json.loads((_ASSETS / "vendor/provenance.json").read_text())
    source = gzip.decompress((_ASSETS / "vendor/molstar.js.gz").read_bytes())
    if hashlib.sha256(source).hexdigest() != provenance["files"]["molstar.js"]["sha256"]:
        raise RuntimeError("Bundled Mol* build does not match its provenance record")
    integrity = "sha384-" + base64.b64encode(hashlib.sha384(source).digest()).decode()
    url = _CDN.format(version=provenance["version"])
    return f'<script src="{url}" integrity="{integrity}" crossorigin="anonymous"></script>'


def _data_block(element_id: str, mime: str, text: str) -> str:
    # Data never enters executable JS; '<' is escaped so no tag can close early.
    safe = text.replace("<", "\\u003c")
    return f'<script type="{mime}" id="{element_id}">{safe}</script>'


def _default_title(display) -> str:
    source = display.dcd or display.pdb
    parent = source.parent
    if parent.name == "artifacts" and parent.parent.parent.name == "nodes":
        job = parent.parent.parent.parent.name
        return f"{job} {parent.parent.name} Mol*"
    return f"{source.stem} Mol*"


def _node_inputs(job_dir: str, node_id: str) -> dict:
    """The structure, trajectory, stage and status of one DAG node, or an error."""
    from mdclaw.node.graph import find_ancestor_artifact
    from mdclaw.visualization._base import _read_node_if_present, _resolve_structure_from_node

    node = _read_node_if_present(job_dir, node_id)
    if node is None:
        return create_validation_error("node_id", f"No readable node {node_id} in {job_dir}",
                                       hints=["List node ids with: mdclaw inspect_job --job-dir <job_dir>"],
                                       code="molstar_node_not_found")
    status, node_type = node.get("status"), str(node.get("node_type") or "")
    stage = node_type if node_type in _STAGES - {"auto"} else "auto"
    # Read from disk: a running node registers its artifacts only when it finishes.
    trajectories = sorted(Path(job_dir, "nodes", node_id, "artifacts").glob("*trajectory.dcd"))
    if trajectories:
        topology = find_ancestor_artifact(job_dir, node_id, "topo", "topology_pdb")
        if topology:
            return {"structure": topology, "trajectory": str(trajectories[0]), "stage": stage, "status": status}
    if status != "completed":
        parents = [str(parent) for parent in node.get("parent_node_ids") or []]
        return {**create_validation_error(
            "node_id", f"{node_id} ({node_type}, status={status}) has no coordinates yet: only production "
            "writes a trajectory while running, and the other stages write theirs when they finish.",
            hints=["Show the parent node, or wait for this node to complete."],
            code="molstar_node_not_ready"),
            **({"next_action": f"mdclaw --job-dir {job_dir} --node-id {parents[0]} export_molstar_page"}
               if len(parents) == 1 else {})}
    structure, _, _, warnings = _resolve_structure_from_node(job_dir, node_id)
    if structure is None or structure.suffix.lower() != ".pdb":
        return create_validation_error("node_id", f"{node_id} has no PDB structure to display",
                                       hints=["A source node holds mmCIF; show its prep child, or render a PyMOL preview."],
                                       warnings=warnings, code="molstar_node_not_ready")
    return {"structure": str(structure), "trajectory": None, "stage": stage, "status": status}


def export_molstar_page(structure_file: str | None = None, trajectory_file: str | None = None,
                        output_file: str | None = None, stage: str = "auto", wrap: bool = True,
                        max_frames: int = 1000, fit: bool = False, title: str | None = None,
                        max_bytes: int = _DEFAULT_MAX_BYTES, job_dir: str | None = None,
                        node_id: str | None = None) -> dict:
    """Write a self-contained Mol* playback page (PDB or matching PDB+DCD).

    For Claude Code, which cannot render the MCP viewer: publish output_file
    as an Artifact and confirm the molecule and Play in the client.

    With --job-dir and --node-id the tool picks the files and stage of that
    node itself. A running production shows the frames written so far: run it
    again and republish the same Artifact to advance.

    Uses the same stage presentation, molecule wrap, production water removal,
    sampling and optional fit as serve_molstar. The retained display frames
    are embedded (12 bytes per atom per frame, base64); production sampling
    lowers frames further so they fit in max_bytes, and the result reports the
    final stride. Mol* 5.13.0 loads from jsDelivr with an SRI hash of the
    bundled build. The page is a complete HTML document that also opens in a
    browser. Sources are read only; output_file defaults to
    <topology stem>_<stage>_molstar.html (<job>_<node>_molstar.html for a node)
    in the current directory. Embedding puts display coordinates in the page:
    do not use this when the data must stay on the host.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        return create_validation_error("max_bytes", "Use a positive integer byte budget",
                                       code="invalid_parameter_value")
    from mdclaw.visualization.molstar_display import DisplayData

    if bool(job_dir) != bool(node_id):
        return create_validation_error("job_dir/node_id", "Pass both job_dir and node_id, or neither.",
                                       code="invalid_parameter_value")
    node_status = None
    if node_id:
        inputs = _node_inputs(job_dir, node_id)
        if "structure" not in inputs:
            return inputs
        structure_file = structure_file or inputs["structure"]
        trajectory_file = trajectory_file or inputs["trajectory"]
        stage = inputs["stage"] if stage == "auto" else stage
        node_status = inputs["status"]
    elif not structure_file:
        return create_validation_error("structure_file", "Pass structure_file, or job_dir with node_id.",
                                       code="invalid_parameter_value")

    def load(frames):
        return DisplayData(structure_file, trajectory_file, stage=stage, wrap=wrap,
                           max_frames=frames, fit=fit)

    try:
        display = load(max_frames)
        frame_bytes = display.info["frame_bytes"]
        per_frame = math.ceil(frame_bytes / 3) * 4
        budget_frames = max_bytes // per_frame
        if budget_frames < 1:
            raise ValueError(f"One frame of {display.info['atom_count']} atoms needs {per_frame} bytes, "
                             f"over max_bytes={max_bytes}")
        if display.info["frame_count"] > budget_frames:
            if not display.info["display"]["production"]:
                raise ValueError(f"{display.info['frame_count']} preparation frames need "
                                 f"{display.info['frame_count'] * per_frame} bytes, over max_bytes={max_bytes}")
            display = load(budget_frames)
        frames = b"".join(display.read(i) for i in range(display.info["frame_count"]))
        script = _molstar_script()
    except ValueError as exc:
        code = ("molstar_page_too_large" if "max_bytes" in str(exc)
                else "molstar_node_not_ready" if node_status == "running" and "no readable frames" in str(exc)
                else "molstar_input_invalid")
        hints = (["Raise max_bytes (pages over 16 MB cannot be published), lower max_frames, or use the MCP viewer / serve_molstar."]
                 if code == "molstar_page_too_large"
                 else ["Wait for the first trajectory frame, then run this again."] if code == "molstar_node_not_ready"
                 else ["Use a readable PDB and its matching DCD."])
        return create_validation_error("structure_file / trajectory_file", str(exc), hints=hints, code=code)
    except (OSError, RuntimeError) as exc:
        return create_validation_error("structure_file / trajectory_file", str(exc),
                                       hints=["Use a readable PDB and its matching DCD; reinstall MDClaw if bundled assets are missing."],
                                       code="molstar_input_invalid")

    summary = display.summary()
    stage_name = summary["display"]["stage"]
    default_name = (f"{Path(job_dir).resolve().name}_{node_id}" if node_id else f"{display.pdb.stem}_{stage_name}")
    out = Path(output_file).expanduser() if output_file else Path.cwd() / f"{default_name}_molstar.html"
    out = out.resolve()
    if out in {display.pdb, display.dcd}:
        return create_validation_error("output_file", "output_file would overwrite an input file",
                                       code="invalid_parameter_value")
    in_progress = node_status == "running"
    source_frames = summary["display"]["source_frames"]
    name = title or _default_title(display)
    if in_progress:
        name += f" (running, {source_frames} frames so far)"
    info = {**display.info, "page_title": name}
    css = gzip.decompress((_ASSETS / "vendor/molstar.css.gz").read_bytes()).decode()
    css += (_ASSETS / "viewer.css").read_text()
    html = (_ASSETS / "index.html").read_text()
    # Keep the shared viewer markup; drop the document shell for the Artifact wrapper.
    body = html[html.index("<body>") + len("<body>"):html.index("</body>")]
    body = body.replace('<span id="files"></span>', '<span id="files"></span><span id="page-title"></span>')
    body = body.replace('<output id="frame-label" for="frame"></output>',
                        '<output id="frame-label" for="frame"></output>'
                        '<label id="speed-label" for="speed">Speed <select id="speed">'
                        '<option value="10">10 fps</option><option value="20">20 fps</option>'
                        '<option value="30" selected>30 fps</option><option value="60">60 fps</option>'
                        '</select></label>')
    code = ((_ASSETS / "mcp-prototype.js").read_text() + "\n" + (_ASSETS / "page.js").read_text())
    escaped_title = name.replace("&", "&amp;").replace("<", "&lt;")
    # gzip takes the PDB from 88 to 17 bytes per atom: uncompressed, a full-system
    # page reaches the 16 MB limit near 110,000 atoms.
    pdb = gzip.compress(display.topology_text.encode(), mtime=0)
    page = "".join([
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
        f"<title>{escaped_title}</title>\n",
        '<meta name="mdclaw-ui-version" content="0.5.1">\n',
        f"<style>{css}#viewer {{ min-height: 360px; }} #speed-label {{ white-space: nowrap; }}</style>\n",
        "</head>\n<body>\n",
        body,
        _data_block("mdclaw-display", "application/json", json.dumps(info)), "\n",
        _data_block("mdclaw-pdb", "text/plain", base64.b64encode(pdb).decode()), "\n",
        _data_block("mdclaw-frames", "text/plain", base64.b64encode(frames).decode()), "\n",
        script, "\n",
        "<script>", code.replace("</script", "<\\/script"), "</script>\n",
        "</body>\n</html>\n",
    ])
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page)
    except OSError as exc:
        return create_validation_error("output_file", str(exc), code="invalid_parameter_value")
    size = out.stat().st_size
    warnings = list(summary["display"]["warnings"])
    if size > 16_000_000:
        warnings.append(f"Page is {size} bytes; Artifacts accept at most 16 MB")
    return {
        "success": True,
        "message": f"Mol* page written: {summary['atom_count']} atoms, {summary['frame_count']} frames, {size:,} bytes",
        "output_file": str(out),
        "page_bytes": size,
        "title": name,
        **summary,
        "warnings": warnings,
        "node_status": node_status,
        "in_progress": in_progress,
        "next_action": ("Publish output_file as an Artifact (Claude Code) or open it in a browser, then ask the "
                        "user to confirm the molecule appears and Play advances frames."
                        + (f" This is a snapshot of {source_frames} frames: run this again and republish the "
                           "same Artifact to advance it." if in_progress else "")),
    }
