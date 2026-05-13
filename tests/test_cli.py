"""Level 1: Unit tests for the mdclaw CLI module.

No external tools (ambertools, openmm, etc.) required.
Tests validate tool discovery, argparse construction, parameter coercion,
and CLI subcommand output.
"""

import json
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest


def _pick_existing_tool(tools, *preferred_names):
    """Return the first preferred tool present in the discovered tool set."""
    for name in preferred_names:
        if name in tools:
            return name
    pytest.skip(f"None of the preferred tools are available: {preferred_names}")


def _dependency_available(module_name):
    return find_spec(module_name) is not None


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
            assert "download_structure" in tools
            assert tools["download_structure"]["is_async"] is True
        else:
            pytest.skip("fetch tools unavailable because research server dependencies are missing")

        sync_tool = _pick_existing_tool(tools, "inspect_molecules", "solvate_structure", "build_amber_system")
        assert tools[sync_tool]["is_async"] is False

    def test_key_tools_present_when_dependencies_available(self):
        from mdclaw._cli import _discover_tools

        tools = _discover_tools()

        if _dependency_available("httpx"):
            assert "fetch_structure" in tools
            assert "download_structure" in tools
        if _dependency_available("pdbfixer"):
            assert "split_molecules" in tools
            assert "inspect_molecules" in tools
            assert "prepare_modified_nucleic" in tools

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
            "download_structure",
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

        # fetch_structure requires --source; omitting it should exit non-zero
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

    def test_node_required_tool_set_covers_dag_mutators(self):
        """Tools that create or complete workflow nodes must be CLI-gated."""
        from mdclaw._cli import _NODE_REQUIRED_TOOLS

        expected = {
            "create_mutated_structure",
            "phosphorylate_residues",
            "prepare_modified_nucleic",
            "analyze_rmsf",
            "analyze_contact_frequency",
        }
        assert expected <= _NODE_REQUIRED_TOOLS
        assert "render_structure_preview" not in _NODE_REQUIRED_TOOLS

    def test_optional_params_have_defaults(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["solvate_structure", "--pdb-file", "test.pdb"])
        assert args.pdb_file == "test.pdb"
        assert args.water_model == "opc"  # default

    def test_embed_in_membrane_pdb_file_is_optional_for_autoresolve(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        _pick_existing_tool(tools, "embed_in_membrane")
        parser = _build_parser(tools)

        args = parser.parse_args(["embed_in_membrane", "--lipids", "POPC"])
        assert args.pdb_file is None
        assert args.lipids == "POPC"
        assert args.water_model == "opc"  # default

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
                "update_job_params",
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
        assert args.list_tools_json is True

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
        params = {param["name"]: param for param in solvate["parameters"]}
        assert params["pdb_file"]["cli_flag"] == "--pdb-file"
        assert params["water_model"]["default"] == "opc"
        assert params["salt"]["cli_action"] == "boolean_optional"

    def test_pep604_optional_params_are_typed_in_parser_and_list_json(self):
        from mdclaw._cli import _build_parser, _discover_tools, _tool_list_json

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "parameterize_metal_ion",
            "--metal-resname", "ZN",
            "--metal-charge", "2",
        ])
        assert args.metal_charge == 2

        if "search_structures" in tools:
            args = parser.parse_args([
                "search_structures",
                "--query", "kinase",
                "--no-has-ligand",
            ])
            assert args.has_ligand is False

        payload = _tool_list_json(tools)
        parameterize = next(
            tool for tool in payload["tools"]
            if tool["name"] == "parameterize_metal_ion"
        )
        params = {param["name"]: param for param in parameterize["parameters"]}
        assert params["metal_charge"]["type"] == "Optional[int]"


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

    def test_is_list_of_dict(self):
        from typing import Dict, List
        from mdclaw._cli import _is_list_of_dict

        assert _is_list_of_dict(list[dict]) is True
        assert _is_list_of_dict(List[Dict[str, str]]) is True
        assert _is_list_of_dict(list[str]) is False
        assert _is_list_of_dict(dict) is False
        assert _is_list_of_dict(str) is False

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


