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
    _node_previously_failed,
    _resolve_dcd_append_mode,
)
from mdclaw._node import (
    begin_node,
    complete_node,
    create_node,
    fail_node,
    init_progress_v3,
)


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
