"""Real file/HTTP/CLI checks for the local viewer; no external network needed."""

import gzip
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import numpy as np
import pytest

from mdclaw.visualization.molstar_viewer import _ASSETS, _inspect_inputs, _make_server, serve_molstar


def test_mcp_playback_under_latency():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed only for the deterministic UI playback tests")
    test = Path(__file__).parent / "js/molstar_playback.test.cjs"
    result = subprocess.run([node, "--test", str(test)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def pair(tmp_path):
    from mdtraj.formats import DCDTrajectoryFile

    pdb = tmp_path / "test structure.pdb"
    pdb.write_text("ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
                   "ATOM      2  CA  ALA A   1       1.400   0.000   0.000  1.00  0.00           C\n"
                   "END\n")
    dcd = tmp_path / "test trajectory.dcd"
    with DCDTrajectoryFile(str(dcd), "w") as out:
        out.write(np.array([[[0, 0, 0], [1.4, 0, 0]], [[0, 0, 0], [1.4, .2, 0]]], dtype=np.float32))
    return pdb, dcd


@pytest.fixture
def running(pair):
    info, files = _inspect_inputs(*(str(p) for p in pair))
    server = _make_server(info, files, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)
    assert not worker.is_alive()


def request(server, suffix="", *, headers=None, method="GET", path=None):
    url = urlsplit(server.local_url)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
    connection.request(method, url.path + suffix if path is None else path, headers=headers or {})
    result = connection.getresponse()
    output = result.status, dict(result.getheaders()), result.read()
    connection.close()
    return output


def test_pair_and_pdb_only(pair):
    original = [p.read_bytes() for p in pair]
    info, files = _inspect_inputs(*(str(p) for p in pair))
    assert info["atom_count"] == 2
    assert info["frame_count"] == 2
    assert set(files) == {"structure.pdb", "trajectory.dcd"}
    info, files = _inspect_inputs(str(pair[0]), None)
    assert info["trajectory_name"] is None
    assert set(files) == {"structure.pdb"}
    assert original == [p.read_bytes() for p in pair]


def test_mismatched_atoms_rejected(pair):
    pair[0].write_text(pair[0].read_text().splitlines()[0] + "\nEND\n")
    result = serve_molstar(*(str(p) for p in pair))
    assert result["code"] == "molstar_input_invalid"
    assert "PDB has 1 atoms but DCD has 2" in result["message"]


def test_multimodel_pdb_without_dcd(pair):
    pdb = pair[0]
    model = pdb.read_text()
    pdb.write_text("MODEL        1\n" + model + "ENDMDL\nMODEL        2\n" + model + "ENDMDL\n")
    info, _ = _inspect_inputs(str(pdb), None)
    assert info["frame_count"] == 2
    assert info["atom_count"] == 2


@pytest.mark.parametrize("kind", ["missing", "empty", "bad_coordinates", "alternate", "multi_model", "invalid_dcd"])
def test_bad_inputs_rejected(pair, kind):
    pdb, dcd = pair
    if kind == "missing":
        pdb.unlink()
    elif kind == "empty":
        pdb.write_text("END\n")
    elif kind == "bad_coordinates":
        pdb.write_text(pdb.read_text().replace("0.000", "  nan", 1))
    elif kind == "alternate":
        pdb.write_text(pdb.read_text().replace(" N   ALA", " N  AALA"))
    elif kind == "multi_model":
        pdb.write_text("MODEL        1\n" + pdb.read_text() + "ENDMDL\nMODEL        2\n" + pdb.read_text())
    else:
        dcd.write_bytes(b"not a trajectory")
    assert serve_molstar(str(pdb), str(dcd))["code"] == "molstar_input_invalid"


@pytest.mark.parametrize("port", [-1, 65536, True, 3.5])
def test_bad_port(pair, port):
    assert serve_molstar(str(pair[0]), port=port)["code"] == "invalid_parameter_value"


def test_local_routes_and_bytes(running, pair):
    assert running.server_address[0] == "127.0.0.1"
    status, headers, body = request(running)
    assert status == 200 and b"MDClaw" in body
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "Access-Control-Allow-Origin" not in headers
    for name, file in (("structure.pdb", pair[0]),):
        status, headers, body = request(running, name)
        assert status == 200 and body == file.read_bytes()
        assert int(headers["Content-Length"]) == len(body)
        status, headers, body = request(running, name, method="HEAD")
        assert status == 200 and not body
    status, _, body = request(running, "manifest.json")
    assert status == 200 and json.loads(body)["frame_count"] == 2
    assert json.loads(body)["frame_bytes"] == 24
    assert request(running, "trajectory.dcd")[0] == 404
    for name in ("viewer.js", "viewer.css", "molstar.js", "molstar.css"):
        status, headers, body = request(running, name)
        assert status == 200
        if name.startswith("molstar."):
            assert headers["Content-Encoding"] == "gzip"
            assert len(gzip.decompress(body)) > 1000


@pytest.mark.parametrize("suffix", ["../pyproject.toml", "%2e%2e/pyproject.toml", "test%20structure.pdb", "LICENSE", "//etc/passwd"])
def test_no_directory_or_arbitrary_file_access(running, suffix):
    assert request(running, suffix)[0] == 404
    assert request(running, path="/")[0] == 404


def test_host_origin_and_changed_file_rejected(running, pair):
    assert request(running, headers={"Host": "untrusted.example"})[0] == 403
    assert request(running, headers={"Origin": "https://untrusted.example"})[0] == 403
    pair[1].write_bytes(pair[1].read_bytes() + b"more")
    assert request(running, "frames/0.bin")[0] == 409


def test_seek_streams_only_one_frame_in_angstroms(running):
    expected = [np.array([[0, 1.4], [0, 0], [0, 0]], dtype="<f4"),
                np.array([[0, 1.4], [0, .2], [0, 0]], dtype="<f4")]
    for index in (1, 0, 1, 0):
        status, headers, body = request(running, f"frames/{index}.bin")
        assert status == 200
        assert len(body) == int(headers["Content-Length"]) == 24
        assert headers["Cache-Control"] == "no-store"
        np.testing.assert_allclose(np.frombuffer(body, dtype="<f4").reshape(3, 2), expected[index])


@pytest.mark.parametrize("suffix", ["frames/-1.bin", "frames/2.bin", "frames/00.bin",
                                    "frames/1.5.bin", "frames/0.bin/extra", "trajectory.dcd"])
def test_bad_frame_routes(running, suffix):
    assert request(running, suffix)[0] == 404


def test_concurrent_frame_requests_do_not_mix_coordinates(running):
    from concurrent.futures import ThreadPoolExecutor

    indices = [0, 1] * 8
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: request(running, f"frames/{i}.bin"), indices))
    for index, (status, _, body) in zip(indices, results):
        assert status == 200
        assert np.frombuffer(body, dtype="<f4")[3] == np.float32(.2 if index else 0)


