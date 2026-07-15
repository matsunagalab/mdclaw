"""Level 1: Unit tests for tool registry and config.

No external tools (ambertools, openmm, etc.) required.
These tests validate the tool registry, server imports, and configuration system.
"""

import importlib

import pytest

from mdclaw._registry import SERVER_REGISTRY


# ---------------------------------------------------------------------------
# Server Registry
# ---------------------------------------------------------------------------


class TestServerRegistry:
    """Test the SERVER_REGISTRY dict in _registry.py."""

    def test_registry_has_all_servers(self):
        assert len(SERVER_REGISTRY) == 18

    def test_registry_keys(self):
        expected = {
            "research",
            "structure",
            "solvation",
            "amber",
            "openmm_system",
            "md_simulation",
            "genesis",
            "surrogate",
            "literature",
            "metal",
            "slurm",
            "node",
            "analyze",
            "visualization",
            "study",
            "evidence",
            "benchmark",
            "throughput",
        }
        assert set(SERVER_REGISTRY.keys()) == expected

    def test_registry_module_paths(self):
        # Every server is now a canonical package under mdclaw/. The package
        # name matches the registry key except for md_simulation, whose package
        # is mdclaw.simulation.
        expected_overrides = {
            "md_simulation": "mdclaw.simulation",
        }
        for name, module_path in SERVER_REGISTRY.items():
            assert module_path == expected_overrides.get(name, f"mdclaw.{name}")


# ---------------------------------------------------------------------------
# Import Servers
# ---------------------------------------------------------------------------


class TestImportServers:
    """Test that server modules can be imported and have TOOLS dicts."""

    @pytest.mark.parametrize(
        ("name", "module_path"),
        tuple(SERVER_REGISTRY.items()),
        ids=tuple(SERVER_REGISTRY),
    )
    def test_each_server_has_tools_dict(self, name, module_path):
        """Each server module exposes a `TOOLS` dict."""
        try:
            mod = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            missing_module = exc.name or ""
            if missing_module == "mdclaw" or missing_module.startswith("mdclaw."):
                pytest.fail(
                    f"Registered server {name!r} is missing module {missing_module!r}"
                )
            pytest.skip(
                f"Cannot import {module_path} (missing dependency {missing_module!r})"
            )
        except ImportError as exc:
            pytest.fail(f"Cannot import registered server {module_path}: {exc}")

        assert hasattr(mod, "TOOLS"), f"{module_path} missing 'TOOLS' dict"
        assert isinstance(mod.TOOLS, dict), f"{module_path}.TOOLS is not a dict"
        assert len(mod.TOOLS) > 0, f"{module_path}.TOOLS is empty"
        for tool_name, fn in mod.TOOLS.items():
            assert callable(fn), f"{module_path}.TOOLS['{tool_name}'] is not callable"

    def test_run_production_has_random_seed_param(self):
        """run_production accepts a random_seed parameter."""
        import inspect
        from mdclaw.simulation.production import run_production

        sig = inspect.signature(run_production)
        assert "random_seed" in sig.parameters, "run_production missing 'random_seed' param"
        param = sig.parameters["random_seed"]
        assert param.default is None, "random_seed default should be None"

    def test_run_production_has_custom_force_params(self):
        """run_production exposes the custom force / CV bias parameters."""
        import inspect
        from mdclaw.simulation.production import run_production

        sig = inspect.signature(run_production)
        for name in (
            "custom_force_script",
            "custom_force_parameters",
        ):
            assert name in sig.parameters, f"run_production missing {name!r}"
            assert sig.parameters[name].default is None
        assert "custom_force_module" not in sig.parameters

    def test_md_simulation_platform_preflight_registered(self):
        """Local-run feasibility preflight is exposed as a CLI/MCP tool."""
        from mdclaw.simulation import TOOLS

        assert "inspect_openmm_platforms" in TOOLS
        assert callable(TOOLS["inspect_openmm_platforms"])
        assert "export_state_pdb" in TOOLS
        assert callable(TOOLS["export_state_pdb"])
        assert "run_minimization" in TOOLS
        assert callable(TOOLS["run_minimization"])

    def test_node_server_exposes_update_workflow_state(self):
        """Batch workflows depend on `mdclaw update_workflow_state` being a
        CLI-registered tool so that status and job-param edits stay consistent
        across node.json and the progress.json index. It consolidates the
        former update_node_status and update_job_params tools."""
        from mdclaw.node import TOOLS

        assert "update_workflow_state" in TOOLS
        assert callable(TOOLS["update_workflow_state"])
        # The merged tool replaced these; they must not linger on the surface.
        assert "update_node_status" not in TOOLS
        assert "update_job_params" not in TOOLS

    def test_node_server_exposes_manage_node_need(self):
        """Open-need management is consolidated behind manage_node_need."""
        from mdclaw.node import TOOLS

        assert "manage_node_need" in TOOLS
        assert callable(TOOLS["manage_node_need"])
        assert "add_node_need" not in TOOLS
        assert "clear_node_need" not in TOOLS
        assert "record_node_need_attempt" not in TOOLS

    def test_node_server_exposes_read_only_inspection_tools(self):
        """Weak-agent re-entry uses read-only node inspection tools."""
        from mdclaw.node import TOOLS

        assert "inspect_job" in TOOLS
        assert callable(TOOLS["inspect_job"])
        assert "wait_node" in TOOLS
        assert callable(TOOLS["wait_node"])
        assert "explain_node" in TOOLS
        assert callable(TOOLS["explain_node"])

    def test_node_server_exposes_create_node_with_continue_from(self):
        """continue_from must remain an exposed parameter of create_node
        so skill docs that call `--continue-from` keep working."""
        import inspect
        from mdclaw.node import TOOLS

        sig = inspect.signature(TOOLS["create_node"])
        assert "continue_from" in sig.parameters


