"""Workflow v2 step agents for MDZen.

Implements the primary workflow:
(1) acquire_structure
(2) select_prepare
(3) structure_decisions
(4) solvate_or_membrane
(5) quick_md
(6) validation

Each step is its own LlmAgent with:
- a focused prompt in src/mdzen/prompts/steps/<step>.md
- a minimal filtered toolset via get_workflow_step_tools()
- shared workflow scratchpad tools (read_workflow_state/update_workflow_state)
"""

from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool import McpToolset

from mdzen.config import get_litellm_model
from mdzen.guardrails import mdzen_before_tool_guardrail
from mdzen.prompts import get_step_instruction
from mdzen.tools.custom_tools import (
    get_quick_md_defaults,
    read_workflow_state,
    run_validation_tool,
    update_workflow_state,
)
from mdzen.tools.mcp_setup import get_workflow_step_tools, get_workflow_step_tools_sse


WorkflowTransport = Literal["stdio", "sse", "http"]


def create_workflow_step_agent(
    step: str,
    transport: WorkflowTransport = "stdio",
    sse_host: str = "localhost",
) -> tuple[LlmAgent, list[McpToolset]]:
    """Create a single workflow v2 step agent."""
    if transport in ("sse", "http"):
        mcp_tools = get_workflow_step_tools_sse(host=sse_host, step=step)
    else:
        mcp_tools = get_workflow_step_tools(step)

    # Shared workflow scratchpad tools
    read_state_tool = FunctionTool(read_workflow_state)
    update_state_tool = FunctionTool(update_workflow_state)

    tools = mcp_tools + [read_state_tool, update_state_tool]

    # Validation step needs the validation FunctionTool
    if step == "validation":
        tools.append(FunctionTool(run_validation_tool))

    # quick_md step: expose defaults to avoid parameter hallucination
    if step == "quick_md":
        tools.append(FunctionTool(get_quick_md_defaults))

    # Model selection: keep step1 lightweight; others use setup model.
    model_key = "clarification" if step == "acquire_structure" else "setup"

    # ADK-style guardrails: enforce tool args (e.g., no-ligands) via before_tool_callback
    # when supported by the installed ADK version.
    agent_kwargs = dict(
        model=LiteLlm(model=get_litellm_model(model_key)),
        name=f"workflow_step_{step}",
        description=f"Workflow v2 step: {step}",
        instruction=get_step_instruction(step),
        tools=tools,
    )
    try:
        if "before_tool_callback" in getattr(LlmAgent, "model_fields", {}):
            agent_kwargs["before_tool_callback"] = mdzen_before_tool_guardrail
    except Exception:
        pass

    agent = LlmAgent(**agent_kwargs)

    return agent, mcp_tools

