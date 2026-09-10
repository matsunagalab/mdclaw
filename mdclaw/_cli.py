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
import asyncio
import difflib
import inspect
import json
import logging
import os
import sys
import threading
import types
import time
import traceback
from pathlib import Path
from typing import NamedTuple, TextIO, Union, get_args, get_origin

from mdclaw import __version__
from mdclaw._benchmark_log import _write_benchmark_harness_record
from mdclaw.confirmation_report import report_confirmation_items
from mdclaw._common import create_validation_error, finalize_error
from mdclaw._envelope import (
    OUTPUT_MODES,
    _load_nodes,
    blocking_ancestor,
    brief_result,
    dag_context,
    helper_stage_hint,
    order_envelope,
    stage_tools_for,
    write_result_file,
)
from mdclaw._registry import SERVER_REGISTRY
from mdclaw.node.snapshot import describe_nodes, node_missing_error, nodes_of_type
from mdclaw.node.constants import CANONICAL_FORWARD_NODE_TYPE, DAG_GUIDANCE, NODE_TYPE_ORDER
from mdclaw._tool_meta import (
    tool_job_dir_is_data,
    tool_node_type,
    tool_parameter_example_map,
    tool_requires_node,
)

# Consolidated tools whose work still exists under another name. Invoking an
# old name returns a structured ``tool_renamed`` error naming the replacement,
# so an agent gets the migration target rather than the generic
# ``tool_not_available`` reply every other unknown name receives. Each value
# must reproduce the OLD tool's behavior, including defaults the replacement
# does not share. Names whose functionality was removed outright belong in
# neither table — they are simply unknown tools.
_RENAMED_TOOLS = {
    "generate_md_evidence_report": "generate_md_report --job-dir <job_dir>",
    "generate_study_evidence_report": "generate_md_report --study-dir <study_dir>",
    "record_study_decision": "record_study_log --record-type decision",
    "record_study_question": "record_study_log --record-type question",
    "record_token_usage": "record_study_log --record-type token_usage",
    "add_node_need": "manage_node_need --action add",
    "clear_node_need": "manage_node_need --action clear",
    "record_node_need_attempt": "manage_node_need --action record_attempt",
    "update_node_status": "update_workflow_state (--node-id/--status)",
    "update_job_params": "update_workflow_state (--params)",
    "download_structure": "fetch_structure --source pdb --pdb-id <ID>",
    "get_alphafold_structure": "fetch_structure --source alphafold --uniprot-id <ID> --format pdb",
    "setup_surrogate_backend": "setup_model_backend --model bioemu",
    "check_surrogate_backend": "check_model_backend --model bioemu",
    "explain_failure": "trace_failure",
}

# Global options that consume a following value, used to locate the subcommand
# token when scanning argv for a renamed tool.
_GLOBAL_VALUE_OPTIONS = {"--job-dir", "--node-id", "--list-json", "--output", "--log-file",
                         "--heartbeat-seconds"}

# Set once per invocation from the global flags; read by the emit helpers.
_OUTPUT_MODE = "brief"
_TOOLS: dict[str, dict] = {}
_HEARTBEAT_DEFAULT_SECONDS = 30.0


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

class _LogTailHandler(logging.Handler):
    """Keep the tail of every log record for failure artifacts.

    stderr shows warnings and above by default (agents merge stderr into the
    JSON stream in half of their invocations, and a clean success must parse),
    but a failure manifest still wants the INFO trail, so it is kept here.
    """

    def __init__(self, limit: int = 65536):
        super().__init__(level=logging.DEBUG)
        self._limit = limit
        self._tail = ""

    def emit(self, record):
        try:
            text = self.format(record) + "\n"
        except Exception:  # noqa: BLE001 - logging must never break the CLI
            return
        self._tail = (self._tail + text)[-self._limit:]

    def get_tail(self) -> str:
        return self._tail

    def reset(self) -> None:
        self._tail = ""


_LOG_TAIL = _LogTailHandler()


def _configure_logging(log_file: str | None = None):
    """stderr gets warnings and above unless MDCLAW_LOG_LEVEL says otherwise.

    ``--log-file`` / ``MDCLAW_LOG_FILE`` receives every record at INFO and
    above, so a quiet stderr costs no evidence.
    """
    root = logging.getLogger()
    root.handlers.clear()
    formatter = logging.Formatter("%(name)s - %(levelname)s - %(message)s")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    env_level = os.getenv("MDCLAW_LOG_LEVEL", "").upper()
    handler.setLevel(getattr(logging, env_level, logging.INFO) if env_level else logging.WARNING)
    root.addHandler(handler)
    _LOG_TAIL.setFormatter(formatter)
    _LOG_TAIL.reset()
    root.addHandler(_LOG_TAIL)
    log_file = log_file or os.getenv("MDCLAW_LOG_FILE")
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
        except OSError as exc:
            root.warning("cannot open log file %s: %s", log_file, exc)
        else:
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.INFO)
            root.addHandler(file_handler)


class _Heartbeat:
    """Say on stderr that a long tool is still running.

    Agents that saw no output for a minute backgrounded stage tools and
    polled them with ``sleep`` (338 polls in 33 attempts of the 2026-09-10
    campaign). One line every ``interval`` seconds, after a ``delay``, keeps
    the command in the foreground.
    """

    def __init__(self, tool_name: str, stream: TextIO, interval: float, delay: float = 20.0):
        self._tool_name = tool_name
        self._stream = stream
        self._interval = max(float(interval), 1.0)
        self._delay = max(float(delay), 0.0)
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="mdclaw-heartbeat", daemon=True)

    def _run(self) -> None:
        if self._stop.wait(self._delay):
            return
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._started
            try:
                self._stream.write(f"[mdclaw] {self._tool_name} still running after {elapsed:.0f}s\n")
                self._stream.flush()
            except Exception:  # noqa: BLE001 - never let progress output break the tool
                return
            if self._stop.wait(self._interval):
                return

    def start(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)


