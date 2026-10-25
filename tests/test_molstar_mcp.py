"""Standard-runtime MCP integration: initialize and switch real PDB/DCD pairs."""

import base64
from pathlib import Path
import sys
import os

import anyio
import numpy as np
import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.parametrize("launch", ["legacy", "cli", "wrapper"])
def test_switch_datasets_in_one_stdio_connection(tmp_path, launch):
    from mdtraj.formats import DCDTrajectoryFile

    pairs = []
    for name, n, frames in (("first", 2, 2), ("second", 3, 3)):
        pdb, dcd = tmp_path / f"{name}.pdb", tmp_path / f"{name}.dcd"
        pdb.write_text("".join(
            f"ATOM  {i + 1:5d}  CA  ALA A{i + 1:4d}    {float(i):8.3f}{0.:8.3f}{0.:8.3f}  1.00  0.00           C\n"
            for i in range(n)) + "END\n")
        xyz = np.arange(frames * n * 3, dtype=np.float32).reshape(frames, n, 3)
        with DCDTrajectoryFile(str(dcd), "w") as out:
            out.write(xyz)
        pairs.append((pdb, dcd, xyz))

    async def check():
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ}
        # Test from a caller workspace, not from the repository cwd. The
        # wrapper must activate the same installed runtime without a shell.
        if launch == "wrapper":
            command, args = str(root / "bin/mdclaw"), ["molstar-mcp"]
            env.pop("CLAUDE_PLUGIN_ROOT", None)
            env["MDCLAW_RUNTIME"] = "pixi"
        else:
            command = sys.executable
            args = ["-m", "mdclaw.visualization.molstar_mcp_prototype"] if launch == "legacy" else ["-m", "mdclaw._cli", "molstar-mcp"]
            env["PYTHONPATH"] = str(root)
        if launch == "legacy":
            args += ["--structure-file", str(pairs[0][0]), "--trajectory-file", str(pairs[0][1])]
        params = StdioServerParameters(command=command, args=args, cwd=str(tmp_path), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                assert {t.name for t in tools} == {"open_molstar_probe", "molstar_probe_data"}
                if launch != "legacy":
                    missing = await client.call_tool("open_molstar_probe", {})
                    assert missing.isError and "structure_file" in missing.content[0].text
                    missing = await client.call_tool("molstar_probe_data", {"kind": "structure"})
                    assert missing.isError
                first = await client.call_tool("open_molstar_probe", {} if launch == "legacy" else {
                    "structure_file": str(pairs[0][0]), "trajectory_file": str(pairs[0][1])})
                assert not first.isError
                first_id = first.structuredContent["dataset_id"]
                args = {"structure_file": str(pairs[1][0]), "trajectory_file": str(pairs[1][1])}
                second = await client.call_tool("open_molstar_probe", args)
                assert not second.isError
                assert second.structuredContent["atom_count"] == 3
                assert second.structuredContent["frame_count"] == 3
                second_id = second.structuredContent["dataset_id"]
                assert first_id != second_id
                pdb_only = await client.call_tool("open_molstar_probe", {"structure_file": str(pairs[1][0]), "stage": "prep"})
                assert not pdb_only.isError
                assert pdb_only.structuredContent["display"]["stage"] == "prep"
                sampled = await client.call_tool("open_molstar_probe", {**args, "max_frames": 2})
                assert not sampled.isError
                assert sampled.structuredContent["frame_count"] == 2
                assert sampled.structuredContent["dataset_id"] != second_id
                again = await client.call_tool("open_molstar_probe", args)
                assert again.structuredContent["dataset_id"] == second_id

                # Older widgets keep their topology and coordinates after a switch.
                for token, (pdb, _, xyz) in zip((first_id, second_id), pairs, strict=True):
                    topology = await client.call_tool("molstar_probe_data", {"kind": "structure", "dataset_id": token})
                    pdb_lines = [line for line in topology.meta["mdclaw"]["pdb"].splitlines() if line.startswith("ATOM")]
                    np.testing.assert_allclose([[float(line[i:i+8]) for i in (30, 38, 46)] for line in pdb_lines], xyz[0])
                    frame = await client.call_tool("molstar_probe_data", {"kind": "frame", "index": len(xyz) - 1, "dataset_id": token})
                    assert not frame.isError
                    assert frame.content == []  # Coordinates must not enter model context.
                    payload = frame.meta["mdclaw"]
                    assert payload["dataset_id"] == token
                    expected = xyz[-1].T.astype("<f4").tobytes()
                    assert base64.b64decode(payload["coordinates"]) == expected

                for name, bad in (
                    ("open_molstar_probe", {**args, "trajectory_file": str(tmp_path / "missing.dcd")}),
                    ("open_molstar_probe", {**args, "structure_file": str(pairs[0][0])}),
                    ("open_molstar_probe", {"structure_file": "relative.pdb", "trajectory_file": "relative.dcd"}),
                    ("molstar_probe_data", {"kind": "frame", "index": 3, "dataset_id": second_id}),
                    ("molstar_probe_data", {"kind": "frame", "index": True, "dataset_id": second_id}),
                    ("molstar_probe_data", {"kind": "structure", "dataset_id": "unknown"}),
                    ("molstar_probe_data", {"kind": "structure", "structure_file": str(pairs[0][0])}),
                ):
                    result = await client.call_tool(name, bad)
                    assert result.isError, (name, bad)
                frame = await client.call_tool("molstar_probe_data", {"kind": "frame", "index": 0, "dataset_id": second_id})
                assert not frame.isError

                # Changed files invalidate only that dataset, and reopening works
                # without restarting the process or the MCP connection.
                pairs[1][0].write_text(pairs[1][0].read_text() + "REMARK updated\n")
                changed = await client.call_tool("molstar_probe_data", {"kind": "structure", "dataset_id": second_id})
                assert changed.isError
                reopened = await client.call_tool("open_molstar_probe", args)
                assert not reopened.isError
                assert reopened.structuredContent["dataset_id"] != second_id
                default = await client.call_tool("open_molstar_probe", {})
                if launch == "legacy":
                    assert default.structuredContent["dataset_id"] == first_id
                else:
                    assert default.isError  # Never select another widget's molecule implicitly.
                html = (await client.read_resource("ui://mdclaw/molstar-probe.html")).contents[0].text
                assert "ui/notifications/tool-result" in html
                assert "mode: 'orthographic'" in html
                assert "playback up to 60 fps" in html
                assert "buffering" in html
                assert "frameRtt" in html
                assert str(pairs[1][0]) not in html

    anyio.run(check)
