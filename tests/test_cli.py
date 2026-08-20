"""Level 1: Unit tests for the mdclaw CLI module.

No external tools (ambertools, openmm, etc.) required.
Tests validate tool discovery, argparse construction, parameter coercion,
and CLI subcommand output.
"""

import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pick_existing_tool(tools, *preferred_names):
    """Return the first preferred tool present in the discovered tool set."""
    for name in preferred_names:
        if name in tools:
            return name
    pytest.skip(f"None of the preferred tools are available: {preferred_names}")


def _dependency_available(module_name):
    return find_spec(module_name) is not None


def test_confirmation_rendering_failure_does_not_replace_tool_result(
    monkeypatch,
    caplog,
):
    import mdclaw._cli as cli

    def fail_rendering(_items):
        raise RuntimeError("renderer broke")

    result = {
        "success": True,
        "confirmation_needed": {"protonation_states": {"states": [{"state": "ASH"}]}},
    }
    monkeypatch.setattr(cli, "report_confirmation_items", fail_rendering)

    with caplog.at_level("WARNING"):
        cli._report_confirmation_items_safely(result)

    assert result["success"] is True
    assert result["confirmation_needed"]
    assert "structured result remains available" in caplog.text


def test_attach_dag_handoff_reports_registered_artifacts(tmp_path):
    from mdclaw._cli import _attach_dag_handoff

    node_dir = tmp_path / "nodes" / "prep_001"
    node_dir.mkdir(parents=True)
    (node_dir / "node.json").write_text(json.dumps({
        "node_type": "prep",
        "status": "completed",
        "artifacts": {"merged_pdb": "artifacts/merged.pdb"},
    }))

    result = _attach_dag_handoff(
        {"success": True}, str(tmp_path), "prep_001"
    )

    assert "resolves artifacts between nodes" in result["dag_guidance"]
    assert result["dag_handoff"] == {
        "node_id": "prep_001",
        "status": "completed",
        "artifact_keys": ["merged_pdb"],
        "next_node_inputs": "auto_resolved",
        "default_forward_branch": {
            "optional": True,
            "node_type": "solv",
            "create_command": (
                f"mdclaw create_node --job-dir {tmp_path.resolve()} "
                "--node-type solv --parent-node-ids prep_001"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Tool Discovery
# ---------------------------------------------------------------------------


class TestToolDiscovery:
    """Test _discover_tools() finds all registered tools."""

    def test_discovers_all_tools(self):
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()
        # Optional scientific/network dependencies may hide several servers.
        assert len(tools) >= 20, f"Expected >=20 tools, got {len(tools)}"

    def test_each_tool_has_required_keys(self):
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()
        for name, info in tools.items():
            assert "fn" in info, f"{name} missing 'fn'"
            assert "is_async" in info, f"{name} missing 'is_async'"
            assert "server" in info, f"{name} missing 'server'"
            assert "description" in info, f"{name} missing 'description'"
            assert callable(info["fn"]), f"{name} fn is not callable"

    def test_duplicate_tool_names_are_rejected(self, monkeypatch):
        import sys
        import types
        from mdclaw import _cli

        mod_a = types.ModuleType("fake_mdclaw_server_a")
        mod_b = types.ModuleType("fake_mdclaw_server_b")

        def fake_tool() -> dict:
            return {"success": True}

        mod_a.TOOLS = {"fake_tool": fake_tool}
        mod_b.TOOLS = {"fake_tool": fake_tool}
        monkeypatch.setitem(sys.modules, "fake_mdclaw_server_a", mod_a)
        monkeypatch.setitem(sys.modules, "fake_mdclaw_server_b", mod_b)
        monkeypatch.setattr(
            _cli,
            "SERVER_REGISTRY",
            {"a": "fake_mdclaw_server_a", "b": "fake_mdclaw_server_b"},
        )

        with pytest.raises(ValueError, match="Duplicate tool name 'fake_tool'"):
            _cli._discover_tools()

    def test_async_detection(self):
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()
        if _dependency_available("httpx"):
            assert "fetch_structure" in tools
            assert tools["fetch_structure"]["is_async"] is True
        else:
            pytest.skip("fetch tools unavailable because research server dependencies are missing")

        sync_tool = _pick_existing_tool(tools, "inspect_molecules", "solvate_structure", "build_amber_system")
        assert tools[sync_tool]["is_async"] is False

    def test_key_tools_present_when_dependencies_available(self):
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()

        assert "generate_surrogate_candidates" in tools
        assert "setup_model_backend" in tools
        assert "check_model_backend" in tools

        if _dependency_available("httpx"):
            assert "fetch_structure" in tools
        if _dependency_available("pdbfixer"):
            assert "split_molecules" in tools
            assert "inspect_molecules" in tools
            assert "prepare_modified_nucleic" in tools

    def test_surrogate_tools_present(self):
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()

        assert "generate_surrogate_candidates" in tools
        assert "setup_model_backend" in tools
        assert "check_model_backend" in tools
        assert tools["generate_surrogate_candidates"]["server"] == "surrogate"

    def test_all_servers_represented(self):
        from mdclaw._cli import _discover_tools
        from mdclaw._registry import SERVER_REGISTRY
        import importlib

        tools = _discover_tools()
        servers_found = {info["server"] for info in tools.values()}
        for server_name in SERVER_REGISTRY:
            # Skip servers that can't be imported (optional deps missing)
            try:
                importlib.import_module(SERVER_REGISTRY[server_name])
            except ImportError:
                continue
            assert server_name in servers_found, f"Server '{server_name}' has no tools"

    def test_node_tools_declare_valid_node_types(self):
        from mdclaw._cli import _discover_tools
        from mdclaw.node.constants import NODE_TYPES

        tools = _discover_tools()
        for tool_name, info in tools.items():
            if info["requires_node"]:
                assert info["node_type"] in NODE_TYPES, (
                    f"{tool_name} has invalid node type {info['node_type']!r}"
                )
            else:
                assert info["node_type"] is None


# ---------------------------------------------------------------------------
# argparse Construction
# ---------------------------------------------------------------------------


class TestArgparseConstruction:
    """Test _build_parser() creates correct subcommands and args."""

    def test_subcommands_created(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)
        tool_name = _pick_existing_tool(
            tools,
            "fetch_structure",
            "solvate_structure",
            "build_amber_system",
        )

        # Parser should have subparsers with all tool names
        # Test by parsing a known tool with --help (should not raise)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([tool_name, "--help"])
        assert exc_info.value.code == 0

    def test_required_params(self):
        """Missing required params causes non-zero exit via main()."""
        from mdclaw._cli import main

        # fetch_structure without node context exits non-zero (node_context_required)
        with pytest.raises(SystemExit) as exc_info:
            main(["fetch_structure"])
        assert exc_info.value.code != 0

    def test_workflow_tools_require_node_context(self):
        """Schema-v3 workflow tools must reject calls without --job-dir/node-id."""
        from mdclaw._cli import main

        if not _dependency_available("httpx"):
            pytest.skip("fetch_structure unavailable because research server dependencies are missing")

        with pytest.raises(SystemExit) as exc_info:
            main(["fetch_structure", "--source", "pdb", "--pdb-id", "1AKE"])
        assert exc_info.value.code != 0

    def test_workflow_help_is_short_and_dag_first(self, capsys):
        from mdclaw._cli import _build_parser

        def fake_workflow(
            structure_file: str | None = None,
            job_dir: str | None = None,
            node_id: str | None = None,
        ) -> dict:
            """Prepare a structure.\n\nLong implementation details should be omitted."""
            return {"success": True}

        parser = _build_parser({
            "fake_workflow": {
                "fn": fake_workflow,
                "is_async": False,
                "server": "fake",
                "description": fake_workflow.__doc__,
                "requires_node": True,
            }
        })

        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["fake_workflow", "--help"])

        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        assert "CLI workflow contract: DAG-only" in output
        assert "Long implementation details" not in output
        assert "--job-dir JOB_DIR" in output
        assert "(str, default: required)" in output

    def test_fetch_structure_infers_pdb_source_before_node_context_gate(self, capsys):
        """Weak agents often call ``fetch_structure --pdb-id``; infer source=pdb."""
        from mdclaw._cli import main

        if not _dependency_available("httpx"):
            pytest.skip("fetch_structure unavailable because research server dependencies are missing")

        with pytest.raises(SystemExit) as exc_info:
            main(["fetch_structure", "--pdb-id", "1AKE"])
        assert exc_info.value.code != 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "node_context_required"
        assert payload["context"]["tool"] == "fetch_structure"

    def test_node_required_tool_set_covers_dag_mutators(self):
        """Tools that create or complete workflow nodes must be CLI-gated."""
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()
        node_required = {
            name for name, info in tools.items() if info.get("requires_node")
        }
        expected = {
            "create_mutated_structure",
            "phosphorylate_residues",
            "prepare_modified_nucleic",
            "run_minimization",
            "analyze_rmsf",
            "analyze_contact_frequency",
        }
        assert expected <= node_required
        assert "render_structure_preview" not in node_required

    def test_cli_tool_failure_records_node_failure_artifact(self, tmp_path, monkeypatch):
        from mdclaw import _cli
        from mdclaw._node import create_node, read_node

        job_dir = tmp_path / "job_cli_failure"
        job_dir.mkdir()
        node = create_node(str(job_dir), "eq")

        def fake_fail(job_dir: str, node_id: str) -> dict:
            print("tool stdout line")
            print("tool stderr line", file=sys.stderr)
            return {
                "success": False,
                "code": "fake_failure",
                "errors": [f"failed {node_id} under {job_dir}"],
                "warnings": [],
            }

        monkeypatch.setattr(_cli, "_discover_tools", lambda: {
            "fake_fail": {
                "fn": fake_fail,
                "is_async": False,
                "server": "fake",
                "description": "Fake failure tool.",
                "requires_node": True,
            }
        })

        with pytest.raises(SystemExit) as exc_info:
            _cli.main([
                "--job-dir", str(job_dir),
                "--node-id", node["node_id"],
                "fake_fail",
            ])
        assert exc_info.value.code == 1

        latest = read_node(str(job_dir), node["node_id"])
        assert latest["metadata"]["failure_code"] == "fake_failure"
        manifest = job_dir / "nodes" / node["node_id"] / latest["artifacts"]["failure"]
        assert manifest.is_file()
        assert (manifest.parent / "tool_result.json").is_file()
        assert "tool stdout line" in (manifest.parent / "stdout_tail.txt").read_text()
        assert "fake_failure" in (manifest.parent / "stdout_tail.txt").read_text()
        assert "tool stderr line" in (manifest.parent / "stderr_tail.txt").read_text()

    def test_cli_rejects_terminal_node_before_tool_call(self, tmp_path, monkeypatch, capsys):
        from mdclaw import _cli
        from mdclaw._node import create_node, fail_node

        job_dir = tmp_path / "job_terminal"
        job_dir.mkdir()
        node = create_node(str(job_dir), "eq")
        fail_node(str(job_dir), node["node_id"], errors=["first failure"])
        called = False

        def should_not_run(job_dir: str, node_id: str) -> dict:
            nonlocal called
            called = True
            return {"success": True}

        monkeypatch.setattr(_cli, "_discover_tools", lambda: {
            "fake_eq": {
                "fn": should_not_run,
                "is_async": False,
                "server": "fake",
                "description": "Fake eq tool.",
                "requires_node": True,
                "node_type": "eq",
            }
        })

        with pytest.raises(SystemExit) as exc_info:
            _cli.main([
                "--job-dir", str(job_dir),
                "--node-id", node["node_id"],
                "fake_eq",
            ])

        assert exc_info.value.code == 1
        assert called is False
        assert json.loads(capsys.readouterr().out)["code"] == "node_terminal"

    def test_cli_unhandled_exception_records_traceback_artifact(self, tmp_path, monkeypatch):
        from mdclaw import _cli
        from mdclaw._node import create_node, read_node

        job_dir = tmp_path / "job_cli_exception"
        job_dir.mkdir()
        node = create_node(str(job_dir), "eq")

        def fake_crash(job_dir: str, node_id: str) -> dict:
            print("crash stdout line")
            print("crash stderr line", file=sys.stderr)
            raise RuntimeError(f"crashed {node_id} under {job_dir}")

        monkeypatch.setattr(_cli, "_discover_tools", lambda: {
            "fake_crash": {
                "fn": fake_crash,
                "is_async": False,
                "server": "fake",
                "description": "Fake crashing tool.",
                "requires_node": True,
            }
        })

        with pytest.raises(SystemExit) as exc_info:
            _cli.main([
                "--job-dir", str(job_dir),
                "--node-id", node["node_id"],
                "fake_crash",
            ])
        assert exc_info.value.code == 1

        latest = read_node(str(job_dir), node["node_id"])
        assert latest["metadata"]["failure_code"] == "unhandled_exception"
        manifest = job_dir / "nodes" / node["node_id"] / latest["artifacts"]["failure"]
        assert manifest.is_file()
        traceback_file = manifest.parent / "traceback.txt"
        assert traceback_file.is_file()
        assert "RuntimeError" in traceback_file.read_text()
        assert "crash stdout line" in (manifest.parent / "stdout_tail.txt").read_text()
        assert "unhandled_exception" in (manifest.parent / "stdout_tail.txt").read_text()
        assert "crash stderr line" in (manifest.parent / "stderr_tail.txt").read_text()

    def test_management_failure_does_not_fail_target_node(
        self, tmp_path, monkeypatch, capsys
    ):
        from mdclaw import _cli
        from mdclaw._node import create_node, read_node
        from mdclaw.node.lifecycle import update_workflow_state

        job_dir = tmp_path / "job_management_failure"
        job_dir.mkdir()
        node = create_node(str(job_dir), "prod")
        monkeypatch.setattr(_cli, "_discover_tools", lambda: {
            "update_workflow_state": {
                "fn": update_workflow_state,
                "is_async": False,
                "server": "node",
                "description": "Update workflow state.",
                "requires_node": False,
            }
        })

        with pytest.raises(SystemExit) as exc_info:
            _cli.main([
                "update_workflow_state",
                "--job-dir", str(job_dir),
                "--node-id", node["node_id"],
                "--status", "completed",
            ])

        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "node_terminal_transition_reserved"
        latest = read_node(str(job_dir), node["node_id"])
        assert latest["status"] == "pending"
        assert latest["artifacts"] == {}

    def test_optional_params_have_defaults(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["solvate_structure", "--pdb-file", "test.pdb"])
        assert args.pdb_file == "test.pdb"
        assert args.water_model == "opc"  # default

    def test_boltz_smiles_list_is_optional_for_protein_only(self):
        from mdclaw._cli import _tool_list_json, _discover_tools

        tools = _discover_tools()
        payload = _tool_list_json(tools)
        boltz = next(
            tool
            for tool in payload["tools"]
            if tool["name"] == "boltz2_protein_from_seq"
        )
        params = {param["name"]: param for param in boltz["parameters"]}
        assert params["amino_acid_sequence_list"]["required"] is True
        assert params["smiles_list"]["required"] is False

    def test_embed_in_membrane_pdb_file_is_optional_for_autoresolve(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        _pick_existing_tool(tools, "embed_in_membrane")
        parser = _build_parser(tools)

        args = parser.parse_args(["embed_in_membrane", "--lipids", "POPC"])
        assert args.pdb_file is None
        assert args.lipids == ["POPC"]
        assert args.water_model == "opc"  # default

        # Repeated tokens arrive as a plain list; the tool itself joins them.
        args = parser.parse_args(
            ["embed_in_membrane", "--lipids", "POPC", "POPE", "CHL1"]
        )
        assert args.lipids == ["POPC", "POPE", "CHL1"]

    def test_embed_in_membrane_lipids_accepts_list_and_colon_string(
        self, monkeypatch, capsys
    ):
        from mdclaw import _cli

        def fake_embed_in_membrane(lipids: list[str] = None) -> dict:
            if isinstance(lipids, (list, tuple)):
                lipids = ":".join(str(item) for item in lipids)
            return {"success": True, "lipids": lipids}

        monkeypatch.setattr(
            _cli,
            "_discover_tools",
            lambda: {
                "embed_in_membrane": {
                    "fn": fake_embed_in_membrane,
                    "description": "fake membrane embed",
                    "is_async": False,
                    "server": "fake",
                }
            },
        )

        with pytest.raises(SystemExit) as exc_info:
            _cli.main(["embed_in_membrane", "--lipids", "POPC:POPE:CHL1"])

        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["lipids"] == "POPC:POPE:CHL1"

        with pytest.raises(SystemExit) as exc_info:
            _cli.main(["embed_in_membrane", "--lipids", "POPC", "POPE", "CHL1"])

        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["lipids"] == "POPC:POPE:CHL1"

        with pytest.raises(SystemExit) as exc_info:
            _cli.main([
                "embed_in_membrane",
                "--json-input",
                '{"lipids": ["POPC:POPE:CHL1"]}',
            ])

        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["lipids"] == "POPC:POPE:CHL1"

    def test_inspect_molecules_structure_file_is_optional_for_autoresolve(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        _pick_existing_tool(tools, "inspect_molecules")
        parser = _build_parser(tools)

        args = parser.parse_args([
            "--job-dir",
            "job_xxx",
            "--node-id",
            "source_001",
            "inspect_molecules",
        ])
        assert args.structure_file is None

    def test_bool_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        # solvate_structure has --salt (bool)
        args = parser.parse_args([
            "solvate_structure",
            "--pdb-file", "test.pdb",
            "--salt",
        ])
        assert args.salt is True

        args = parser.parse_args([
            "solvate_structure",
            "--pdb-file", "test.pdb",
            "--no-salt",
        ])
        assert args.salt is False

        args = parser.parse_args([
            "solvate_structure",
            "--pdb-file", "test.pdb",
            "--salt", "true",
        ])
        assert args.salt is True

        args = parser.parse_args([
            "solvate_structure",
            "--pdb-file", "test.pdb",
            "--salt", "false",
        ])
        assert args.salt is False

    def test_list_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "set_policy",
            "--allowed-partitions", "gpu", "cpu",
        ])
        assert args.allowed_partitions == ["gpu", "cpu"]

    def test_json_input(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        json_str = '{"pdb_file": "test.pdb", "water_model": "opc"}'
        args = parser.parse_args(["solvate_structure", "--json-input", json_str])
        assert args.json_input == json_str

    def test_run_production_custom_force_flags(self):
        """run_production exposes the custom force / CV bias CLI flags."""
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_production",
            "--custom-force-script", "energy.py",
            "--custom-force-parameters", '{"k": 1000.0, "d0": 1.2}',
        ])
        assert args.custom_force_script == "energy.py"
        # custom_force_parameters is a JSON dict at the CLI boundary
        assert args.custom_force_parameters == '{"k": 1000.0, "d0": 1.2}'

    def test_path_params_parse_as_path(self):
        from mdclaw._cli import _build_parser

        def fake_tool(input_path: Path) -> dict:
            return {"success": True, "input_path": str(input_path)}

        parser = _build_parser({
            "fake_path": {
                "fn": fake_tool,
                "description": "fake path tool",
                "is_async": False,
                "server": "fake",
            }
        })

        args = parser.parse_args(["fake_path", "--input-path", "input.pdb"])
        assert isinstance(args.input_path, Path)
        assert args.input_path == Path("input.pdb")

    def test_json_input_values_are_coerced_to_annotations(self, monkeypatch, capsys):
        from mdclaw import _cli

        def fake_tool(count: int, input_path: Path) -> dict:
            return {
                "success": True,
                "count_type": type(count).__name__,
                "input_path_type": type(input_path).__name__,
            }

        monkeypatch.setattr(
            _cli,
            "_discover_tools",
            lambda: {
                "fake_tool": {
                    "fn": fake_tool,
                    "description": "fake tool",
                    "is_async": False,
                    "server": "fake",
                }
            },
        )

        with pytest.raises(SystemExit) as exc_info:
            _cli.main([
                "fake_tool",
                "--json-input",
                '{"count": "7", "input_path": "input.pdb"}',
            ])

        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count_type"] == "int"
        assert payload["input_path_type"] == "PosixPath"

    def test_invalid_json_input_returns_structured_json(self, capsys):
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["solvate_structure", "--json-input", "{bad"])

        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert payload["error_type"] == "ValidationError"
        assert payload["code"] == "invalid_json_input"
        assert payload["context"]["field"] == "--json-input"

    def test_invalid_json_typed_argument_returns_structured_json(self, capsys, tmp_path):
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_workflow_state",
                "--job-dir", str(tmp_path),
                "--params", "{bad",
            ])

        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert payload["code"] == "invalid_json_input"
        assert payload["context"]["field"] == "--params"

    def test_list_flag(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["--list"])
        assert args.list_tools is True

    def test_list_json_flag(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["--list-json"])
        assert args.list_tools_json == ""

        args = parser.parse_args(["--list-json", "run_minimization"])
        assert args.list_tools_json == "run_minimization"

    def test_tool_list_json_schema(self):
        from mdclaw._cli import _tool_list_json, _discover_tools

        tools = _discover_tools()
        payload = _tool_list_json(tools)

        assert payload["success"] is True
        assert payload["total"] == len(tools)
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert "solvate_structure" in tool_names
        solvate = next(tool for tool in payload["tools"]
                       if tool["name"] == "solvate_structure")
        assert solvate["requires_node"] is True
        assert solvate["node_type"] == "solv"
        params = {param["name"]: param for param in solvate["parameters"]}
        assert params["pdb_file"]["cli_flag"] == "--pdb-file"
        assert params["water_model"]["default"] == "opc"
        assert params["salt"]["cli_action"] == "boolean_optional"
        assert params["salt"]["accepted_cli_forms"] == [
            "--salt",
            "--no-salt",
            "--salt true",
            "--salt false",
        ]
        assert params["job_dir"]["required"] is True
        assert params["job_dir"]["has_default"] is False
        assert params["node_id"]["required"] is True
        assert params["node_id"]["has_default"] is False

        prepare = next(tool for tool in payload["tools"]
                       if tool["name"] == "prepare_complex")
        prepare_params = {param["name"]: param for param in prepare["parameters"]}
        assert prepare_params["protonation_states"]["cli_flag"] == "--protonation-states"
        assert prepare_params["protonation_states"]["expects_json"] is True
        assert prepare_params["protonation_states"]["json_examples"] == [
            {"A:11": "GLH"},
            [{"chain": "A", "resnum": 11, "state": "GLH"}],
        ]
        assert prepare_params["disulfide_pairs"]["json_examples"][0][0] == {
            "cys1": {"chain": "A", "resnum": 6},
            "cys2": {"chain": "A", "resnum": 127},
        }

        targeted = _tool_list_json(tools, "solvate_structure")
        assert targeted["success"] is True
        assert targeted["total"] == 1
        assert targeted["tools"] == [
            {key: value for key, value in solvate.items() if key != "description"}
        ]

    def test_text_tool_list_is_a_compact_name_index(self, capsys):
        from mdclaw._cli import _print_tool_list

        _print_tool_list({
            "example_tool": {
                "server": "example",
                "description": "description that should not fill the index",
            },
        })

        output = capsys.readouterr().out
        assert output.splitlines()[:3] == [
            "MD workflow: follow the matching skill; prefer MDClaw CLI tools over custom scripts.",
            "Preparation stage: use prepare_complex; use focused helpers only when directed.",
            "DAG: create_node -> explain_node -> stage tool. Inspect: mdclaw --list-json <tool>.",
        ]
        assert "example_tool" in output
        assert "mdclaw --list-json <tool>" in output
        assert "description that should not fill the index" not in output

    def test_targeted_list_json_is_compact_and_unknown_is_structured(self, capsys):
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--list-json", "run_minimization"])
        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        assert len(output.splitlines()) == 1
        payload = json.loads(output)
        assert payload["total"] == 1
        assert payload["tools"][0]["name"] == "run_minimization"
        assert "description" not in payload["tools"][0]

        # A consolidated name names its replacement whichever way it is asked
        # about: an agent that introspects before calling must not be told less
        # than one that just calls.
        with pytest.raises(SystemExit) as exc_info:
            main(["--list-json", "record_study_decision"])
        assert exc_info.value.code == 1
        output = capsys.readouterr().out
        assert len(output.splitlines()) == 1
        payload = json.loads(output)
        assert payload["code"] == "tool_renamed"
        assert payload["context"]["tool"] == "record_study_decision"
        assert payload["context"]["replacement"].startswith("record_study_log")

        with pytest.raises(SystemExit) as exc_info:
            main(["--list-json", "not_a_real_mdclaw_tool"])
        assert exc_info.value.code == 1
        output = capsys.readouterr().out
        assert len(output.splitlines()) == 1
        payload = json.loads(output)
        assert payload["success"] is False
        assert payload["code"] == "tool_not_available"
        assert payload["context"]["tool"] == "not_a_real_mdclaw_tool"

    def test_node_required_tools_name_their_workflow_entry(self):
        """A node-required tool must say where job_dir/node_id come from.

        Introspecting a tool before calling it is what the skill tells agents to
        do. Returning "these two are required" without naming the route left
        them holding a parameter list and no way in — the observed prelude to
        improvising outside the DAG.
        """
        from mdclaw._cli import _discover_tools, _tool_list_json

        tools = _discover_tools()
        payload = _tool_list_json(tools, "prepare_complex")
        entry = payload["tools"][0]["workflow_entry"]
        assert any("bootstrap_md_workflow" in step for step in entry)
        assert any("create_node" in step and "prep" in step for step in entry)
        assert any("explain_node" in step for step in entry)
        assert any(step.endswith("prepare_complex ...") for step in entry)

        # Tools that run without node context must not carry the route.
        plain = _tool_list_json(tools, "inspect_molecules")["tools"][0]
        assert plain["requires_node"] is False
        assert "workflow_entry" not in plain

    def test_optional_params_are_typed_in_parser_and_list_json(self):
        from mdclaw._cli import _build_parser, _discover_tools, _tool_list_json

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "clean_ligand",
            "--ligand-pdb", "ligand.pdb",
            "--ligand-id", "LIG",
            "--expected-net-charge", "2",
        ])
        assert args.expected_net_charge == 2

        if "search_structures" in tools:
            args = parser.parse_args([
                "search_structures",
                "--query", "kinase",
                "--no-has-ligand",
            ])
            assert args.has_ligand is False

        payload = _tool_list_json(tools)
        clean_ligand = next(
            tool for tool in payload["tools"]
            if tool["name"] == "clean_ligand"
        )
        params = {param["name"]: param for param in clean_ligand["parameters"]}
        assert params["expected_net_charge"]["type"] == "Optional[int]"


