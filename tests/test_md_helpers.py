"""Unit tests for pure helpers extracted from md_simulation_server.

These helpers are callable without OpenMM so they run in the default
non-slow pytest lane. See test_server_smoke.py for the full simulation
smoke tests.
"""

from pathlib import Path

import pytest

from mdclaw.md_simulation_server import (
    _DCD_MAGIC,
    _compute_step_plan,
    _dcd_has_valid_header,
    _detect_ensemble_mismatch,
    _node_previously_failed,
    _resolve_dcd_append_mode,
    run_production,
)
from mdclaw._node import (
    begin_node,
    create_node,
    fail_node,
    init_progress_v3,
)
from mdclaw._node import complete_node as _real_complete_node


def complete_node(job_dir, node_id, artifacts, **kwargs):
    """Test wrapper that touches placeholder artifact files first.

    Mirrors the wrapper in tests/test_node.py; see its docstring for the
    rationale. Tests that exercise the strict guard explicitly should call
    ``_real_complete_node``.
    """
    node_dir = Path(job_dir) / "nodes" / node_id
    for rel_path in artifacts.values():
        if not isinstance(rel_path, str) or not rel_path:
            continue
        full = node_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if not full.exists():
            full.touch()
    return _real_complete_node(job_dir, node_id, artifacts, **kwargs)


class TestComputeStepPlan:
    """Covers the step-count arithmetic used by run_production.

    Contract: ``simulation_time_ns`` is always "time to run in this call".
    The plan adds that onto the caller-provided ``current_step`` so that
    eq→prod (current_step=0 by design) and prod→prod (current_step>0)
    both extend by exactly the requested duration.
    """

    def test_fresh_run_from_zero(self):
        plan = _compute_step_plan(simulation_time_ns=1.0,
                                  timestep_fs=4.0,
                                  current_step=0)
        assert plan == {
            "start_step": 0,
            "start_time_ns": 0.0,
            "steps_to_run": 250_000,
            "num_steps": 250_000,
        }

    def test_restart_extension_adds_on_top_of_current_step(self):
        plan = _compute_step_plan(simulation_time_ns=0.5,
                                  timestep_fs=4.0,
                                  current_step=250_000)
        assert plan == {
            "start_step": 250_000,
            "start_time_ns": 1.0,
            "steps_to_run": 125_000,
            "num_steps": 375_000,
        }

    def test_zero_additional_runs_no_steps_but_preserves_counters(self):
        plan = _compute_step_plan(simulation_time_ns=0.0,
                                  timestep_fs=4.0,
                                  current_step=250_000)
        assert plan == {
            "start_step": 250_000,
            "start_time_ns": 1.0,
            "steps_to_run": 0,
            "num_steps": 250_000,
        }

    def test_two_fs_timestep(self):
        """Non-HMR case: 1 ns @ 2 fs = 500 000 steps."""
        plan = _compute_step_plan(simulation_time_ns=1.0,
                                  timestep_fs=2.0,
                                  current_step=0)
        assert plan["steps_to_run"] == 500_000
        assert plan["num_steps"] == 500_000

    def test_sub_nanosecond_truncates_toward_zero(self):
        """int() floors positive values — a duration smaller than one
        step at the given timestep yields zero steps, not an exception."""
        plan = _compute_step_plan(simulation_time_ns=1e-9,
                                  timestep_fs=4.0,
                                  current_step=0)
        assert plan["steps_to_run"] == 0
        assert plan["num_steps"] == 0

    @pytest.mark.parametrize("current_step,timestep_fs,expected_ns", [
        (250_000, 4.0, 1.0),
        (500_000, 2.0, 1.0),
        (0, 4.0, 0.0),
        (1_000_000, 4.0, 4.0),
    ])
    def test_start_time_ns_is_derived_from_step_and_timestep(
        self, current_step, timestep_fs, expected_ns
    ):
        plan = _compute_step_plan(0.0, timestep_fs, current_step)
        assert plan["start_time_ns"] == pytest.approx(expected_ns)


