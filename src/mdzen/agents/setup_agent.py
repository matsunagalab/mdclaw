"""Phase 2: Setup Agent for MDZen.

This agent executes the 4-step MD setup workflow:
1. prepare_complex - Structure preparation and ligand parameterization
2. solvate - Add water box
3. build_topology - Generate Amber prmtop/rst7
4. run_simulation - Execute MD with OpenMM

Uses scratchpad mode with explicit markdown scratchpad for state tracking.
"""

from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool import McpToolset

from mdzen.config import get_litellm_model
from mdzen.prompts import get_setup_instruction
from mdzen.tools.mcp_setup import get_setup_tools, get_setup_tools_sse
from mdzen.tools.custom_tools import (
    read_scratchpad,
    update_scratchpad,
)


def create_setup_agent(
    transport: Literal["stdio", "sse", "http"] = "stdio",
    sse_host: str = "localhost",
) -> tuple[LlmAgent, list[McpToolset]]:
    """Create the Phase 2 setup agent.

    This agent:
    1. Reads SimulationBrief from session.state["simulation_brief"]
    2. Executes 4-step workflow using MCP tools
    3. Tracks progress via scratchpad file
    4. Stores outputs in session.state["outputs"]

    Args:
        transport: MCP transport mode:
            - "stdio": subprocess-based (default, for CLI)
            - "sse" or "http": HTTP-based using Streamable HTTP /mcp endpoint (for Colab)
        sse_host: Hostname for HTTP servers (only used when transport="sse" or "http")

    Returns:
        Tuple of (LlmAgent, list of McpToolset instances to close after use)
    """
    # Get all MCP tools for setup workflow based on transport mode
    if transport in ("sse", "http"):
        mcp_tools = get_setup_tools_sse(host=sse_host)
    else:
        mcp_tools = get_setup_tools()

    instruction = get_setup_instruction()

    # Create FunctionTools for scratchpad workflow
    read_scratchpad_tool = FunctionTool(read_scratchpad)
    update_scratchpad_tool = FunctionTool(update_scratchpad)

    # Combine MCP tools with scratchpad tools
    all_tools = mcp_tools + [read_scratchpad_tool, update_scratchpad_tool]

    agent = LlmAgent(
        model=LiteLlm(model=get_litellm_model("setup")),
        name="setup_agent",
        description="Executes 4-step MD setup workflow",
        instruction=instruction,
        tools=all_tools,
        output_key="setup_result",  # Saves final result to session.state
    )

    return agent, mcp_tools
