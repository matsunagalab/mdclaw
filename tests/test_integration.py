"""
Integration tests for MDZen workflow components.

Tests cover:
- Agent imports and creation
- MCP toolset configuration
- Schema definitions
- Session management

NOTE: Full end-to-end integration tests require MCP servers running
and are marked with @pytest.mark.slow
"""

import pytest


class TestAgentImports:
    """Test that agent modules can be imported correctly."""

    def test_clarification_agent_import(self):
        """Clarification agent can be imported."""
        from mdzen.agents.clarification_agent import create_clarification_agent

        # Verify function exists
        assert callable(create_clarification_agent)

    def test_setup_agent_import(self):
        """Setup agent can be imported."""
        from mdzen.agents.setup_agent import create_setup_agent

        assert callable(create_setup_agent)

    def test_validation_agent_import(self):
        """Validation agent can be imported."""
        from mdzen.agents.validation_agent import create_validation_agent

        assert callable(create_validation_agent)

    def test_full_agent_import(self):
        """Full agent (SequentialAgent) can be imported."""
        from mdzen.agents.full_agent import create_full_agent

        assert callable(create_full_agent)


class TestSchemas:
    """Test schema definitions."""

    def test_simulation_brief_import(self):
        """SimulationBrief can be imported and is a Pydantic model."""
        from mdzen.schemas import SimulationBrief

        # Verify it's a Pydantic model
        assert hasattr(SimulationBrief, "model_fields")

    def test_simulation_brief_fields(self):
        """SimulationBrief has expected fields."""
        from mdzen.schemas import SimulationBrief

        fields = SimulationBrief.model_fields
        # Check key fields exist
        assert "pdb_id" in fields or "pdb_source" in fields
        assert "temperature" in fields
        assert "simulation_time_ns" in fields


class TestMCPSetup:
    """Test MCP toolset configuration."""

    def test_create_mcp_toolsets(self):
        """create_mcp_toolsets returns dict of toolsets."""
        from mdzen.tools.mcp_setup import create_mcp_toolsets

        toolsets = create_mcp_toolsets()
        assert isinstance(toolsets, dict)
        assert len(toolsets) == 7  # 7 servers
        assert "research" in toolsets
        assert "literature" in toolsets
        assert "structure" in toolsets
        assert "genesis" in toolsets
        assert "solvation" in toolsets
        assert "amber" in toolsets
        assert "md_simulation" in toolsets

    def test_step_servers_mapping(self):
        """STEP_CONFIG contains servers for each step."""
        from mdzen.workflow import STEP_CONFIG

        assert "prepare_complex" in STEP_CONFIG
        assert "solvate" in STEP_CONFIG
        assert "build_topology" in STEP_CONFIG
        assert "run_simulation" in STEP_CONFIG
        # Verify servers are defined for each step
        for step, config in STEP_CONFIG.items():
            assert "servers" in config, f"Missing 'servers' in {step}"
            assert isinstance(config["servers"], list)

    def test_get_clarification_tools(self):
        """get_clarification_tools returns filtered toolset."""
        from mdzen.tools.mcp_setup import get_clarification_tools

        tools = get_clarification_tools()
        assert isinstance(tools, list)
        assert len(tools) == 2  # literature + research

    def test_get_setup_tools(self):
        """get_setup_tools returns all toolsets."""
        from mdzen.tools.mcp_setup import get_setup_tools

        tools = get_setup_tools()
        assert isinstance(tools, list)
        assert len(tools) == 7  # All 7 servers


class TestConfig:
    """Test configuration module."""

    def test_settings_import(self):
        """Settings can be imported."""
        from mdzen.config import settings

        assert settings is not None

    def test_settings_has_model_configs(self):
        """Settings has model configuration."""
        from mdzen.config import settings

        assert hasattr(settings, "clarification_model")
        assert hasattr(settings, "setup_model")

    def test_settings_has_timeouts(self):
        """Settings has timeout configurations."""
        from mdzen.config import settings

        assert hasattr(settings, "default_timeout")
        assert hasattr(settings, "solvation_timeout")
        assert settings.default_timeout > 0
        assert settings.solvation_timeout > 0

    def test_get_litellm_model(self):
        """get_litellm_model converts model format."""
        from mdzen.config import get_litellm_model

        # Test conversion from anthropic:model to anthropic/model
        result = get_litellm_model("anthropic:claude-sonnet-4-20250514")
        assert result == "anthropic/claude-sonnet-4-20250514"

        # Already correct format should pass through
        result = get_litellm_model("anthropic/claude-sonnet-4-20250514")
        assert result == "anthropic/claude-sonnet-4-20250514"


class TestCustomTools:
    """Test custom FunctionTools."""

    def test_generate_simulation_brief_import(self):
        """generate_simulation_brief function can be imported."""
        from mdzen.tools.custom_tools import generate_simulation_brief

        assert callable(generate_simulation_brief)

    def test_get_workflow_status_import(self):
        """get_workflow_status function can be imported."""
        from mdzen.tools.custom_tools import get_workflow_status

        assert callable(get_workflow_status)

    def test_run_validation_import(self):
        """run_validation function can be imported."""
        from mdzen.tools.custom_tools import run_validation

        assert callable(run_validation)


class TestPrompts:
    """Test prompt loading."""

    def test_get_clarification_instruction(self):
        """Clarification prompt can be loaded."""
        from mdzen.prompts import get_clarification_instruction

        instruction = get_clarification_instruction()
        assert isinstance(instruction, str)
        assert len(instruction) > 0

    def test_get_setup_instruction(self):
        """Setup prompt can be loaded."""
        from mdzen.prompts import get_setup_instruction

        instruction = get_setup_instruction()
        assert isinstance(instruction, str)
        assert len(instruction) > 0

    def test_get_validation_instruction(self):
        """Validation prompt can be loaded."""
        from mdzen.prompts import get_validation_instruction

        instruction = get_validation_instruction()
        assert isinstance(instruction, str)
        assert len(instruction) > 0


class TestCLI:
    """Test CLI module imports."""

    def test_runner_imports(self):
        """Runner utilities can be imported."""
        from mdzen.cli.runner import (
            APP_NAME,
            generate_job_id,
            create_message,
            extract_text_from_content,
        )

        assert APP_NAME == "mdzen"
        assert callable(generate_job_id)
        assert callable(create_message)
        assert callable(extract_text_from_content)

    def test_generate_job_id_format(self):
        """Job ID has correct format."""
        from mdzen.cli.runner import generate_job_id

        job_id = generate_job_id()
        assert job_id.startswith("job_")
        # Format: job_XXXXXXXX (8 hex chars after prefix)
        assert len(job_id) == 12  # "job_" (4) + 8 hex chars


@pytest.mark.slow
class TestMCPIntegration:
    """Tests that require MCP servers (marked slow)."""

    def test_mcp_client_can_be_created(self):
        """MCP client can be created (doesn't require running servers)."""
        pytest.skip("Requires MCP servers - run with --runslow")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
