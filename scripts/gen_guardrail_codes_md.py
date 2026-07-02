"""Generate skills/common/guardrail-codes.md from the SSOT registry.

Run with: conda run -n mdclaw python scripts/gen_guardrail_codes_md.py

The skill doc is a rendered view of ``mdclaw.guardrail_codes.GUARDRAIL_CODES``.
``tests/test_guardrail_code_registry.py`` checks the rendered doc for drift, so
regenerate it whenever the registry changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mdclaw.guardrail_codes import GUARDRAIL_CODES  # noqa: E402

DOC_PATH = _REPO_ROOT / "skills" / "common" / "guardrail-codes.md"

HEADER = """# Guardrail Codes

Branch on stable `code` values from tool JSON. Do not parse stderr or long
human-readable messages.

This table is generated from `mdclaw/guardrail_codes.py`
(`scripts/gen_guardrail_codes_md.py`); edit the registry, not this file.

| Code | Action |
|------|--------|
"""

FOOTER = """
If a code is unknown, report `code`, `message`, `errors`, `warnings`, and
`hints` to the user instead of inventing a workaround.
"""


def render() -> str:
    rows = [
        f"| `{code}` | {action} |"
        for code, action in sorted(GUARDRAIL_CODES.items())
    ]
    return HEADER + "\n".join(rows) + "\n" + FOOTER


def main() -> None:
    DOC_PATH.write_text(render())
    print(f"Wrote {len(GUARDRAIL_CODES)} codes to {DOC_PATH}")


if __name__ == "__main__":
    main()