class TestDcdHasValidHeader:
    """Byte-level checks for the DCD append guard.

    DCDReporter(append=True) cannot open a 0-byte or truncated file — it
    raises an opaque ValueError deep inside OpenMM. Gating on the
    fixed-record-84 + CORD magic lets run_production discard stale
    orphans cleanly before the reporter constructor runs.
    """

    def test_missing_file(self, tmp_path):
        assert _dcd_has_valid_header(tmp_path / "absent.dcd") is False

    def test_zero_byte(self, tmp_path):
        p = tmp_path / "empty.dcd"
        p.write_bytes(b"")
        assert _dcd_has_valid_header(p) is False

    def test_truncated_under_eight_bytes(self, tmp_path):
        p = tmp_path / "short.dcd"
        p.write_bytes(b"\x54\x00\x00\x00COR")  # missing final "D"
        assert _dcd_has_valid_header(p) is False

    def test_wrong_magic(self, tmp_path):
        p = tmp_path / "wrong.dcd"
        p.write_bytes(b"\x54\x00\x00\x00XXXX" + b"\x00" * 100)
        assert _dcd_has_valid_header(p) is False

    def test_valid_magic(self, tmp_path):
        p = tmp_path / "ok.dcd"
        p.write_bytes(_DCD_MAGIC + b"\x00" * 100)
        assert _dcd_has_valid_header(p) is True

    def test_directory_not_file(self, tmp_path):
        d = tmp_path / "mydir.dcd"
        d.mkdir()
        assert _dcd_has_valid_header(d) is False


@pytest.fixture
def guard_job_dir(tmp_path):
    jd = tmp_path / "job_guard"
    jd.mkdir()
    init_progress_v3(str(jd), "job_guard")
    return jd


class TestNodePreviouslyFailed:
    """Captures node-status sentinel ONLY when it is currently `failed`.

    Contract: must be called *before* begin_node() otherwise the failure
    is invisible — the guard documented this explicitly.
    """

    def test_returns_false_when_job_dir_missing(self):
        assert _node_previously_failed(None, None) is False
        assert _node_previously_failed(None, "prod_001") is False
        assert _node_previously_failed("/tmp/does_not_exist", None) is False

    def test_returns_false_for_nonexistent_node(self, guard_job_dir):
        # node_id was never created
        assert _node_previously_failed(str(guard_job_dir), "prod_999") is False

    def test_returns_false_when_pending(self, guard_job_dir):
        r = create_node(str(guard_job_dir), "prod")
        assert _node_previously_failed(str(guard_job_dir), r["node_id"]) is False

    def test_returns_false_when_running(self, guard_job_dir):
        r = create_node(str(guard_job_dir), "prod")
        begin_node(str(guard_job_dir), r["node_id"])
        assert _node_previously_failed(str(guard_job_dir), r["node_id"]) is False

    def test_returns_false_when_completed(self, guard_job_dir):
        r = create_node(str(guard_job_dir), "prod")
        complete_node(str(guard_job_dir), r["node_id"], artifacts={})
        assert _node_previously_failed(str(guard_job_dir), r["node_id"]) is False

    def test_returns_true_when_failed(self, guard_job_dir):
        r = create_node(str(guard_job_dir), "prod")
        begin_node(str(guard_job_dir), r["node_id"])
        fail_node(str(guard_job_dir), r["node_id"], errors=["boom"])
        assert _node_previously_failed(str(guard_job_dir), r["node_id"]) is True


