"""MDClaw CLI — invoke any tool directly from the command line.

Usage:
    mdclaw --list                          # List all tools
    mdclaw --list-json [tool]              # Machine-readable tool contract
    mdclaw --version                       # Show version
    mdclaw <tool> --help                   # Tool-specific help
    mdclaw <tool> [--param value ...]      # Run a tool
    mdclaw <tool> --json-input '{...}'     # Pass all params as JSON

Output is always JSON on stdout; logs go to stderr.
"""

import argparse
import ast
import asyncio
import inspect
import json
import logging
import math
import os
import sys
import types
import time
import traceback
from pathlib import Path
from typing import TextIO, Union, get_args, get_origin

from mdclaw import __version__
from mdclaw._common import finalize_error
from mdclaw._registry import SERVER_REGISTRY
from mdclaw.node.constants import CANONICAL_FORWARD_NODE_TYPE, DAG_GUIDANCE
from mdclaw._tool_meta import tool_job_dir_is_data, tool_node_type, tool_requires_node

# Tools consolidated during the schema-v3 refactor. Old names are no longer
# registered as CLI subcommands; invoking one returns a structured
# ``tool_renamed`` error pointing at the replacement so weak agents get a
# deterministic recovery signal instead of an argparse "invalid choice" dump.
# The underlying Python functions still exist for direct importers.
_RENAMED_TOOLS = {
    "record_study_decision": "record_study_log --record-type decision",
    "record_study_question": "record_study_log --record-type question",
    "record_token_usage": "record_study_log --record-type token_usage",
    "add_node_need": "manage_node_need --action add",
    "clear_node_need": "manage_node_need --action clear",
    "record_node_need_attempt": "manage_node_need --action record_attempt",
    "update_node_status": "update_workflow_state (--node-id/--status)",
    "update_job_params": "update_workflow_state (--params)",
}

# Global options that consume a following value, used to locate the subcommand
# token when scanning argv for a renamed tool.
_GLOBAL_VALUE_OPTIONS = {"--job-dir", "--node-id", "--list-json"}


