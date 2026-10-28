"""The condition keys a node type's stage tools can cross-check.

``create_node --conditions`` records intent that the stage tool must confirm:
when it runs, it reports ``actual_conditions`` and every declared key must be
among them with a matching value, or the node fails. A key that no stage tool
of the node type ever reports therefore makes a node that can never run, and
in MDDataBench v5 that happened in 56 attempts (``chain_ranges`` on prep,
``buffer_distance`` on solv, ``total_time_ps`` on prod, eq conditions on min).

The vocabulary is read from the stage tools themselves rather than from a
table kept by hand (``condition_hints`` explains why a table drifts): the
keys of every ``actual_conditions={...}`` literal in a tool's body, plus the
tool's parameters when the body passes a dict it builds elsewhere
(``prepare_complex`` reports all of its arguments). It is the union over the
node type's tools, so it is never stricter than the most permissive tool; a
key it admits can still be refused at run time by the tool actually used.
Node types with a tool that never cross-checks conditions (``source``) and
``analyze`` (whose conditions are the analysis contract, validated on their
own) are not screened.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import textwrap
from typing import Optional

from mdclaw._registry import SERVER_REGISTRY
from mdclaw._tool_meta import tool_node_type

UNSCREENED_NODE_TYPES = frozenset({"analyze"})


@functools.lru_cache(maxsize=1)
def _stage_tools() -> dict[str, tuple]:
    by_type: dict[str, list] = {}
    for module_path in SERVER_REGISTRY.values():
        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - an optional server must not block node creation
            continue
        for fn in (getattr(module, "TOOLS", {}) or {}).values():
            node_type = tool_node_type(fn)
            if node_type:
                by_type.setdefault(node_type, []).append(fn)
    return {node_type: tuple(fns) for node_type, fns in by_type.items()}


def _parameters(fn) -> set[str]:
    """A tool's arguments, less node context and file locations (the DAG
    resolves those; they are never reported as conditions)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return set()
    return {name for name in params
            if not name.startswith("_") and name not in ("job_dir", "node_id")
            and not name.endswith(("_file", "_dir", "_path"))}


def _literal_keys(expr: ast.AST) -> tuple[set[str], bool]:
    """Keys of a dict expression and whether it may hold others."""
    if isinstance(expr, ast.Dict):
        keys, open_ = set(), False
        for key in expr.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            else:
                open_ = True  # ``**other`` or a computed key
        return keys, open_
    if isinstance(expr, ast.IfExp):
        a, open_a = _literal_keys(expr.body)
        b, open_b = _literal_keys(expr.orelse)
        return a | b, open_a or open_b
    return set(), True


def _actual_conditions_values(tree: ast.AST) -> Optional[list[ast.AST]]:
    """The expressions passed as ``actual_conditions=`` in ``tree``, with a
    bare name followed to the values assigned to it (``actual_conditions=_actual``
    in run_equilibration since the 2026-10-29 run guard). None when no call
    passes ``actual_conditions``."""
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            assigned.setdefault(node.target.id, []).append(node.value)
    values: list[ast.AST] = []
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "actual_conditions":
                found = True
                if isinstance(keyword.value, ast.Name):
                    values.extend(assigned.get(keyword.value.id) or [keyword.value])
                else:
                    values.append(keyword.value)
    return values if found else None


def literal_condition_keys(fn) -> Optional[frozenset]:
    """The keys ``fn`` reports as ``actual_conditions`` when its source spells
    them out as a dict literal (directly or through one name); None when the
    dict is computed, so the vocabulary is unknown before the tool runs. The
    submit-time condition check uses this strict form."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))
    except (OSError, TypeError, SyntaxError):
        return None
    values = _actual_conditions_values(tree)
    if not values:
        return None
    keys: set[str] = set()
    for value in values:
        literal, is_open = _literal_keys(value)
        if is_open:
            return None
        keys |= literal
    return frozenset(keys)


def reported_condition_keys(fn) -> Optional[frozenset]:
    """Keys one stage tool can report, or None when it never cross-checks."""
    fn = inspect.unwrap(fn)
    module = inspect.getmodule(fn)
    try:
        module_source = inspect.getsource(module) if module else ""
    except (OSError, TypeError):
        return frozenset(_parameters(fn))
    if "actual_conditions" not in module_source:
        return None
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return frozenset(_parameters(fn))
    keys: set[str] = set()
    values = _actual_conditions_values(tree)
    found = values is not None
    open_ = False
    for value in values or []:
        literal, is_open = _literal_keys(value)
        keys |= literal
        open_ = open_ or is_open
    if not found or open_:
        keys |= _parameters(fn)
    return frozenset(keys)


@functools.lru_cache(maxsize=None)
def stage_condition_vocabulary(node_type: str) -> Optional[frozenset]:
    """Every key a ``node_type`` stage tool can cross-check, or None when the
    node type is not screened."""
    if node_type in UNSCREENED_NODE_TYPES:
        return None
    fns = _stage_tools().get(node_type) or ()
    if not fns:
        return None
    vocabulary: set[str] = set()
    for fn in fns:
        keys = reported_condition_keys(fn)
        if keys is None:
            return None
        vocabulary |= keys
    return frozenset(vocabulary)


def node_types_reporting(key: str) -> list[str]:
    """Other node types whose stage tools report ``key`` (for "declare it there")."""
    return sorted(t for t in _stage_tools() if (stage_condition_vocabulary(t) or frozenset()) and
                  key in (stage_condition_vocabulary(t) or frozenset()))
