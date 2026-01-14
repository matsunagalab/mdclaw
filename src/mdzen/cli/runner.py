"""Shared runner logic for MDZen CLI.

Centralizes event processing, session management, and user interaction patterns.
This module eliminates code duplication between batch and interactive modes.
"""

import os
import sys
from typing import Any

from google.genai import types
from rich.console import Console

# Import shared generate_job_id from common
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
from common.utils import generate_job_id as _generate_job_id  # noqa: E402


# Constants
APP_NAME = "mdzen"
DEFAULT_USER = "default"


def generate_job_id(length: int = 8) -> str:
    """Generate unique job identifier using UUID.

    Args:
        length: Length of ID (default: 8 characters)

    Returns:
        Unique job ID string in format job_XXXXXXXX
    """
    return _generate_job_id(length=length, prefix="job_")


def create_message(text: str) -> types.Content:
    """Create a user message content object.

    Args:
        text: Message text

    Returns:
        ADK Content object with user role
    """
    return types.Content(role="user", parts=[types.Part(text=text)])


def extract_text_from_content(content: Any) -> str:
    """Extract plain text from ADK Content object.

    Args:
        content: google.genai.types.Content or similar object

    Returns:
        Formatted text string
    """
    if content is None:
        return ""

    # If it's already a string, return it
    if isinstance(content, str):
        return content

    # Try to extract from parts
    if hasattr(content, "parts"):
        texts = []
        seen_texts: set[str] = set()  # Deduplicate parts with same content
        for i, part in enumerate(content.parts):
            if hasattr(part, "text") and part.text:
                # Skip duplicate text parts
                if part.text in seen_texts:
                    continue
                seen_texts.add(part.text)
                texts.append(part.text)
        # Debug: show number of unique parts
        # print(f"DEBUG: {len(texts)} unique text parts from {len(list(content.parts))} parts")
        return "\n".join(texts)

    # Fallback to string representation
    return str(content)


async def run_agent_with_events(
    runner,
    session_id: str,
    message: types.Content,
    console: Console,
    show_progress: bool = True,
    known_agents: set[str] | None = None,
) -> int:
    """Run agent and process events with unified handling.

    Args:
        runner: ADK Runner instance
        session_id: Session identifier
        message: User message to send
        console: Rich console for output
        show_progress: Whether to show progress messages
        known_agents: Set of agent names to show (None = show all)

    Returns:
        Number of events processed
    """
    event_count = 0
    last_printed_text: str | None = None  # Track last printed response for dedup

    async for event in runner.run_async(
        user_id=DEFAULT_USER,
        session_id=session_id,
        new_message=message,
    ):
        event_count += 1

        if hasattr(event, "author") and event.author:
            # Filter by known agents if specified
            if known_agents and event.author not in known_agents and event.author != "user":
                continue

            if event.is_final_response():
                text = extract_text_from_content(event.content)
                if text:
                    # Skip duplicate responses (ADK may emit from both sub-agent and parent)
                    if text == last_printed_text:
                        continue
                    last_printed_text = text
                    console.print(f"\n[green]Agent:[/green]\n{text}\n")
            elif show_progress and hasattr(event, "content") and event.content:
                text = extract_text_from_content(event.content)
                if text:
                    first_line = text.split("\n")[0][:80]
                    console.print(f"[dim][{event.author}] {first_line}...[/dim]")

    return event_count


def display_results(state: dict, console: Console) -> None:
    """Display workflow results.

    Args:
        state: Session state dictionary
        console: Rich console for output
    """
    import json

    # Check if workflow completed all steps
    completed_steps_raw = state.get("completed_steps", [])
    if isinstance(completed_steps_raw, str):
        try:
            completed_steps = json.loads(completed_steps_raw)
        except json.JSONDecodeError:
            completed_steps = []
    else:
        completed_steps = completed_steps_raw or []

    required_steps = ["prepare_complex", "solvate", "build_topology", "run_simulation"]
    missing_steps = [s for s in required_steps if s not in completed_steps]
    workflow_complete = len(missing_steps) == 0

    if state.get("validation_result"):
        validation = state["validation_result"]
        if isinstance(validation, dict) and "final_report" in validation:
            if workflow_complete:
                console.print("\n[bold green]Workflow Complete![/bold green]")
            else:
                console.print("\n[bold red]Workflow Failed![/bold red]")
                console.print(f"[red]Missing steps: {missing_steps}[/red]")
            console.print(validation["final_report"])
        else:
            if workflow_complete:
                console.print("\n[bold green]Complete![/bold green]")
            else:
                console.print("\n[bold red]Workflow Incomplete![/bold red]")
                console.print(f"[red]Missing steps: {missing_steps}[/red]")
            console.print(f"Validation result type: {type(validation)}")
            console.print(f"Session directory: {state.get('session_dir')}")
    else:
        if not workflow_complete:
            console.print("\n[bold red]Workflow Failed![/bold red]")
            console.print(f"[red]Missing steps: {missing_steps}[/red]")
        else:
            console.print("\n[yellow]Warning: No validation result[/yellow]")
        console.print(f"Session directory: {state.get('session_dir')}")

    # Show generated files
    outputs = state.get("outputs", {})
    if outputs:
        console.print("\n[bold]Generated Files:[/bold]")
        for key, value in outputs.items():
            if key != "session_dir":
                console.print(f"  {key}: {value}")


def display_debug_state(state: dict, console: Console) -> None:
    """Display debug information about session state.

    Args:
        state: Session state dictionary
        console: Rich console for output
    """
    console.print(f"\n[dim]State keys: {list(state.keys())}[/dim]")
    console.print(f"[dim]simulation_brief: {state.get('simulation_brief') is not None}[/dim]")
    console.print(f"[dim]completed_steps: {state.get('completed_steps', [])}[/dim]")
    console.print(f"[dim]validation_result: {state.get('validation_result') is not None}[/dim]")


__all__ = [
    # Constants
    "APP_NAME",
    "DEFAULT_USER",
    # Session helpers
    "generate_job_id",
    "create_message",
    # Content helpers
    "extract_text_from_content",
    # Runner helpers
    "run_agent_with_events",
    # Display helpers
    "display_results",
    "display_debug_state",
]