def _result_file_path(job_dir: str | None, node_id: str | None) -> str | None:
    if not job_dir or not node_id:
        return None
    node_dir = Path(job_dir) / "nodes" / node_id
    return str(node_dir / "result.json") if node_dir.is_dir() else None


def _emit_result(
    result,
    *,
    exit_code: int,
    job_dir: str | None = None,
    node_id: str | None = None,
    requires_node: bool = False,
    stderr_tail: str = "",
    stderr_stream: TextIO | None = None,
) -> None:
    """Print the agent-facing envelope and exit.

    Every exit path of the CLI comes through here so the first keys, the
    ``dag``/``next`` blocks, the result file and the output mode are the same
    for successes, refusals and crashes.
    """
    payload = result
    if isinstance(result, dict):
        payload = dict(result)
        context = dag_context(job_dir, node_id, _TOOLS) if job_dir else {}
        for key, value in context.items():
            payload.setdefault(key, value)
        result_file = _result_file_path(job_dir, node_id) if requires_node else None
        payload = order_envelope(
            payload,
            node_id=node_id,
            node_status=context.get("node_status"),
            result_file=result_file,
            include_node_keys=requires_node,
        )
        if result_file:
            write_result_file(payload, job_dir, node_id)
        if _OUTPUT_MODE == "brief":
            payload = brief_result(payload, result_file=result_file)
    stream = stderr_stream or sys.stderr
    if stderr_tail.strip():
        # Agents often merge stderr into the JSON stream; give a parser that
        # sees both a line to split on.
        try:
            stream.write("--- mdclaw result follows on stdout ---\n")
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    try:
        if _OUTPUT_MODE == "id" and isinstance(payload, dict):
            print(payload.get("node_id") or payload.get("job_dir") or "")
        else:
            json.dump(payload, sys.stdout, indent=2, default=str)
            print()
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader went away (``| head``). The node's result.json is already
        # written; keep the interpreter from failing again at exit.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        exit_code = exit_code or 0
    sys.exit(exit_code)


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
    """Point root stream handlers at ``new_stream`` while capturing output.

    mdclaw loggers propagate to the root logger (see ``_common.setup_logger``),
    so swapping the root handlers is sufficient to capture tool log output.
    """
    swaps: list[tuple[logging.StreamHandler, TextIO]] = []
    for handler in logging.getLogger().handlers:
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
    return get_origin(hint) is list and get_args(hint) == (str,)


def _is_bare_list(hint) -> bool:
    """True for ``list`` / ``typing.List`` with no element type."""
    if hint is list:
        return True
    return get_origin(hint) is list and not get_args(hint)


def _is_dict_type(hint) -> bool:
    """Check if hint is dict or Dict[...]."""
    return get_origin(hint) is dict or hint is dict


def _is_list_of(hint, element) -> bool:
    """True when hint is a list whose element type is *element* (dict/list)."""
    if get_origin(hint) is not list:
        return False
    args = get_args(hint)
    return bool(args) and (get_origin(args[0]) is element or args[0] is element)


def _takes_json(hint) -> bool:
    """True when the argument expects a JSON string at the CLI boundary.

    Covers ``dict``, ``list[dict]``, and ``list[list[...]]`` (including under
    ``Optional[...]``) — none of these are expressible as a flat CLI list.
    ``list[str]`` stays on the plain ``nargs='+'`` path.
    """
    hint, _ = _unwrap_optional(hint)
    return _is_dict_type(hint) or _is_list_of(hint, dict) or _is_list_of(hint, list)


def _is_path_type(hint) -> bool:
    """True for pathlib.Path CLI parameters, including Optional[Path]."""
    inner, _ = _unwrap_optional(hint)
    return inner is Path


class _ParamSpec(NamedTuple):
    """One CLI-visible tool parameter from a single introspection pass.

    The argparse builder, the ``--list-json`` schema, and kwargs assembly in
    ``main()`` all consume this same view, so they cannot drift apart.
    """

    name: str
    cli_flag: str
    hint: object
    inner: object
    optional: bool
    required: bool
    default: object


