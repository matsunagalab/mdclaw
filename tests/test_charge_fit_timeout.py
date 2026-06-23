"""Unit tests for fallback ligand charge-fitting and NAGL precharge helpers.

The fallback charge-fitting budget (antechamber/sqm AM1-BCC) must be raisable
but never shortenable: an agent driving the CLI — including a weak LLM — must
not be able to set a short timeout and induce spurious ``SQM_timeout`` build
failures on large ligands such as AP5. These tests pin the floor/clamp
behavior, guard expiry semantics, and formal-charge validation.

Run with: conda run -n mdclaw pytest tests/test_charge_fit_timeout.py -v
"""

import threading
import time

import pytest

pytest.importorskip("openmm")
pytest.importorskip("openmmforcefields")

from mdclaw.amber.openmm_build import (
    _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS,
    _assign_nagl_partial_charges,
    _charge_fit_timeout_guard,
    _resolve_charge_fit_timeout,
    _validate_ligand_formal_charges,
)

FLOOR = _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS


def test_default_is_floor(monkeypatch):
    monkeypatch.delenv("MDCLAW_CHARGE_FIT_TIMEOUT", raising=False)
    assert _resolve_charge_fit_timeout() == FLOOR


def test_blank_env_is_floor(monkeypatch):
    monkeypatch.setenv("MDCLAW_CHARGE_FIT_TIMEOUT", "   ")
    assert _resolve_charge_fit_timeout() == FLOOR


@pytest.mark.parametrize("requested", [1, 60, FLOOR - 1, 0, -5])
def test_below_floor_clamps_up(monkeypatch, requested):
    """A short request can never shorten the budget below the floor."""
    monkeypatch.setenv("MDCLAW_CHARGE_FIT_TIMEOUT", str(requested))
    assert _resolve_charge_fit_timeout() == FLOOR


def test_above_floor_is_honored(monkeypatch):
    """The budget may be raised for exceptionally large ligands."""
    monkeypatch.setenv("MDCLAW_CHARGE_FIT_TIMEOUT", str(FLOOR + 1200))
    assert _resolve_charge_fit_timeout() == FLOOR + 1200


def test_non_numeric_falls_back_to_floor(monkeypatch):
    monkeypatch.setenv("MDCLAW_CHARGE_FIT_TIMEOUT", "forever")
    assert _resolve_charge_fit_timeout() == FLOOR


def test_guard_raises_timeout_on_main_thread():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        with _charge_fit_timeout_guard(1):
            time.sleep(5)
    # Fired near the 1 s alarm, not after the full 5 s sleep.
    assert time.monotonic() - start < 4


def test_guard_no_raise_when_block_is_fast():
    with _charge_fit_timeout_guard(FLOOR):
        pass  # returns well within budget; no alarm should fire


def test_guard_skips_off_main_thread():
    """Off the main thread the alarm is unavailable; the guard must not crash
    and must not raise even if the block outlives the requested budget."""
    outcome = {}

    def worker():
        try:
            with _charge_fit_timeout_guard(1):
                time.sleep(1.5)
            outcome["result"] = "completed"
        except BaseException as exc:  # noqa: BLE001
            outcome["result"] = f"raised:{type(exc).__name__}"

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert outcome["result"] == "completed"


class _FakeMolecule:
    def __init__(self, total_charge):
        self.total_charge = total_charge
        self.partial_charges = None


def test_validate_ligand_formal_charges_accepts_graph_charge():
    records, errors = _validate_ligand_formal_charges(
        [{"residue_name": "ACE", "net_charge": -1}],
        [_FakeMolecule(-1.0)],
    )

    assert errors == []
    assert records[0]["status"] == "passed"
    assert records[0]["formal_charge_e"] == -1.0


def test_validate_ligand_formal_charges_rejects_metadata_only_override():
    records, errors = _validate_ligand_formal_charges(
        [{"residue_name": "ACE", "expected_net_charge": -1}],
        [_FakeMolecule(0.0)],
    )

    assert records[0]["status"] == "failed"
    assert "charged SMILES/SDF" in errors[0]


def test_nagl_unavailable_leaves_am1bcc_fallback_records():
    pytest.importorskip("openmm")
    records = _assign_nagl_partial_charges(
        [{"residue_name": "ACE", "ligand_instance_id": "A:ACE:1"}],
        [_FakeMolecule(0.0)],
    )

    assert records
    if records[0]["status"] == "fallback":
        assert records[0]["method"] == "am1bcc_fallback"