class TestResolveDcdAppendMode:
    """Append decision + stale-artifact cleanup logic.

    Covers the three cases the guard is designed to handle:
    1. Fresh run (no trajectory yet) — never append, never touch files.
    2. Valid partial DCD + running/completed node — legacy mid-run
       restart path, append preserved.
    3. Invalid DCD OR failed status — delete stale artifacts, fall back
       to fresh write, emit a warning string the caller can surface.
    """

    def _write_valid_dcd(self, p: Path, extra_bytes: int = 100) -> None:
        p.write_bytes(_DCD_MAGIC + b"\x00" * extra_bytes)

    def test_fresh_run_never_appends(self, tmp_path):
        """append_requested=False (no restart_from) → do_append=False."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        do_append, warning, removed = _resolve_dcd_append_mode(
            traj, en, append_requested=False, prior_failed=False
        )
        assert do_append is False
        assert warning is None
        assert removed == []

    def test_restart_but_no_existing_dcd(self, tmp_path):
        """append_requested=True, file does not exist → no append, no cleanup."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        do_append, warning, removed = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=False
        )
        assert do_append is False
        assert warning is None
        assert removed == []

    def test_valid_dcd_running_status_preserves_legacy_append(self, tmp_path):
        """Mid-run restart into a still-running node: legacy path kept."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        self._write_valid_dcd(traj)
        en.write_text("step kJ/mol\n0 -100\n")

        do_append, warning, removed = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=False
        )
        assert do_append is True
        assert warning is None
        assert removed == []
        # Files are NOT touched
        assert traj.exists() and traj.stat().st_size > 0
        assert en.exists() and en.stat().st_size > 0

    def test_failed_status_discards_even_valid_dcd(self, tmp_path):
        """A failed retry must not silently append, even if the DCD is
        syntactically valid — we can't guarantee step/frame alignment
        with the checkpoint that was written independently."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        self._write_valid_dcd(traj)
        en.write_text("step kJ/mol\n0 -100\n")

        do_append, warning, removed = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=True
        )
        assert do_append is False
        assert warning is not None
        assert "failed status" in warning
        assert removed == [traj, en]
        assert not traj.exists()
        assert not en.exists()

    def test_zero_byte_dcd_is_discarded(self, tmp_path):
        """The actual Google-Drive-sync reproducer: running status (or
        unknown), but the file is 0 bytes → invalid header → cleanup."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        traj.write_bytes(b"")
        en.write_bytes(b"")

        do_append, warning, removed = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=False
        )
        assert do_append is False
        assert warning is not None
        assert "invalid/empty DCD header" in warning
        assert not traj.exists()
        assert not en.exists()

    def test_missing_energy_file_still_cleans_existing_dcd(self, tmp_path):
        """If stale DCD exists but energy.dat was already cleaned up by
        hand, the unlink of the missing energy is a no-op (inside the
        loop's `if stale.exists()` guard) and the DCD is still removed."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        traj.write_bytes(b"")  # invalid

        do_append, warning, removed = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=False
        )
        assert do_append is False
        assert warning is not None
        assert not traj.exists()
        assert not en.exists()
        assert removed == [traj, en]  # returned even when energy was absent

    def test_warning_message_names_the_reason(self, tmp_path):
        """The warning text differentiates failed-status from invalid-header
        so users can tell which sentinel fired."""
        traj = tmp_path / "trajectory.dcd"
        en = tmp_path / "energy.dat"
        self._write_valid_dcd(traj)

        _, w_failed, _ = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=True
        )
        assert "failed status" in w_failed
        assert "invalid/empty DCD header" not in w_failed

        # Re-create the files after the prior test cleaned them up
        traj.write_bytes(b"")
        en.write_bytes(b"")

        _, w_invalid, _ = _resolve_dcd_append_mode(
            traj, en, append_requested=True, prior_failed=False
        )
        assert "invalid/empty DCD header" in w_invalid
        assert "failed status" not in w_invalid


class TestEnsembleMismatchDetection:
    """Cover the guardrail that catches an NPT-equilibrated state.xml
    being loaded into a prod context that has no MonteCarloBarostat —
    OpenMM's loadState would otherwise raise a confusing
    ``setParameter() with invalid parameter name: MonteCarloPressure``.

    The helper takes a ``state_xml_path`` (real file) and a
    ``system_has_barostat`` bool so the test stays free of OpenMM."""

    def _write_state(self, path: Path, *, with_barostat: bool) -> None:
        body = (
            '<?xml version="1.0" ?>\n'
            '<State openmmVersion="8.2.0">\n'
            '  <Positions>...</Positions>\n'
        )
        if with_barostat:
            body += '  <Parameter name="MonteCarloPressure" value="101325.0"/>\n'
        body += '</State>\n'
        path.write_text(body)

    def test_npt_state_into_nvt_system_returns_kind(self, tmp_path):
        """State has barostat parameters but the System has none →
        hard-fail kind tag so caller emits a structured error before
        loadState would explode."""
        p = tmp_path / "eq_state.xml"
        self._write_state(p, with_barostat=True)
        assert (
            _detect_ensemble_mismatch(p, system_has_barostat=False)
            == "npt_state_nvt_system"
        )

    def test_nvt_state_into_npt_system_returns_kind(self, tmp_path):
        """System has a barostat but state has no NPT parameters →
        soft-warning kind tag (the simulation can still run; the
        initial pressure may not match the eq's final state)."""
        p = tmp_path / "eq_state.xml"
        self._write_state(p, with_barostat=False)
        assert (
            _detect_ensemble_mismatch(p, system_has_barostat=True)
            == "nvt_state_npt_system"
        )

    def test_matched_npt_returns_none(self, tmp_path):
        """Both have a barostat → matched, no warning."""
        p = tmp_path / "eq_state.xml"
        self._write_state(p, with_barostat=True)
        assert (
            _detect_ensemble_mismatch(p, system_has_barostat=True) is None
        )

    def test_matched_nvt_returns_none(self, tmp_path):
        """Neither has a barostat → matched, no warning."""
        p = tmp_path / "eq_state.xml"
        self._write_state(p, with_barostat=False)
        assert (
            _detect_ensemble_mismatch(p, system_has_barostat=False) is None
        )


