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
    auto_answer: bool = typer.Option(
        False,
        "--auto-answer",
        help="(Tests) Enable auto-answer for pending questions (external/LLM/default).",
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

    if auto_answer:
        import os

        os.environ["MDZEN_AUTO_ANSWER"] = "true"

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

    # Set current session for MCP servers to use
    from common.utils import set_current_session
    set_current_session(session_dir)

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
    from mdzen.state.session_manager import (
        initialize_session_state,
        get_session_state,
        update_session_state,
    )
    from mdzen.utils import suppress_adk_unknown_agent_warnings
    import json
    from pathlib import Path
    from mdzen.workflow import get_next_workflow_v2_step

    # Initialize session
    await initialize_session_state(
        session_service=session_service,
        app_name=APP_NAME,
        user_id=DEFAULT_USER,
        session_id=session_id,
        session_dir=session_dir,
    )

    console.print(f"[dim]Session dir: {session_dir}[/dim]\n")

    console.print("[dim]Running stepwise workflow (batch)...[/dim]\n")
    from mdzen.agents.workflow_step_agent import create_workflow_step_agent

    def _load_disk_workflow_state() -> dict:
        path = Path(session_dir) / "workflow_state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_disk_workflow_state(wf: dict) -> None:
        path = Path(session_dir) / "workflow_state.json"
        try:
            path.write_text(json.dumps(wf, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

    def _newest_match(patterns: list[str]) -> str:
        base = Path(session_dir)
        matches: list[Path] = []
        for pat in patterns:
            matches.extend(list(base.glob(pat)))
        matches = [p for p in matches if p.exists()]
        if not matches:
            return ""
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(matches[0])

    async def _recover_state_after_step() -> None:
        wf = _load_disk_workflow_state()
        if not wf:
            return
        current = wf.get("current_step") or ""
        completed = list(wf.get("completed_steps") or [])
        changed = False

        # Step (1): acquire_structure - infer downloaded structure file
        if current == "acquire_structure" and not wf.get("structure_file"):
            struct = _newest_match(["*.pdb", "*.cif", "*.ent", "**/AF-*.pdb", "**/AF-*.cif"])
            if struct:
                wf["structure_file"] = struct
                if "acquire_structure" not in completed:
                    completed.append("acquire_structure")
                wf["completed_steps"] = completed
                nxt = get_next_workflow_v2_step("acquire_structure")
                if nxt:
                    wf["current_step"] = nxt
                changed = True

        # If the model forgot to persist select_prepare outputs, infer selected_structure_file.
        # In the updated workflow, select_prepare should produce a protein-only file for checks.
        if current == "select_prepare" and not wf.get("selected_structure_file"):
            selected = _newest_match(
                [
                    "**/selected_structure*.pdb",
                    "**/split/**/protein_*.pdb",
                    "**/split_*/**/protein_*.pdb",
                ]
            )
            if selected:
                wf["selected_structure_file"] = selected
                if "select_prepare" not in completed:
                    completed.append("select_prepare")
                wf["completed_steps"] = completed
                nxt = get_next_workflow_v2_step("select_prepare")
                if nxt:
                    wf["current_step"] = nxt
                changed = True

        if changed:
            _save_disk_workflow_state(wf)
            await update_session_state(
                session_service,
                APP_NAME,
                DEFAULT_USER,
                session_id,
                {
                    "workflow_state": json.dumps(wf, ensure_ascii=False, default=str),
                    "workflow_current_step": str(wf.get("current_step") or ""),
                },
            )

    try:
        initial_request = request

        while True:
            state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)
            wf_raw = state.get("workflow_state", "")
            wf_state = {}
            if isinstance(wf_raw, str) and wf_raw:
                try:
                    wf_state = json.loads(wf_raw)
                except Exception:
                    wf_state = {}
            elif isinstance(wf_raw, dict):
                wf_state = wf_raw

            current_step = wf_state.get("current_step") or "acquire_structure"
            awaiting = bool(wf_state.get("awaiting_user_input"))
            questions = wf_state.get("pending_questions") or []

            # -----------------------------------------------------------------
            # Hard guard (batch): deterministically set chain/ligand choices
            # so we never proceed without explicit selection in v2.
            # -----------------------------------------------------------------
            if current_step == "select_prepare" and not wf_state.get("selection_chains"):
                structure_file = wf_state.get("structure_file") or ""
                if structure_file:
                    try:
                        import gemmi
                        import re

                        AMINO_ACIDS = {
                            "ALA","ARG","ASN","ASP","CYS","GLN","GLU","GLY","HIS",
                            "ILE","LEU","LYS","MET","PHE","PRO","SER","THR","TRP",
                            "TYR","VAL","SEC","PYL",
                        }
                        WATER = {"HOH","WAT","H2O","DOD","D2O","TIP3","SOL","OPC"}
                        IONS = {"NA","CL","K","MG","CA","ZN","FE","MN","CU","CO","NI","CD","HG"}

                        st = gemmi.read_structure(structure_file)
                        st.setup_entities()
                        model = st[0]

                        protein_chains: list[str] = []
                        ligand_resnames: set[str] = set()
                        for chain in model:
                            has_protein = False
                            for res in chain:
                                rn = res.name.strip().upper()
                                if rn in AMINO_ACIDS:
                                    has_protein = True
                                elif rn in WATER or rn in IONS:
                                    continue
                                else:
                                    if len(list(res)) >= 3:
                                        ligand_resnames.add(rn)
                            if has_protein and chain.name:
                                protein_chains.append(chain.name)

                        protein_chains = list(dict.fromkeys(protein_chains))
                        ligands = sorted(ligand_resnames)

                        # Parse intent from initial request (best-effort)
                        req_low = initial_request.lower()
                        include_ligands = True
                        if any(tok in req_low for tok in ["no ligand", "without ligand", "exclude ligand", "remove ligand", "no ligands"]):
                            include_ligands = False

                        # Chains: if request mentions a chain explicitly (A/B/...), use it; else default A if present.
                        selected_chains: list[str] = []
                        letters = re.findall(r"\b(chain\s*)?([A-Za-z0-9])\b", initial_request, flags=re.IGNORECASE)
                        mentioned = {m[1].upper() for m in letters if m and m[1]}
                        if mentioned:
                            selected_chains = [c for c in protein_chains if c.upper() in mentioned]
                        if not selected_chains and protein_chains:
                            # Prefer chain A if present
                            selected_chains = ["A"] if "A" in {c.upper() for c in protein_chains} else [protein_chains[0]]

                        wf_state["selection_chains"] = selected_chains
                        wf_state["include_types"] = ["protein", "ion"] + (["ligand"] if include_ligands else [])
                        wf_state["last_step_summary"] = (
                            f"(batch default) chains={selected_chains}, include_ligands={include_ligands}, detected_ligands={ligands}"
                        )

                        # Persist for the step agent to use
                        (Path(session_dir) / "workflow_state.json").write_text(
                            json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                        await update_session_state(
                            session_service,
                            APP_NAME,
                            DEFAULT_USER,
                            session_id,
                            {
                                "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                                "workflow_current_step": "select_prepare",
                            },
                        )
                    except Exception:
                        pass

            completed = wf_state.get("completed_steps") or []
            if "validation" in completed and not awaiting:
                break

            if awaiting:
                # Write questions for external drivers, then auto-accept defaults.
                try:
                    from mdzen.cli.auto_answer import write_questions_json, resolve_answer

                    write_questions_json(session_dir, session_id=session_id, wf_state=wf_state)
                    next_message_text = await resolve_answer(
                        session_dir,
                        wf_state,
                        questions,
                        mode="default",
                        expected_step=current_step,
                    )
                except Exception:
                    next_message_text = None

                if not next_message_text:
                    console.print(
                        "[red]Batch mode could not auto-answer pending questions; stopping.[/red]"
                    )
                    break
            else:
                # Normal progression between steps
                next_message_text = initial_request if current_step == "acquire_structure" else "continue"

            with suppress_adk_unknown_agent_warnings():
                step_agent, step_toolsets = create_workflow_step_agent(current_step)
                runner = Runner(
                    app_name=APP_NAME,
                    agent=step_agent,
                    session_service=session_service,
                )
                await run_agent_with_events(
                    runner=runner,
                    session_id=session_id,
                    message=create_message(next_message_text),
                    console=console,
                    show_progress=True,
                    known_agents={step_agent.name},
                )
                await _recover_state_after_step()

        # Show results
        state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)
        display_debug_state(state, console)
        display_results(state, console)
    finally:
        # Note: We skip explicit MCP cleanup because it causes anyio task context
        # errors. The OS cleans up resources when the process exits.
        pass


async def _run_interactive(session_service, session_id: str, session_dir: str, request: str):
    """Run in interactive mode with stepwise workflow v2."""
    from google.adk.runners import Runner
    from mdzen.state.session_manager import (
        initialize_session_state,
        get_session_state,
        save_chat_history,
        update_session_state,
    )
    from mdzen.utils import suppress_adk_unknown_agent_warnings
    import json
    import os
    import sys
    from pathlib import Path
    from mdzen.workflow import get_next_workflow_v2_step

    # Track all toolsets for cleanup
    all_toolsets = []

    auto_answer_enabled = (os.environ.get("MDZEN_AUTO_ANSWER", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())

    # Create async prompt function (TTY only). In non-TTY environments, require auto-answer.
    async_prompt = None
    if is_tty:
        from prompt_toolkit import PromptSession

        prompt_session = PromptSession()

        async def async_prompt(message: str) -> str:
            return await prompt_session.prompt_async(message)
    elif not auto_answer_enabled:
        console.print(
            "[red]Input is not a terminal. Use --print (-p) or --auto-answer with answers.json injection.[/red]"
        )
        return

    # Initialize session
    await initialize_session_state(
        session_service=session_service,
        app_name=APP_NAME,
        user_id=DEFAULT_USER,
        session_id=session_id,
        session_dir=session_dir,
    )

    try:
        initial_request = request
        console.print("\n[bold]Workflow v2: (1)→(2)→(3)→(4)→(quick_md)→(validation)[/bold]")
        console.print("[dim]Running stepwise workflow...[/dim]\n")

        from mdzen.agents.workflow_step_agent import create_workflow_step_agent

        next_message_text = request

        def _load_disk_workflow_state() -> dict:
            path = Path(session_dir) / "workflow_state.json"
            if not path.exists():
                return {}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        def _save_disk_workflow_state(wf: dict) -> None:
            path = Path(session_dir) / "workflow_state.json"
            try:
                path.write_text(json.dumps(wf, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            except Exception:
                pass

        def _newest_match(patterns: list[str]) -> str:
            base = Path(session_dir)
            matches: list[Path] = []
            for pat in patterns:
                matches.extend(list(base.glob(pat)))
            matches = [p for p in matches if p.exists()]
            if not matches:
                return ""
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return str(matches[0])

        async def _recover_state_after_step() -> None:
            """Best-effort recovery when the model forgets to call update_workflow_state()."""
            wf = _load_disk_workflow_state()
            if not wf:
                return
            current = wf.get("current_step") or ""
            completed = list(wf.get("completed_steps") or [])
            changed = False

            if current == "acquire_structure" and not wf.get("structure_file"):
                struct = _newest_match(["*.pdb", "*.cif", "*.ent", "**/AF-*.pdb", "**/AF-*.cif"])
                if struct:
                    wf["structure_file"] = struct
                    if "acquire_structure" not in completed:
                        completed.append("acquire_structure")
                    wf["completed_steps"] = completed
                    nxt = get_next_workflow_v2_step("acquire_structure")
                    if nxt:
                        wf["current_step"] = nxt
                    changed = True

            if current == "select_prepare" and not wf.get("selected_structure_file"):
                selected = _newest_match(
                    [
                        "**/selected_structure*.pdb",
                        "**/split/**/protein_*.pdb",
                        "**/split_*/**/protein_*.pdb",
                    ]
                )
                if selected:
                    wf["selected_structure_file"] = selected
                    if "select_prepare" not in completed:
                        completed.append("select_prepare")
                    wf["completed_steps"] = completed
                    nxt = get_next_workflow_v2_step("select_prepare")
                    if nxt:
                        wf["current_step"] = nxt
                    changed = True

            if current == "solvate_or_membrane" and not (wf.get("solvated_pdb") or wf.get("membrane_pdb")):
                solv = _newest_match(["**/solvated.pdb", "**/solvate/**/*.pdb"])
                mem = _newest_match(["**/membrane.pdb", "**/membrane/**/*.pdb"])
                if solv:
                    wf["solvated_pdb"] = solv
                    wf["solvation_type"] = wf.get("solvation_type") or "explicit"
                    changed = True
                elif mem:
                    wf["membrane_pdb"] = mem
                    wf["solvation_type"] = wf.get("solvation_type") or "membrane"
                    changed = True

            if current == "quick_md" and not (wf.get("parm7") and wf.get("rst7")):
                parm7 = _newest_match(["**/*.parm7"])
                rst7 = _newest_match(["**/*.rst7"])
                traj = _newest_match(["**/trajectory.dcd", "**/*.dcd"])
                if parm7 and rst7:
                    wf["parm7"] = parm7
                    wf["rst7"] = rst7
                    if traj:
                        wf["trajectory"] = traj
                    if "quick_md" not in completed:
                        completed.append("quick_md")
                    wf["completed_steps"] = completed
                    nxt = get_next_workflow_v2_step("quick_md")
                    if nxt:
                        wf["current_step"] = nxt
                    changed = True

            if current == "validation" and not wf.get("validation_result"):
                val = _newest_match(["**/validation_result.json"])
                if val:
                    try:
                        wf["validation_result"] = json.loads(Path(val).read_text(encoding="utf-8"))
                        if "validation" not in completed:
                            completed.append("validation")
                        wf["completed_steps"] = completed
                        wf["current_step"] = wf.get("current_step") or "validation"
                        changed = True
                    except Exception:
                        pass

            if changed:
                _save_disk_workflow_state(wf)
                await update_session_state(
                    session_service,
                    APP_NAME,
                    DEFAULT_USER,
                    session_id,
                    {
                        "workflow_state": json.dumps(wf, ensure_ascii=False, default=str),
                        "workflow_current_step": str(wf.get("current_step") or ""),
                    },
                )

        # Step loop
        while True:
            state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)
            wf_raw = state.get("workflow_state", "")
            wf_state = {}
            if isinstance(wf_raw, str) and wf_raw:
                try:
                    wf_state = json.loads(wf_raw)
                except Exception:
                    wf_state = {}
            elif isinstance(wf_raw, dict):
                wf_state = wf_raw

            current_step = wf_state.get("current_step") or "acquire_structure"
            awaiting = bool(wf_state.get("awaiting_user_input"))
            questions = wf_state.get("pending_questions") or []

            # -----------------------------------------------------------------
            # Guard: clear stale acquire_structure questions if a structure file
            # already exists (common when small models incorrectly re-ask).
            # -----------------------------------------------------------------
            if current_step == "acquire_structure" and awaiting:
                # If the model is asking again despite the initial request containing a PDB ID,
                # re-inject it as a minimal hint on the next turn.
                # This helps small models that ignored the initial instruction.
                try:
                    import re

                    m = re.search(r"\b([0-9][A-Za-z0-9]{3})\b", str(initial_request or ""))
                    injected_pdb = m.group(1).upper() if m else ""
                except Exception:
                    injected_pdb = ""

                struct = wf_state.get("structure_file") or ""
                if not struct:
                    struct = _newest_match(["*.pdb", "*.cif", "*.ent", "**/AF-*.pdb", "**/AF-*.cif"])
                if struct:
                    wf_state["structure_file"] = struct
                    wf_state["awaiting_user_input"] = False
                    wf_state["pending_questions"] = []
                    completed = list(wf_state.get("completed_steps") or [])
                    if "acquire_structure" not in completed:
                        completed.append("acquire_structure")
                    wf_state["completed_steps"] = completed
                    nxt = get_next_workflow_v2_step("acquire_structure")
                    if nxt:
                        wf_state["current_step"] = nxt
                    wf_state["last_step_summary"] = "Auto-recovered structure_file; skipping stale acquire_structure questions"
                    try:
                        (Path(session_dir) / "workflow_state.json").write_text(
                            json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                    await update_session_state(
                        session_service,
                        APP_NAME,
                        DEFAULT_USER,
                        session_id,
                        {
                            "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                            "workflow_current_step": str(wf_state.get("current_step") or ""),
                        },
                    )
                    # Restart loop with the recovered state
                    continue

                # No structure file yet, but we can still help by re-running acquire_structure
                # with an explicit minimal message containing the PDB ID.
                if injected_pdb:
                    wf_state["awaiting_user_input"] = False
                    # Keep pending_questions (for UI), but do not block progression on them.
                    wf_state["last_step_summary"] = f"Re-injecting PDB ID for acquire_structure: {injected_pdb}"
                    try:
                        (Path(session_dir) / "workflow_state.json").write_text(
                            json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                    await update_session_state(
                        session_service,
                        APP_NAME,
                        DEFAULT_USER,
                        session_id,
                        {
                            "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                            "workflow_current_step": "acquire_structure",
                        },
                    )
                    # Run the step again with an explicit hint.
                    next_message_text = f"PDB ID: {injected_pdb}"
                    with suppress_adk_unknown_agent_warnings():
                        step_agent, step_toolsets = create_workflow_step_agent("acquire_structure")
                        all_toolsets.extend(step_toolsets)
                        runner = Runner(
                            app_name=APP_NAME,
                            agent=step_agent,
                            session_service=session_service,
                        )
                        await run_agent_with_events(
                            runner=runner,
                            session_id=session_id,
                            message=create_message(next_message_text),
                            console=console,
                            show_progress=True,
                            known_agents={step_agent.name},
                        )
                        await _recover_state_after_step()
                    continue

            # -----------------------------------------------------------------
            # Guard: clear stale select_prepare questions after user already answered.
            #
            # Some models may re-trigger awaiting_user_input via update_workflow_state()
            # even after chain/ligand selections are present. When that happens, the CLI
            # would keep re-asking. If selections exist, treat those questions as stale
            # and continue with select_prepare execution instead of prompting again.
            # -----------------------------------------------------------------
            if current_step == "select_prepare" and awaiting:
                answered_select = bool(wf_state.get("selection_chains")) and bool(wf_state.get("include_types"))
                if answered_select and questions:
                    blob = " ".join(str(q).lower() for q in questions if str(q).strip())
                    looks_like_select_q = any(
                        k in blob
                        for k in [
                            "which protein chains",
                            "protein chains to simulate",
                            "ligands detected",
                            "include ligands",
                        ]
                    )
                    if looks_like_select_q:
                        wf_state["awaiting_user_input"] = False
                        wf_state["pending_questions"] = []
                        wf_state["last_step_summary"] = "Cleared stale select_prepare questions (already answered)"
                        try:
                            (Path(session_dir) / "workflow_state.json").write_text(
                                json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                                encoding="utf-8",
                            )
                        except Exception:
                            pass
                        await update_session_state(
                            session_service,
                            APP_NAME,
                            DEFAULT_USER,
                            session_id,
                            {
                                "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                                "workflow_current_step": "select_prepare",
                            },
                        )
                        awaiting = False
                        questions = []

            # -----------------------------------------------------------------
            # Hard guard: ensure chain/ligand selection is not skipped.
            # - If initial prompt contains sufficient instruction (e.g., "chain A", "no ligand"),
            #   apply deterministically without asking.
            # - Otherwise ask only when needed (multiple protein chains or ligands detected).
            # -----------------------------------------------------------------
            if current_step == "select_prepare" and not awaiting and not wf_state.get("selection_chains"):
                structure_file = wf_state.get("structure_file") or ""
                if structure_file:
                    # Detect chain/ligand options directly (do not rely on LLM).
                    try:
                        import gemmi
                        import re

                        AMINO_ACIDS = {
                            "ALA","ARG","ASN","ASP","CYS","GLN","GLU","GLY","HIS",
                            "ILE","LEU","LYS","MET","PHE","PRO","SER","THR","TRP",
                            "TYR","VAL","SEC","PYL",
                        }
                        WATER = {"HOH","WAT","H2O","DOD","D2O","TIP3","SOL","OPC"}
                        IONS = {"NA","CL","K","MG","CA","ZN","FE","MN","CU","CO","NI","CD","HG"}

                        st = gemmi.read_structure(structure_file)
                        st.setup_entities()
                        model = st[0]

                        protein_chains: list[str] = []
                        ligand_resnames: set[str] = set()

                        for chain in model:
                            has_protein = False
                            for res in chain:
                                rn = res.name.strip().upper()
                                if rn in AMINO_ACIDS:
                                    has_protein = True
                                elif rn in WATER or rn in IONS:
                                    continue
                                else:
                                    # Ligand-like residue
                                    if len(list(res)) >= 3:
                                        ligand_resnames.add(rn)
                            if has_protein:
                                protein_chains.append(chain.name)

                        protein_chains = list(dict.fromkeys([c for c in protein_chains if c]))
                        ligands = sorted(ligand_resnames)

                        req_low = (initial_request or "").lower()

                        # Determine if user already specified chains explicitly (prefer "chain A" forms).
                        mentioned = set(re.findall(r"\bchains?\s*([a-z0-9])\b", req_low))
                        mentioned |= set(re.findall(r"\bchain\s*([a-z0-9])\b", req_low))
                        mentioned = {m.upper() for m in mentioned if m}
                        selected_chains: list[str] = []
                        if mentioned:
                            selected_chains = [c for c in protein_chains if c.upper() in mentioned]

                        # Ligand handling from initial request
                        include_ligands: bool | None = None
                        if any(tok in req_low for tok in ["no ligand", "no ligands", "without ligand", "exclude ligand", "remove ligand", "protein only", "apo"]):
                            include_ligands = False
                        elif any(tok in req_low for tok in ["with ligand", "include ligand", "keep ligand", "holo"]):
                            include_ligands = True

                        pending = []
                        if len(protein_chains) > 1 and not selected_chains:
                            pending.append(
                                f"Which protein chains to simulate? Options: {', '.join(protein_chains)} "
                                f"(default: {protein_chains[0]})"
                            )
                        if ligands and include_ligands is None:
                            pending.append(
                                f"Ligands detected: {', '.join(ligands)}. Include ligands? (yes/no, default: yes)"
                            )

                        # If no questions needed, deterministically set selection and continue.
                        if not pending:
                            if not selected_chains:
                                # Default: single chain -> that chain; else prefer A if present.
                                if len(protein_chains) == 1:
                                    selected_chains = [protein_chains[0]]
                                elif "A" in {c.upper() for c in protein_chains}:
                                    selected_chains = ["A"]
                                elif protein_chains:
                                    selected_chains = [protein_chains[0]]

                            if include_ligands is None:
                                include_ligands = False if not ligands else True

                            wf_state["selection_chains"] = selected_chains
                            wf_state["include_types"] = ["protein", "ion"] + (["ligand"] if include_ligands else [])
                            wf_state["last_step_summary"] = (
                                f"(deterministic) chains={selected_chains}, include_ligands={include_ligands}"
                            )
                            (Path(session_dir) / "workflow_state.json").write_text(
                                json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                                encoding="utf-8",
                            )
                            await update_session_state(
                                session_service,
                                APP_NAME,
                                DEFAULT_USER,
                                session_id,
                                {
                                    "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                                    "workflow_current_step": "select_prepare",
                                },
                            )
                            # Continue loop; next iteration will run the step agent with selections in state.
                            continue

                        if pending:
                            wf_state["awaiting_user_input"] = True
                            wf_state["pending_questions"] = pending
                            wf_state["detected_protein_chains"] = protein_chains
                            wf_state["detected_ligands"] = ligands
                            # Persist for UI + next turn parsing
                            (Path(session_dir) / "workflow_state.json").write_text(
                                json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                                encoding="utf-8",
                            )
                            await update_session_state(
                                session_service,
                                APP_NAME,
                                DEFAULT_USER,
                                session_id,
                                {
                                    "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                                    "workflow_current_step": "select_prepare",
                                },
                            )
                            # Loop will prompt user next
                            awaiting = True
                            questions = pending
                    except Exception:
                        # If local parsing fails, fall back to LLM behavior.
                        pass

            # Completion condition
            completed = wf_state.get("completed_steps") or []
            if "validation" in completed and not awaiting:
                break

            # If waiting for user input, prompt and re-run same step
            if awaiting:
                # Write machine-readable questions for external drivers (Cursor/Claude Code/CI).
                try:
                    from mdzen.cli.auto_answer import write_questions_json

                    write_questions_json(session_dir, session_id=session_id, wf_state=wf_state)
                except Exception:
                    pass

                if questions:
                    console.print("\n[yellow]Questions:[/yellow]")
                    for q in questions:
                        console.print(f"  - {q}")
                user_input = ""
                if auto_answer_enabled:
                    # Prefer external injection, then optional LLM, then defaults.
                    try:
                        from mdzen.cli.auto_answer import resolve_answer

                        seq = (
                            os.environ.get("MDZEN_AUTO_ANSWER_SEQUENCE", "external,llm,default")
                            .strip()
                            .split(",")
                        )
                        for mode in [m.strip() for m in seq if m.strip()]:
                            ans = await resolve_answer(
                                session_dir,
                                wf_state,
                                questions,
                                mode=mode,
                                expected_step=current_step,
                            )
                            if ans:
                                user_input = ans
                                break
                    except Exception:
                        user_input = ""

                # If still empty, fall back to human TTY prompt (interactive use).
                if not user_input and async_prompt is not None:
                    user_input = (await async_prompt(">> ")).strip()

                if not user_input:
                    console.print("[red]No answer available (auto-answer/human input). Stopping.[/red]")
                    return

                if user_input.lower() in ["quit", "exit", "q"]:
                    console.print("[yellow]Session ended.[/yellow]")
                    return

                # Parse user answers for select_prepare (deterministic)
                if current_step == "select_prepare":
                    detected_chains = wf_state.get("detected_protein_chains") or []
                    detected_ligands = wf_state.get("detected_ligands") or []
                    pending_qs = wf_state.get("pending_questions") or []

                    # If the LLM produced the questions (without our deterministic pre-detection),
                    # `detected_*` may be missing. Re-detect from the actual structure file so that
                    # answers like "A, no" reliably disable ligand inclusion.
                    if (not detected_chains) or (detected_ligands is None) or (detected_ligands == []):
                        try:
                            import gemmi

                            structure_file = str(wf_state.get("structure_file") or "")
                            if structure_file:
                                AMINO_ACIDS = {
                                    "ALA","ARG","ASN","ASP","CYS","GLN","GLU","GLY","HIS",
                                    "ILE","LEU","LYS","MET","PHE","PRO","SER","THR","TRP",
                                    "TYR","VAL","SEC","PYL",
                                    # Amber/protonation variants (common)
                                    "HID","HIE","HIP","CYX","CYM","ASH","GLH","LYN",
                                }
                                WATER = {"HOH","WAT","H2O","DOD","D2O","TIP3","SOL","OPC"}
                                IONS = {"NA","CL","K","MG","CA","ZN","FE","MN","CU","CO","NI","CD","HG"}

                                st = gemmi.read_structure(structure_file)
                                st.setup_entities()
                                model = st[0]

                                protein_chains: list[str] = []
                                ligand_resnames: set[str] = set()
                                for chain in model:
                                    has_protein = False
                                    for res in chain:
                                        rn = res.name.strip().upper()
                                        if rn in AMINO_ACIDS:
                                            has_protein = True
                                        elif rn in WATER or rn in IONS:
                                            continue
                                        else:
                                            if len(list(res)) >= 3:
                                                ligand_resnames.add(rn)
                                    if has_protein and chain.name:
                                        protein_chains.append(chain.name)

                                detected_chains = list(dict.fromkeys(protein_chains))
                                detected_ligands = sorted(ligand_resnames)
                        except Exception:
                            pass

                    import re

                    text = user_input.strip()
                    low = text.lower()

                    # Determine which questions must be answered on this turn.
                    # We only enforce what we asked (avoids blocking when a question wasn't relevant).
                    pending_blob = " ".join(str(q).lower() for q in pending_qs if str(q).strip())
                    require_chain = any(
                        k in pending_blob
                        for k in [
                            "which protein chains",
                            "protein chains to simulate",
                            "which protein chains to simulate",
                        ]
                    )
                    require_ligand = any(
                        k in pending_blob
                        for k in [
                            "ligands detected",
                            "include ligands",
                            "include ligands?",
                        ]
                    )

                    def _looks_like_model_question(t: str) -> bool:
                        return any(
                            k in t
                            for k in [
                                "which model",
                                "model?",
                                "who are you",
                                "what model",
                                "あなたは誰",
                                "どのモデル",
                                "モデル",
                            ]
                        )

                    def _ensure_guidance_questions(qs: list[str]) -> list[str]:
                        qs = [str(q) for q in (qs or []) if str(q).strip()]
                        guidance = "Reply examples: `A, no` / `A, yes` / `all, no`"
                        if not any("reply example" in q.lower() for q in qs):
                            qs.append(guidance)
                        return qs

                    async def _reask_without_progress(reason: str) -> None:
                        wf_state["awaiting_user_input"] = True
                        wf_state["pending_questions"] = _ensure_guidance_questions(pending_qs or questions or [])
                        wf_state["last_step_summary"] = reason
                        (Path(session_dir) / "workflow_state.json").write_text(
                            json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                        await update_session_state(
                            session_service,
                            APP_NAME,
                            DEFAULT_USER,
                            session_id,
                            {
                                "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                                "workflow_current_step": "select_prepare",
                            },
                        )

                    # Meta question handling: answer briefly, then guide back to required inputs.
                    if _looks_like_model_question(low):
                        try:
                            from mdzen.config import get_litellm_model

                            console.print(
                                "[dim]Models (MDZen): "
                                f"acquire_structure={get_litellm_model('clarification')}, "
                                f"select_prepare={get_litellm_model('setup')}[/dim]"
                            )
                        except Exception:
                            console.print("[dim]Model info unavailable in this environment.[/dim]")

                        console.print(
                            "[yellow]To continue, please answer the pending questions (chain + ligand).[/yellow]"
                        )
                        await _reask_without_progress("Meta question received; re-asking for chain/ligand selection")
                        continue

                    # Chains (only accept defaults when chain question wasn't required)
                    selected_chains: list[str] = []
                    chain_answered = False
                    if any(tok in low for tok in ["all", "both", "全部"]):
                        if detected_chains:
                            selected_chains = list(detected_chains)
                            chain_answered = True
                    else:
                        # Extract single-letter chain IDs (A,B,...) that appear in input
                        letters = re.findall(r"\b([A-Za-z0-9])\b", text)
                        if letters:
                            wanted = {c.upper() for c in letters}
                            selected_chains = [c for c in detected_chains if c.upper() in wanted]
                            chain_answered = bool(selected_chains)

                    if not selected_chains and detected_chains and not require_chain:
                        selected_chains = [detected_chains[0]]

                    # Ligand include/exclude (only accept defaults when ligand question wasn't required)
                    include_ligands: bool | None = None
                    ligand_answered = False

                    yes_pat = re.compile(r"\b(yes|y|include|keep|with|holo)\b", re.IGNORECASE)
                    no_pat = re.compile(r"\b(no|n|exclude|remove|without|apo)\b", re.IGNORECASE)
                    yes_jp = any(tok in text for tok in ["はい", "含め", "入れる", "保持", "あり"])
                    no_jp = any(tok in text for tok in ["いいえ", "除外", "外す", "なし", "無し"])

                    said_yes = bool(yes_pat.search(text)) or yes_jp
                    said_no = bool(no_pat.search(text)) or no_jp
                    if said_yes and said_no:
                        # Ambiguous answer -> ask again
                        console.print(
                            "[yellow]I couldn't tell whether you want ligands included or excluded.[/yellow]"
                        )
                        await _reask_without_progress("Ambiguous ligand answer; re-asking")
                        continue

                    if require_ligand:
                        if said_yes:
                            include_ligands = True
                            ligand_answered = True
                        elif said_no:
                            include_ligands = False
                            ligand_answered = True
                    else:
                        # If we did not require a ligand answer, fall back to detected ligands.
                        include_ligands = bool(detected_ligands)

                    # Enforce that required questions were actually answered.
                    if require_chain and not chain_answered:
                        console.print(
                            "[yellow]Please specify which protein chains to simulate (e.g., A / B / all).[/yellow]"
                        )
                        await _reask_without_progress("Missing chain selection; re-asking")
                        continue
                    if require_ligand and not ligand_answered:
                        console.print(
                            "[yellow]Please answer whether to include ligands (yes/no).[/yellow]"
                        )
                        await _reask_without_progress("Missing ligand selection; re-asking")
                        continue

                    # Safety fallback (should be rare): if we still have no chains, default when possible.
                    if not selected_chains and detected_chains:
                        selected_chains = [detected_chains[0]]

                    # If ligands were detected but user didn't answer (shouldn't happen due to checks),
                    # default to including ligands.
                    if include_ligands is None:
                        include_ligands = True

                    include_types = ["protein", "ion"] + (["ligand"] if include_ligands else [])

                    wf_state["selection_chains"] = selected_chains
                    wf_state["include_types"] = include_types
                    wf_state["awaiting_user_input"] = False
                    wf_state["pending_questions"] = []
                    wf_state["last_step_summary"] = (
                        f"User selected chains={selected_chains}, include_ligands={include_ligands}"
                    )

                    (Path(session_dir) / "workflow_state.json").write_text(
                        json.dumps(wf_state, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8",
                    )
                    await update_session_state(
                        session_service,
                        APP_NAME,
                        DEFAULT_USER,
                        session_id,
                        {
                            "workflow_state": json.dumps(wf_state, ensure_ascii=False, default=str),
                            "workflow_current_step": "select_prepare",
                        },
                    )
                    next_message_text = "continue"
                else:
                    next_message_text = user_input
            else:
                # Normal progression between steps
                # Step 1 uses the original request; others can proceed with "continue"
                if current_step != "acquire_structure":
                    next_message_text = "continue"

            # Run current step agent
            with suppress_adk_unknown_agent_warnings():
                step_agent, step_toolsets = create_workflow_step_agent(current_step)
                all_toolsets.extend(step_toolsets)
                runner = Runner(
                    app_name=APP_NAME,
                    agent=step_agent,
                    session_service=session_service,
                )

                await run_agent_with_events(
                    runner=runner,
                    session_id=session_id,
                    message=create_message(next_message_text),
                    console=console,
                    show_progress=True,
                    known_agents={step_agent.name},
                )
                await _recover_state_after_step()

        # Save chat history
        try:
            chat_file = await save_chat_history(
                session_service, APP_NAME, DEFAULT_USER, session_id, session_dir
            )
            if chat_file:
                console.print(f"[dim]Chat history saved: {chat_file}[/dim]")
        except Exception:
            pass

        # Show results (validation report + generated files)
        state = await get_session_state(session_service, APP_NAME, DEFAULT_USER, session_id)
        display_results(state, console)
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
