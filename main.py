"""
MDZen - Molecular Dynamics Setup AI Agent

Main entry point for the MDZen workflow system using Google ADK.
MDZen = MD + 膳（お膳立て）/ 禅（シンプルさ）
"""

# Load environment variables from .env file (must be before other imports)
from dotenv import load_dotenv
load_dotenv()

import typer  # noqa: E402
import asyncio  # noqa: E402
from typing import Optional  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from mdzen.cli.runner import (  # noqa: E402
    APP_NAME,
    DEFAULT_USER,
    generate_job_id,
    create_message,
    extract_text_from_content,
    display_results,
    display_debug_state,
    run_agent_with_events,
)

app = typer.Typer(help="MDZen - AI Agent for Molecular Dynamics Setup")
console = Console()

# Subcommands
try:
    from mdzen.cli.benchmark import benchmark_app

    app.add_typer(benchmark_app, name="benchmark")
except Exception:
    # Benchmark CLI is optional at runtime; main MD workflow should still work.
    pass


def _normalize_model_name(model: str) -> str:
    """Normalize short model names to full provider:model format.

    Examples:
        gpt-4o -> openai:gpt-4o
        claude-sonnet -> anthropic:claude-sonnet-4-20250514
        gemini-flash -> google:gemini-2.0-flash
    """
    # Already has provider prefix
    if ":" in model:
        return model

    # Common aliases
    aliases = {
        # OpenAI
        "gpt-4o": "openai:gpt-4o",
        "gpt-4o-mini": "openai:gpt-4o-mini",
        "gpt-4": "openai:gpt-4",
        "gpt-4-turbo": "openai:gpt-4-turbo",
        "o1": "openai:o1",
        "o1-mini": "openai:o1-mini",
        "o1-preview": "openai:o1-preview",
        # Anthropic
        "claude-opus": "anthropic:claude-opus-4-5-20251101",
        "claude-sonnet": "anthropic:claude-sonnet-4-20250514",
        "claude-haiku": "anthropic:claude-haiku-4-5-20251001",
        "opus": "anthropic:claude-opus-4-5-20251101",
        "sonnet": "anthropic:claude-sonnet-4-20250514",
        "haiku": "anthropic:claude-haiku-4-5-20251001",
        # Google
        "gemini-flash": "google:gemini-2.0-flash",
        "gemini-pro": "google:gemini-1.5-pro",
        "gemini": "google:gemini-2.0-flash",
    }

    if model.lower() in aliases:
        return aliases[model.lower()]

    # If starts with known prefix, add provider
    if model.startswith("gpt-") or model.startswith("o1"):
        return f"openai:{model}"
    elif model.startswith("claude-"):
        return f"anthropic:{model}"
    elif model.startswith("gemini"):
        return f"google:{model}"

    # Unknown, return as-is (will likely fail with clear error)
    return model


def _run_with_suppressed_cleanup(coro):
    """Run async coroutine and suppress MCP async generator cleanup errors.

    MCP's stdio_client uses async generators that cause "Attempted to exit
    cancel scope in a different task" errors during cleanup. These are harmless
    but noisy. We suppress them by:
    1. Disabling Python's asyncgen hooks
    2. Setting a custom unraisablehook to ignore these errors
    3. Setting a custom exception handler on the event loop
    4. Suppressing stderr during the final cleanup phase
    """
    import os
    import sys

    # Disable async generator finalization hooks
    sys.set_asyncgen_hooks(firstiter=None, finalizer=None)

    # Override unraisablehook to suppress async generator cleanup errors
    original_unraisablehook = sys.unraisablehook

    def ignore_mcp_cleanup_errors(unraisable):
        """Ignore MCP stdio_client async generator cleanup errors."""
        # Check if this is an MCP async generator error
        obj_repr = repr(unraisable.object) if unraisable.object else ""
        exc_str = str(unraisable.exc_value) if unraisable.exc_value else ""

        # Suppress known MCP cleanup errors
        if "stdio_client" in obj_repr or "cancel scope" in exc_str:
            return

        # For other errors, use original hook
        original_unraisablehook(unraisable)

    sys.unraisablehook = ignore_mcp_cleanup_errors

    # Create event loop with custom exception handler
    loop = asyncio.new_event_loop()

    def ignore_asyncgen_errors(loop, context):
        """Ignore async generator cleanup errors from MCP."""
        msg = context.get("message", "")
        exc = context.get("exception")
        # Suppress known MCP cleanup errors
        if "stdio_client" in msg or "cancel scope" in str(exc):
            return
        # For other errors, use default handler
        loop.default_exception_handler(context)

    loop.set_exception_handler(ignore_asyncgen_errors)

    exit_code = 0
    try:
        return loop.run_until_complete(coro)
    except Exception:
        # Print traceback for real failures. Note that when MDZEN_HARD_EXIT=1 (default),
        # we will terminate the process after cleanup; this ensures users/benchmarks
        # can still see the error and get a non-zero exit code.
        import traceback

        exit_code = 1
        traceback.print_exc()
        raise
    finally:
        # Suppress stderr before cleanup to hide any async generator errors
        # that might bypass our hooks
        hard_exit = os.environ.get("MDZEN_HARD_EXIT", "1") not in {"0", "false", "False"}
        sys.stderr = open(os.devnull, "w") if hard_exit else sys.stderr
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass  # Ignore cleanup errors
        finally:
            try:
                loop.close()
            except Exception:
                pass
            if hard_exit:
                # Force exit to prevent any remaining cleanup errors.
                # This avoids "Attempted to exit cancel scope in a different task" errors
                # that occur when Python's garbage collector cleans up remaining
                # async generators from a different task context.
                os._exit(exit_code)