class TestRestartFromErrorFailsNode:
    """Issue 1 regression: ``run_production`` used to early-return
    ``{"success": False, ...}`` without flipping the prod node out of
    ``pending`` when ``resolve_node_inputs`` returned a
    ``restart_from_error``. That left the DAG silently lying about
    whether the node was attempted, breaking re-entry semantics. The
    fix calls ``begin_node`` + ``fail_node`` before returning."""

    def test_continue_from_missing_artifact_marks_node_failed(
        self, tmp_path
    ):
        jd = tmp_path / "job"
        jd.mkdir()
        init_progress_v3(str(jd))
        # Build the bare minimum DAG: an "anchor" prod (prod_001) with
        # parents stubbed via an empty-but-completed chain. We don't
        # need real parm7/state — the early-return for
        # restart_from_error fires before any artifact is opened.
        # parents of prod_001 are required to construct continued_from
        # validation, so we synthesize a minimal source→prep→solv→topo→eq
        # spine and complete eq with a state artifact (so prod_001 is a
        # plausible "anchor" prod), then create prod_001 and a child
        # prod_002 that --continues-from prod_001 *without* prod_001
        # ever finishing — i.e. prod_001 has no state/checkpoint.
        for nt, parents in [
            ("source", None),
            ("prep", ["source_001"]),
            ("solv", ["prep_001"]),
            ("topo", ["solv_001"]),
            ("eq", ["topo_001"]),
        ]:
            create_node(str(jd), nt, parent_node_ids=parents)
        complete_node(str(jd), "source_001",
                      artifacts={"structure_file": "x.cif"})
        complete_node(str(jd), "prep_001",
                      artifacts={"merged_pdb": "x.pdb"})
        complete_node(str(jd), "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        complete_node(str(jd), "topo_001",
                      artifacts={"parm7": "artifacts/system.parm7",
                                 "rst7": "artifacts/system.rst7"})
        complete_node(str(jd), "eq_001",
                      artifacts={"state": "artifacts/equilibrated.xml"},
                      metadata={"final_step": 0})
        # prod_001 is the anchor — exists but never ran (no artifacts).
        create_node(str(jd), "prod", parent_node_ids=["eq_001"])
        # prod_002 continues from prod_001; resolver must refuse to auto-resolve
        # because prod_001 is not completed.
        create_node(str(jd), "prod", continue_from="prod_001")

        result = run_production(
            simulation_time_ns=0.001,
            job_dir=str(jd),
            node_id="prod_002",
        )

        # Returned dict reports the failure.
        assert result["success"] is False
        assert any("prod_001" in e and "completed" in e for e in result["errors"])

        # Node status must reflect the failure (was the bug: status
        # used to stay "pending"). Read node.json directly. fail_node
        # stores errors under metadata.errors (no top-level errors key).
        import json
        nj = json.loads(
            (jd / "nodes" / "prod_002" / "node.json").read_text()
        )
        assert nj["status"] == "failed"
        recorded_errors = nj.get("metadata", {}).get("errors", [])
        assert any("prod_001" in e and "completed" in e for e in recorded_errors), (
            f"node.json metadata.errors should mention the unfinished parent: "
            f"got {recorded_errors!r}"
        )
