"""Local, read-only Mol* viewer shared by desktop agent browsers."""

from __future__ import annotations

import json
import math
import os
import secrets
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from mdclaw._common import create_validation_error

_ASSETS = Path(__file__).with_name("molstar")


def _signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _public_origin(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ValueError("public_origin must be an HTTPS origin, e.g. https://viewer.example.org")
    # Also validates malformed/out-of-range ports.
    _ = parsed.port
    return f"https://{parsed.netloc}"


def _inspect_inputs(structure_file: str, trajectory_file: str | None) -> tuple[dict, dict]:
    """Inspect without rewriting, stripping, reordering, or fitting any atoms."""
    pdb = Path(structure_file).expanduser().resolve(strict=True)
    if not pdb.is_file() or pdb.suffix.lower() != ".pdb":
        raise ValueError("structure_file must be a PDB file")
    count, models, alternate = 0, 0, False
    with pdb.open() as handle:
        for line in handle:
            if line.startswith("MODEL "):
                models += 1
            if line.startswith(("ATOM  ", "HETATM")):
                if models <= 1:
                    count += 1
                alternate |= bool(line[16:17].strip())
                if not all(math.isfinite(float(line[start:start + 8])) for start in (30, 38, 46)):
                    raise ValueError("PDB contains non-finite coordinates")
    if count == 0:
        raise ValueError("PDB contains no ATOM/HETATM records")
    files = {"structure.pdb": (pdb, "chemical/x-pdb", None)}
    info = {"structure_name": pdb.name, "atom_count": count, "trajectory_name": None,
            "frame_count": max(1, models), "molstar_version": "5.13.0"}
    if trajectory_file:
        if alternate or models > 1:
            raise ValueError("DCD needs a single-model PDB without alternate locations; use its MD topology PDB")
        dcd = Path(trajectory_file).expanduser().resolve(strict=True)
        if not dcd.is_file() or dcd.suffix.lower() != ".dcd":
            raise ValueError("trajectory_file must be a DCD file")
        from mdtraj.formats import DCDTrajectoryFile

        with DCDTrajectoryFile(str(dcd)) as trajectory:
            frames = len(trajectory)
            xyz, _, _ = trajectory.read(n_frames=1)
        if frames < 1 or len(xyz) != 1:
            raise ValueError("DCD contains no readable frames")
        if xyz.shape[1] != count:
            raise ValueError(f"PDB has {count} atoms but DCD has {xyz.shape[1]}; use the matching topology PDB")
        files["trajectory.dcd"] = (dcd, "application/octet-stream", None)
        info.update(trajectory_name=dcd.name, frame_count=frames,
                    frame_format="float32-le-xyz-planar-angstrom", frame_bytes=count * 12)
    return info, files


def _make_server(info: dict, files: dict, port: int,
                 public_origin: str | None = None, display=None) -> ThreadingHTTPServer:
    """Expose only the explicit input files and bundled assets, under a random URL."""
    if display is None:
        from mdclaw.visualization.molstar_display import DisplayData
        display = DisplayData(files["structure.pdb"][0], files.get("trajectory.dcd", (None,))[0])
    info = display.info
    prefix = "/" + secrets.token_urlsafe(24) + "/"
    # The original DCD is deliberately not an HTTP route.
    routes = {name: value for name, value in files.items() if name != "trajectory.dcd"}
    for name, mime in (("index.html", "text/html; charset=utf-8"),
                       ("viewer.js", "text/javascript; charset=utf-8"),
                       ("presentation.js", "text/javascript; charset=utf-8"),
                       ("viewer.css", "text/css; charset=utf-8")):
        routes[name] = (_ASSETS / ("mcp-prototype.js" if name == "presentation.js" else name), mime, None)
    for name, mime in (("molstar.js", "text/javascript"), ("molstar.css", "text/css")):
        routes[name] = (_ASSETS / "vendor" / (name + ".gz"), mime, "gzip")
    # A trajectory still being written must not change underneath a browser load.
    signatures = {name: _signature(p) for name, (p, _, _) in routes.items()}
    manifest = json.dumps(info).encode()
    reader = display

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Do not log capability URLs or every frame request.

        def do_HEAD(self):
            self._get(head=True)

        def do_GET(self):
            self._get(head=False)

        def _get(self, *, head: bool):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            allowed_hosts = {expected_host}
            allowed_origins = {f"http://{expected_host}"}
            if public_origin:
                allowed_hosts.add(urlsplit(public_origin).netloc)
                allowed_origins.add(public_origin)
            if self.headers.get("Host") not in allowed_hosts:
                self.send_error(403, "Use the printed 127.0.0.1 viewer URL")
                return
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                self.send_error(403, "Cross-origin access is not allowed")
                return
            path = urlsplit(self.path).path
            if not path.startswith(prefix):
                self.send_error(404)
                return
            name = path[len(prefix):] or "index.html"
            frame = re.fullmatch(r"frames/(0|[1-9][0-9]{0,9})\.bin", name)
            if frame and reader is not None:
                index = int(frame[1])
                if index >= info["frame_count"]:
                    self.send_error(404, "Frame out of range")
                    return
                try:
                    # Topology must not change independently of the trajectory.
                    if _signature(files["structure.pdb"][0]) != signatures["structure.pdb"]:
                        raise ValueError("PDB changed; restart the viewer")
                    reader.check_unchanged()
                    data = None if head else reader.read(index)
                except (OSError, ValueError, RuntimeError):
                    self.send_error(409, "Frame unavailable or input changed; restart with finished files")
                    return
                self._headers("application/octet-stream", info["frame_bytes"])
                if data is not None:
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            if name == "structure.pdb":
                try:
                    display.check_unchanged()
                except (OSError, ValueError):
                    self.send_error(409, "Input changed; reopen viewer")
                    return
                data = display.topology_text.encode()
                self._headers("chemical/x-pdb", len(data))
                if not head:
                    self.wfile.write(data)
                return
            if name == "manifest.json":
                self._headers("application/json", len(manifest))
                if not head:
                    self.wfile.write(manifest)
                return
            if name not in routes:
                self.send_error(404)
                return
            file, mime, encoding = routes[name]
            try:
                with file.open("rb") as handle:
                    stat = os.fstat(handle.fileno())
                    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != signatures[name]:
                        self.send_error(409, "File changed; stop and restart the viewer")
                        return
                    self._headers(mime, stat.st_size, encoding)
                    if not head:
                        while data := handle.read(1024 * 1024):
                            self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_error(404, "File unavailable; restart the viewer")

        def _headers(self, mime: str, length: int, encoding: str | None = None):
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # The upstream Mol* bundle generates code (e.g. its expression evaluator).
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-eval'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                             "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
                             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

    class ViewerServer(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            if reader is not None:
                reader.close()

    server = ViewerServer(("127.0.0.1", port), Handler)
    server.local_url = f"http://127.0.0.1:{server.server_port}{prefix}"
    server.viewer_url = f"{public_origin}{prefix}" if public_origin else server.local_url
    return server


def serve_molstar(structure_file: str, trajectory_file: str | None = None, port: int = 0,
                  public_origin: str | None = None, stage: str = "auto", wrap: bool = True,
                  max_frames: int = 1000, fit: bool = False) -> dict:
    """Serve a PDB or matching PDB+DCD in a local interactive Mol* viewer.

    Runs in the foreground until Ctrl-C (no daemon). Start in a persistent
    terminal/background command, then open the viewer_url printed to stderr
    in the Codex or Claude Code desktop Browser pane. Port 0 selects a free
    port; set a fixed port for a Claude Code preview server configuration.
    Reads the DCD on the host and sends only the requested frame's coordinates
    (12 bytes per atom), plus the initial PDB and bundled viewer assets.
    stage selects auto/source/prep/solv/topo/min/eq/prod/analyze presentation.
    auto uses a DAG source stage, otherwise prod for DCD and prep for PDB.
    Periodic frames are molecule-wrapped by default. Production removes water
    and retains at most max_frames (0 keeps every frame). fit=True explicitly
    aligns protein backbone to a sampled iterative average; default is no fit.
    The DCD file is never served; no file export or DAG mutations.
    DCD atom order must match the PDB; equal counts cannot prove atom identity.
    Use an SSH tunnel with the same local/remote port for a remote Mac.
    For an authenticated HTTPS reverse proxy, set public_origin to its exact
    origin. This only permits that Host/Origin and prints the client URL;
    it does not create a tunnel, publish a site, or configure authentication.
    The server always binds 127.0.0.1. Final JSON is emitted after Ctrl-C.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        return create_validation_error("port", "Use an integer from 0 to 65535", code="invalid_parameter_value")
    try:
        public_origin = _public_origin(public_origin)
    except ValueError as exc:
        return create_validation_error("public_origin", str(exc), code="invalid_parameter_value")
    try:
        info, files = _inspect_inputs(structure_file, trajectory_file)
        from mdclaw.visualization.molstar_display import DisplayData
        display = DisplayData(structure_file, trajectory_file, stage=stage, wrap=wrap, max_frames=max_frames, fit=fit)
        info = display.info
    except (OSError, ValueError, RuntimeError) as exc:
        return create_validation_error("structure_file / trajectory_file", str(exc),
                                       hints=["Use a readable PDB and its matching, finished DCD."],
                                       code="molstar_input_invalid")
    try:
        server = _make_server(info, files, port, public_origin, display)
    except (OSError, ValueError, RuntimeError) as exc:
        return create_validation_error("viewer", str(exc),
                                       hints=["Use --port 0 for a free port; reinstall MDClaw if bundled assets are missing."],
                                       code="molstar_server_failed")
    with server:
        print(json.dumps({"viewer_url": server.viewer_url, "local_url": server.local_url, **display.summary()}), file=sys.stderr, flush=True)
        print("Mol* is ready. Open viewer_url in the app Browser. Stop with Ctrl-C.", file=sys.stderr, flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    return {"success": True, "message": "Mol* viewer stopped", "viewer_url": server.viewer_url}