def _tool_param_specs(fn, *, requires_node: bool = False) -> list[_ParamSpec]:
    """Compute the CLI parameter specs for a tool function."""
    sig = inspect.signature(fn)
    try:
        hints = {k: v for k, v in inspect.get_annotations(fn, eval_str=True).items()
                 if k != "return"}
    except Exception:
        hints = {}
    specs = []
    for pname, param in sig.parameters.items():
        # Underscore-prefixed kwargs are internal (used by Python callers for
        # dispatch plumbing). They never become CLI flags.
        if pname.startswith("_"):
            continue
        hint = hints.get(pname, param.annotation)
        if hint is inspect.Parameter.empty:
            hint = str  # fallback
        inner, is_optional = _unwrap_optional(hint)
        if _is_bare_list(inner):
            # A bare ``list`` is neither ``list[str]`` (nargs) nor a JSON
            # parameter, so argparse handed the raw string through and the
            # tool unpacked its characters. Refuse the contract instead of
            # guessing (modeller_from_alignment --disulfide-patches, 2026-09-10).
            raise TypeError(
                f"{getattr(fn, '__name__', fn)}.{pname}: bare list annotation; "
                "use list[str], list[dict], list[list[...]] or dict"
            )
        required = (
            param.default is inspect.Parameter.empty and not is_optional
        ) or (
            requires_node and pname in {"job_dir", "node_id"}
        )
        specs.append(_ParamSpec(
            name=pname,
            cli_flag="--" + pname.replace("_", "-"),
            hint=hint,
            inner=inner,
            optional=is_optional,
            required=required,
            default=param.default,
        ))
    return specs


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
        description=(
            "MDClaw CLI: MD workflow stages as one job DAG. Each stage is "
            "create_node -> (explain_node) -> stage tool with --job-dir/--node-id; "
            "inputs resolve from the parent node. Results are JSON on stdout."
        ),
        epilog=(
            "Stages and their tools: mdclaw --workflow | all tools: mdclaw --list | "
            "one tool's parameters: mdclaw --list-json <tool> | mdclaw <tool> --help"
        ),
    )
    parser.add_argument("--version", action="version", version=f"mdclaw {__version__}")
    parser.add_argument(
        "--list", action="store_true", dest="list_tools",
        help="List all tools: stage tools by DAG stage, then the rest by server.",
    )
    parser.add_argument(
        "--workflow", action="store_true", dest="show_workflow",
        help="Describe the job DAG: stage order, stage tools, the per-stage commands and rules.",
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
        "--job-dir", type=str, default=None, dest="_global_job_dir", metavar="JOB_DIR",
        help="Job directory for node-based state tracking (schema v3).",
    )
    parser.add_argument(
        "--node-id", type=str, default=None, dest="_global_node_id", metavar="NODE_ID",
        help="Node ID for node-based state tracking (requires --job-dir).",
    )
    parser.add_argument(
        "--output", choices=list(OUTPUT_MODES), default=os.getenv("MDCLAW_OUTPUT", "brief"),
        dest="_output_mode",
        help=(
            "brief (default): envelope first, large values stubbed and kept in the node's "
            "result.json; full: everything inline; id: only the node id (create_node)."
        ),
    )
    parser.add_argument(
        "--log-file", type=str, default=None, dest="_log_file", metavar="PATH",
        help="Write INFO logs to this file; stderr then carries warnings only (MDCLAW_LOG_FILE).",
    )
    parser.add_argument(
        "--heartbeat-seconds", type=float, default=None, dest="_heartbeat_seconds", metavar="SECONDS",
        help="Progress line interval on stderr while a tool runs; 0 disables (MDCLAW_HEARTBEAT_SECONDS).",
    )

    # ``metavar`` and no per-parser ``help`` keep ``mdclaw --help`` to one
    # screen; the 80-tool index is ``--list`` and one tool's help is
    # ``mdclaw <tool> --help`` (both unchanged).
    subparsers = parser.add_subparsers(dest="tool_name", metavar="<tool>")

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
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub.add_argument(
            "--json-input", type=str, default=None,
            help="Pass all parameters as a JSON string.",
        )


        for spec in _tool_param_specs(fn, requires_node=bool(info.get("requires_node"))):
            default = spec.default if spec.default is not inspect.Parameter.empty else None
            if spec.inner is bool:
                default_val = spec.default if spec.default is not inspect.Parameter.empty else False
                sub.add_argument(
                    spec.cli_flag,
                    nargs="?",
                    const=True,
                    type=_parse_cli_bool,
                    default=default_val,
                    help=f"(bool: true/false, default: {default_val})",
                )
                sub.add_argument(
                    f"--no-{spec.name.replace('_', '-')}",
                    dest=spec.name,
                    action="store_false",
                    default=argparse.SUPPRESS,
                    help=f"(set {spec.name}=false)",
                )
            elif _is_list_of_str(spec.inner):
                sub.add_argument(
                    spec.cli_flag,
                    nargs="+",
                    action="extend",
                    default=default,
                    help="(list of str, required)" if spec.required else "(list of str)",
                )
            elif _takes_json(spec.inner):
                example = (
                    '\'{"key":"val"}\''
                    if _is_dict_type(spec.inner)
                    else '\'[{"key":"val"}, ...]\''
                )
                sub.add_argument(
                    spec.cli_flag,
                    type=str,
                    default=default,
                    help=(
                        f"(JSON string, e.g. {example}, required)" if spec.required
                        else f"(JSON string, e.g. {example})"
                    ),
                )
            else:
                if spec.inner is int:
                    arg_type, label = int, "int"
                elif spec.inner is float:
                    arg_type, label = float, "float"
                elif _is_path_type(spec.inner):
                    arg_type, label = Path, "Path"
                else:
                    arg_type, label = str, "str"
                sub.add_argument(
                    spec.cli_flag,
                    type=arg_type,
                    default=default,
                    help=(
                        f"({label}, default: required)" if spec.required
                        else f"({label}, default: {default})"
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


def _fail_node_if_running(job_dir: str | None, node_id: str | None, errors: list[str]) -> bool:
    """Seal a node the tool began but never finished.

    A tool that calls ``begin_node`` and then raises leaves the node ``running``
    with no ``tool_failed`` event: the next attempt starts on a node that looks
    busy, and ``inspect_job`` reports a stage in progress that nothing is
    running. This is the CLI's last line: whatever the tool, a node that is
    still ``running`` when its process is about to exit with an error is
    failed here. Returns True when a status change was made.
    """
    if not job_dir or not node_id:
        return False
    try:
        from mdclaw._node import fail_node, read_node

        if read_node(job_dir, node_id).get("status") != "running":
            return False
        fail_node(job_dir, node_id, errors=list(errors))
        return True
    except Exception:  # noqa: BLE001 - never mask the failure being reported
        return False


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
            # This is the run that just failed the node, not a later observer:
            # the tool sealed the node without the tool name, argv or exit code,
            # so this record has to own `latest` for trace_failure to see them.
            same_invocation=True,
        )
    except Exception:
        # Failure recording must never mask the tool failure that is already
        # being returned to the caller.
        return


def _json_stdout_tail(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str) + "\n"


def _report_confirmation_items_safely(result: object) -> None:
    """Keep optional human rendering from invalidating a completed tool result."""
    if not isinstance(result, dict) or not result.get("confirmation_needed"):
        return
    try:
        report_confirmation_items(result["confirmation_needed"])
    except Exception as exc:  # noqa: BLE001 - final JSON remains authoritative
        logging.getLogger(__name__).warning(
            "Could not render confirmation_needed summary; the structured result "
            "remains available: %s: %s",
            type(exc).__name__,
            exc,
        )


def _json_error_and_exit(
    error: dict,
    *,
    job_dir: str | None = None,
    node_id: str | None = None,
    requires_node: bool = False,
) -> None:
    _emit_result(finalize_error(error), exit_code=1, job_dir=job_dir, node_id=node_id,
                 requires_node=requires_node)


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
    node = None
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
            nodes, _params = _load_nodes(job_dir)
            blocker = blocking_ancestor(node, nodes)
            if blocker is None:
                return None
            # Refuse here, before the tool starts: a stage tool that resolves
            # its inputs itself seals the node as failed on a pending parent,
            # which spends a node the agent only ran too early (chains of
            # pending nodes are legitimate since parent auto-resolution
            # accepts open parents).
            code = "parent_not_completed"
            message = (
                f"Parent '{blocker[0]}' of '{node_id}' is {blocker[1]}; '{node_id}' "
                "cannot run yet and stays pending (not spent)"
            )

    error = create_validation_error(
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
    hints, next_action = _preflight_fix(
        code, tool_name=tool_name, job_dir=job_dir, node_id=node_id,
        expected_node_type=expected_node_type, actual_status=actual_status,
        node=node if code in {"node_terminal", "parent_not_completed"} else None,
    )
    if hints:
        error["hints"] = hints
    if next_action:
        error["next_action"] = next_action
    return error


def _preflight_fix(code, *, tool_name, job_dir, node_id, expected_node_type,
                   actual_status, node=None):
    """Hints and the next command for a preflight refusal.

    The refusal used to name the invariant only; agents then re-ran the same
    command or guessed ids (18 ``node_terminal`` and 12 ``node_missing``-class
    errors on 2026-09-10). Name the node to use or the command that creates it.
    """
    nodes, params = _load_nodes(job_dir)
    if code == "parent_not_completed":
        blocker_id, blocker_status = blocking_ancestor(node or {}, nodes) or (None, None)
        blocker_type = (nodes.get(blocker_id) or {}).get("type")
        rerun = f"mdclaw --job-dir {job_dir} --node-id {node_id} {tool_name} ..."
        hints = ["Parents run first; the CLI does not run them for you. This node is not "
                 "spent: rerun the same command once the parent is completed, or submit "
                 "the chain with submit_job --dependency afterok:<parent job>."]
        if blocker_status == "failed":
            trace = f"mdclaw trace_failure --job-dir {job_dir} --node-id {blocker_id}"
            hints.append(f"'{blocker_id}' failed; re-running '{node_id}' cannot succeed.")
            return hints, (f"{trace}, then create a NEW {blocker_type} node and a NEW "
                           f"{expected_node_type} node from it")
        if blocker_status in {"running", "queued"}:
            return hints, (f"mdclaw wait_node --job-dir {job_dir} --node-id {blocker_id}; "
                           f"then {rerun}")
        tools = stage_tools_for(blocker_type, _TOOLS, params)
        tool = tools[0] if tools else f"<{blocker_type} stage tool>"
        return hints, (f"mdclaw --job-dir {job_dir} --node-id {blocker_id} {tool} ...; "
                       f"then {rerun}")
    if code == "node_missing":
        missing = node_missing_error(job_dir, node_id, expected_type=expected_node_type)
        return missing["hints"], missing["next_action"]
    if code == "node_type_mismatch":
        same = nodes_of_type(nodes, expected_node_type)
        open_same = [nid for nid in same if nodes[nid].get("status") in ("pending", "queued", "running")]
        hints = [
            f"{tool_name} runs on {expected_node_type} nodes; "
            + (f"existing: {describe_nodes(nodes, same)}" if same
               else f"this job has no {expected_node_type} node yet")
        ]
        if len(open_same) == 1:
            return hints, f"mdclaw --job-dir {job_dir} --node-id {open_same[0]} {tool_name} ..."
        actual_type = (nodes.get(node_id) or {}).get("type")
        if (CANONICAL_FORWARD_NODE_TYPE.get(actual_type) == expected_node_type
                and actual_status == "completed"):
            return hints, (f"mdclaw create_node --job-dir {job_dir} --node-type {expected_node_type} "
                           f"--parent-node-ids {node_id}, then run {tool_name} on the returned node_id")
        return hints, (f"mdclaw create_node --job-dir {job_dir} --node-type {expected_node_type} "
                       f"(parent auto-resolved), then run {tool_name} on the returned node_id")
    if code == "node_terminal":
        parents = list((node or {}).get("parent_node_ids") or [])
        branch = f"mdclaw create_node --job-dir {job_dir} --node-type {expected_node_type}"
        if parents:
            branch += f" --parent-node-ids {' '.join(parents)}"
        hints = ["Nodes run once; a completed or failed node is sealed. Put corrected "
                 "arguments on a new node with the same parents (a branch)."]
        if actual_status == "failed":
            trace = f"mdclaw trace_failure --job-dir {job_dir} --node-id {node_id}"
            hints.append(f"Why it failed: {trace}")
            return hints, f"{trace}, then branch: {branch}"
        forward = CANONICAL_FORWARD_NODE_TYPE.get(expected_node_type)
        if forward:
            return hints, (f"This stage is done; continue: mdclaw create_node --job-dir {job_dir} "
                           f"--node-type {forward} --parent-node-ids {node_id}")
        return hints, f"Branch a variant: {branch}"
    return [], None


def _unknown_parameter_error(tool_name, unknown, spec_by_name, *, requires_node,
                             job_dir=None, node_id=None) -> dict:
    """``unknown_parameter`` instead of the TypeError the tool would raise.

    Seen 21 times on 2026-09-10 as ``unhandled_exception`` ("got an unexpected
    keyword argument 'job_dir'"), mostly helpers called with node context.
    """
    import difflib

    accepted = sorted(spec_by_name)
    hints = []
    for name in unknown:
        close = difflib.get_close_matches(name, accepted, n=2, cutoff=0.6)
        if close:
            hints.append(f"'{name}': did you mean {' or '.join(repr(c) for c in close)}?")
    if {"job_dir", "node_id"} & set(unknown) and not requires_node:
        hints.append(helper_stage_hint(tool_name, job_dir, node_id) or (
            f"{tool_name} takes no node context (job_dir/node_id); it is a standalone helper."))
    if len(accepted) <= 30:
        hints.append(f"Accepted parameters: {', '.join(accepted)}")
    else:
        hints.append(f"Accepted parameters: mdclaw --list-json {tool_name}")
    message = f"{tool_name} does not accept: {', '.join(unknown)}"
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "unknown_parameter",
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": hints,
        "context": {"tool": tool_name, "unknown_parameters": unknown,
                    "accepted_parameters": accepted, "code": "unknown_parameter"},
        "recoverable": True,
    }


def _node_context_not_applicable_error(tool_name, *, job_dir=None, node_id=None) -> dict:
    """A helper was given ``--job-dir/--node-id``; it would silently ignore them."""
    stage_hint = helper_stage_hint(tool_name, job_dir, node_id)
    message = (
        f"{tool_name} is a standalone helper: --job-dir/--node-id do not apply and "
        "would be ignored (it reads and writes no node state)."
    )
    hints = [stage_hint] if stage_hint else [
        f"Run {tool_name} without --job-dir/--node-id; only stage tools "
        "(mdclaw --workflow) record node state."
    ]
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "node_context_not_applicable",
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": hints,
        "next_action": (stage_hint.split(": ", 2)[-1] if stage_hint
                        else f"mdclaw {tool_name} ... (without --job-dir/--node-id)"),
        "context": {"tool": tool_name, "job_dir": job_dir, "node_id": node_id,
                    "code": "node_context_not_applicable"},
        "recoverable": True,
    }


