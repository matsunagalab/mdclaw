"""Molstar MCP Apps server: UI -> MCP tool -> one host-side coordinate frame.

Run with `mdclaw molstar-mcp` in the standard MDClaw runtime. Select files
with open_molstar_probe after connecting; no HTTP listener is opened.
The historical module/tool names remain compatible with existing connections.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import os
from pathlib import Path
import secrets
import time

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from mdclaw.visualization.molstar_viewer import _ASSETS
from mdclaw.visualization.molstar_display import DisplayData

URI = "ui://mdclaw/molstar-probe.html"
MIME = "text/html;profile=mcp-app"


def make_html() -> str:
    """Bundle viewer code only. No PDB or DCD is embedded in this resource."""
    html = (_ASSETS / "index.html").read_text()
    css = gzip.decompress((_ASSETS / "vendor/molstar.css.gz").read_bytes()).decode()
    css += (_ASSETS / "viewer.css").read_text()
    packed = base64.b64encode((_ASSETS / "vendor/molstar.js.gz").read_bytes()).decode()
    bootstrap = """
    (async () => {
        try {
            const bytes = Uint8Array.from(atob('PACKED'), c => c.charCodeAt(0));
            const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
            const script = document.createElement('script');
            script.textContent = await new Response(stream).text();
            document.head.append(script);
            await startMolstarProbe();
        } catch (error) {
            document.getElementById('status').textContent = 'Probe failed: ' + error.message;
        }
    })();
    """.replace("PACKED", packed)
    js = (_ASSETS / "mcp-prototype.js").read_text()
    html = html.replace('<link rel="stylesheet" href="molstar.css">', "<style>" + css + "</style>")
    html = html.replace('<link rel="stylesheet" href="viewer.css">', "")
    html = html.replace('<script src="molstar.js" defer></script>', "")
    html = html.replace('<script src="viewer.js" defer></script>', "")
    html = html.replace('<script src="presentation.js" defer></script>', "")
    html = html.replace('<div id="status"', '<div>PROTOTYPE · MCP frame streaming</div><div id="transport" role="status">Waiting for MCP host…</div><div id="status"')
    # Escape script closing sequences in code; never insert data into executable JS.
    html = html.replace("</body>", "<script>" + (js + bootstrap).replace("</script", "<\\/script") + "</script></body>")
    license_text = (_ASSETS / "vendor/LICENSE").read_text()
    return html + "\n<!-- Molstar MIT license\n" + license_text.replace("--", "- -") + "\n-->"


class _Dataset(DisplayData):
    def __init__(self, pdb, dcd=None, **options):
        super().__init__(pdb, dcd, **options)
        self.id = secrets.token_urlsafe(24)
        self.info = {**self.info, "dataset_id": self.id}

    def payload(self, kind, index=None):
        self.check_unchanged()
        if kind == "structure":
            data = {"info": self.info, "pdb": self.topology_text}
            size = len(self.topology_text.encode())
        elif kind == "frame":
            frame = self.read(index)
            size = len(frame)
            data = {"index": index, "source_index": self.indices[index],
                    "coordinates": base64.b64encode(frame).decode()}
        else:
            raise ValueError("Unknown data kind")
        self.check_unchanged()
        return {**data, "dataset_id": self.id}, size


def build_server(pdb: str | None = None, dcd: str | None = None):
    if dcd and not pdb:
        raise ValueError("trajectory_file requires structure_file")
    default = _Dataset(pdb, dcd) if pdb else None
    datasets = {default.id: default} if default else {}
    server = Server("mdclaw-molstar-prototype", version="0.4.0")
    requests = 0
    annotations = types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                        idempotentHint=True, openWorldHint=False)

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(name="open_molstar_probe", title="Open Molstar MCP prototype",
                       description="Open a host-side PDB, optionally with DCD, in Molstar. Pass absolute paths for a new target or omit both for the startup target. stage controls preparation versus production representation. Whole-molecule wrap defaults on; production hides water and samples at most max_frames. fit defaults off. Only requested frames are transferred.",
                       inputSchema={"type": "object", "properties": {
                           "structure_file": {"type": "string", "minLength": 1, "description": "Absolute host path to the matching PDB topology"},
                           "trajectory_file": {"type": "string", "minLength": 1, "description": "Absolute host path to the DCD"},
                           "stage": {"type": "string", "enum": ["auto", "source", "prep", "solv", "topo", "min", "eq", "prod", "analyze"], "default": "auto"},
                           "wrap": {"type": "boolean", "default": True},
                           "max_frames": {"type": "integer", "minimum": 0, "default": 1000, "description": "Production preview frame limit; 0 keeps all"},
                           "fit": {"type": "boolean", "default": False, "description": "Fit protein backbone to iterative sampled average only on explicit request"}},
                           "additionalProperties": False},
                       annotations=annotations,
                       _meta={"ui": {"resourceUri": URI}, "openai/outputTemplate": URI,
                              "openai/widgetAccessible": True}),
            types.Tool(name="molstar_probe_data", title="Read one Molstar probe frame",
                       description="UI-only read of an already selected dataset. Does not accept file paths.",
                       inputSchema={"type": "object", "properties": {
                           "kind": {"type": "string", "enum": ["structure", "frame"]},
                           "dataset_id": {"type": "string"},
                           "index": {"type": "integer", "minimum": 0}},
                           "required": ["kind"], "additionalProperties": False},
                       annotations=annotations,
                       _meta={"ui": {"resourceUri": URI, "visibility": ["app"]},
                              "openai/widgetAccessible": True}),
        ]

    @server.list_resources()
    async def list_resources():
        return [types.Resource(uri=URI, name="Molstar MCP prototype", mimeType=MIME)]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != URI:
            raise ValueError("Unknown resource")
        return [ReadResourceContents(content=make_html(), mime_type=MIME,
                                     meta={"ui": {"prefersBorder": True,
                                                  "csp": {"connectDomains": [], "resourceDomains": []}}})]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        nonlocal requests
        if name == "open_molstar_probe":
            if not arguments.get("structure_file") and default is None:
                raise ValueError("Choose a molecule with an absolute structure_file path; trajectory_file is optional")
            if arguments.get("trajectory_file") and not arguments.get("structure_file"):
                raise ValueError("trajectory_file requires structure_file")
            for key in ("structure_file", "trajectory_file"):
                if arguments.get(key) and not Path(arguments[key]).is_absolute():
                    raise ValueError("Use absolute host paths")
            options = {key: arguments[key] for key in ("stage", "wrap", "max_frames", "fit") if key in arguments}
            if arguments.get("structure_file") or options:
                selected = _Dataset(arguments.get("structure_file") or str(default.pdb),
                                    arguments.get("trajectory_file") if arguments.get("structure_file") else default.dcd,
                                    **options)
                selected = next((item for item in datasets.values()
                                 if (item.pdb, item.dcd, item.signatures, item.options) ==
                                 (selected.pdb, selected.dcd, selected.signatures, selected.options)), selected)
                datasets[selected.id] = selected
            else:
                selected = default
                selected.check_unchanged()
            info = selected.summary()
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Molstar MCP prototype: {info['structure_name']} / {info['trajectory_name']}. Use the attached interactive viewer.")],
                                        structuredContent={**info, "transport": "MCP tools/call"})
        if name != "molstar_probe_data":
            raise ValueError("Unknown tool")
        # No token is the startup default, for older UI instances only. Never
        # use a mutable global 'current molecule' shared by multiple widgets.
        selected = datasets.get(arguments.get("dataset_id", default.id if default else None))
        if selected is None:
            raise ValueError("Unknown dataset; call open_molstar_probe again")
        start = time.perf_counter()
        data, size = selected.payload(arguments["kind"], arguments.get("index"))
        requests += 1
        # Binary/topology data are UI metadata, not LLM-readable tool content.
        return types.CallToolResult(content=[], structuredContent={
            "request": requests, "bytes": size,
            "host_read_ms": round((time.perf_counter() - start) * 1000, 2)},
            _meta={"mdclaw": data})

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure-file", help="Optional startup structure; normally selected after connecting")
    parser.add_argument("--trajectory-file")
    args = parser.parse_args(argv)
    if args.trajectory_file and not args.structure_file:
        parser.error("--trajectory-file requires --structure-file")
    # DCD's native library writes diagnostics to fd 1. Keep those off MCP stdout.
    protocol_stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)

    async def run():
        server = build_server(args.structure_file, args.trajectory_file)
        async with stdio_server(stdout=anyio.wrap_file(protocol_stdout)) as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
