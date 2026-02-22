"""Level 1: Unit tests for the mdclaw CLI module.

No external tools (ambertools, openmm, etc.) required.
Tests validate tool discovery, argparse construction, parameter coercion,
and CLI subcommand output.
"""

import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Tool Discovery
# ---------------------------------------------------------------------------


class TestToolDiscovery:
    """Test _discover_tools() finds all registered tools."""

    def test_discovers_all_tools(self):
        from servers._cli import _discover_tools

        tools = _discover_tools()
        assert len(tools) >= 45, f"Expected >=45 tools, got {len(tools)}"

    def test_each_tool_has_required_keys(self):
        from servers._cli import _discover_tools

        tools = _discover_tools()
        for name, info in tools.items():
            assert "fn" in info, f"{name} missing 'fn'"
            assert "is_async" in info, f"{name} missing 'is_async'"
            assert "server" in info, f"{name} missing 'server'"
            assert "description" in info, f"{name} missing 'description'"
            assert callable(info["fn"]), f"{name} fn is not callable"

    def test_async_detection(self):
        from servers._cli import _discover_tools

        tools = _discover_tools()
        # download_structure is async
        assert tools["download_structure"]["is_async"] is True
        # inspect_molecules is sync
        assert tools["inspect_molecules"]["is_async"] is False

    def test_all_servers_represented(self):
        from servers._cli import _discover_tools
        from servers._registry import SERVER_REGISTRY

        tools = _discover_tools()
        servers_found = {info["server"] for info in tools.values()}
        for server_name in SERVER_REGISTRY:
            assert server_name in servers_found, f"Server '{server_name}' has no tools"


# ---------------------------------------------------------------------------
# argparse Construction
# ---------------------------------------------------------------------------