def _explicit_parameter_names(raw_argv: list[str], specs, json_keys=()) -> set[str]:
    """Parameters the caller actually passed (flags on the command line or JSON keys)."""
    names = set(json_keys or ())
    tokens = list(raw_argv)
    for spec in specs:
        negative = f"--no-{spec.name.replace('_', '-')}"
        if any(token == spec.cli_flag or token.startswith(spec.cli_flag + "=") or token == negative
               for token in tokens):
            names.add(spec.name)
    return names


def _attach_receipt(result, *, tool_name: str, node_type, explicit: dict, node_mode: bool):
    """Add the ``applied`` receipt and use its summary as the success message."""
    if not isinstance(result, dict) or result.get("success") is False:
        return result
    try:
        from mdclaw._receipt import build_receipt

        receipt = build_receipt(tool_name=tool_name, node_type=node_type, result=result,
                                explicit=explicit, node_mode=node_mode)
    except Exception as exc:  # noqa: BLE001 - a receipt must never break a completed run
        logging.getLogger(__name__).warning("Could not build the applied receipt: %s: %s",
                                            type(exc).__name__, exc)
        return result
    result["applied"] = receipt
    if not result.get("message"):
        handoff = result.get("dag_handoff") if isinstance(result.get("dag_handoff"), dict) else {}
        prefix = " ".join(str(part) for part in (handoff.get("node_id"), handoff.get("status")) if part)
        result["message"] = f"{prefix}: {receipt['summary']}" if prefix else receipt["summary"]
    return result