def _attach_dag_handoff(result, job_dir, node_id):
    """Add the completed node contract to a workflow tool result."""
    if not isinstance(result, dict) or not job_dir or not node_id:
        return result
    result.setdefault("dag_guidance", DAG_GUIDANCE)
    try:
        node = json.loads(
            (Path(job_dir) / "nodes" / node_id / "node.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        return result
    handoff = {
        "node_id": node_id,
        "status": node.get("status"),
        "artifact_keys": sorted((node.get("artifacts") or {}).keys()),
        "next_node_inputs": "auto_resolved",
    }
    node_type = node.get("node_type") or node.get("type")
    next_node_type = CANONICAL_FORWARD_NODE_TYPE.get(node_type)
    if node.get("status") == "completed" and next_node_type:
        handoff["default_forward_branch"] = {
            "optional": True,
            "node_type": next_node_type,
            "create_command": (
                f"mdclaw create_node --job-dir {Path(job_dir).resolve()} "
                f"--node-type {next_node_type} --parent-node-ids {node_id}"
            ),
        }
    result["dag_handoff"] = handoff
    return result


def _detect_subcommand(argv: list[str]) -> str | None:
    """Return the subcommand token from ``argv``, skipping global options."""
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            option = tok.split("=", 1)[0]
            if option in _GLOBAL_VALUE_OPTIONS and "=" not in tok:
                skip_next = True
            continue
        return tok
    return None


# ---------------------------------------------------------------------------
# Logging: force all loggers to stderr so stdout stays clean JSON
# ---------------------------------------------------------------------------

def _configure_logging():
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s - %(levelname)s - %(message)s"))
    root.addHandler(handler)


class _TailCaptureStream:
    """Capture a bounded text tail while optionally teeing writes onward."""

    def __init__(self, wrapped: TextIO, *, limit: int = 65536, tee: bool = True):
        self._wrapped = wrapped
        self._limit = limit
        self._tail = ""
        self._tee = tee

    def write(self, text):
        text = str(text)
        written = self._wrapped.write(text) if self._tee else len(text)
        self._tail = (self._tail + text)[-self._limit:]
        return written

    def flush(self):
        return self._wrapped.flush()

    def get_tail(self) -> str:
        return self._tail

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _swap_logging_stream(old_stream: TextIO, new_stream: TextIO) -> list[tuple[logging.StreamHandler, TextIO]]:
    """Point stream handlers at ``new_stream`` while capturing tool output."""
    swaps: list[tuple[logging.StreamHandler, TextIO]] = []
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if (
                isinstance(handler, logging.StreamHandler)
                and getattr(handler, "stream", None) is old_stream
            ):
                handler.setStream(new_stream)
                swaps.append((handler, old_stream))
    return swaps


def _restore_logging_stream(swaps: list[tuple[logging.StreamHandler, TextIO]]) -> None:
    for handler, old_stream in reversed(swaps):
        try:
            handler.setStream(old_stream)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def _discover_tools() -> dict[str, dict]:
    """Import all servers and collect tool functions from TOOLS dicts.

    Returns:
        dict mapping tool_name -> {
            "fn": callable,
            "is_async": bool,
            "server": str,
            "description": str,
        }
    """
    import importlib

    tools: dict[str, dict] = {}
    for server_name, module_path in SERVER_REGISTRY.items():
        try:
            mod = importlib.import_module(module_path)
        except ImportError as e:
            print(f"Warning: cannot import {module_path}: {e}", file=sys.stderr)
            continue
        module_tools = getattr(mod, "TOOLS", {})
        for tool_name, fn in module_tools.items():
            if tool_name in tools:
                first_server = tools[tool_name]["server"]
                raise ValueError(
                    f"Duplicate tool name '{tool_name}' registered by "
                    f"servers '{first_server}' and '{server_name}'"
                )
            tools[tool_name] = {
                "fn": fn,
                "is_async": inspect.iscoroutinefunction(fn),
                "server": server_name,
                "description": inspect.getdoc(fn) or "",
                "requires_node": tool_requires_node(fn),
                "node_type": tool_node_type(fn),
                "job_dir_is_data": tool_job_dir_is_data(fn),
            }
    return tools


# ---------------------------------------------------------------------------
# Type helpers for argparse
# ---------------------------------------------------------------------------

def _unwrap_optional(hint):
    """If hint is Optional[X] (Union[X, None]), return (X, True). Else (hint, False)."""
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        args = get_args(hint)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == len(args):
            return hint, False
        if len(non_none) == 1:
            return non_none[0], True
        return hint, True  # complex Union — treat as optional
    return hint, False


def _is_list_of_str(hint) -> bool:
    """Check if hint is list[str] or List[str]."""
    origin = get_origin(hint)
    if origin is list:
        args = get_args(hint)
        return args == (str,)
    return False


_CLI_REPEATED_STRING_PARAMS = frozenset({
    ("embed_in_membrane", "lipids"),
})


def _is_cli_repeated_string_param(tool_name: str, pname: str) -> bool:
    """Return true for string parameters that accept repeated CLI tokens."""
    return (tool_name, pname) in _CLI_REPEATED_STRING_PARAMS


def _is_dict_type(hint) -> bool:
    """Check if hint is dict or Dict[...]."""
    origin = get_origin(hint)
    return origin is dict or hint is dict


def _is_list_of_dict(hint) -> bool:
    """Check if hint is list[dict] / list[Dict[...]] / List[dict] etc.

    Used to route structured list arguments (e.g. ``submit_array_job
    tasks=list[dict]``) through the same JSON-string argparse path as
    plain dict arguments — they're not expressible as a flat CLI list.
    """
    origin = get_origin(hint)
    if origin is not list:
        return False
    args = get_args(hint)
    if not args:
        return False
    inner = args[0]
    inner_origin = get_origin(inner)
    return inner_origin is dict or inner is dict


def _is_list_of_list(hint) -> bool:
    """Check if hint is list[list[...]] / List[List[...]].

    Nested-list arguments (e.g. ``atom_pairs=list[list[int]]`` for
    ``analyze_distance``) are not expressible as a flat CLI list
    either; route them through the JSON-string argparse path.
    """
    origin = get_origin(hint)
    if origin is not list:
        return False
    args = get_args(hint)
    if not args:
        return False
    inner = args[0]
    inner_origin = get_origin(inner)
    return inner_origin is list or inner is list


def _takes_json(hint) -> bool:
    """True when the argument expects a JSON string at the CLI boundary.

    Covers ``dict`` / ``Dict[...]``, ``list[dict]`` / ``List[Dict[...]]``,
    and ``list[list[...]]`` (including under ``Optional[...]``).
    ``list[str]`` stays on the plain ``nargs='+'`` path — that's a
    better CLI UX for flat lists.
    """
    hint, _ = _unwrap_optional(hint)
    return _is_dict_type(hint) or _is_list_of_dict(hint) or _is_list_of_list(hint)


def _is_path_type(hint) -> bool:
    """True for pathlib.Path CLI parameters, including Optional[Path]."""
    inner, _ = _unwrap_optional(hint)
    return inner is Path


def _coerce_value(value, hint):
    """Coerce a CLI value to the target type."""
    if hint is None or hint is inspect.Parameter.empty:
        return value

    inner, _ = _unwrap_optional(hint)

    if inner is bool:
        # Parsed by the boolean CLI arguments, so the value is already bool.
        return value
    if inner is int:
        return int(value)
    if inner is float:
        return float(value)
    if inner is str:
        return str(value)
    if inner is Path:
        if isinstance(value, Path):
            return value
        return Path(value)
    if _is_list_of_str(inner):
        # nargs='+' gives us a list already
        if isinstance(value, list):
            return value
        return [value]
    if _takes_json(inner):
        # JSON string -> dict / list[dict]
        if isinstance(value, str):
            return json.loads(value)
        return value
    return value


def _parse_cli_bool(value: str) -> bool:
    """Parse an explicit boolean value while keeping flag-only CLI support."""
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("expected 'true' or 'false'")


# ---------------------------------------------------------------------------
# argparse construction
# ---------------------------------------------------------------------------

def _build_parser(tools: dict[str, dict]) -> argparse.ArgumentParser:
    """Build the top-level parser and one subparser per tool."""
    parser = argparse.ArgumentParser(
        prog="mdclaw",
        description="MDClaw CLI — run MD tools from the command line.",
    )
    parser.add_argument("--version", action="version", version=f"mdclaw {__version__}")
    parser.add_argument(
        "--list", action="store_true", dest="list_tools",
        help="List all available tools grouped by server.",
    )
    parser.add_argument(
        "--list-json", nargs="?", const="", default=None,
        dest="list_tools_json", metavar="TOOL",
        help=(
            "List available tools and CLI parameters as machine-readable JSON; "
            "optionally show one exact tool."
        ),
    )
    parser.add_argument(
        "--job-dir", type=str, default=None, dest="_global_job_dir",
        help="Job directory for node-based state tracking (schema v3).",
    )
    parser.add_argument(
        "--node-id", type=str, default=None, dest="_global_node_id",
        help="Node ID for node-based state tracking (requires --job-dir).",
    )

    subparsers = parser.add_subparsers(dest="tool_name")

    for tool_name, info in sorted(tools.items()):
        fn = info["fn"]
        desc_first_line = (info["description"].split("\n")[0].strip()
                          if info["description"] else "")
        if info.get("requires_node"):
            description = (
                f"{desc_first_line}\n\n"
                "CLI workflow contract: DAG-only. Pass --job-dir and --node-id "
                "after create_node and explain_node. Input artifacts are resolved "
                "from the DAG; file arguments cannot override DAG inputs. Use "
                f"'mdclaw --list-json {tool_name}' for the complete parameter schema."
            )
        else:
            description = info["description"]
        sub = subparsers.add_parser(
            tool_name,
            help=desc_first_line,
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub.add_argument(
            "--json-input", type=str, default=None,
            help="Pass all parameters as a JSON string.",
        )

        sig = inspect.signature(fn)
        hints = {}
        try:
            hints = {k: v for k, v in inspect.get_annotations(fn, eval_str=True).items()
                     if k != "return"}
        except Exception:
            pass

        for pname, param in sig.parameters.items():
            # Underscore-prefixed kwargs are internal (used by Python
            # callers for dispatch plumbing, e.g. multi-branch analyze
            # helpers). They never become CLI flags.
            if pname.startswith("_"):
                continue
            hint = hints.get(pname, param.annotation)
            if hint is inspect.Parameter.empty:
                hint = str  # fallback

            inner, is_optional = _unwrap_optional(hint)
            cli_name = "--" + pname.replace("_", "-")
            required = (
                param.default is inspect.Parameter.empty and not is_optional
            ) or (
                info.get("requires_node") and pname in {"job_dir", "node_id"}
            )

            if _is_cli_repeated_string_param(tool_name, pname):
                sub.add_argument(
                    cli_name,
                    nargs="+",
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=(
                        "(str values joined with ':', default: "
                        f"{param.default if param.default is not inspect.Parameter.empty else 'required'})"
                    ),
                )
            elif inner is bool:
                default_val = param.default if param.default is not inspect.Parameter.empty else False
                sub.add_argument(
                    cli_name,
                    nargs="?",
                    const=True,
                    type=_parse_cli_bool,
                    default=default_val,
                    help=f"(bool: true/false, default: {default_val})",
                )
                sub.add_argument(
                    f"--no-{pname.replace('_', '-')}",
                    dest=pname,
                    action="store_false",
                    default=argparse.SUPPRESS,
                    help=f"(set {pname}=false)",
                )
            elif _is_list_of_str(inner):
                sub.add_argument(
                    cli_name,
                    nargs="+",
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help="(list of str, required)" if required else "(list of str)",
                )
            elif _takes_json(inner):
                example = (
                    '\'{"key":"val"}\''
                    if _is_dict_type(inner)
                    else '\'[{"key":"val"}, ...]\''
                )
                sub.add_argument(
                    cli_name,
                    type=str,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=f"(JSON string, e.g. {example}, required)" if required else f"(JSON string, e.g. {example})",
                )
            elif inner is int:
                sub.add_argument(
                    cli_name,
                    type=int,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=(
                        "(int, default: required)" if required else
                        f"(int, default: {param.default})"
                    ),
                )
            elif inner is float:
                sub.add_argument(
                    cli_name,
                    type=float,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=(
                        "(float, default: required)" if required else
                        f"(float, default: {param.default})"
                    ),
                )
            elif _is_path_type(inner):
                sub.add_argument(
                    cli_name,
                    type=Path,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=(
                        "(Path, default: required)" if required else
                        f"(Path, default: {param.default})"
                    ),
                )
            else:
                # Default: str
                sub.add_argument(
                    cli_name,
                    type=str,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=(
                        "(str, default: required)" if required else
                        f"(str, default: {param.default})"
                    ),
                )

    return parser


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _run_tool(fn, is_async: bool, kwargs: dict):
    """Execute a tool function (sync or async) and return its result."""
    if is_async:
        return asyncio.run(fn(**kwargs))
    return fn(**kwargs)


def _benchmark_stage_for_tool(tool_name: str, kwargs: dict) -> str:
    """Best-effort stage label for benchmark harness execution records."""
    if tool_name == "create_node":
        return "dag"
    mapping = {
        "fetch_structure": "source",
        "download_structure": "source",
        "get_alphafold_structure": "source",
        "register_local_structure": "source",
        "list_source_candidates": "source",
        "prepare_complex": "prep",
        "create_mutated_structure": "prep",
        "phosphorylate_residues": "prep",
        "prepare_modified_nucleic": "prep",
        "solvate_structure": "prep",
        "embed_in_membrane": "prep",
        "build_amber_system": "topo",
        "build_openmm_system": "topo",
        "package_openmm_submission": "package",
        "package_mdprep_submission": "package",
        "run_minimization": "min",
        "export_state_pdb": "export",
        "run_equilibration": "eq",
        "run_production": "prod",
        "concat_trajectory": "analysis",
        "fit_trajectory": "analysis",
        "analyze_rmsd": "analysis",
        "analyze_distance": "analysis",
        "analyze_q_value": "analysis",
        "analyze_rmsf": "analysis",
        "analyze_contact_frequency": "analysis",
    }
    return mapping.get(tool_name, tool_name)


def _write_benchmark_harness_record(
    *,
    tool_name: str,
    kwargs: dict,
    exit_code: int,
    started_at: float,
) -> None:
    """Append a measured CLI invocation record when a benchmark runner asks.

    The hook is opt-in through ``MDCLAW_BENCHMARK_HARNESS_LOG`` so ordinary
    command-line usage is unchanged. The benchmark runner later folds this
    JSONL into ``harness_execution.json``.
    """
    log_path = os.environ.get("MDCLAW_BENCHMARK_HARNESS_LOG")
    if not log_path:
        return
    elapsed = time.monotonic() - started_at
    if not math.isfinite(elapsed) or elapsed < 0:
        elapsed = 0.0
    argv = [Path(sys.argv[0]).name or "mdclaw", *sys.argv[1:]]
    record = {
        "stage": _benchmark_stage_for_tool(tool_name, kwargs),
        "command": " ".join(str(part) for part in argv),
        "tool": tool_name,
        "exit_code": int(exit_code),
        "walltime_seconds": round(float(elapsed), 6),
        "recorded_at": datetime_now_utc(),
    }
    run_id = os.environ.get("MDCLAW_BENCHMARK_RUN_ID")
    task_id = os.environ.get("MDCLAW_BENCHMARK_TASK_ID")
    if run_id:
        record["run_id"] = run_id
    if task_id:
        record["task_id"] = task_id
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception:
        # Harness logging must never break the underlying CLI command.
        return


def datetime_now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cli_validation_error(
    field: str,
    message: str,
    *,
    code: str,
    actual: str | None = None,
    expected: str | None = None,
) -> dict:
    context = {"field": field, "actual": actual, "expected": expected, "code": code}
    hints = [f"Check the '{field}' parameter"]
    if expected:
        hints.append(f"Expected: {expected}")
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": code,
        "message": f"Validation failed for '{field}': {message}",
        "hints": hints,
        "context": context,
        "recoverable": True,
        "errors": [f"{field}: {message}"],
        "warnings": [],
    }


def _build_recovery_hint(job_dir: str, node_id: str) -> dict | None:
    """Return a recovery suggestion when a tool fails on an unresolved parent.

    When a workflow tool fails with ``input_resolution_blocked`` because a parent
    node is stuck
    ``running``/``failed``/``pending``, surface a structured ``create_node``
    suggestion for the blocking parent's stage so a weak agent re-creates the
    stuck ancestor instead of re-running the same blocked node. Best-effort;
    any error is swallowed (the hint is not part of the tool contract).
    """
    try:
        from mdclaw._node import input_resolution_recovery

        return input_resolution_recovery(job_dir, node_id)
    except Exception:
        return None


def _record_cli_node_failure(
    *,
    job_dir: str | None,
    node_id: str | None,
    tool_name: str,
    result: dict,
    exit_code: int,
    stdout_tail: str | None = None,
    stderr_tail: str | None = None,
    traceback_text: str | None = None,
) -> None:
    """Best-effort CLI-level failure evidence persistence for DAG nodes."""
    if not job_dir or not node_id:
        return
    try:
        from mdclaw._node import cli_argv, read_node, record_node_failure

        # A recoverable argument error detected before the tool starts is a
        # corrected invocation of the same pending node, not a failed attempt.
        if (
            result.get("error_type") == "ValidationError"
            and result.get("recoverable") is True
            and read_node(job_dir, node_id).get("status") == "pending"
        ):
            return

        record_node_failure(
            job_dir,
            node_id,
            result,
            tool=tool_name,
            argv=cli_argv(),
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            traceback_text=traceback_text,
        )
    except Exception:
        # Failure recording must never mask the tool failure that is already
        # being returned to the caller.
        return


def _json_stdout_tail(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str) + "\n"


def _json_error_and_exit(error: dict) -> None:
    json.dump(finalize_error(error), sys.stdout, indent=2, default=str)
    print()
    sys.exit(1)


def _node_type_preflight_error(
    *,
    tool_name: str,
    job_dir: str,
    node_id: str,
    expected_node_type: str,
) -> dict | None:
    """Return a structured error for a wrong-type or terminal workflow node."""
    actual_node_type = None
    actual_status = None
    try:
        from mdclaw._node import read_node

        node = read_node(job_dir, node_id)
        actual_node_type = node.get("node_type")
        actual_status = node.get("status")
    except FileNotFoundError:
        code = "node_missing"
        message = f"Node '{node_id}' does not exist under {job_dir}"
    except (AttributeError, OSError, ValueError) as exc:
        code = "node_json_invalid"
        message = f"Cannot read node '{node_id}': {exc}"
    else:
        if actual_node_type != expected_node_type:
            code = "node_type_mismatch"
            message = (
                f"Tool '{tool_name}' requires a '{expected_node_type}' node, but "
                f"'{node_id}' has type '{actual_node_type}'"
            )
        elif actual_status in {"completed", "failed"}:
            code = "node_terminal"
            message = (
                f"Node '{node_id}' is terminal (status={actual_status!r}); "
                "create a new node instead"
            )
        else:
            return None

    error = _cli_validation_error(
        "node_id",
        message,
        code=code,
        actual=actual_node_type,
        expected=expected_node_type,
    )
    error["context"].update({
        "tool": tool_name,
        "job_dir": job_dir,
        "node_id": node_id,
        "expected_node_type": expected_node_type,
        "actual_node_type": actual_node_type,
        "actual_status": actual_status,
    })
    return error


def _load_json_cli(value: str, field: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        _json_error_and_exit(
            _cli_validation_error(
                field,
                f"Invalid JSON: {e.msg}",
                code="invalid_json_input",
                actual=value,
                expected="Valid JSON object or array as required by the argument",
            )
        )


def _normalize_repeated_string_value(value, *, sep: str = ":"):
    """Join repeated string CLI values while preserving scalar strings."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("[", "(")) and stripped.endswith(("]", ")")):
            try:
                parsed = ast.literal_eval(stripped)
            except (SyntaxError, ValueError):
                return value
            if isinstance(parsed, (list, tuple)):
                return _normalize_repeated_string_value(parsed, sep=sep)
        return value
    if not isinstance(value, (list, tuple)):
        return value
    parts = [str(item).strip() for item in value if str(item).strip()]
    return sep.join(parts)


def _coerce_cli_param_value(tool_name: str, pname: str, value, hint):
    """Coerce one CLI value, preserving repeated-string parameters."""
    if _is_cli_repeated_string_param(tool_name, pname):
        value = _normalize_repeated_string_value(value)
    return _coerce_value(value, hint)


def _apply_cli_convenience_defaults(tool_name: str, kwargs: dict) -> None:
    """Apply narrow CLI-only defaults that reduce weak-agent retry loops."""
    if tool_name == "fetch_structure" and not kwargs.get("source"):
        source_fields = [
            ("pdb", "pdb_id"),
            ("alphafold", "uniprot_id"),
            ("local", "file_path"),
        ]
        matches = [
            (source, field)
            for source, field in source_fields
            if kwargs.get(field) not in {None, ""}
        ]
        if len(matches) == 1:
            kwargs["source"] = matches[0][0]

    if tool_name == "embed_in_membrane" and "lipids" in kwargs:
        kwargs["lipids"] = _normalize_repeated_string_value(kwargs["lipids"])


# ---------------------------------------------------------------------------
# --list output
# ---------------------------------------------------------------------------

def _type_label(hint) -> str:
    """Return a compact, stable label for a CLI parameter type."""
    inner, is_optional = _unwrap_optional(hint)
    if inner is inspect.Parameter.empty:
        label = "str"
    elif inner is bool:
        label = "bool"
    elif inner is int:
        label = "int"
    elif inner is float:
        label = "float"
    elif inner is str:
        label = "str"
    elif _is_path_type(inner):
        label = "Path"
    elif _is_list_of_str(inner):
        label = "list[str]"
    elif _is_dict_type(inner):
        label = "dict"
    elif _is_list_of_dict(inner):
        label = "list[dict]"
    elif _is_list_of_list(inner):
        label = "list[list]"
    else:
        label = getattr(inner, "__name__", str(inner))
    return f"Optional[{label}]" if is_optional else label


def _jsonable_default(value):
    """Normalize inspect defaults for JSON schema output."""
    if value is inspect.Parameter.empty:
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, dict)):
        return value
    return str(value)


def _tool_parameter_schemas(tool_name: str, fn) -> list[dict]:
    sig = inspect.signature(fn)
    requires_node_context = tool_requires_node(fn)
    hints = {}
    try:
        hints = {k: v for k, v in inspect.get_annotations(fn, eval_str=True).items()
                 if k != "return"}
    except Exception:
        pass

    params = []
    for pname, param in sig.parameters.items():
        if pname.startswith("_"):
            continue
        hint = hints.get(pname, param.annotation)
        if hint is inspect.Parameter.empty:
            hint = str
        inner, is_optional = _unwrap_optional(hint)
        required = (
            param.default is inspect.Parameter.empty and not is_optional
        ) or (
            requires_node_context and pname in {"job_dir", "node_id"}
        )
        cli_flag = f"--{pname.replace('_', '-')}"
        entry = {
            "name": pname,
            "cli_flag": cli_flag,
            "type": _type_label(hint),
            "required": required,
            "has_default": (
                not required and param.default is not inspect.Parameter.empty
            ),
            "default": None if required else _jsonable_default(param.default),
        }
        if inner is bool:
            entry["cli_action"] = "boolean_optional"
            entry["accepted_cli_forms"] = [
                cli_flag,
                f"--no-{pname.replace('_', '-')}",
                f"{cli_flag} true",
                f"{cli_flag} false",
            ]
        elif _is_cli_repeated_string_param(tool_name, pname):
            entry["nargs"] = "+"
            entry["join_repeated_values_with"] = ":"
        elif _is_list_of_str(inner):
            entry["nargs"] = "+"
        elif _takes_json(inner):
            entry["expects_json"] = True
        if tool_job_dir_is_data(fn) and pname == "job_dir":
            entry["job_dir_role"] = "data"
        params.append(entry)
    return params


def _tool_list_json(
    tools: dict[str, dict],
    requested_tool: str | None = None,
) -> dict:
    """Build a machine-readable projection of all tools or one exact tool."""
    if requested_tool is not None and requested_tool not in tools:
        return {
            "success": False,
            "error_type": "ValidationError",
            "code": "tool_not_available",
            "message": f"Unknown MDClaw tool '{requested_tool}'",
            "errors": [f"Tool '{requested_tool}' is not present in CLI discovery"],
            "warnings": [],
            "hints": [
                "Run 'mdclaw --list-json' to see exact available tool names.",
            ],
            "context": {"tool": requested_tool, "code": "tool_not_available"},
            "recoverable": True,
        }

    selected_tools = (
        {requested_tool: tools[requested_tool]}
        if requested_tool is not None
        else tools
    )
    payload = {
        "success": True,
        "version": __version__,
        "total": len(selected_tools),
        "tools": [],
    }
    for tool_name, info in sorted(selected_tools.items()):
        description = info["description"]
        summary = description.split("\n")[0].strip() if description else ""
        tool_payload = {
            "name": tool_name,
            "server": info["server"],
            "summary": summary,
            "is_async": info["is_async"],
            "requires_node": info.get("requires_node", tool_requires_node(info["fn"])),
            "node_type": info.get("node_type", tool_node_type(info["fn"])),
            "job_dir_is_data": info.get("job_dir_is_data", tool_job_dir_is_data(info["fn"])),
            "parameters": _tool_parameter_schemas(tool_name, info["fn"]),
        }
        if requested_tool is None:
            tool_payload["description"] = description
        payload["tools"].append(tool_payload)
    return payload


def _print_tool_list(tools: dict[str, dict]) -> None:
    """Print a compact tool-name index grouped by server."""
    by_server: dict[str, list[str]] = {}
    for tool_name, info in tools.items():
        by_server.setdefault(info["server"], []).append(tool_name)

    print("Tool index. Inspect one: mdclaw --list-json <tool>")
    for server_name in sorted(by_server):
        print(f"\n[{server_name}]")
        print("  " + "  ".join(sorted(by_server[server_name])))
    print(f"\nTotal: {len(tools)} tools")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Node-context and job-dir-is-data requirements are declared at each tool's
# definition site via the ``@node_tool`` / ``@job_dir_data_tool`` markers in
# ``mdclaw._tool_meta`` and read during ``_discover_tools`` (see the
# ``requires_node`` / ``job_dir_is_data`` fields). This removes the old
# hardcoded frozensets that had to be kept in sync by hand.
#
# ``_NODE_REQUIRED_TOOLS`` / ``_JOB_DIR_DATA_TOOLS`` remain available as derived,
# lazily-computed module attributes for backward compatibility (tests and any
# external callers that import them). They are computed from the declarative
# markers, so they can never desync from the tools themselves.


def _node_required_tools() -> frozenset[str]:
    return frozenset(
        name for name, info in _discover_tools().items()
        if info.get("requires_node")
    )


def _job_dir_data_tools() -> frozenset[str]:
    return frozenset(
        name for name, info in _discover_tools().items()
        if info.get("job_dir_is_data")
    )


def __getattr__(name: str):
    # PEP 562 module-level lazy attributes. Kept for import/monkeypatch
    # compatibility; the runtime gate below uses the per-tool discovery flags.
    if name == "_NODE_REQUIRED_TOOLS":
        return _node_required_tools()
    if name == "_JOB_DIR_DATA_TOOLS":
        return _job_dir_data_tools()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main(argv: list[str] | None = None) -> None:
    _configure_logging()

    tools = _discover_tools()

    # Intercept consolidated tool names before argparse so the agent gets a
    # structured ``tool_renamed`` code instead of an "invalid choice" error.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    subcommand = _detect_subcommand(raw_argv)
    if subcommand in _RENAMED_TOOLS:
        replacement = _RENAMED_TOOLS[subcommand]
        _json_error_and_exit({
            "success": False,
            "error_type": "ValidationError",
            "code": "tool_renamed",
            "message": (
                f"Tool '{subcommand}' was consolidated. Use: {replacement}."
            ),
            "errors": [f"{subcommand} was renamed/merged into {replacement}"],
            "warnings": [],
            "hints": [
                f"Run 'mdclaw {replacement.split()[0]} --help' for the new interface.",
                "See `mdclaw --list-json` for the current tool surface.",
            ],
            "context": {
                "tool": subcommand,
                "replacement": replacement,
                "code": "tool_renamed",
            },
            "recoverable": True,
        })

    parser = _build_parser(tools)
    args = parser.parse_args(argv)

    # --list
    if args.list_tools:
        _print_tool_list(tools)
        sys.exit(0)

    # --list-json
    if args.list_tools_json is not None:
        requested_tool = args.list_tools_json or None
        payload = _tool_list_json(tools, requested_tool)
        if requested_tool is None:
            json.dump(payload, sys.stdout, indent=2, default=str)
        else:
            if not payload.get("success"):
                payload = finalize_error(payload)
            json.dump(payload, sys.stdout, separators=(",", ":"), default=str)
        print()
        sys.exit(0 if payload.get("success") else 1)

    # No subcommand
    if not args.tool_name:
        parser.print_help()
        sys.exit(0)

    tool_name = args.tool_name
    info = tools[tool_name]
    fn = info["fn"]
    is_async = info["is_async"]
    requires_node = info.get("requires_node", tool_requires_node(fn))

    # Resolve node-mode flags (global --job-dir/--node-id or per-tool kwargs)
    _global_job_dir = getattr(args, "_global_job_dir", None)
    _global_node_id = getattr(args, "_global_node_id", None)

    # Build kwargs
    missing: list[str] = []
    if args.json_input:
        kwargs = _load_json_cli(args.json_input, "--json-input")
        sig = inspect.signature(fn)
        hints = {}
        try:
            hints = {k: v for k, v in inspect.get_annotations(fn, eval_str=True).items()
                     if k != "return"}
        except Exception:
            pass
        for pname, value in list(kwargs.items()):
            if pname not in sig.parameters or value is None:
                continue
            hint = hints.get(pname, sig.parameters[pname].annotation)
            kwargs[pname] = _coerce_cli_param_value(tool_name, pname, value, hint)
        _apply_cli_convenience_defaults(tool_name, kwargs)
    else:
        sig = inspect.signature(fn)
        hints = {}
        try:
            hints = {k: v for k, v in inspect.get_annotations(fn, eval_str=True).items()
                     if k != "return"}
        except Exception:
            pass

        kwargs = {}
        args_dict = vars(args)
        # Propagate global --job-dir/--node-id into the per-tool namespace so
        # that downstream missing-arg checks see them. The subparser declares
        # its own --job-dir/--node-id when the tool signature has those
        # parameters, but argparse does not mirror the global flags into the
        # subparser's namespace automatically.
        if _global_job_dir is not None and args_dict.get("job_dir") is None:
            args_dict["job_dir"] = _global_job_dir
        if _global_node_id is not None and args_dict.get("node_id") is None:
            args_dict["node_id"] = _global_node_id
        for pname, param in sig.parameters.items():
            hint = hints.get(pname, param.annotation)
            _, is_optional = _unwrap_optional(hint) if hint is not inspect.Parameter.empty else (hint, False)
            cli_key = pname  # argparse converts hyphens back to underscores
            value = args_dict.get(cli_key)
            if value is None and param.default is inspect.Parameter.empty and not is_optional:
                missing.append(f"--{pname.replace('_', '-')}")
                continue
            if value is None:
                continue
            if _takes_json(_unwrap_optional(hint)[0]) and isinstance(value, str):
                value = _load_json_cli(value, f"--{pname.replace('_', '-')}")
            value = _coerce_cli_param_value(tool_name, pname, value, hint)
            kwargs[pname] = value

        _apply_cli_convenience_defaults(tool_name, kwargs)
        if kwargs.get("source"):
            missing = [
                item for item in missing
                if not (tool_name == "fetch_structure" and item == "--source")
            ]

    # Resolve effective job_dir/node_id: global flags take precedence over
    # per-tool kwargs (which come from the subparser's --job-dir/--node-id).
    effective_job_dir = (
        None
        if info.get("job_dir_is_data", tool_job_dir_is_data(fn))
        else _global_job_dir or kwargs.get("job_dir")
    )
    effective_node_id = _global_node_id or kwargs.get("node_id")
    # Use one canonical path for preflight, failure recording, and execution.
    if effective_job_dir:
        effective_job_dir = str(Path(effective_job_dir).resolve())

    expected_node_type = info.get("node_type", tool_node_type(fn))
    if expected_node_type and effective_job_dir and effective_node_id:
        preflight_error = _node_type_preflight_error(
            tool_name=tool_name,
            job_dir=effective_job_dir,
            node_id=effective_node_id,
            expected_node_type=expected_node_type,
        )
        if preflight_error:
            _json_error_and_exit(preflight_error)

    if missing:
        error = {
            "success": False,
            "error_type": "ValidationError",
            "code": "missing_required_arguments",
            "message": (
                f"{tool_name} is missing required arguments: "
                f"{', '.join(missing)}"
            ),
            "errors": [f"missing required argument: {m}" for m in missing],
            "warnings": [],
            "hints": [
                f"Run 'mdclaw --list-json {tool_name}' to see the exact "
                "required parameters and defaults.",
            ],
            "context": {"tool": tool_name, "missing": missing,
                        "code": "missing_required_arguments"},
            "recoverable": True,
        }
        if requires_node:
            _record_cli_node_failure(
                job_dir=effective_job_dir,
                node_id=effective_node_id,
                tool_name=tool_name,
                result=error,
                exit_code=1,
                stdout_tail=_json_stdout_tail(error),
            )
        _json_error_and_exit(error)

    if effective_node_id and not effective_job_dir:
        _json_error_and_exit({
            "success": False,
            "error_type": "ValidationError",
            "code": "node_id_requires_job_dir",
            "message": "--node-id requires --job-dir",
            "errors": ["--node-id was provided without --job-dir"],
            "warnings": [],
            "hints": ["Pass both --job-dir and --node-id together."],
            "context": {"tool": tool_name, "code": "node_id_requires_job_dir"},
            "recoverable": True,
        })
    if requires_node and (not effective_job_dir or not effective_node_id):
        _json_error_and_exit({
            "success": False,
            "error_type": "ValidationError",
            "code": "node_context_required",
            "message": (
                f"{tool_name} requires both --job-dir and --node-id in "
                "schema v3 mode"
            ),
            "errors": [
                f"{tool_name} is a workflow tool and must run with node context"
            ],
            "warnings": [],
            "hints": [
                "Create the node first: mdclaw create_node --job-dir <job_dir> "
                "--node-type <type> [--parent-node-ids ...]",
                "Then run the tool: mdclaw --job-dir <job_dir> --node-id "
                f"<node_id> {tool_name} ...",
                "Use 'mdclaw inspect_job --job-dir <job_dir>' to inspect the "
                "DAG, then 'mdclaw explain_node --job-dir <job_dir> "
                "--node-id <node_id>' before running an existing node.",
            ],
            "context": {
                "tool": tool_name,
                "job_dir": effective_job_dir,
                "node_id": effective_node_id,
                "code": "node_context_required",
            },
            "recoverable": True,
        })

    # Inject global schema-v3 context when the tool accepts it.
    sig = inspect.signature(fn)
    if effective_job_dir and "job_dir" in sig.parameters:
        kwargs["job_dir"] = effective_job_dir
    if effective_node_id and "node_id" in sig.parameters:
        kwargs["node_id"] = effective_node_id

    # Execute
    started_at = time.monotonic()
    tool_stdout_tail = ""
    tool_stderr_tail = ""
    try:
        stdout_capture = _TailCaptureStream(sys.stdout, tee=False)
        stderr_capture = _TailCaptureStream(sys.stderr)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        logging_swaps: list[tuple[logging.StreamHandler, TextIO]] = []
        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture
            logging_swaps = _swap_logging_stream(old_stderr, stderr_capture)
            result = _run_tool(fn, is_async, kwargs)
        finally:
            tool_stdout_tail = stdout_capture.get_tail()
            tool_stderr_tail = stderr_capture.get_tail()
            _restore_logging_stream(logging_swaps)
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        # Determine exit code
        exit_code = 0
        if isinstance(result, dict) and result.get("success") is False:
            exit_code = 1
            # Normalize every failure to the single error contract so weak
            # agents always see a stable code, a next_action, and a non-empty
            # hint list, regardless of which tool produced the failure.
            result = finalize_error(
                result,
                job_dir=effective_job_dir,
                node_id=effective_node_id,
            )
        # Failure counterpart: when a workflow tool is blocked by a non-completed
        # parent, tell the agent to create a new node of the blocking parent's
        # stage rather than re-running this same blocked node.
        if (
            isinstance(result, dict)
            and result.get("code") == "input_resolution_blocked"
            and effective_job_dir
            and effective_node_id
            and "recovery_hint" not in result
        ):
            recovery = _build_recovery_hint(effective_job_dir, effective_node_id)
            if recovery:
                result["recovery_hint"] = recovery
        if isinstance(result, dict) and exit_code and requires_node:
            _record_cli_node_failure(
                job_dir=effective_job_dir,
                node_id=effective_node_id,
                tool_name=tool_name,
                result=result,
                exit_code=exit_code,
                stdout_tail=(
                    f"{tool_stdout_tail}\n--- mdclaw final JSON ---\n"
                    f"{_json_stdout_tail(result)}"
                    if tool_stdout_tail
                    else _json_stdout_tail(result)
                ),
                stderr_tail=tool_stderr_tail or None,
            )
        if not exit_code and requires_node:
            result = _attach_dag_handoff(
                result,
                effective_job_dir,
                effective_node_id,
            )
        _write_benchmark_harness_record(
            tool_name=tool_name,
            kwargs=kwargs,
            exit_code=exit_code,
            started_at=started_at,
        )
        json.dump(result, sys.stdout, indent=2, default=str)
        print()  # trailing newline
        sys.exit(exit_code)
    except Exception as e:
        _write_benchmark_harness_record(
            tool_name=tool_name,
            kwargs=kwargs,
            exit_code=1,
            started_at=started_at,
        )
        error_out = finalize_error(
            {
                "message": f"{tool_name} raised {type(e).__name__}: {e}",
                "error_type": type(e).__name__,
                "code": "unhandled_exception",
                "errors": [str(e)],
            },
            job_dir=effective_job_dir,
            node_id=effective_node_id,
        )
        stdout_tail = (
            f"{tool_stdout_tail}\n--- mdclaw final JSON ---\n"
            f"{_json_stdout_tail(error_out)}"
            if tool_stdout_tail
            else _json_stdout_tail(error_out)
        )
        if requires_node:
            _record_cli_node_failure(
                job_dir=effective_job_dir,
                node_id=effective_node_id,
                tool_name=tool_name,
                result=error_out,
                exit_code=1,
                stdout_tail=stdout_tail,
                stderr_tail=tool_stderr_tail or None,
                traceback_text=traceback.format_exc(),
            )
        json.dump(error_out, sys.stdout, indent=2, default=str)
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
