"""Scan mdclaw/ for guardrail ``code`` string literals and write a golden set.

Run with: conda run -n mdclaw python scripts/gen_guardrail_codes.py

This is the phase-0 safety net for the refactor: it captures every stable
``code`` value the package can emit so later structural changes cannot silently
add, drop, or rename an agent-facing failure code without updating the golden
(tests/data/guardrail_codes.json).

The scanner is intentionally source-based (AST + literal matching) rather than
runtime-based, because most codes are only produced on error paths that unit
tests do not all exercise.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "mdclaw"
GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "data" / "guardrail_codes.json"


def _looks_like_code(value: str) -> bool:
    """Heuristic: stable codes are lower_snake_case identifiers."""
    if not value or len(value) > 80:
        return False
    if value != value.lower():
        return False
    return all(ch.isalnum() or ch in {"_"} for ch in value) and any(
        ch.isalpha() for ch in value
    )


def iter_guardrail_codes(root: Path = PACKAGE_ROOT) -> set[str]:
    """Return the set of guardrail ``code`` literals defined under ``root``.

    Matches two shapes:
    - ``"code": "<literal>"`` dictionary entries
    - ``code="<literal>"`` keyword arguments and ``code = "<literal>"`` assigns
    """
    codes: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # dict literal: {"code": "..."}
            if isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "code"
                        and isinstance(val, ast.Constant)
                        and isinstance(val.value, str)
                        and _looks_like_code(val.value)
                    ):
                        codes.add(val.value)
            # keyword: code="..."
            if isinstance(node, ast.keyword) and node.arg == "code":
                val = node.value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    if _looks_like_code(val.value):
                        codes.add(val.value)
            # assignment: code = "..."  /  x["code"] = "..."
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str) and _looks_like_code(node.value.value):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "code":
                            codes.add(node.value.value)
                        elif (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "code"
                        ):
                            codes.add(node.value.value)
    return codes


def main() -> None:
    codes = sorted(iter_guardrail_codes())
    GOLDEN.write_text(json.dumps(codes, indent=2) + "\n")
    print(f"Wrote {len(codes)} guardrail codes to {GOLDEN}")


if __name__ == "__main__":
    main()