def _load_json_cli(value: str, field: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        _json_error_and_exit(
            create_validation_error(
                field,
                f"Invalid JSON: {e.msg}",
                code="invalid_json_input",
                actual=value,
                expected="Valid JSON object or array as required by the argument",
            )
        )


# ---------------------------------------------------------------------------
# --list output
# ---------------------------------------------------------------------------

def _type_label(hint) -> str:
    """Return a compact, stable label for a CLI parameter type."""
    inner, is_optional = _unwrap_optional(hint)
    if inner is inspect.Parameter.empty:
        label = "str"
    elif inner in (bool, int, float, str):
        label = inner.__name__
    elif _is_path_type(inner):
        label = "Path"
    elif _is_list_of_str(inner):
        label = "list[str]"
    elif _is_dict_type(inner):
        label = "dict"
    elif _is_list_of(inner, dict):
        label = "list[dict]"
    elif _is_list_of(inner, list):
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
    parameter_examples = tool_parameter_example_map(fn)
    job_dir_is_data = tool_job_dir_is_data(fn)
    params = []
    for spec in _tool_param_specs(fn, requires_node=tool_requires_node(fn)):
        entry = {
            "name": spec.name,
            "cli_flag": spec.cli_flag,
            "type": _type_label(spec.hint),
            "required": spec.required,
            "has_default": (
                not spec.required and spec.default is not inspect.Parameter.empty
            ),
            "default": None if spec.required else _jsonable_default(spec.default),
        }
        if spec.inner is bool:
            entry["cli_action"] = "boolean_optional"
            entry["accepted_cli_forms"] = [
                spec.cli_flag,
                f"--no-{spec.name.replace('_', '-')}",
                f"{spec.cli_flag} true",
                f"{spec.cli_flag} false",
            ]
        elif _is_list_of_str(spec.inner):
            entry["nargs"] = "+"
        elif _takes_json(spec.inner):
            entry["expects_json"] = True
            if spec.name in parameter_examples:
                entry["json_examples"] = parameter_examples[spec.name]
        if job_dir_is_data and spec.name == "job_dir":
            entry["job_dir_role"] = "data"
        params.append(entry)
    return params


def _missing_tool_error(tool_name: str, tools: dict[str, dict]) -> dict:
    """Explain a name the CLI cannot run, however the agent asked for it.

    A consolidated name yields the replacement command; anything else yields
    the generic 'no such tool' reply. Running the name and introspecting it
    with ``--list-json`` therefore give the same answer — an agent that checks
    before calling must not be told less than one that just calls.
    """
    replacement = _RENAMED_TOOLS.get(tool_name)
    if replacement is None:
        return _unknown_tool_error(tool_name, tools)
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "tool_renamed",
        "message": f"Tool '{tool_name}' was consolidated. Use: {replacement}.",
        "errors": [f"{tool_name} was renamed/merged into {replacement}"],
        "warnings": [],
        "hints": [
            f"Run 'mdclaw {replacement.split()[0]} --help' for the new interface.",
            "See `mdclaw --list-json` for the current tool surface.",
        ],
        "context": {
            "tool": tool_name,
            "replacement": replacement,
            "code": "tool_renamed",
        },
        "recoverable": True,
    }