@app.command()
def run(
    request: Optional[str] = typer.Argument(
        None,
        help="MD setup request (optional, prompts if not provided)",
    ),
    print_mode: bool = typer.Option(
        False,
        "--print",
        "-p",
        help="Run in non-interactive mode (no human-in-the-loop)",
    ),
    resume: Optional[str] = typer.Option(
        None,
        "--resume",
        "-r",
        help="Resume a specific session by ID (e.g., job_abc12345)",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Model to use (e.g., 'gpt-4o', 'claude-sonnet', 'gemini-flash'). Overrides auto-detection.",
    ),
):
    """Run MD setup using Google ADK.

    Examples:
        # Interactive mode
        python main.py run "Setup MD for PDB 1AKE"

        # Non-interactive mode (like claude -p)
        python main.py run -p "Setup MD for PDB 1AKE, 1ns at 300K"

        # Resume specific session (like claude -r)
        python main.py run -r job_abc12345

        # Specify model explicitly
        python main.py run -m gpt-4o "Setup MD for PDB 1AKE"
        python main.py run -m claude-sonnet "Setup MD for PDB 1AKE"
    """
    # Handle model option
    if model:
        import os
        # Normalize model name to full provider:model format
        model_normalized = _normalize_model_name(model)
        os.environ["MDZEN_CLARIFICATION_MODEL"] = model_normalized
        os.environ["MDZEN_SETUP_MODEL"] = model_normalized
        # Reload settings to pick up the new env vars
        from mdzen import config
        config.settings = config.Settings()

    _run_with_suppressed_cleanup(_run_async(request, print_mode, resume))


async def _run_async(
    request: Optional[str],
    print_mode: bool,
    resume: Optional[str],
):
    """Async implementation of the run command."""
    from pathlib import Path

    try:
        from mdzen.state.session_manager import (
            create_session_service,
            create_session_directory,
        )
    except ImportError as e:
        console.print(f"[red]Import error: {e}[/red]")
        console.print("\nMake sure you have installed google-adk:")
        console.print("  pip install -e '.[adk]'")
        raise typer.Exit(1)

    # Determine session ID
    if resume:
        # -r: Use specified session ID
        session_id = resume if resume.startswith("job_") else f"job_{resume}"
    else:
        # New session
        session_id = generate_job_id()

    # Extract job_id from session_id
    job_id = session_id.replace("job_", "") if session_id.startswith("job_") else session_id

    # Create session directory (or use existing for resume)
    session_dir = create_session_directory(job_id)

    # Create session service with DB inside job directory
    db_path = Path(session_dir) / "session.db"
    session_service = create_session_service(
        db_path=db_path,
        in_memory=print_mode,  # Use in-memory for print mode
    )

    console.print("=" * 60)
    console.print("[bold cyan]MDZen (Google ADK)[/bold cyan]")
    console.print(f"Session ID: {session_id}")
    console.print(f"Mode: {'Non-interactive' if print_mode else 'Interactive'}")
    console.print("=" * 60)

    # Get initial request if not provided
    if not request:
        console.print("\nDescribe your MD simulation setup:")
        console.print("(e.g., 'Setup MD for PDB 1AKE in water, 1 ns at 300K')")
        request = input("\n> ").strip()

        if request.lower() in ["quit", "exit", "q"]:
            console.print("[yellow]Session ended.[/yellow]")
            return

    if print_mode:
        await _run_batch(session_service, session_id, session_dir, request)
    else:
        await _run_interactive(session_service, session_id, session_dir, request)