def test_stream_reads_one_frame_and_head_reads_none(pair, monkeypatch):
    import mdtraj.formats

    info, files = _inspect_inputs(*(str(p) for p in pair))
    original = mdtraj.formats.DCDTrajectoryFile
    reads, seeks, closes = [], [], []

    class Reader:
        def __init__(self, path):
            self.handle = original(path)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

        def read(self, **kwargs):
            reads.append(kwargs)
            return self.handle.read(**kwargs)

        def seek(self, index):
            seeks.append(index)
            self.handle.seek(index)

        def close(self):
            closes.append(True)
            self.handle.close()

    from mdclaw.visualization.molstar_display import DisplayData
    display = DisplayData(*pair)
    monkeypatch.setattr(mdtraj.formats, "DCDTrajectoryFile", Reader)
    server = _make_server(info, files, 0, display=display)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        status, headers, body = request(server, "frames/0.bin", method="HEAD")
        assert status == 200 and not body and headers["Content-Length"] == "24"
        assert not reads
        for index in (1, 0):
            assert request(server, f"frames/{index}.bin")[0] == 200
        assert reads == [{"n_frames": 1}, {"n_frames": 1}]
        assert seeks == [1, 0]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
    assert closes == [True, True]


def test_changed_pdb_refuses_frames(running, pair):
    pair[0].write_text(pair[0].read_text() + "REMARK changed\n")
    assert request(running, "frames/0.bin")[0] == 409


def test_https_proxy_origin_is_explicit(pair):
    info, files = _inspect_inputs(*(str(p) for p in pair))
    server = _make_server(info, files, 0, "https://viewer.example.org")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        assert server.viewer_url.startswith("https://viewer.example.org/")
        assert server.server_address[0] == "127.0.0.1"
        for headers in ({"Host": "viewer.example.org", "Origin": "https://viewer.example.org"},
                        {"Origin": "https://viewer.example.org"}):
            assert request(server, "frames/1.bin", headers=headers)[0] == 200
        assert request(server, headers={"Host": "attacker.example.org"})[0] == 403
        assert request(server, headers={"Host": "viewer.example.org", "Origin": "https://attacker.example.org"})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize("origin", ["http://viewer.example.org", "https://viewer.example.org/path",
                                    "https://user:pass@viewer.example.org", "https://viewer.example.org?x=1",
                                    "https://viewer.example.org:99999", "https://", "https://a b"])
def test_invalid_public_origin(pair, origin):
    assert serve_molstar(str(pair[0]), public_origin=origin)["code"] == "invalid_parameter_value"


def test_busy_port_is_structured(running, pair):
    assert serve_molstar(str(pair[0]), port=running.server_port)["code"] == "molstar_server_failed"


def test_vendor_integrity():
    vendor = _ASSETS / "vendor"
    info = json.loads((vendor / "provenance.json").read_text())
    assert info["version"] == "5.13.0"
    for name, expected in info["files"].items():
        compressed = (vendor / (name + ".gz")).read_bytes()
        assert hashlib.sha256(compressed).hexdigest() == expected["gzip_sha256"]
        assert hashlib.sha256(gzip.decompress(compressed)).hexdigest() == expected["sha256"]
    assert "MIT" in (vendor / "LICENSE").read_text()


def test_cli_start_and_ctrl_c(pair, tmp_path):
    """Readiness is available while running; stopping releases the listening port."""
    import time

    log = tmp_path / "server.log"
    with log.open("w") as stderr:
        process = subprocess.Popen([sys.executable, "-m", "mdclaw._cli", "serve_molstar",
                                    "--structure-file", str(pair[0]), "--trajectory-file", str(pair[1]),
                                    "--heartbeat-seconds", "0"], stdout=subprocess.PIPE, stderr=stderr, text=True)
        try:
            ready = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                for line in log.read_text().splitlines():
                    if line.startswith('{"viewer_url"'):
                        ready = json.loads(line)
                        break
                if ready or process.poll() is not None:
                    break
                time.sleep(.05)
            assert ready, log.read_text()
            url = urlsplit(ready["viewer_url"])
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
            connection.request("GET", url.path + "manifest.json")
            assert json.loads(connection.getresponse().read())["frame_count"] == 2
            connection.close()
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=10)
            assert process.returncode == 0, log.read_text()
            assert json.loads(stdout)["success"] is True
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=1)
            with pytest.raises(OSError):
                connection.connect()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_cli_discovery():
    from mdclaw._cli import _discover_tools

    assert "serve_molstar" in _discover_tools()