def _unknown_tool_error(tool_name: str, tools: dict[str, dict]) -> dict:
    """Structured 'no such tool' reply for any name the CLI does not have.

    Typos, invented names, and tools dropped in an earlier release all land
    here. The CLI must never answer an unknown name with an argparse dump on
    stderr: agents recover from a stable ``code`` on stdout, so a name they
    guessed wrong has to come back as JSON like every other failure.
    """
    suggestions = difflib.get_close_matches(tool_name, tools, n=3, cutoff=0.85)
    hints = ["Run 'mdclaw --list-json' to see exact available tool names."]
    if suggestions:
        hints.insert(0, f"Did you mean: {', '.join(suggestions)}?")
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "tool_not_available",
        "message": f"Unknown MDClaw tool '{tool_name}'",
        "errors": [f"Tool '{tool_name}' is not present in CLI discovery"],
        "warnings": [],
        "hints": hints,
        "context": {
            "tool": tool_name,
            "code": "tool_not_available",
            "suggestions": suggestions,
        },
        "recoverable": True,
    }


def _tool_list_json(
    tools: dict[str, dict],
    requested_tool: str | None = None,
) -> dict:
    """Build a machine-readable projection of all tools or one exact tool."""
    if requested_tool is not None and requested_tool not in tools:
        return _missing_tool_error(requested_tool, tools)

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
        requires_node = info.get("requires_node", tool_requires_node(info["fn"]))
        node_type = info.get("node_type", tool_node_type(info["fn"]))
        tool_payload = {
            "name": tool_name,
            "server": info["server"],
            "summary": summary,
            "is_async": info["is_async"],
            "requires_node": requires_node,
            "node_type": node_type,
            "job_dir_is_data": info.get("job_dir_is_data", tool_job_dir_is_data(info["fn"])),
            "parameters": _tool_parameter_schemas(tool_name, info["fn"]),
        }
        if requires_node:
            # Saying "job_dir and node_id are required" without saying where
            # they come from leaves an agent that introspects before calling —
            # which is what the skill tells it to do — holding a parameter list
            # and no route into the workflow. Name the route here.
            tool_payload["workflow_entry"] = [
                "mdclaw bootstrap_md_workflow --study-dir <study_dir> "
                "--question <question> --md-goal <goal>",
                f"mdclaw create_node --job-dir <job_dir> --node-type {node_type}",
                "mdclaw explain_node --job-dir <job_dir> --node-id <node_id>",
                f"mdclaw --job-dir <job_dir> --node-id <node_id> {tool_name} ...",
            ]
        if requested_tool is None:
            tool_payload["description"] = description
        payload["tools"].append(tool_payload)
    return payload


_STAGE_TOOL_NOTES = {
    "solvate_structure": "explicit water",
    "embed_in_membrane": "membrane regime",
    "build_amber_system": "Amber force fields; the default topology builder",
    "build_openmm_system": "OpenMM force fields",
    "prepare_complex": "the prep stage tool: split, clean, merge, ligands",
    "fetch_structure": "PDB / AlphaFold / local file into the source node",
    "register_local_structure": "an already-prepared local structure",
}
_STAGE_TOOL_PREFERENCE = ("prepare_complex", "solvate_structure", "build_amber_system",
                          "fetch_structure")


def _stage_tools_by_type(tools: dict[str, dict]) -> dict[str, list[str]]:
    """Stage tools grouped by node type, in workflow order, preferred tool first."""
    grouped: dict[str, list[str]] = {}
    for tool_name, info in tools.items():
        node_type = info.get("node_type")
        if node_type:
            grouped.setdefault(node_type, []).append(tool_name)
    ordered: dict[str, list[str]] = {}
    for node_type in NODE_TYPE_ORDER:
        names = sorted(grouped.get(node_type, []))
        names.sort(key=lambda n: (n not in _STAGE_TOOL_PREFERENCE, n))
        ordered[node_type] = names
    for node_type in sorted(set(grouped) - set(NODE_TYPE_ORDER)):
        ordered[node_type] = sorted(grouped[node_type])
    return ordered


def _print_tool_list(tools: dict[str, dict]) -> None:
    """Print a compact tool-name index: stage tools by stage, the rest by server."""
    print("MDClaw tools. Stage tools run with --job-dir/--node-id and record node state;")
    print("everything else is a standalone helper or a DAG/cluster utility.")
    print("Workflow: mdclaw --workflow. One tool's parameters: mdclaw --list-json <tool>.")
    print("\nStage tools by DAG stage (" + " > ".join(NODE_TYPE_ORDER) + "):")
    stage_names: set[str] = set()
    for node_type, names in _stage_tools_by_type(tools).items():
        if names:
            print(f"  {node_type:<8} " + "  ".join(names))
            stage_names.update(names)
    by_server: dict[str, list[str]] = {}
    for tool_name, info in tools.items():
        if tool_name not in stage_names:
            by_server.setdefault(info["server"], []).append(tool_name)
    print("\nOther tools by server (no node state unless the tool says otherwise):")
    for server_name in sorted(by_server):
        print(f"\n[{server_name}]")
        print("  " + "  ".join(sorted(by_server[server_name])))
    print(f"\nTotal: {len(tools)} tools")