async def _run_batch(session_service, session_id: str, session_dir: str, request: str):
    """Run in batch mode (no interrupts)."""
    from google.adk.runners import Runner
    from mdzen.agents.full_agent import create_full_agent
    from mdzen.state.session_manager import (
        initialize_session_state,
        get_session_state,
    )

    # Initialize session
    await initialize_session_state(
        session_service=session_service,
        app_name=APP_NAME,
        user_id=DEFAULT_USER,
        session_id=session_id,
        session_dir=session_dir,
    )

    console.print(f"[dim]Session dir: {session_dir}[/dim]\n")

    # Create full agent and runner
    agent, toolsets = create_full_agent()
    runner = Runner(
        app_name=APP_NAME,
        agent=agent,
        session_service=session_service,
    )

    console.print("[dim]Running full workflow...[/dim]\n")

    try:
        # Run agent with event processing
        event_count = 0
        async for event in runner.run_async(
            user_id=DEFAULT_USER,
            session_id=session_id,
            new_message=create_message(request),
        ):
            event_count += 1
            if hasattr(event, "author") and event.author:
                if event.is_final_response():
                    console.print(f"[green]Final response from {event.author}[/green]")
                elif hasattr(event, "content") and event.content:
                    text = extract_text_from_content(event.content)
                    if text:
                        first_line = text.split("\n")[0][:80]
                        console.print(f"[dim][{event.author}] {first_line}...[/dim]")

        console.print(f"[dim]Total events: {event_count}[/dim]")

        # Show results
        state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)
        display_debug_state(state, console)
        display_results(state, console)
    finally:
        # Note: We skip explicit MCP cleanup because it causes anyio task context
        # errors. The OS cleans up resources when the process exits.
        pass


async def _run_interactive(session_service, session_id: str, session_dir: str, request: str):
    """Run in interactive mode with human-in-the-loop."""
    from google.adk.runners import Runner
    from mdzen.agents.full_agent import (
        create_clarification_only_agent,
        create_setup_validation_agent,
    )
    from mdzen.state.session_manager import (
        initialize_session_state,
        get_session_state,
        save_chat_history,
        update_session_state,
    )
    from mdzen.utils import suppress_adk_unknown_agent_warnings
    from prompt_toolkit import PromptSession

    # Track all toolsets for cleanup
    all_toolsets = []

    # Create async prompt session
    prompt_session = PromptSession()

    async def async_prompt(message: str) -> str:
        return await prompt_session.prompt_async(message)

    # Initialize session
    await initialize_session_state(
        session_service=session_service,
        app_name=APP_NAME,
        user_id=DEFAULT_USER,
        session_id=session_id,
        session_dir=session_dir,
    )

    try:
        # Phase 1: Clarification
        console.print("\n[bold]Phase 1: Clarification[/bold]")
        console.print("[dim]Analyzing your request...[/dim]\n")

        clarification_agent, clarification_toolsets = create_clarification_only_agent()
        all_toolsets.extend(clarification_toolsets)
        runner = Runner(
            app_name=APP_NAME,
            agent=clarification_agent,
            session_service=session_service,
        )

        # Run clarification
        await run_agent_with_events(
            runner=runner,
            session_id=session_id,
            message=create_message(request),
            console=console,
            show_progress=False,
        )

        # Interactive clarification loop
        state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)

        while True:
            simulation_brief = state.get("simulation_brief")

            if simulation_brief:
                # The detailed version is already printed by the LLM agent via
                # _format_simulation_brief_summary() in custom_tools.py
                console.print("\n[yellow]Options:[/yellow]")
                console.print("  - Type 'continue' or 'yes' to proceed to Setup phase")
                console.print("  - Type 'quit' to exit")
                console.print("  - Or provide feedback to modify the brief\n")

                user_input = (await async_prompt(">> ")).strip()

                if user_input.lower() in ["quit", "exit", "q"]:
                    console.print("[yellow]Session ended.[/yellow]")
                    return
                elif user_input.lower() in ["continue", "yes", "y", "ok", "proceed"]:
                    # Save clarification chat history for validation review
                    clarification_log = await save_chat_history(
                        session_service, APP_NAME, DEFAULT_USER, session_id, session_dir
                    )
                    if clarification_log:
                        await update_session_state(
                            session_service, APP_NAME, DEFAULT_USER, session_id,
                            {"clarification_log_path": clarification_log}
                        )
                        console.print(f"[dim]Clarification log saved: {clarification_log}[/dim]")
                    break
                else:
                    # User wants to modify - send feedback
                    await run_agent_with_events(
                        runner=runner,
                        session_id=session_id,
                        message=create_message(user_input),
                        console=console,
                        show_progress=False,
                    )
                    state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)
            else:
                # No brief yet, ask for more info
                user_input = (await async_prompt(">> ")).strip()

                if user_input.lower() in ["quit", "exit", "q"]:
                    console.print("[yellow]Session ended.[/yellow]")
                    return

                await run_agent_with_events(
                    runner=runner,
                    session_id=session_id,
                    message=create_message(user_input),
                    console=console,
                    show_progress=False,
                )
                state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)

        # Phase 2-3: Setup and Validation
        console.print("\n[bold]Phase 2-3: Setup & Validation[/bold]")
        console.print("[dim]Executing MD setup workflow... This may take a few minutes.[/dim]\n")

        # Suppress ADK warnings using context manager
        with suppress_adk_unknown_agent_warnings():
            setup_agent, setup_toolsets = create_setup_validation_agent()
            all_toolsets.extend(setup_toolsets)
            runner = Runner(
                app_name=APP_NAME,
                agent=setup_agent,
                session_service=session_service,
            )

            # Known agents in setup_validation_agent hierarchy
            known_agents = {"setup_validation_agent", "setup_agent", "validation_agent"}

            await run_agent_with_events(
                runner=runner,
                session_id=session_id,
                message=create_message("continue"),
                console=console,
                show_progress=True,
                known_agents=known_agents,
            )

        # Show results
        state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)

        if state.get("validation_result"):
            validation = state["validation_result"]
            if "final_report" in validation:
                console.print("\n[bold green]Setup Complete![/bold green]")
                console.print(validation["final_report"])

        # Show generated files
        display_results(state, console)

        # Save chat history to job directory
        session_dir = state.get("session_dir", "")
        if session_dir:
            chat_file = await save_chat_history(
                session_service, APP_NAME, DEFAULT_USER, session_id, session_dir
            )
            if chat_file:
                console.print(f"[dim]Chat history saved: {chat_file}[/dim]")

        console.print(f"\n[green]Session complete! Session ID: {session_id}[/green]")
        console.print(f"[dim]Session directory: {session_dir}[/dim]")
    finally:
        # Note: We skip explicit MCP cleanup because it causes anyio task context
        # errors. The OS cleans up resources when the process exits.
        pass