# ---------------------------------------------------------------------------
# Config (get_timeout in servers/_common.py)
# ---------------------------------------------------------------------------


class TestConfig:
    """Test get_timeout() in servers/_common.py."""

    def test_timeout_defaults(self):
        from mdclaw._common import get_timeout

        assert get_timeout("default") == 300
        assert get_timeout("solvation") == 7200
        assert get_timeout("membrane") == 7200
        assert get_timeout("amber") == 3600  # bumped in Fix A to cover ~450-residue fusions
        assert get_timeout("md_simulation") == 3600
        assert get_timeout("visualization") == 300
        assert get_timeout("structure") == 600

    def test_timeout_unknown_type(self):
        from mdclaw._common import get_timeout

        # Unknown type falls back to default
        assert get_timeout("unknown_thing") == 300

    def test_env_override(self, monkeypatch):
        """MDCLAW_AMBER_TIMEOUT=999 overrides the default."""
        monkeypatch.setenv("MDCLAW_AMBER_TIMEOUT", "999")
        from mdclaw._common import get_timeout

        assert get_timeout("amber") == 999

    def test_invalid_env_override_falls_back(self, monkeypatch):
        """Invalid timeout env vars should not crash CLI discovery or tools."""
        monkeypatch.setenv("MDCLAW_SOLVATION_TIMEOUT", "not-an-int")
        from mdclaw._common import get_timeout

        assert get_timeout("solvation") == 7200

    def test_non_positive_env_override_falls_back(self, monkeypatch):
        monkeypatch.setenv("MDCLAW_DEFAULT_TIMEOUT", "0")
        from mdclaw._common import get_timeout

        assert get_timeout("default") == 300


# ---------------------------------------------------------------------------
# HPC Utilities (get_module_loads)
# ---------------------------------------------------------------------------


class TestHPCUtilities:
    """Test HPC utility functions in servers/_common.py."""

    def test_get_module_loads_empty(self, monkeypatch):
        monkeypatch.delenv("MDCLAW_MODULE_LOADS", raising=False)
        from mdclaw._common import get_module_loads

        assert get_module_loads() == []

    def test_get_module_loads_set(self, monkeypatch):
        monkeypatch.setenv("MDCLAW_MODULE_LOADS", "cuda/12.0 amber/24")
        from mdclaw._common import get_module_loads

        assert get_module_loads() == ["cuda/12.0", "amber/24"]

    def test_get_module_loads_whitespace_only(self, monkeypatch):
        monkeypatch.setenv("MDCLAW_MODULE_LOADS", "   ")
        from mdclaw._common import get_module_loads

        assert get_module_loads() == []


# ---------------------------------------------------------------------------
# Package Init
# ---------------------------------------------------------------------------


class TestPackageInit:
    """Test servers package __init__.py."""

    def test_version(self):
        """``__version__`` must stay in sync with ``pyproject.toml``.

        Derives the expected value from the single source of truth instead of a
        hardcoded literal, so a release bump (see ``docs/developer/release.md``)
        cannot leave this test asserting a stale version.
        """
        import tomllib
        from pathlib import Path

        from mdclaw import __version__

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        expected = tomllib.loads(pyproject.read_text())["project"]["version"]
        assert __version__ == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
