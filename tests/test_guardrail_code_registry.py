"""Guardrail ``code`` drift guard (plan phase 0, tightened in phase B).

MDClaw's weak-agent contract is: branch on stable ``code`` strings, never on
human-readable messages. That only holds if the set of emitted codes stays
under control. This test scans the package source for every ``code`` literal
and compares it to a committed golden set.

Phase 0: detect drift against tests/data/guardrail_codes.json.
Phase B: additionally require every emitted code to be present in the
``mdclaw.guardrail_codes`` single-source-of-truth registry (skipped
automatically until that module exists).
"""

import json
import sys
from pathlib import Path

import pytest

# Make the repo-root ``scripts`` package importable regardless of pytest's
# import mode / rootdir handling.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gen_guardrail_codes import iter_guardrail_codes  # noqa: E402

GOLDEN_PATH = Path(__file__).parent / "data" / "guardrail_codes.json"


def _load_golden() -> set[str]:
    return set(json.loads(GOLDEN_PATH.read_text()))


def test_guardrail_codes_match_golden():
    golden = _load_golden()
    found = iter_guardrail_codes()

    added = sorted(found - golden)
    removed = sorted(golden - found)
    assert not added and not removed, (
        "Guardrail code set drifted. If intentional, regenerate with "
        "`python scripts/gen_guardrail_codes.py`.\n"
        f"Added: {added}\nRemoved: {removed}"
    )


def test_emitted_codes_are_registered_when_registry_exists():
    """Once the SSOT registry lands, every emitted code must be registered."""
    try:
        from mdclaw.guardrail_codes import GUARDRAIL_CODES
    except ImportError:
        pytest.skip("mdclaw.guardrail_codes SSOT registry not present yet")

    found = iter_guardrail_codes()
    unregistered = sorted(found - set(GUARDRAIL_CODES))
    assert not unregistered, (
        "These emitted codes are missing from mdclaw.guardrail_codes.GUARDRAIL_CODES: "
        + ", ".join(unregistered)
    )


def test_registry_has_no_stale_codes():
    """The SSOT registry must not carry codes the package never emits."""
    try:
        from mdclaw.guardrail_codes import GUARDRAIL_CODES
    except ImportError:
        pytest.skip("mdclaw.guardrail_codes SSOT registry not present yet")

    found = iter_guardrail_codes()
    stale = sorted(set(GUARDRAIL_CODES) - found)
    assert not stale, (
        "These registry codes are no longer emitted anywhere in mdclaw/. "
        "Remove them from mdclaw.guardrail_codes.GUARDRAIL_CODES: " + ", ".join(stale)
    )


def test_every_registered_code_has_an_action():
    try:
        from mdclaw.guardrail_codes import GUARDRAIL_CODES
    except ImportError:
        pytest.skip("mdclaw.guardrail_codes SSOT registry not present yet")

    empty = sorted(code for code, action in GUARDRAIL_CODES.items() if not str(action).strip())
    assert not empty, "These registered codes have an empty action: " + ", ".join(empty)


def test_guardrail_codes_doc_matches_registry():
    """skills/common/guardrail-codes.md is generated from the registry."""
    try:
        from mdclaw.guardrail_codes import GUARDRAIL_CODES  # noqa: F401
    except ImportError:
        pytest.skip("mdclaw.guardrail_codes SSOT registry not present yet")

    from scripts.gen_guardrail_codes_md import DOC_PATH, render

    expected = render()
    actual = DOC_PATH.read_text()
    assert actual == expected, (
        "skills/common/guardrail-codes.md is out of sync with the registry. "
        "Regenerate with `python scripts/gen_guardrail_codes_md.py`."
    )