# ---------------------------------------------------------------------------
# Parameter Coercion
# ---------------------------------------------------------------------------


class TestParameterCoercion:
    """Test _coerce_value and _unwrap_optional helpers."""

    def test_unwrap_optional_str(self):
        from typing import Optional
        from mdclaw._cli import _unwrap_optional

        inner, is_opt = _unwrap_optional(Optional[str])
        assert inner is str
        assert is_opt is True

    def test_unwrap_pep604_optional(self):
        from mdclaw._cli import _unwrap_optional

        inner, is_opt = _unwrap_optional(int | None)
        assert inner is int
        assert is_opt is True

    def test_unwrap_non_optional(self):
        from mdclaw._cli import _unwrap_optional

        inner, is_opt = _unwrap_optional(str)
        assert inner is str
        assert is_opt is False

    def test_is_list_of_str(self):
        from typing import List
        from mdclaw._cli import _is_list_of_str

        assert _is_list_of_str(List[str]) is True
        assert _is_list_of_str(list[str]) is True
        assert _is_list_of_str(str) is False
        assert _is_list_of_str(List[int]) is False

    def test_is_dict_type(self):
        from typing import Dict
        from mdclaw._cli import _is_dict_type

        assert _is_dict_type(dict) is True
        assert _is_dict_type(Dict[str, str]) is True
        assert _is_dict_type(str) is False

    def test_is_list_of(self):
        from typing import Dict, List
        from mdclaw._cli import _is_list_of

        assert _is_list_of(list[dict], dict) is True
        assert _is_list_of(List[Dict[str, str]], dict) is True
        assert _is_list_of(list[list[int]], list) is True
        assert _is_list_of(list[str], dict) is False
        assert _is_list_of(dict, dict) is False
        assert _is_list_of(str, dict) is False

    def test_takes_json(self):
        from typing import Dict
        from mdclaw._cli import _takes_json

        assert _takes_json(dict) is True
        assert _takes_json(list[dict]) is True
        assert _takes_json(list[dict] | None) is True
        # Optional[list[dict]] strips down to list[dict]
        assert _takes_json(list[Dict[str, str]]) is True
        assert _takes_json(str) is False
        assert _takes_json(list[str]) is False

    def test_coerce_json_to_dict(self):
        from mdclaw._cli import _coerce_value

        result = _coerce_value('{"key": "val"}', dict)
        assert result == {"key": "val"}

    def test_coerce_json_to_list_of_dict(self):
        """Regression: list[dict] args (e.g. submit_array_job.tasks) must
        accept a JSON string at the CLI boundary and deserialize. Before
        the fix, _coerce_value fell through to the str default and the
        tool received a literal JSON string rather than a list.
        """
        from mdclaw._cli import _coerce_value

        payload = '[{"job_dir": "/x", "node_id": "prod_001", "command": "echo"}]'
        result = _coerce_value(payload, list[dict])
        assert isinstance(result, list)
        assert result[0]["node_id"] == "prod_001"

    def test_coerce_int(self):
        from mdclaw._cli import _coerce_value

        assert _coerce_value("42", int) == 42
        assert _coerce_value("42", int | None) == 42

    def test_coerce_float(self):
        from mdclaw._cli import _coerce_value

        assert _coerce_value("3.14", float) == 3.14


