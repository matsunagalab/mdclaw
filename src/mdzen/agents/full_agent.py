"""Primary workflow agent for MDZen.

This module defines the *stepwise* workflow (v2):
(1) acquire_structure
(2) select_prepare
(3) structure_decisions
(4) solvate_or_membrane
(5) quick_md
(6) validation

Each step is a dedicated LlmAgent with a focused prompt and minimal tools.
"""

from google.adk.agents import SequentialAgent
from google.adk.tools.mcp_tool import McpToolset

from mdzen.agents.workflow_step_agent import create_workflow_step_agent
from mdzen.workflow import WORKFLOW_STEPS


def create_full_agent() -> tuple[SequentialAgent, list[McpToolset]]:
    """Create the full stepwise workflow agent (v2).

    Note: This is primarily intended for non-interactive/batch execution.
    Interactive mode typically runs steps in a loop to allow human input
    between steps.
    """
    return create_workflow_agent()


def create_workflow_agent(steps: list[str] | None = None) -> tuple[SequentialAgent, list[McpToolset]]:
    """Create a SequentialAgent that runs the v2 workflow steps."""
    steps = steps or WORKFLOW_STEPS

    sub_agents = []
    all_toolsets: list[McpToolset] = []
    for step in steps:
        step_agent, toolsets = create_workflow_step_agent(step)
        sub_agents.append(step_agent)
        all_toolsets.extend(toolsets)

    agent = SequentialAgent(
        name="workflow_agent",
        description="MDZen stepwise workflow (v2)",
        sub_agents=sub_agents,
    )
    return agent, all_toolsets
