"""Level 1: Unit tests for unified MCP server and config.

No external tools (ambertools, openmm, etc.) required.
These tests validate the MCP server registry, import machinery, and configuration system.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Server Registry
# ---------------------------------------------------------------------------


class TestServerRegistry:
    """Test the SERVER_REGISTRY dict in _mcp_main.py."""

    def test_registry_has_all_servers(self):
        from servers._mcp_main import SERVER_REGISTRY

        assert len(SERVER_REGISTRY) == 8

    def test_registry_keys(self):
        from servers._mcp_main import SERVER_REGISTRY

        expected = {
            "research",
            "structure",
            "solvation",
            "amber",
            "md_simulation",
            "genesis",
            "literature",
            "metal",
        }
        assert set(SERVER_REGISTRY.keys()) == expected

    def test_registry_module_paths(self):
        from servers._mcp_main import SERVER_REGISTRY

        for name, module_path in SERVER_REGISTRY.items():
            assert module_path == f"servers.{name}_server"


# ---------------------------------------------------------------------------
# Import Servers
# ---------------------------------------------------------------------------


class TestImportServers:
    """Test _import_servers function."""

    def test_import_all_servers(self):
        """_import_servers(None) should attempt to import all 8 servers."""
        from servers._mcp_main import SERVER_REGISTRY, _import_servers

        # We patch importlib.import_module to track which modules are imported
        imported = []
        original_import = importlib.import_module

        def mock_import(name):
            imported.append(name)
            return original_import(name)

        with patch("importlib.import_module", side_effect=mock_import):
            _import_servers(None)

        for name, module_path in SERVER_REGISTRY.items():
            assert module_path in imported, f"{module_path} was not imported"

    def test_import_selective(self):
        """_import_servers(["research"]) should import only research."""
        imported = []
        original_import = importlib.import_module

        def mock_import(name):
            imported.append(name)
            return original_import(name)

        with patch("importlib.import_module", side_effect=mock_import):
            from servers._mcp_main import _import_servers

            _import_servers(["research"])

        assert "servers.research_server" in imported
        assert "servers.structure_server" not in imported

    def test_import_unknown_server(self, capsys):
        """Unknown name prints warning, doesn't crash."""
        from servers._mcp_main import _import_servers

        _import_servers(["nonexistent_server"])
        captured = capsys.readouterr()
        assert "Unknown server" in captured.err
        assert "nonexistent_server" in captured.err

    def test_each_server_has_mcp_attribute(self):
        """Each server module exposes a `mcp` FastMCP instance."""
        from servers._mcp_main import SERVER_REGISTRY

        for name, module_path in SERVER_REGISTRY.items():
            try:
                mod = importlib.import_module(module_path)
            except ImportError:
                pytest.skip(f"Cannot import {module_path} (missing dependency)")
            assert hasattr(mod, "mcp"), f"{module_path} missing 'mcp' attribute"


# ---------------------------------------------------------------------------
# Config (get_timeout in servers/_common.py)
# ---------------------------------------------------------------------------


class TestConfig:
    """Test get_timeout() in servers/_common.py."""

    def test_timeout_defaults(self):
        from servers._common import get_timeout

        assert get_timeout("default") == 300
        assert get_timeout("solvation") == 7200
        assert get_timeout("membrane") == 7200
        assert get_timeout("amber") == 900
        assert get_timeout("md_simulation") == 3600
        assert get_timeout("structure") == 600

    def test_timeout_unknown_type(self):
        from servers._common import get_timeout

        # Unknown type falls back to default
        assert get_timeout("unknown_thing") == 300

    def test_env_override(self, monkeypatch):
        """MDZEN_AMBER_TIMEOUT=999 overrides the default."""
        monkeypatch.setenv("MDZEN_AMBER_TIMEOUT", "999")
        from servers._common import get_timeout

        assert get_timeout("amber") == 999


# ---------------------------------------------------------------------------
# Package Init
# ---------------------------------------------------------------------------


class TestPackageInit:
    """Test servers package __init__.py."""

    def test_version(self):
        from servers import __version__

        assert __version__ == "0.4.0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