# ---------------------------------------------------------------------------
# Subprocess Tests (--list, --version, --help)
# ---------------------------------------------------------------------------


def _run_checkout_cli(*args, cwd=None):
    """Run the checkout's CLI regardless of pytest's invocation directory."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, "-m", "mdclaw._cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        env=env,
    )


class TestSubprocessCLI:
    """Test CLI via subprocess to verify entry point behavior."""

    def test_version(self):
        result = _run_checkout_cli("--version")
        assert result.returncode == 0
        assert "mdclaw" in result.stdout

    def test_list(self):
        result = _run_checkout_cli("--list")
        assert result.returncode == 0
        assert "solvate_structure" in result.stdout
        assert "mdclaw --list-json <tool>" in result.stdout
        assert "Total:" in result.stdout

    def test_list_json(self):
        result = _run_checkout_cli("--list-json")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert "solvate_structure" in tool_names

    def test_tool_help(self):
        result = _run_checkout_cli("solvate_structure", "--help")
        assert result.returncode == 0
        assert "--pdb-file" in result.stdout

    def test_startup_writes_nothing_to_the_working_directory(self, tmp_path):
        """Discovery imports every tool module; none may touch the filesystem.

        `--list` and `--version` do no work, so anything that appears in the
        working directory came from an import-time side effect.
        """
        result = _run_checkout_cli("--list", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_starts_from_a_read_only_working_directory(self, tmp_path):
        """HPC binds and read-only SIF mounts must not stop the CLI starting."""
        workdir = tmp_path / "read-only"
        workdir.mkdir()
        workdir.chmod(0o555)
        try:
            try:
                (workdir / "probe").touch()
            except OSError:
                pass
            else:
                pytest.skip("this user can write to a 0555 directory (root?)")
            result = _run_checkout_cli("--version", cwd=workdir)
        finally:
            workdir.chmod(0o755)
        assert result.returncode == 0, result.stderr
        assert "mdclaw" in result.stdout

    def test_no_args_shows_help(self):
        result = _run_checkout_cli()
        assert result.returncode == 0
        # Should print help text
        assert "mdclaw" in result.stdout.lower() or "usage" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Tool List Output
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HPC Parameters
# ---------------------------------------------------------------------------


class TestHPCParameters:
    """Test new HPC-related CLI parameters for run_production."""

    def test_platform_param(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_production",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
            "--platform", "CUDA",
            "--device-index", "0",
        ])
        assert args.platform == "CUDA"
        assert args.device_index == "0"

    def test_restart_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_production",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
            "--restart-from", "checkpoint.chk",
        ])
        assert args.restart_from == "checkpoint.chk"

    def test_equilibration_restart_from_param(self):
        """run_equilibration accepts --restart-from for eq → eq chaining
        (NPT → NVT → NPT across multiple eq nodes). The CLI auto-derives
        the flag from the new function parameter."""
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_equilibration",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
            "--restart-from", "prior_eq_state.xml",
        ])
        assert args.restart_from == "prior_eq_state.xml"

        # Default is None — fresh equilibration runs from the topo state.xml.
        args_default = parser.parse_args([
            "run_equilibration",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
        ])
        assert args_default.restart_from is None

    def test_minimization_tool_flags(self):
        """run_minimization is a first-class workflow tool."""
        from mdclaw._cli import _build_parser, _discover_tools, _tool_list_json

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_minimization",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
            "--state-xml-file", "sys.state.xml",
            "--max-iterations", "10",
            "--restraint-atoms", "heavy",
        ])
        assert args.max_iterations == 10
        assert args.restraint_atoms == "heavy"

        payload = _tool_list_json(tools)
        run_min = next(
            tool for tool in payload["tools"]
            if tool["name"] == "run_minimization"
        )
        flags = {param["cli_flag"] for param in run_min["parameters"]}
        assert "--max-iterations" in flags
        assert "--restraint-atoms" in flags
        assert "--restraint-force-constant" in flags

    def test_equilibration_time_flags(self):
        """run_equilibration exposes duration flags so agents do not
        convert ns to steps themselves."""
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_equilibration",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
            "--nvt-time-ns", "0.05",
            "--npt-time-ns", "0.1",
            "--pressure-bar", "1.0",
        ])
        assert args.nvt_time_ns == 0.05
        assert args.npt_time_ns == 0.1
        assert args.nvt_steps is None
        assert args.npt_steps is None
        assert args.pressure_bar == 1.0

    def test_equilibration_time_flags_in_list_json(self):
        from mdclaw._cli import _discover_tools, _tool_list_json

        tools = _discover_tools()
        payload = _tool_list_json(tools)
        run_eq = next(
            tool for tool in payload["tools"]
            if tool["name"] == "run_equilibration"
        )
        flags = {param["cli_flag"]: param for param in run_eq["parameters"]}
        assert "--nvt-time-ns" in flags
        assert "--npt-time-ns" in flags
        assert "--nvt-steps" in flags
        assert "--npt-steps" in flags
        assert "--stage" not in flags
        assert "--stage-time-ns" not in flags
        assert "--stage-steps" not in flags
        assert flags["--nvt-time-ns"]["type"] == "Optional[float]"
        assert flags["--npt-time-ns"]["type"] == "Optional[float]"
        assert flags["--nvt-steps"]["type"] == "Optional[int]"
        assert flags["--npt-steps"]["type"] == "Optional[int]"

    def test_hmr_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_production",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
            "--hmr",
            "--timestep-fs", "4.0",
        ])
        assert args.hmr is True
        assert args.timestep_fs == 4.0

    def test_run_settings_default_to_topology_inheritance(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_production",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
        ])
        assert args.hmr is None
        assert args.timestep_fs is None
        assert args.platform == "auto"
        assert args.device_index is None
        assert args.restart_from is None


# ---------------------------------------------------------------------------
# Tool List Output
# ---------------------------------------------------------------------------


class TestSlurmCLIParameters:
    """Test SLURM tool CLI parameter mapping."""

    def test_submit_job_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "submit_job",
            "--script", "echo hello",
            "--partition", "gpu",
            "--gpus", "1",
            "--time-limit", "12:00:00",
        ])
        assert args.script == "echo hello"
        assert args.partition == "gpu"
        assert args.gpus == 1
        assert args.time_limit == "12:00:00"

    def test_check_job_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["check_job", "--job-id", "12345"])
        assert args.job_id == "12345"

    def test_inspect_cluster_no_required(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        # inspect_cluster has no required params
        args = parser.parse_args(["inspect_cluster"])
        assert hasattr(args, "tool_name")

    def test_slurm_tools_in_list(self, capsys):
        from mdclaw._cli import _discover_tools, _print_tool_list

        tools = _discover_tools()
        _print_tool_list(tools)
        captured = capsys.readouterr()
        assert "[slurm]" in captured.out
        assert "submit_job" in captured.out
        assert "check_job" in captured.out
        assert "inspect_cluster" in captured.out


class TestPolicyCLIParameters:
    """Test policy tool CLI parameter mapping."""

    def test_set_policy_params(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "set_policy",
            "--allowed-partitions", "gpu", "cpu",
            "--max-gpus-per-job", "2",
            "--max-nodes", "1",
            "--default-account", "myproject",
        ])
        assert args.allowed_partitions == ["gpu", "cpu"]
        assert args.max_gpus_per_job == 2
        assert args.max_nodes == 1
        assert args.default_account == "myproject"

    def test_show_policy_no_required(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["show_policy"])
        assert hasattr(args, "tool_name")


class TestToolListOutput:
    """Test _print_tool_list formatting."""

    def test_tool_list_grouped_by_server(self, capsys):
        from mdclaw._cli import _discover_tools, _print_tool_list

        tools = _discover_tools()
        _print_tool_list(tools)
        captured = capsys.readouterr()

        assert "[solvation]" in captured.out
        assert "[slurm]" in captured.out
        assert "[node]" in captured.out
        assert "Total:" in captured.out


class TestNodeCLIParameters:
    """Argparse-level regression guards for the node-server CLI tools.

    ``mdclaw create_node --continue-from prod_001`` and
    ``mdclaw update_workflow_state --job-dir ... --node-id ... --status ...``
    are user-facing contracts referenced by skill docs. These tests make
    sure the parser round-trip stays stable even if the underlying
    function signatures are refactored.
    """

    def test_create_node_accepts_continue_from(self):
        """--continue-from prod_001 parses into args.continue_from."""
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "create_node",
            "--job-dir", "/tmp/job",
            "--node-type", "prod",
            "--continue-from", "prod_001",
        ])
        assert args.tool_name == "create_node"
        assert args.continue_from == "prod_001"
        assert args.node_type == "prod"
        # parent_node_ids should default to None / empty when not given
        assert not args.parent_node_ids

    def test_create_node_accepts_continue_from_and_parent_ids_together(self):
        """The parser must not reject ``--continue-from`` +
        ``--parent-node-ids`` at the argparse layer — that combination
        is validated at the *tool* layer (it is a runtime error), so
        the parser accepting both is a feature, not a bug. This test
        pins that contract so it can't silently change to a parser-level
        rejection that bypasses the nicer tool-level error message."""
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "create_node",
            "--job-dir", "/tmp/job",
            "--node-type", "prod",
            "--continue-from", "prod_001",
            "--parent-node-ids", "prod_001",
        ])
        assert args.continue_from == "prod_001"
        assert args.parent_node_ids == ["prod_001"]

    def test_create_node_rejects_global_node_id(self, tmp_path, capsys):
        from mdclaw._cli import main

        job_dir = tmp_path / "global_node_id"
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--job-dir", str(job_dir),
                "--node-id", "my_chosen_id",
                "create_node",
                "--node-type", "prep",
            ])

        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "create_node_id_not_allowed"
        assert "create_node assigns the node id" in payload["message"]
        assert "use the node_id returned by create_node" in payload["message"].lower()
        assert not job_dir.exists()

    def test_create_node_rejects_per_tool_node_id(self, tmp_path, capsys):
        from mdclaw._cli import main

        job_dir = tmp_path / "per_tool_node_id"
        with pytest.raises(SystemExit) as exc_info:
            main([
                "create_node",
                "--job-dir", str(job_dir),
                "--node-id", "my_chosen_id",
                "--node-type", "prep",
            ])

        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "create_node_id_not_allowed"
        assert "create_node assigns the node id" in payload["message"]
        assert "use the node_id returned by create_node" in payload["message"].lower()
        assert not job_dir.exists()

    def test_create_node_rejects_mutual_exclusion_at_tool_layer(self, tmp_path):
        """End-to-end guard: passing both options through the CLI entry
        point yields a non-zero exit (the tool layer catches the mutual
        exclusion) — the argparse accepting both is intentional."""
        from mdclaw._cli import main

        # --job-dir bootstraps progress.json; no real prod node exists, so
        # the tool will also fail validation of the parent reference, but
        # the key assertion is simply "CLI exits non-zero cleanly".
        with pytest.raises(SystemExit) as exc_info:
            main([
                "create_node",
                "--job-dir", str(tmp_path),
                "--node-type", "prod",
                "--continue-from", "prod_001",
                "--parent-node-ids", "prod_001",
            ])
        assert exc_info.value.code != 0

    def test_create_analyze_node_accepts_analysis_data_scope(self, tmp_path, capsys):
        from mdclaw._cli import main
        from mdclaw._node import create_node

        create_node(str(tmp_path), "prod")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "create_node",
                "--job-dir", str(tmp_path),
                "--node-type", "analyze",
                "--parent-node-ids", "prod_001",
                "--conditions", '{"analysis_data_scope":"production_chain"}',
            ])

        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert payload["node_id"] == "analyze_001"

    def test_create_analyze_node_rejects_missing_analysis_data_scope(
        self, tmp_path, capsys
    ):
        from mdclaw._cli import main
        from mdclaw._node import create_node

        create_node(str(tmp_path), "prod")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "create_node",
                "--job-dir", str(tmp_path),
                "--node-type", "analyze",
                "--parent-node-ids", "prod_001",
            ])

        assert exc_info.value.code != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert "analysis_data_scope" in payload["error"]

    def test_update_workflow_state_accepts_status_flags(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "update_workflow_state",
            "--job-dir", "/tmp/job",
            "--node-id", "prod_001",
            "--status", "submitted",
        ])
        assert args.tool_name == "update_workflow_state"
        assert args.job_dir == "/tmp/job"
        assert args.node_id == "prod_001"
        assert args.status == "submitted"

    def test_update_workflow_state_status_requires_node_id(self, tmp_path, capsys):
        """Passing --status without --node-id returns a structured code."""
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_workflow_state",
                "--job-dir", str(tmp_path),
                "--status", "queued",
            ])
        assert exc_info.value.code != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "update_state_status_requires_node_id"

    def test_update_workflow_state_requires_a_target(self, tmp_path, capsys):
        """Neither status nor params yields update_state_no_target."""
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_workflow_state",
                "--job-dir", str(tmp_path),
            ])
        assert exc_info.value.code != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "update_state_no_target"

    def test_update_workflow_state_status_end_to_end(self, tmp_path):
        """Create a node, flip its status via the CLI, then confirm node.json
        and progress.json agree.
        """
        import json
        from mdclaw._cli import main
        from mdclaw._node import create_node

        create_node(str(tmp_path), "prod")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_workflow_state",
                "--job-dir", str(tmp_path),
                "--node-id", "prod_001",
                "--status", "submitted",
            ])
        assert exc_info.value.code == 0

        node = json.loads(
            (tmp_path / "nodes" / "prod_001" / "node.json").read_text()
        )
        progress = json.loads((tmp_path / "progress.json").read_text())
        assert node["status"] == "queued"
        assert progress["nodes"]["prod_001"]["status"] == "queued"

    def test_update_workflow_state_accepts_params_json_dict(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "update_workflow_state",
            "--job-dir", "/tmp/job",
            "--params", '{"execution_mode":"autonomous"}',
        ])
        assert args.tool_name == "update_workflow_state"
        assert args.job_dir == "/tmp/job"
        assert args.params == '{"execution_mode":"autonomous"}'

    def test_update_workflow_state_params_end_to_end(self, tmp_path):
        import json
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_workflow_state",
                "--job-dir", str(tmp_path),
                "--params", '{"execution_mode":"autonomous"}',
            ])
        assert exc_info.value.code == 0

        progress = json.loads((tmp_path / "progress.json").read_text())
        assert progress["params"]["execution_mode"] == "autonomous"

    def test_renamed_tool_returns_structured_code(self, capsys):
        """Old consolidated tool names emit a structured tool_renamed code."""
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_node_status",
                "--job-dir", "/tmp/job",
                "--node-id", "prod_001",
                "--status", "queued",
            ])
        assert exc_info.value.code != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "tool_renamed"
        assert "update_workflow_state" in payload["context"]["replacement"]

    def test_global_job_dir_node_id_satisfy_subparser_required_params(self, tmp_path):
        """Skill docs invoke node tools with global flags placed BEFORE the
        subcommand (``mdclaw --job-dir X --node-id Y <tool> ...``). The
        subparser also declares ``--job-dir``/``--node-id`` whenever the
        tool signature has those parameters, so the CLI must forward the
        global values into the per-tool namespace before the missing-args
        check runs. Otherwise node-required tools like ``fetch_structure``
        error out even when the global flags were supplied.
        """
        from mdclaw._cli import main
        from mdclaw._node import create_node

        if not _dependency_available("httpx"):
            pytest.skip("fetch_structure unavailable because research server dependencies are missing")

        create_node(str(tmp_path), "source")
        src = tmp_path / "input.pdb"
        src.write_text("HEADER    test\nEND\n")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "--job-dir", str(tmp_path),
                "--node-id", "source_001",
                "fetch_structure",
                "--source", "local",
                "--file-path", str(src),
            ])
        assert exc_info.value.code == 0

    def test_fetch_structure_infers_local_source_from_file_path(self, tmp_path):
        from mdclaw._cli import main
        from mdclaw._node import create_node

        if not _dependency_available("httpx"):
            pytest.skip("fetch_structure unavailable because research server dependencies are missing")

        create_node(str(tmp_path), "source")
        src = tmp_path / "input.pdb"
        src.write_text("HEADER    test\nEND\n")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "--job-dir", str(tmp_path),
                "--node-id", "source_001",
                "fetch_structure",
                "--file-path", str(src),
            ])
        assert exc_info.value.code == 0

    def test_fetch_structure_accepts_assembly_ids_list(self):
        from mdclaw._cli import _build_parser, _discover_tools

        if not _dependency_available("httpx"):
            pytest.skip("fetch_structure unavailable because research server dependencies are missing")

        parser = _build_parser(_discover_tools())
        args = parser.parse_args([
            "--job-dir", "/tmp/job",
            "--node-id", "source_001",
            "fetch_structure",
            "--source", "pdb",
            "--pdb-id", "1AKE",
            "--assembly-ids", "1", "2",
            "--assembly-chain-naming", "short",
        ])

        assert args.assembly_ids == ["1", "2"]
        assert args.assembly_chain_naming == "short"


class TestStudyAndEvidenceCLIParameters:
    """Argparse-level guards for optional study/evidence tools."""

    def test_init_study_accepts_metadata_json(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "init_study",
            "--study-dir", "/tmp/study",
            "--title", "screen",
            "--metadata", '{"owner":"lab"}',
        ])
        assert args.study_dir == "/tmp/study"
        assert args.title == "screen"
        assert args.metadata == '{"owner":"lab"}'

    def test_record_study_log_accepts_list_args(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "record_study_log",
            "--study-dir", "/tmp/study",
            "--record-type", "decision",
            "--phase", "plan",
            "--decision", "run",
            "--reason", "test",
            "--inputs", "study.json", "progress.json",
            "--outputs", "plan.json",
        ])
        assert args.record_type == "decision"
        assert args.inputs == ["study.json", "progress.json"]
        assert args.outputs == ["plan.json"]

    def test_record_study_plan_accepts_plan_json(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "record_study_plan",
            "--study-dir", "/tmp/study",
            "--plan", (
                '{"question":"q","md_goal":"g","jobs":[],'
                '"analysis":[],"decision":{}}'
            ),
        ])
        assert args.study_dir == "/tmp/study"
        assert '"md_goal":"g"' in args.plan

    def test_generate_md_evidence_report_parses_target_json(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "generate_md_evidence_report",
            "--job-dir", "/tmp/job",
            "--target", '{"protein":"P12345"}',
        ])
        assert args.job_dir == "/tmp/job"
        assert args.target == '{"protein":"P12345"}'

    def test_generate_study_evidence_report_accepts_plan_id(self):
        from mdclaw._cli import _build_parser, _discover_tools

        parser = _build_parser(_discover_tools())
        args = parser.parse_args([
            "generate_study_evidence_report",
            "--study-dir", "/tmp/study",
            "--plan-id", "revision-1",
        ])

        assert args.plan_id == "revision-1"

    def test_init_study_end_to_end(self, tmp_path):
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "init_study",
                "--study-dir", str(tmp_path / "study"),
                "--title", "screen",
            ])
        assert exc_info.value.code == 0
        assert (tmp_path / "study" / "study.json").is_file()

    def test_add_study_job_preserves_relative_job_dir(self, tmp_path):
        from mdclaw._cli import main

        study_dir = tmp_path / "study"
        with pytest.raises(SystemExit) as exc_info:
            main([
                "init_study",
                "--study-dir", str(study_dir),
                "--title", "screen",
            ])
        assert exc_info.value.code == 0

        with pytest.raises(SystemExit) as exc_info:
            main([
                "add_study_job",
                "--study-dir", str(study_dir),
                "--job-id", "wt",
                "--job-dir", "jobs/wt",
                "--create-job-dir",
            ])
        assert exc_info.value.code == 0
        assert (study_dir / "jobs" / "wt").is_dir()

    def test_record_study_plan_end_to_end(self, tmp_path):
        from mdclaw._cli import main

        study_dir = tmp_path / "study"
        with pytest.raises(SystemExit) as exc_info:
            main([
                "init_study",
                "--study-dir", str(study_dir),
                "--title", "screen",
            ])
        assert exc_info.value.code == 0

        with pytest.raises(SystemExit) as exc_info:
            main([
                "record_study_plan",
                "--study-dir", str(study_dir),
                "--plan", (
                    '{"question":"q","md_goal":"g","jobs":[],'
                    '"analysis":[],"decision":{}}'
                ),
            ])
        assert exc_info.value.code == 0
        plan = json.loads((study_dir / "study_plan.json").read_text())
        assert plan["plan"]["question"] == "q"


def test_benchmark_min_stage_is_reserved_for_run_minimization():
    from mdclaw._benchmark_log import _benchmark_stage_for_tool

    assert _benchmark_stage_for_tool("run_minimization") == "min"
    assert _benchmark_stage_for_tool("create_node") == "dag"
    assert _benchmark_stage_for_tool("export_state_pdb") == "export"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_benchmark_harness_record_appends_jsonl(tmp_path, monkeypatch):
    """The stage-record hook is a cross-repo protocol: the standalone benchmark
    harnesses parse this JSONL, so the write path itself needs a test here."""
    from mdclaw import _benchmark_log

    log = tmp_path / "harness_execution.jsonl"
    monkeypatch.setenv("MDCLAW_BENCHMARK_HARNESS_LOG", str(log))
    monkeypatch.setenv("MDCLAW_BENCHMARK_RUN_ID", "r1")
    monkeypatch.setenv("MDCLAW_BENCHMARK_TASK_ID", "t1")
    monkeypatch.setattr("sys.argv", ["mdclaw", "run_minimization", "--x"])

    _benchmark_log._write_benchmark_harness_record(
        tool_name="run_minimization", exit_code=0,
        started_at=__import__("time").monotonic(),
    )

    record = json.loads(log.read_text().splitlines()[0])
    assert record["stage"] == "min"
    assert record["tool"] == "run_minimization"
    assert record["run_id"] == "r1" and record["task_id"] == "t1"
    assert record["exit_code"] == 0
    assert record["walltime_seconds"] >= 0


def test_benchmark_harness_record_is_noop_without_env(tmp_path, monkeypatch):
    from mdclaw import _benchmark_log

    monkeypatch.delenv("MDCLAW_BENCHMARK_HARNESS_LOG", raising=False)
    _benchmark_log._write_benchmark_harness_record(
        tool_name="run_minimization", exit_code=0,
        started_at=__import__("time").monotonic(),
    )
    assert list(tmp_path.iterdir()) == []