class TestSubprocessCLI:
    """Test CLI via subprocess to verify entry point behavior."""

    def test_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "mdclaw._cli", "--version"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "mdclaw" in result.stdout

    def test_list(self):
        result = subprocess.run(
            [sys.executable, "-m", "mdclaw._cli", "--list"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "solvate_structure" in result.stdout
        assert "Total:" in result.stdout

    def test_list_json(self):
        result = subprocess.run(
            [sys.executable, "-m", "mdclaw._cli", "--list-json"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert "solvate_structure" in tool_names
        assert "init_benchmark_run" not in tool_names
        assert "summarize_benchmark_run" not in tool_names

    def test_tool_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "mdclaw._cli", "solvate_structure", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "--pdb-file" in result.stdout

    def test_no_args_shows_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "mdclaw._cli"],
            capture_output=True, text=True,
        )
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
            "--nvt-time-ns", "0.1",
            "--npt-time-ns", "0.1",
        ])
        assert args.nvt_time_ns == 0.1
        assert args.npt_time_ns == 0.1
        assert args.nvt_steps is None
        assert args.npt_steps is None

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
        assert flags["--nvt-time-ns"]["type"] == "Optional[float]"
        assert flags["--npt-time-ns"]["type"] == "Optional[float]"

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

    def test_hmr_default_true(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_production",
            "--system-xml-file", "sys.system.xml",
            "--topology-pdb-file", "sys.topology.pdb",
        ])
        assert args.hmr is True
        assert args.timestep_fs == 4.0
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
    ``mdclaw update_node_status --job-dir ... --node-id ... --status ...``
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

    def test_update_node_status_accepts_required_flags(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "update_node_status",
            "--job-dir", "/tmp/job",
            "--node-id", "prod_001",
            "--status", "submitted",
        ])
        assert args.tool_name == "update_node_status"
        assert args.job_dir == "/tmp/job"
        assert args.node_id == "prod_001"
        assert args.status == "submitted"

    def test_update_node_status_requires_all_three_fields(self):
        """Missing any of job-dir / node-id / status exits non-zero."""
        from mdclaw._cli import main

        for missing in (
            ["update_node_status", "--node-id", "x", "--status", "s"],
            ["update_node_status", "--job-dir", "/tmp/j", "--status", "s"],
            ["update_node_status", "--job-dir", "/tmp/j", "--node-id", "x"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main(missing)
            assert exc_info.value.code != 0, f"expected failure for: {missing}"

    def test_update_node_status_end_to_end(self, tmp_path):
        """Smoke test the full CLI path for update_node_status against a
        real temp job directory: create a node, flip its status via the
        CLI entry point, then confirm both node.json and progress.json
        agree.
        """
        import json
        from mdclaw._cli import main
        from mdclaw._node import create_node

        create_node(str(tmp_path), "prod")

        # main() always raises SystemExit (exit_code=0 on success)
        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_node_status",
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

    def test_update_job_params_accepts_json_dict(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "update_job_params",
            "--job-dir", "/tmp/job",
            "--params", '{"execution_mode":"autonomous"}',
        ])
        assert args.tool_name == "update_job_params"
        assert args.job_dir == "/tmp/job"
        assert args.params == '{"execution_mode":"autonomous"}'

    def test_update_job_params_end_to_end(self, tmp_path):
        import json
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc_info:
            main([
                "update_job_params",
                "--job-dir", str(tmp_path),
                "--params", '{"execution_mode":"autonomous"}',
            ])
        assert exc_info.value.code == 0

        progress = json.loads((tmp_path / "progress.json").read_text())
        assert progress["params"]["execution_mode"] == "autonomous"

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

    def test_record_study_decision_accepts_list_args(self):
        from mdclaw._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "record_study_decision",
            "--study-dir", "/tmp/study",
            "--phase", "plan",
            "--decision", "run",
            "--reason", "test",
            "--inputs", "study.json", "progress.json",
            "--outputs", "plan.json",
        ])
        assert args.inputs == ["study.json", "progress.json"]
        assert args.outputs == ["plan.json"]

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