def _workflow_text(tools: dict[str, dict]) -> str:
    """The DAG contract in one screen: what ``--help`` cannot say per tool.

    Skill-less agents rebuilt this model from tool help, package source and
    trial and error (11 help reads and a source grep per attempt on
    2026-09-10). Everything here is structural; scientific choices stay with
    the skills.
    """
    lines = [
        "MDClaw job DAG (schema v3)",
        "",
        "Stages, in order:  " + " > ".join(NODE_TYPE_ORDER),
        "Stage tools (run with --job-dir/--node-id; inputs resolve from the parent node):",
    ]
    for node_type, names in _stage_tools_by_type(tools).items():
        if not names:
            continue
        described = []
        for name in names:
            note = _STAGE_TOOL_NOTES.get(name)
            described.append(f"{name} ({note})" if note else name)
        lines.append(f"  {node_type:<8} " + ", ".join(described))
    lines += [
        "",
        "Start a job (creates study/jobs/main and its source node):",
        "  mdclaw bootstrap_md_workflow --study-dir <dir> --question \"<request>\" "
        "[--pdb-id 1ABC]",
        "",
        "Every stage, in this order:",
        "  1. mdclaw create_node --job-dir <job_dir> --node-type <stage>"
        "        # parent auto-resolved; returns node_id (or use --output id)",
        "  2. mdclaw explain_node --job-dir <job_dir> --node-id <node_id>"
        "        # optional: ready_to_run, resolved inputs, blocking codes",
        "  3. mdclaw --job-dir <job_dir> --node-id <node_id> <stage tool> [options]"
        "  # runs the stage and records the node",
        "  Each result carries 'dag' (frontier and statuses) and 'next' "
        "(the next command); 'inspect_job --job-dir <job_dir>' shows the whole DAG.",
        "",
        "Rules the CLI enforces:",
        "  - Nodes run once. completed/failed nodes are sealed; to redo a stage, "
        "create a new node with the same parents (--parent-node-ids) and run it there.",
        "  - A parent must be completed before its child runs (pending chains may be "
        "created ahead and submitted with Slurm dependencies).",
        "  - Never pass artifact paths between nodes; stage tools resolve them.",
        "  - Standalone helpers (clean_protein, split_molecules, merge_structures, ...) "
        "read and write no node state; inside a job use the stage tool.",
        "  - Node status becomes completed only through the stage tool, never via "
        "update_workflow_state.",
        "",
        "Batch execution (min/eq/prod on a cluster):",
        "  mdclaw submit_job --job-dir <job_dir> --node-id <node_id> "
        "--script \"mdclaw --job-dir <job_dir> --node-id <node_id> run_minimization ...\" "
        "--gpus 1 [--dependency afterok:<slurm_job_id>]",
        "",
        "Output: JSON on stdout; stderr carries warnings and a heartbeat only "
        "(--log-file <path> for INFO logs).",
        "  --output brief (default): envelope first, large values stubbed, full result "
        "in <job_dir>/nodes/<node_id>/result.json; --output full; --output id (create_node).",
        "  On failure: code, message, hints, next_action; "
        "mdclaw trace_failure --job-dir <job_dir> --node-id <node_id> for a sealed node.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------



def main(argv: list[str] | None = None) -> None:
    _configure_logging()

    tools = _discover_tools()

    # Catch any name the CLI does not have before argparse sees it, so the
    # agent gets a structured code on stdout instead of an "invalid choice"
    # dump on stderr.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    subcommand = _detect_subcommand(raw_argv)
    if subcommand is not None and subcommand not in tools:
        _json_error_and_exit(_missing_tool_error(subcommand, tools))

    parser = _build_parser(tools)
    args = parser.parse_args(argv)
    global _OUTPUT_MODE, _TOOLS
    _OUTPUT_MODE = args._output_mode
    _TOOLS = tools
    if args._log_file:
        _configure_logging(args._log_file)
    heartbeat_seconds = (
        args._heartbeat_seconds if args._heartbeat_seconds is not None
        else float(os.getenv("MDCLAW_HEARTBEAT_SECONDS", _HEARTBEAT_DEFAULT_SECONDS))
    )

    # --list
    if args.list_tools:
        _print_tool_list(tools)
        sys.exit(0)

    # --workflow
    if args.show_workflow:
        print(_workflow_text(tools))
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

    # Build kwargs — both paths consume the same _ParamSpec view.
    specs = _tool_param_specs(fn, requires_node=requires_node)
    spec_by_name = {spec.name: spec for spec in specs}
    missing: list[str] = []
    if args.json_input:
        kwargs = _load_json_cli(args.json_input, "--json-input")
        if not isinstance(kwargs, dict):
            _json_error_and_exit(create_validation_error(
                "--json-input", "must be a JSON object of parameter names to values",
                code="invalid_json_input", actual=type(kwargs).__name__,
                expected="JSON object",
            ))
        json_keys = list(kwargs)
        unknown = sorted(str(k) for k in kwargs if k not in spec_by_name)
        if unknown:
            _json_error_and_exit(_unknown_parameter_error(
                tool_name, unknown, spec_by_name, requires_node=requires_node,
                job_dir=_global_job_dir or kwargs.get("job_dir"),
                node_id=_global_node_id or kwargs.get("node_id"),
            ))
        for pname, value in list(kwargs.items()):
            spec = spec_by_name.get(pname)
            if spec is None or value is None:
                continue
            kwargs[pname] = _coerce_value(value, spec.hint)
        if _global_job_dir is not None and kwargs.get("job_dir") is None and "job_dir" in spec_by_name:
            kwargs["job_dir"] = _global_job_dir
        if _global_node_id is not None and kwargs.get("node_id") is None and "node_id" in spec_by_name:
            kwargs["node_id"] = _global_node_id
        missing = [
            spec.cli_flag
            for spec in specs
            if kwargs.get(spec.name) is None
            and spec.default is inspect.Parameter.empty
            and not spec.optional
        ]
    else:
        json_keys = []
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
        for spec in specs:
            value = args_dict.get(spec.name)
            if value is None:
                if spec.default is inspect.Parameter.empty and not spec.optional:
                    missing.append(spec.cli_flag)
                continue
            if _takes_json(spec.hint) and isinstance(value, str):
                value = _load_json_cli(value, spec.cli_flag)
            kwargs[spec.name] = value

    explicit_kwargs = {
        name: kwargs.get(name)
        for name in _explicit_parameter_names(raw_argv, specs, json_keys)
        if name in spec_by_name
    }

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

    if (
        (_global_job_dir is not None or _global_node_id is not None)
        and not requires_node
        and "job_dir" not in spec_by_name
        and "node_id" not in spec_by_name
    ):
        _json_error_and_exit(_node_context_not_applicable_error(
            tool_name, job_dir=_global_job_dir, node_id=_global_node_id,
        ), job_dir=_global_job_dir, node_id=_global_node_id)

    expected_node_type = info.get("node_type", tool_node_type(fn))
    if expected_node_type and effective_job_dir and effective_node_id:
        preflight_error = _node_type_preflight_error(
            tool_name=tool_name,
            job_dir=effective_job_dir,
            node_id=effective_node_id,
            expected_node_type=expected_node_type,
        )
        if preflight_error:
            _json_error_and_exit(preflight_error, job_dir=effective_job_dir,
                                 node_id=effective_node_id, requires_node=True)

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
                *([helper_hint] if (helper_hint := helper_stage_hint(
                    tool_name, effective_job_dir, effective_node_id)) else []),
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
        _json_error_and_exit(error, job_dir=effective_job_dir, node_id=effective_node_id,
                             requires_node=requires_node)

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
    if effective_job_dir and "job_dir" in spec_by_name:
        kwargs["job_dir"] = effective_job_dir
    if effective_node_id and "node_id" in spec_by_name:
        kwargs["node_id"] = effective_node_id

    # Execute
    started_at = time.monotonic()
    tool_stdout_tail = ""
    tool_stderr_tail = ""
    raw_stderr_tail = ""
    try:
        stdout_capture = _TailCaptureStream(sys.stdout, tee=False)
        stderr_capture = _TailCaptureStream(sys.stderr)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        logging_swaps: list[tuple[logging.StreamHandler, TextIO]] = []
        heartbeat = None
        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture
            logging_swaps = _swap_logging_stream(old_stderr, stderr_capture)
            _LOG_TAIL.reset()
            if heartbeat_seconds and heartbeat_seconds > 0:
                heartbeat = _Heartbeat(tool_name, stderr_capture, heartbeat_seconds).start()
            result = _run_tool(fn, is_async, kwargs)
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            tool_stdout_tail = stdout_capture.get_tail()
            # Raw stderr (tool prints, warnings) plus the INFO log trail that
            # no longer reaches stderr by default.
            raw_stderr_tail = stderr_capture.get_tail()
            log_tail = _LOG_TAIL.get_tail()
            tool_stderr_tail = (log_tail + raw_stderr_tail) if log_tail and log_tail not in raw_stderr_tail else raw_stderr_tail
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
            result = _attach_receipt(
                result, tool_name=tool_name, node_type=expected_node_type,
                explicit=explicit_kwargs,
                node_mode=bool(effective_job_dir and effective_node_id),
            )
        _write_benchmark_harness_record(
            tool_name=tool_name,
            exit_code=exit_code,
            started_at=started_at,
        )
        _report_confirmation_items_safely(result)
        _emit_result(
            result,
            exit_code=exit_code,
            job_dir=effective_job_dir or (result.get("job_dir") if isinstance(result, dict) else None),
            node_id=effective_node_id or (result.get("node_id") if isinstance(result, dict) else None),
            requires_node=requires_node,
            stderr_tail=raw_stderr_tail,
            stderr_stream=old_stderr,
        )
    except SystemExit:
        raise
    except Exception as e:
        _write_benchmark_harness_record(
            tool_name=tool_name,
            exit_code=1,
            started_at=started_at,
        )
        from mdclaw.node.lifecycle import NodeSealedError

        if isinstance(e, NodeSealedError):
            # Not an internal error: the caller re-ran a stage on a node that
            # already finished. Say so with the code that names the fix.
            error_payload = {
                "message": f"{tool_name}: {e}",
                "error_type": type(e).__name__,
                "code": "node_terminal",
                "errors": [str(e)],
                "hints": [
                    "Nodes run once. Create a new node with the same parents "
                    "(mdclaw create_node --parent-node-ids <parent>) and run the "
                    "stage there; a sealed node is never rewritten.",
                ],
            }
        else:
            error_payload = {
                "message": f"{tool_name} raised {type(e).__name__}: {e}",
                "error_type": type(e).__name__,
                "code": "unhandled_exception",
                "errors": [str(e)],
            }
        error_out = finalize_error(
            error_payload,
            job_dir=effective_job_dir,
            node_id=effective_node_id,
        )
        stdout_tail = (
            f"{tool_stdout_tail}\n--- mdclaw final JSON ---\n"
            f"{_json_stdout_tail(error_out)}"
            if tool_stdout_tail
            else _json_stdout_tail(error_out)
        )
        # Whatever the tool's node contract, a node this invocation began and
        # abandoned is failed before the process exits.
        sealed_here = _fail_node_if_running(
            effective_job_dir, effective_node_id, error_out.get("errors") or [str(e)]
        )
        if sealed_here:
            error_out.setdefault("context", {})["node_failed_by_cli"] = True
        if requires_node or sealed_here:
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
        _emit_result(
            error_out,
            exit_code=1,
            job_dir=effective_job_dir,
            node_id=effective_node_id,
            requires_node=requires_node,
            stderr_tail=tool_stderr_tail,
        )


if __name__ == "__main__":
    main()
