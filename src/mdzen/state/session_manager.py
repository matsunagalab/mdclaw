"""Session management for MDZen.

This module provides session service configuration and state initialization
for ADK-based workflows.
"""

import sys
from pathlib import Path
from typing import Optional

from google.adk.sessions import InMemorySessionService, DatabaseSessionService

# Import shared generate_job_id from common
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from common.utils import generate_job_id  # noqa: E402

from mdzen.config import get_output_dir


def create_session_service(
    db_path: str | Path,
    in_memory: bool = False,
) -> InMemorySessionService | DatabaseSessionService:
    """Create a session service for state persistence.

    Args:
        db_path: Full path to SQLite database file (e.g., job_xxx/session.db)
        in_memory: Use in-memory storage (no persistence)

    Returns:
        Configured session service instance
    """
    if in_memory:
        return InMemorySessionService()

    # Ensure parent directory exists
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create database session service
    # Note: Use sqlite+aiosqlite for async compatibility
    return DatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{db_path}"
    )


async def initialize_session_state(
    session_service,
    app_name: str,
    user_id: str,
    session_id: str,
    session_dir: str,
) -> None:
    """Create and initialize a new session with required state.

    Args:
        session_service: SessionService instance
        app_name: Application name
        user_id: User identifier
        session_id: Session ID (format: job_XXXXXXXX)
        session_dir: Path to session directory (already created)
    """
    import json
    from datetime import datetime

    # Initialize state with required fields
    # IMPORTANT: State must be passed during create_session, not modified after
    # InMemorySessionService doesn't persist modifications to session.state
    # Workflow v2 shared state (stored both in ADK state and as a file in session_dir)
    workflow_state = {
        "current_step": "acquire_structure",
        "completed_steps": [],
        "awaiting_user_input": False,
        "pending_questions": [],
        "last_step_summary": "",
        "structure_file": "",
        "merged_pdb": "",
        "structure_analysis": {},
        "solvation_type": "",
        "solvated_pdb": "",
        "membrane_pdb": "",
        "box_dimensions": {},
        "parm7": "",
        "rst7": "",
        "trajectory": "",
        "validation_result": {},
    }

    initial_state = {
        "session_dir": session_dir,
        "completed_steps": [],
        "outputs": {"session_dir": session_dir},
        "simulation_brief": None,
        "validation_result": None,
        # v2 workflow state
        "workflow_state": json.dumps(workflow_state, ensure_ascii=False, default=str),
        "workflow_current_step": "acquire_structure",
    }

    # Create session with initial state (async method)
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        state=initial_state,
    )

    # Save session info to job directory for traceability
    db_path = Path(session_dir) / "session.db"
    session_info = {
        "session_id": session_id,
        "app_name": app_name,
        "user_id": user_id,
        "created_at": datetime.now().isoformat(),
        "session_db": str(db_path),
        "session_dir": session_dir,
    }
    session_info_file = Path(session_dir) / "session_info.json"
    session_info_file.write_text(json.dumps(session_info, indent=2))

    # Also write the workflow_state.json file so recovery logic can work even if
    # the model forgets to call update_workflow_state().
    try:
        (Path(session_dir) / "workflow_state.json").write_text(
            json.dumps(workflow_state, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def create_session_directory(job_id: Optional[str] = None) -> str:
    """Create a unique session directory for workflow outputs.

    The directory is created under the configured output directory.
    Uses consistent job_XXXXXXXX naming format.

    Args:
        job_id: Optional job ID (generated if not provided)

    Returns:
        Absolute path to the session directory
    """
    output_base = get_output_dir()
    if job_id is None:
        job_id = generate_job_id()
    session_dir = output_base / f"job_{job_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    return str(session_dir.resolve())


async def get_session_state(
    session_service,
    app_name: str,
    user_id: str,
    session_id: str,
) -> dict:
    """Get current state from a session.

    Args:
        session_service: SessionService instance
        app_name: Application name
        user_id: User identifier
        session_id: Session ID

    Returns:
        Current session state dictionary
    """
    session = await session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    if session is None:
        return {}

    return dict(session.state)


async def update_session_state(
    session_service,
    app_name: str,
    user_id: str,
    session_id: str,
    updates: dict,
) -> None:
    """Update session state with new values.

    Args:
        session_service: SessionService instance
        app_name: Application name
        user_id: User identifier
        session_id: Session ID
        updates: Dictionary of state updates
    """
    session = await session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    if session is not None:
        for key, value in updates.items():
            session.state[key] = value


async def save_chat_history(
    session_service,
    app_name: str,
    user_id: str,
    session_id: str,
    session_dir: str,
) -> str:
    """Save chat history to the job directory as a markdown file.

    Args:
        session_service: SessionService instance
        app_name: Application name
        user_id: User identifier
        session_id: Session ID
        session_dir: Path to session directory

    Returns:
        Path to saved chat history file
    """
    import json
    from datetime import datetime

    session = await session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    if session is None:
        return ""

    # Create chat history file
    chat_file = Path(session_dir) / "chat_history.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# MDZen Chat History",
        "",
        f"**Session ID**: {session_id}",
        f"**Timestamp**: {timestamp}",
        "",
        "---",
        "",
    ]

    # Extract messages from session events if available
    if hasattr(session, "events") and session.events:
        for event in session.events:
            if hasattr(event, "content") and event.content:
                role = getattr(event, "author", "unknown")
                if hasattr(event.content, "parts"):
                    for part in event.content.parts:
                        # Text messages
                        if hasattr(part, "text") and part.text:
                            lines.append(f"### {role}")
                            lines.append("")
                            lines.append(part.text)
                            lines.append("")
                            lines.append("---")
                            lines.append("")
                        # Tool calls (MCP tools, function tools)
                        if hasattr(part, "function_call") and part.function_call:
                            fc = part.function_call
                            tool_name = getattr(fc, "name", "unknown_tool")
                            tool_args = getattr(fc, "args", {})
                            lines.append(f"### {role} (Tool Call)")
                            lines.append("")
                            lines.append(f"**Tool**: `{tool_name}`")
                            lines.append("")
                            lines.append("**Arguments**:")
                            lines.append("```json")
                            lines.append(json.dumps(tool_args, indent=2, default=str))
                            lines.append("```")
                            lines.append("")
                            lines.append("---")
                            lines.append("")
                        # Tool responses
                        if hasattr(part, "function_response") and part.function_response:
                            fr = part.function_response
                            tool_name = getattr(fr, "name", "unknown_tool")
                            response = getattr(fr, "response", {})
                            lines.append(f"### {role} (Tool Response)")
                            lines.append("")
                            lines.append(f"**Tool**: `{tool_name}`")
                            lines.append("")
                            # Truncate long responses to avoid bloating the log
                            response_str = json.dumps(response, indent=2, default=str)
                            if len(response_str) > 2000:
                                response_str = response_str[:2000] + "\n... (truncated)"
                            lines.append("**Response**:")
                            lines.append("```json")
                            lines.append(response_str)
                            lines.append("```")
                            lines.append("")
                            lines.append("---")
                            lines.append("")

    # Also save state summary
    state = dict(session.state)
    lines.append("## Session State Summary")
    lines.append("")

    # Save simulation brief if present
    if state.get("simulation_brief"):
        brief = state["simulation_brief"]
        lines.append("### Simulation Brief")
        lines.append("```json")
        lines.append(json.dumps(brief, indent=2, default=str))
        lines.append("```")
        lines.append("")

    # Save completed steps
    if state.get("completed_steps"):
        lines.append("### Completed Steps")
        for step in state["completed_steps"]:
            lines.append(f"- {step}")
        lines.append("")

    # Save outputs
    if state.get("outputs"):
        lines.append("### Generated Files")
        for key, value in state["outputs"].items():
            if key != "session_dir":
                lines.append(f"- **{key}**: `{value}`")
        lines.append("")

    # Write file
    chat_file.write_text("\n".join(lines))

    return str(chat_file)