@app.command()
def list_servers():
    """List available MCP servers."""
    table = Table(title="Available MCP Servers")
    table.add_column("Server", style="cyan")
    table.add_column("Description", style="green")

    servers = [
        ("research_server", "PDB/AlphaFold/UniProt retrieval and structure inspection"),
        ("literature_server", "PubMed literature search via NCBI E-utilities"),
        ("structure_server", "Structure repair, ligand GAFF2 parameterization"),
        ("genesis_server", "Boltz-2 structure prediction from FASTA sequences"),
        ("solvation_server", "Solvation (water box) and membrane embedding via packmol-memgen"),
        ("amber_server", "Amber topology (parm7) and coordinate (rst7) generation via tleap"),
        ("md_simulation_server", "MD execution with OpenMM, trajectory analysis with MDTraj"),
    ]

    for server, desc in servers:
        table.add_row(server, desc)

    console.print(table)


@app.command()
def info():
    """Show system information."""
    console.print("[bold]MDZen: AI Agent for Molecular Dynamics Setup[/bold]")
    console.print()
    console.print("Powered by [cyan]Google Agent Development Kit (ADK)[/cyan]")
    console.print()
    console.print("Features:")
    console.print("  - Boltz-2 structure and affinity prediction")
    console.print("  - AmberTools ligand parameterization (AM1-BCC)")
    console.print("  - smina molecular docking")
    console.print("  - OpenMM MD simulation execution")
    console.print("  - 3-phase workflow: Clarification -> Setup -> Validation")
    console.print()
    console.print("Commands:")
    console.print("  [cyan]python main.py run[/cyan]              - Interactive mode (recommended)")
    console.print("  [cyan]python main.py run --batch[/cyan]      - Batch mode (no interaction)")
    console.print("  [cyan]python main.py list-servers[/cyan]     - List available MCP servers")
    console.print("  [cyan]python main.py info[/cyan]             - Show this information")
    console.print()
    console.print("For usage, run: [cyan]python main.py --help[/cyan]")


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
