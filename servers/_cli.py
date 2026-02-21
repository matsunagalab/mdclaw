"""MDClaw CLI — invoke any tool directly from the command line.

Usage:
    mdclaw --list                          # List all tools
    mdclaw --version                       # Show version
    mdclaw <tool> --help                   # Tool-specific help
    mdclaw <tool> [--param value ...]      # Run a tool
    mdclaw <tool> --json-input '{...}'     # Pass all params as JSON

Output is always JSON on stdout; logs go to stderr.
"""

import argparse
import asyncio
import inspect
import json
import logging
import sys
from typing import Union, get_args, get_origin

from servers import __version__
from servers._registry import SERVER_REGISTRY


# ---------------------------------------------------------------------------
# Logging: force all loggers to stderr so stdout stays clean JSON
# ---------------------------------------------------------------------------

def _configure_logging():
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s - %(levelname)s - %(message)s"))
    root.addHandler(handler)


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
            tools[tool_name] = {
                "fn": fn,
                "is_async": inspect.iscoroutinefunction(fn),
                "server": server_name,
                "description": inspect.getdoc(fn) or "",
            }
    return tools


# ---------------------------------------------------------------------------
# Type helpers for argparse
# ---------------------------------------------------------------------------

def _unwrap_optional(hint):
    """If hint is Optional[X] (Union[X, None]), return (X, True). Else (hint, False)."""
    origin = get_origin(hint)
    if origin is Union:
        args = get_args(hint)
        non_none = [a for a in args if a is not type(None)]
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


def _is_dict_type(hint) -> bool:
    """Check if hint is dict or Dict[...]."""
    origin = get_origin(hint)
    return origin is dict or hint is dict


def _coerce_value(value, hint):
    """Coerce a CLI value to the target type."""
    if hint is None or hint is inspect.Parameter.empty:
        return value

    inner, _ = _unwrap_optional(hint)

    if inner is bool:
        # Handled by BooleanOptionalAction, value is already bool
        return value
    if inner is int:
        return int(value)
    if inner is float:
        return float(value)
    if inner is str:
        return str(value)
    if _is_list_of_str(inner):
        # nargs='+' gives us a list already
        if isinstance(value, list):
            return value
        return [value]
    if _is_dict_type(inner):
        # JSON string -> dict
        if isinstance(value, str):
            return json.loads(value)
        return value
    return value


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

    subparsers = parser.add_subparsers(dest="tool_name")

    for tool_name, info in sorted(tools.items()):
        fn = info["fn"]
        desc_first_line = (info["description"].split("\n")[0].strip()
                          if info["description"] else "")
        sub = subparsers.add_parser(
            tool_name,
            help=desc_first_line,
            description=info["description"],
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
            hint = hints.get(pname, param.annotation)
            if hint is inspect.Parameter.empty:
                hint = str  # fallback

            inner, is_optional = _unwrap_optional(hint)
            cli_name = "--" + pname.replace("_", "-")
            required = param.default is inspect.Parameter.empty and not is_optional

            if inner is bool:
                default_val = param.default if param.default is not inspect.Parameter.empty else False
                sub.add_argument(
                    cli_name,
                    action=argparse.BooleanOptionalAction,
                    default=default_val,
                    help=f"(bool, default: {default_val})",
                )
            elif _is_list_of_str(inner):
                sub.add_argument(
                    cli_name,
                    nargs="+",
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help="(list of str, required)" if required else "(list of str)",
                )
            elif _is_dict_type(inner):
                sub.add_argument(
                    cli_name,
                    type=str,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help='(JSON string, e.g. \'{"key":"val"}\', required)' if required else '(JSON string, e.g. \'{"key":"val"}\')',
                )
            elif inner is int:
                sub.add_argument(
                    cli_name,
                    type=int,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=f"(int, default: {param.default if param.default is not inspect.Parameter.empty else 'required'})",
                )
            elif inner is float:
                sub.add_argument(
                    cli_name,
                    type=float,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=f"(float, default: {param.default if param.default is not inspect.Parameter.empty else 'required'})",
                )
            else:
                # Default: str
                sub.add_argument(
                    cli_name,
                    type=str,
                    default=param.default if param.default is not inspect.Parameter.empty else None,
                    required=False,
                    help=f"(str, default: {param.default if param.default is not inspect.Parameter.empty else 'required'})",
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


# ---------------------------------------------------------------------------
# --list output
# ---------------------------------------------------------------------------

def _print_tool_list(tools: dict[str, dict]) -> None:
    """Print tools grouped by server."""
    by_server: dict[str, list[tuple[str, str]]] = {}
    for tool_name, info in tools.items():
        by_server.setdefault(info["server"], []).append(
            (tool_name, info["description"].split("\n")[0].strip() if info["description"] else "")
        )

    for server_name in sorted(by_server):
        print(f"\n[{server_name}]")
        for tname, desc in sorted(by_server[server_name]):
            print(f"  {tname:40s} {desc[:60]}")
    print(f"\nTotal: {len(tools)} tools")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    _configure_logging()

    tools = _discover_tools()
    parser = _build_parser(tools)
    args = parser.parse_args(argv)

    # --list
    if args.list_tools:
        _print_tool_list(tools)
        sys.exit(0)

    # No subcommand
    if not args.tool_name:
        parser.print_help()
        sys.exit(0)

    tool_name = args.tool_name
    info = tools[tool_name]
    fn = info["fn"]
    is_async = info["is_async"]

    # Build kwargs
    if args.json_input:
        kwargs = json.loads(args.json_input)
    else:
        sig = inspect.signature(fn)
        hints = {}
        try:
            hints = {k: v for k, v in inspect.get_annotations(fn, eval_str=True).items()
                     if k != "return"}
        except Exception:
            pass

        kwargs = {}
        missing = []
        args_dict = vars(args)
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
            if _is_dict_type(_unwrap_optional(hint)[0]) and isinstance(value, str):
                value = json.loads(value)
            kwargs[pname] = value

        if missing:
            parser.error(f"the following arguments are required: {', '.join(missing)}")

    # Execute
    try:
        result = _run_tool(fn, is_async, kwargs)
        # Determine exit code
        exit_code = 0
        if isinstance(result, dict) and result.get("success") is False:
            exit_code = 1
        json.dump(result, sys.stdout, indent=2, default=str)
        print()  # trailing newline
        sys.exit(exit_code)
    except Exception as e:
        error_out = {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }
        json.dump(error_out, sys.stdout, indent=2, default=str)
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
