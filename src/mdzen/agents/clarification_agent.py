"""Phase 1: Clarification Agent for MDZen.

This agent handles user interaction to gather MD simulation requirements
and generates a structured SimulationBrief.
"""

from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool import McpToolset

from mdzen.config import get_litellm_model
from mdzen.prompts import get_clarification_instruction
from mdzen.tools.mcp_setup import get_clarification_tools, get_clarification_tools_sse
from mdzen.tools.custom_tools import generate_simulation_brief, get_session_dir, save_context


def create_clarification_agent(
    transport: Literal["stdio", "sse", "http"] = "stdio",
    sse_host: str = "localhost",
) -> tuple[LlmAgent, list[McpToolset]]:
    """Create the Phase 1 clarification agent.

    This agent:
    1. Gets session_dir via get_session_dir tool
    2. Uses download_structure/inspect_molecules to analyze structures
    3. Asks clarification questions based on inspection
    4. Generates SimulationBrief via generate_simulation_brief tool
    5. Saves result to session.state["simulation_brief"] via output_key

    Args:
        transport: MCP transport mode:
            - "stdio": subprocess-based (default, for CLI)
            - "sse" or "http": HTTP-based using Streamable HTTP /mcp endpoint (for Colab)
        sse_host: Hostname for HTTP servers (only used when transport="sse" or "http")

    Returns:
        Tuple of (LlmAgent, list of McpToolset instances to close after use)
    """
    # Get MCP tools for structure inspection based on transport mode
    if transport in ("sse", "http"):
        mcp_tools = get_clarification_tools_sse(host=sse_host)
    else:
        mcp_tools = get_clarification_tools()

    # Create FunctionTools for session management and brief generation
    get_session_dir_tool = FunctionTool(get_session_dir)
    generate_brief_tool = FunctionTool(generate_simulation_brief)
    save_context_tool = FunctionTool(save_context)

    # Combine all tools
    all_tools = mcp_tools + [get_session_dir_tool, generate_brief_tool, save_context_tool]

    instruction = get_clarification_instruction()

    agent = LlmAgent(
        model=LiteLlm(model=get_litellm_model("clarification")),
        name="clarification_agent",
        description="Gathers MD simulation requirements and generates SimulationBrief",
        instruction=instruction,
        tools=all_tools,
        # NOTE: Do NOT use output_key="simulation_brief" here!
        # The generate_simulation_brief tool saves the brief dict to session.state["simulation_brief"].
        # If we set output_key="simulation_brief", ADK would overwrite it with the agent's text response.
    )

    return agent, mcp_tools