class TestArgparseConstruction:
    """Test _build_parser() creates correct subcommands and args."""

    def test_subcommands_created(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        # Parser should have subparsers with all tool names
        # Test by parsing a known tool with --help (should not raise)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["download_structure", "--help"])
        assert exc_info.value.code == 0

    def test_required_params(self):
        """Missing required params causes non-zero exit via main()."""
        from servers._cli import main

        # download_structure requires --pdb-id; omitting it should exit non-zero
        with pytest.raises(SystemExit) as exc_info:
            main(["download_structure"])
        assert exc_info.value.code != 0

    def test_optional_params_have_defaults(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["download_structure", "--pdb-id", "1AKE"])
        assert args.pdb_id == "1AKE"
        assert args.format == "pdb"  # default

    def test_bool_params(self):
        from servers._cli import _build_parser, _discover_tools

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
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "split_molecules",
            "--structure-file", "test.pdb",
            "--select-chains", "A", "B",
        ])
        assert args.select_chains == ["A", "B"]

    def test_json_input(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        json_str = '{"pdb_id": "1AKE", "format": "cif"}'
        args = parser.parse_args(["download_structure", "--json-input", json_str])
        assert args.json_input == json_str

    def test_list_flag(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["--list"])
        assert args.list_tools is True


# ---------------------------------------------------------------------------
# Parameter Coercion
# ---------------------------------------------------------------------------


class TestParameterCoercion:
    """Test _coerce_value and _unwrap_optional helpers."""

    def test_unwrap_optional_str(self):
        from typing import Optional
        from servers._cli import _unwrap_optional

        inner, is_opt = _unwrap_optional(Optional[str])
        assert inner is str
        assert is_opt is True

    def test_unwrap_non_optional(self):
        from servers._cli import _unwrap_optional

        inner, is_opt = _unwrap_optional(str)
        assert inner is str
        assert is_opt is False

    def test_is_list_of_str(self):
        from typing import List
        from servers._cli import _is_list_of_str

        assert _is_list_of_str(List[str]) is True
        assert _is_list_of_str(list[str]) is True
        assert _is_list_of_str(str) is False
        assert _is_list_of_str(List[int]) is False

    def test_is_dict_type(self):
        from typing import Dict
        from servers._cli import _is_dict_type

        assert _is_dict_type(dict) is True
        assert _is_dict_type(Dict[str, str]) is True
        assert _is_dict_type(str) is False

    def test_coerce_json_to_dict(self):
        from servers._cli import _coerce_value

        result = _coerce_value('{"key": "val"}', dict)
        assert result == {"key": "val"}

    def test_coerce_int(self):
        from servers._cli import _coerce_value

        assert _coerce_value("42", int) == 42

    def test_coerce_float(self):
        from servers._cli import _coerce_value

        assert _coerce_value("3.14", float) == 3.14


# ---------------------------------------------------------------------------
# Subprocess Tests (--list, --version, --help)
# ---------------------------------------------------------------------------


class TestSubprocessCLI:
    """Test CLI via subprocess to verify entry point behavior."""

    def test_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "servers._cli", "--version"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "mdclaw" in result.stdout

    def test_list(self):
        result = subprocess.run(
            [sys.executable, "-m", "servers._cli", "--list"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "download_structure" in result.stdout
        assert "Total:" in result.stdout

    def test_tool_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "servers._cli", "download_structure", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "--pdb-id" in result.stdout

    def test_no_args_shows_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "servers._cli"],
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
    """Test new HPC-related CLI parameters for run_md_simulation."""

    def test_platform_param(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_md_simulation",
            "--prmtop-file", "sys.parm7",
            "--inpcrd-file", "sys.rst7",
            "--platform", "CUDA",
            "--device-index", "0",
        ])
        assert args.platform == "CUDA"
        assert args.device_index == "0"

    def test_restart_params(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_md_simulation",
            "--prmtop-file", "sys.parm7",
            "--inpcrd-file", "sys.rst7",
            "--restart-from", "checkpoint.chk",
        ])
        assert args.restart_from == "checkpoint.chk"

    def test_hmr_params(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_md_simulation",
            "--prmtop-file", "sys.parm7",
            "--inpcrd-file", "sys.rst7",
            "--hmr",
            "--timestep-fs", "4.0",
        ])
        assert args.hmr is True
        assert args.timestep_fs == 4.0

    def test_hmr_default_false(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args([
            "run_md_simulation",
            "--prmtop-file", "sys.parm7",
            "--inpcrd-file", "sys.rst7",
        ])
        assert args.hmr is False
        assert args.platform == "auto"
        assert args.device_index is None
        assert args.restart_from is None


# ---------------------------------------------------------------------------
# Tool List Output
# ---------------------------------------------------------------------------


class TestSlurmCLIParameters:
    """Test SLURM tool CLI parameter mapping."""

    def test_submit_job_params(self):
        from servers._cli import _build_parser, _discover_tools

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
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["check_job", "--job-id", "12345"])
        assert args.job_id == "12345"

    def test_inspect_cluster_no_required(self):
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        # inspect_cluster has no required params
        args = parser.parse_args(["inspect_cluster"])
        assert hasattr(args, "tool_name")

    def test_slurm_tools_in_list(self, capsys):
        from servers._cli import _discover_tools, _print_tool_list

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
        from servers._cli import _build_parser, _discover_tools

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
        from servers._cli import _build_parser, _discover_tools

        tools = _discover_tools()
        parser = _build_parser(tools)

        args = parser.parse_args(["show_policy"])
        assert hasattr(args, "tool_name")


class TestToolListOutput:
    """Test _print_tool_list formatting."""

    def test_tool_list_grouped_by_server(self, capsys):
        from servers._cli import _discover_tools, _print_tool_list

        tools = _discover_tools()
        _print_tool_list(tools)
        captured = capsys.readouterr()

        assert "[research]" in captured.out
        assert "[structure]" in captured.out
        assert "[solvation]" in captured.out
        assert "Total:" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
