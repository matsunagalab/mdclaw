"""Level 1: Unit tests for tool registry and config.

No external tools (ambertools, openmm, etc.) required.
These tests validate the tool registry, server imports, and configuration system.
"""

import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Server Registry
# ---------------------------------------------------------------------------


class TestServerRegistry:
    """Test the SERVER_REGISTRY dict in _registry.py."""

    def test_registry_has_all_servers(self):
        from mdclaw._registry import SERVER_REGISTRY

        assert len(SERVER_REGISTRY) == 10

    def test_registry_keys(self):
        from mdclaw._registry import SERVER_REGISTRY

        expected = {
            "research",
            "structure",
            "solvation",
            "amber",
            "md_simulation",
            "genesis",
            "literature",
            "metal",
            "slurm",
            "node",
        }
        assert set(SERVER_REGISTRY.keys()) == expected

    def test_registry_module_paths(self):
        from mdclaw._registry import SERVER_REGISTRY

        for name, module_path in SERVER_REGISTRY.items():
            assert module_path == f"mdclaw.{name}_server"


# ---------------------------------------------------------------------------
# Import Servers
# ---------------------------------------------------------------------------


class TestImportServers:
    """Test that server modules can be imported and have TOOLS dicts."""

    def test_each_server_has_tools_dict(self):
        """Each server module exposes a `TOOLS` dict."""
        from mdclaw._registry import SERVER_REGISTRY

        for name, module_path in SERVER_REGISTRY.items():
            try:
                mod = importlib.import_module(module_path)
            except ImportError:
                pytest.skip(f"Cannot import {module_path} (missing dependency)")
            assert hasattr(mod, "TOOLS"), f"{module_path} missing 'TOOLS' dict"
            assert isinstance(mod.TOOLS, dict), f"{module_path}.TOOLS is not a dict"
            assert len(mod.TOOLS) > 0, f"{module_path}.TOOLS is empty"
            for tool_name, fn in mod.TOOLS.items():
                assert callable(fn), f"{module_path}.TOOLS['{tool_name}'] is not callable"

    def test_run_production_has_random_seed_param(self):
        """run_production accepts a random_seed parameter."""
        import inspect
        from mdclaw.md_simulation_server import run_production

        sig = inspect.signature(run_production)
        assert "random_seed" in sig.parameters, "run_production missing 'random_seed' param"
        param = sig.parameters["random_seed"]
        assert param.default is None, "random_seed default should be None"

    def test_node_server_exposes_update_node_status(self):
        """Batch workflows depend on `mdclaw update_node_status` being a
        CLI-registered tool so that status edits stay consistent across
        node.json and the progress.json index."""
        from mdclaw.node_server import TOOLS

        assert "update_node_status" in TOOLS
        assert callable(TOOLS["update_node_status"])

    def test_node_server_exposes_create_node_with_continue_from(self):
        """continue_from must remain an exposed parameter of create_node
        so skill docs that call `--continue-from` keep working."""
        import inspect
        from mdclaw.node_server import TOOLS

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
        assert get_timeout("amber") == 900
        assert get_timeout("md_simulation") == 3600
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
        from mdclaw import __version__

        assert __version__ == "0.5.0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
