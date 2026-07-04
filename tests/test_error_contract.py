"""CI enforcement of the single MDClaw error contract.

Every ``success: false`` result an agent can see must be uniform so weak LLMs
can branch on a stable ``code`` and act on a concrete ``next_action`` without
parsing logs. The CLI boundary routes every failure through
``mdclaw._common.finalize_error``; this test pins the contract that function
guarantees and checks it against representative real tool failures from across
the server modules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mdclaw._common import (
    ERROR_TAIL_CHARS,
    MAX_ERRORS,
    MAX_HINTS,
    create_file_not_found_error,
    create_tool_not_available_error,
    create_validation_error,
    finalize_error,
)
from mdclaw.guardrail_codes import GUARDRAIL_CODES, guardrail_action


def _assert_contract(result: dict) -> None:
    """Assert a failure dict satisfies the single MDClaw error contract."""
    assert result.get("success") is False, result

    code = result.get("code")
    assert isinstance(code, str) and code, f"missing code: {result}"
    assert code in GUARDRAIL_CODES, f"code {code!r} not registered"

    assert result.get("message"), f"missing message: {result}"

    hints = result.get("hints")
    assert isinstance(hints, list) and hints, f"missing hints: {result}"
    assert hints[0] == guardrail_action(code), (
        f"first hint must be the registry action for {code!r}"
    )
    assert len(hints) <= MAX_HINTS, f"too many hints: {len(hints)}"

    assert result.get("next_action"), f"missing next_action: {result}"
    assert isinstance(result.get("recoverable"), bool)

    errors = result.get("errors")
    assert isinstance(errors, list) and errors, f"missing errors: {result}"
    assert len(errors) <= MAX_ERRORS, f"too many errors: {len(errors)}"
    # Each error line is tail-bounded (allow a little slack for the marker).
    for entry in errors:
        assert len(str(entry)) <= ERROR_TAIL_CHARS + 200


def _write_minimal_pdb(path: Path) -> None:
    path.write_text(
        "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
        "END\n"
    )


# ---------------------------------------------------------------------------
# finalize_error invariants
# ---------------------------------------------------------------------------


def test_finalize_error_defaults_uncoded_failure():
    result = finalize_error({"errors": ["something broke"]})
    _assert_contract(result)
    assert result["code"] == "unhandled_error"


def test_finalize_error_from_exception():
    result = finalize_error(RuntimeError("boom"))
    _assert_contract(result)
    assert result["code"] == "unhandled_error"
    assert "boom" in result["errors"][0]


def test_finalize_error_bounds_long_error_and_hints():
    result = finalize_error(
        {
            "code": "missing_pdb_file",
            "message": "no pdb",
            "errors": ["x" * 10000],
            "hints": [f"hint {i}" for i in range(50)],
        }
    )
    _assert_contract(result)
    assert len(result["errors"][0]) <= ERROR_TAIL_CHARS + 200
    assert "truncated" in result["errors"][0]


def test_finalize_error_next_action_uses_trace_failure_with_node_context():
    result = finalize_error(
        {"code": "missing_pdb_file", "message": "no pdb"},
        job_dir="/tmp/job",
        node_id="topo_001",
    )
    _assert_contract(result)
    assert "trace_failure" in result["next_action"]
    assert "topo_001" in result["next_action"]


def test_finalize_error_is_idempotent():
    once = finalize_error({"code": "missing_pdb_file", "message": "no pdb"})
    twice = finalize_error(dict(once))
    _assert_contract(twice)
    assert twice["code"] == once["code"]
    assert twice["hints"] == once["hints"]


# ---------------------------------------------------------------------------
# Shared builders already satisfy the contract
# ---------------------------------------------------------------------------


def test_builders_satisfy_contract():
    _assert_contract(create_file_not_found_error("missing.pdb", "structure file"))
    _assert_contract(create_tool_not_available_error("pdb4amber"))
    _assert_contract(
        create_validation_error(
            "water_model",
            "Unknown water model",
            code="unknown_water_model",
        )
    )


# ---------------------------------------------------------------------------
# Representative real tool failures, as seen at the CLI boundary
# ---------------------------------------------------------------------------


def test_representative_tool_failures_satisfy_contract():
    from mdclaw.amber.build_system import build_amber_system
    from mdclaw.simulation.equilibrate import run_equilibration
    from mdclaw.metal.detect import detect_metal_ions
    from mdclaw.solvation import solvate_structure

    failures = [
        solvate_structure(pdb_file=None),
        run_equilibration(system_xml_file=None, topology_pdb_file=None),
        build_amber_system(
            pdb_file="missing.pdb", forcefield="ff19SB", water_model="opccc"
        ),
        detect_metal_ions(pdb_file="missing_metal.pdb"),
    ]

    for raw in failures:
        assert raw.get("success") is False
        # The CLI boundary finalizes every failure; the contract must hold there.
        _assert_contract(finalize_error(raw))


# ---------------------------------------------------------------------------
# End-to-end: the CLI itself emits the contract
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "mdclaw._cli", *args],
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_cli_preflight_failure_satisfies_contract():
    # run_production is a workflow tool; invoking it without node context is a
    # deterministic preflight failure routed through _json_error_and_exit.
    result = _run_cli("run_production")
    _assert_contract(result)
    assert result["code"] == "node_context_required"


def test_cli_unknown_tool_or_exception_is_structured(tmp_path):
    # A missing required argument path also flows through the contract.
    result = _run_cli("solvate_structure")
    _assert_contract(result)
