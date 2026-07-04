"""Unit tests for pure helpers extracted from md_simulation_server.

These helpers are callable without OpenMM so they run in the default
non-slow pytest lane. See test_server_smoke.py for the full simulation
smoke tests.
"""

import sys
import re
import types
from pathlib import Path

import pytest

from mdclaw.simulation.equilibrate import run_equilibration
from mdclaw.simulation.integrator_plan import (
    _compute_step_plan,
    _equilibration_steps_from_time_ns,
    _resolve_equilibration_stage_steps,
)
from mdclaw.simulation.platform import inspect_openmm_platforms
from mdclaw.simulation.production import run_production
from mdclaw.simulation.restart import (
    _DCD_MAGIC,
    _dcd_has_valid_header,
    _detect_ensemble_mismatch,
    _node_previously_failed,
    _resolve_dcd_append_mode,
)
from mdclaw._node import (
    begin_node,
    create_node,
    fail_node,
    init_progress_v3,
)
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node


def test_inspect_openmm_platforms_reports_only_context_usable_platforms(monkeypatch):
    class FakePlatformObj:
        def __init__(self, name, speed):
            self._name = name
            self._speed = speed

        def getName(self):
            return self._name

        def getSpeed(self):
            return self._speed

    fake_platforms = [
        FakePlatformObj("Reference", 1.0),
        FakePlatformObj("CPU", 10.0),
        FakePlatformObj("OpenCL", 50.0),
    ]

    class FakePlatform:
        @staticmethod
        def getNumPlatforms():
            return len(fake_platforms)

        @staticmethod
        def getPlatform(index):
            return fake_platforms[index]

        @staticmethod
        def getPlatformByName(name):
            for platform in fake_platforms:
                if platform.getName() == name:
                    return platform
            raise ValueError(name)

    class FakeSystem:
        def addParticle(self, _mass):
            return None

    class FakeVerletIntegrator:
        def __init__(self, _timestep):
            pass

    class FakeContext:
        def __init__(self, _system, _integrator, platform=None):
            platform = platform or FakePlatform.getPlatformByName("CPU")
            if platform.getName() == "OpenCL":
                raise RuntimeError("No compatible OpenCL platform is available")
            self._platform = platform

        def getPlatform(self):
            return self._platform

    fake_openmm = types.SimpleNamespace(
        Context=FakeContext,
        Platform=FakePlatform,
        System=FakeSystem,
        VerletIntegrator=FakeVerletIntegrator,
        unit=types.SimpleNamespace(femtoseconds=1.0),
    )
    monkeypatch.setitem(sys.modules, "openmm", fake_openmm)

    result = inspect_openmm_platforms(atom_count=10000, solvent_type="explicit")

    assert result["success"] is True
    assert result["platforms"] == ["Reference", "CPU"]
    assert result["gpu_platforms"] == []
    assert result["fastest_platform"] == "CPU"
    assert result["default_platform"] == "CPU"
    assert result["local_feasibility"] == "slow_on_cpu"
    assert result["unusable_platforms"] == [
        {
            "platform": "OpenCL",
            "error": (
                "RuntimeError: No compatible OpenCL platform is available"
            ),
        }
    ]
    assert any("OpenCL is registered" in warning for warning in result["warnings"])


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


class TestEquilibrationTimeResolution:
    """Covers user-facing equilibration duration flags.

    Weak agents should not need to convert ns to MD steps themselves; the
    tool resolves the requested time against the active timestep.
    """

    def test_time_ns_uses_four_fs_hmr_default_arithmetic(self):
        assert _equilibration_steps_from_time_ns(0.1, 4.0) == 25_000

    def test_time_ns_uses_two_fs_non_hmr_arithmetic(self):
        assert _equilibration_steps_from_time_ns(0.1, 2.0) == 50_000

    def test_stage_resolution_prefers_time_flag_without_agent_math(self):
        steps, requested_time_ns, effective_time_ns = _resolve_equilibration_stage_steps(
            stage_name="nvt",
            steps=None,
            time_ns=0.1,
            default_steps=250_000,
            timestep_fs=4.0,
        )
        assert steps == 25_000
        assert requested_time_ns == pytest.approx(0.1)
        assert effective_time_ns == pytest.approx(0.1)

    def test_stage_resolution_preserves_legacy_default_steps(self):
        steps, requested_time_ns, effective_time_ns = _resolve_equilibration_stage_steps(
            stage_name="nvt",
            steps=None,
            time_ns=None,
            default_steps=250_000,
            timestep_fs=4.0,
        )
        assert steps == 250_000
        assert requested_time_ns is None
        assert effective_time_ns == pytest.approx(1.0)

    def test_stage_resolution_rejects_time_and_steps_together(self):
        with pytest.raises(ValueError, match="specify either nvt_time_ns or nvt_steps"):
            _resolve_equilibration_stage_steps(
                stage_name="nvt",
                steps=50_000,
                time_ns=0.1,
                default_steps=250_000,
                timestep_fs=4.0,
            )

    def test_stage_resolution_rejects_sub_step_positive_time(self):
        with pytest.raises(ValueError, match="shorter than one integration step"):
            _equilibration_steps_from_time_ns(1e-12, 4.0)


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
    """Classify barostat / saved-state inconsistencies for warning purposes.

    The ensemble-agnostic loader transfers only positions/velocities/box
    and never touches Context parameters, so neither mismatch case
    raises. The helper now drives a soft warning either way (still
    safe to load) — the kind tag distinguishes whether the new run is
    dropping NPT info or starting an NPT system from an NVT-saved state.
    """

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
        """NPT saved state into NVT system: barostat parameters dropped,
        warning tag returned."""
        p = tmp_path / "eq_state.xml"
        self._write_state(p, with_barostat=True)
        assert (
            _detect_ensemble_mismatch(p, system_has_barostat=False)
            == "npt_state_nvt_system"
        )

    def test_nvt_state_into_npt_system_returns_kind(self, tmp_path):
        """NVT saved state into NPT system: new barostat starts in
        default state and re-equilibrates volume — warning tag."""
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


class TestLoadStateIntoSimulation:
    """Unit coverage for the ensemble-agnostic state loader.

    The integration tests in ``test_server_smoke.py`` and
    ``test_pipeline_eq_chain_dag.py`` exercise this through
    ``run_equilibration`` / ``run_production`` end-to-end; these cases
    isolate the loader on a tiny periodic LJ Argon system so the round-trip
    of positions / velocities / box and the barostat-drop behaviour are
    asserted directly.
    """

    @pytest.fixture(autouse=True)
    def _require_openmm(self):
        pytest.importorskip("openmm")

    def _build_lj_simulation(self, *, with_barostat: bool):
        from openmm import (
            LangevinMiddleIntegrator,
            MonteCarloBarostat,
            NonbondedForce,
            System,
            Vec3,
        )
        from openmm.app import Element, Simulation, Topology
        from openmm.unit import (
            bar,
            dalton,
            femtoseconds,
            kelvin,
            nanometer,
            picosecond,
        )

        n_particles = 8
        box_nm = 6.0

        system = System()
        nb = NonbondedForce()
        for _ in range(n_particles):
            system.addParticle(40.0 * dalton)
            nb.addParticle(0.0, 0.34, 0.99)
        nb.setNonbondedMethod(NonbondedForce.CutoffPeriodic)
        nb.setCutoffDistance(0.9 * nanometer)
        system.addForce(nb)
        system.setDefaultPeriodicBoxVectors(
            Vec3(box_nm, 0, 0) * nanometer,
            Vec3(0, box_nm, 0) * nanometer,
            Vec3(0, 0, box_nm) * nanometer,
        )
        if with_barostat:
            system.addForce(MonteCarloBarostat(1.0 * bar, 300.0 * kelvin))

        topology = Topology()
        chain = topology.addChain()
        argon = Element.getBySymbol("Ar")
        for i in range(n_particles):
            res = topology.addResidue("AR", chain)
            topology.addAtom(f"AR{i}", argon, res)

        integrator = LangevinMiddleIntegrator(
            300 * kelvin, 1.0 / picosecond, 2 * femtoseconds
        )
        return Simulation(topology, system, integrator)

    def _seed_positions_velocities(self, sim):
        """Place 8 atoms on a 0.5 nm grid line and seed thermal velocities."""
        from openmm import Vec3
        from openmm.unit import kelvin, nanometer

        positions = [Vec3(i * 0.5, 0, 0) for i in range(8)] * nanometer
        sim.context.setPositions(positions)
        sim.context.setVelocitiesToTemperature(300 * kelvin)

    def test_xml_transfers_positions_velocities_box(self, tmp_path):
        """saveState → _load_state_into_simulation round-trips
        positions/velocities/box exactly when the target System has the
        same Force composition. Barostat presence is handled in a
        separate test."""
        import numpy as np
        from openmm.unit import nanometer, picosecond

        from mdclaw.simulation.restart import _load_state_into_simulation

        src = self._build_lj_simulation(with_barostat=False)
        self._seed_positions_velocities(src)
        src.step(2)
        state_path = tmp_path / "src.xml"
        src.saveState(str(state_path))

        src_state = src.context.getState(getPositions=True, getVelocities=True)
        src_pos = src_state.getPositions(asNumpy=True).value_in_unit(nanometer)
        src_vel = src_state.getVelocities(asNumpy=True).value_in_unit(
            nanometer / picosecond
        )
        src_box = [
            v.value_in_unit(nanometer)
            for v in src_state.getPeriodicBoxVectors()
        ]

        target = self._build_lj_simulation(with_barostat=False)
        info = _load_state_into_simulation(target, state_path, is_periodic=True)
        assert info["format"] == "xml"

        tgt_state = target.context.getState(
            getPositions=True, getVelocities=True
        )
        tgt_pos = tgt_state.getPositions(asNumpy=True).value_in_unit(nanometer)
        tgt_vel = tgt_state.getVelocities(asNumpy=True).value_in_unit(
            nanometer / picosecond
        )
        tgt_box = [
            v.value_in_unit(nanometer)
            for v in tgt_state.getPeriodicBoxVectors()
        ]

        np.testing.assert_allclose(tgt_pos, src_pos, atol=1e-6)
        np.testing.assert_allclose(tgt_vel, src_vel, atol=1e-6)
        for sv, tv in zip(src_box, tgt_box):
            np.testing.assert_allclose(
                [c for c in tv], [c for c in sv], atol=1e-6
            )

    def test_xml_npt_state_into_nvt_system_does_not_raise(self, tmp_path):
        """NPT-saved state contains a ``MonteCarloPressure`` Context
        parameter; loading via the legacy ``simulation.loadState`` would
        raise ``setParameter() with invalid parameter name``. The
        ensemble-agnostic loader transfers only positions/velocities/box
        and skips Context parameters, so the same XML resumes cleanly
        into a barostat-free System and a step succeeds."""
        from mdclaw.simulation.restart import _load_state_into_simulation

        npt = self._build_lj_simulation(with_barostat=True)
        self._seed_positions_velocities(npt)
        npt.step(2)
        state_path = tmp_path / "npt.xml"
        npt.saveState(str(state_path))

        # Sanity check: the saved XML records the barostat parameter.
        assert "MonteCarloPressure" in state_path.read_text()

        nvt = self._build_lj_simulation(with_barostat=False)
        info = _load_state_into_simulation(nvt, state_path, is_periodic=True)
        assert info["format"] == "xml"
        # Step must succeed — would raise on the dropped barostat parameter
        # under the legacy loadState path.
        nvt.step(1)

    def test_xml_state_without_velocities_rethermalizes(self, tmp_path):
        from openmm.unit import nanometer, picosecond

        from mdclaw.simulation.restart import _load_state_into_simulation

        src = self._build_lj_simulation(with_barostat=False)
        self._seed_positions_velocities(src)
        state_path = tmp_path / "positions_only.xml"
        src.saveState(str(state_path))
        state_path.write_text(
            re.sub(
                r"\s*<Velocities>.*?</Velocities>",
                "",
                state_path.read_text(),
                flags=re.DOTALL,
            )
        )

        target = self._build_lj_simulation(with_barostat=False)
        info = _load_state_into_simulation(
            target,
            state_path,
            is_periodic=True,
            temperature_kelvin=300.0,
            random_seed=123,
        )

        assert info["format"] == "xml"
        assert info["velocities_present"] is False
        assert info["velocities_rethermalized"] is True
        state = target.context.getState(getVelocities=True)
        velocities = state.getVelocities(asNumpy=True).value_in_unit(
            nanometer / picosecond
        )
        assert abs(velocities).sum() > 0
        target.step(1)

    def test_xml_particle_count_mismatch_rejected(self, tmp_path):
        from mdclaw.simulation.restart import _load_state_into_simulation

        src = self._build_lj_simulation(with_barostat=False)
        self._seed_positions_velocities(src)
        state_path = tmp_path / "bad_particle_count.xml"
        src.saveState(str(state_path))
        state_path.write_text(
            re.sub(
                r"\s*<Position [^>]*/>",
                "",
                state_path.read_text(),
                count=1,
            )
        )

        target = self._build_lj_simulation(with_barostat=False)
        with pytest.raises(ValueError, match="Restart state could not be deserialized"):
            _load_state_into_simulation(target, state_path, is_periodic=True)

    def test_xml_velocity_count_mismatch_is_hard_failure(
        self, monkeypatch, tmp_path
    ):
        from openmm import XmlSerializer

        from mdclaw.simulation.restart import _load_state_into_simulation

        src = self._build_lj_simulation(with_barostat=False)
        self._seed_positions_velocities(src)
        state_path = tmp_path / "bad_velocity_count.xml"
        state_path.write_text("<State/>")
        state = src.context.getState(getPositions=True, getVelocities=True)
        positions = state.getPositions()
        velocities = list(state.getVelocities())[:-1]

        class FakeState:
            def getPositions(self):
                return positions

            def getVelocities(self):
                return velocities

        monkeypatch.setattr(
            XmlSerializer,
            "deserialize",
            staticmethod(lambda _text: FakeState()),
        )

        target = self._build_lj_simulation(with_barostat=False)
        with pytest.raises(ValueError, match="Restart state velocity count mismatch"):
            _load_state_into_simulation(target, state_path, is_periodic=True)

    def test_chk_path_takes_load_checkpoint_branch(self, tmp_path):
        """Binary checkpoint route requires identical System layout but
        is the fast same-GPU bit-exact path. Verify the loader returns
        ``"checkpoint"`` and the target context advances after the call."""
        from mdclaw.simulation.restart import _load_state_into_simulation

        src = self._build_lj_simulation(with_barostat=False)
        self._seed_positions_velocities(src)
        src.step(2)
        chk_path = tmp_path / "src.chk"
        src.saveCheckpoint(str(chk_path))

        target = self._build_lj_simulation(with_barostat=False)
        info = _load_state_into_simulation(target, chk_path, is_periodic=True)
        assert info["format"] == "checkpoint"
        # And the resumed context must be steppable.
        target.step(1)


class TestRestartPersistenceHelpers:
    def test_restart_random_seed_offsets_and_avoids_zero(self):
        from mdclaw.simulation.restart import _restart_random_seed

        assert _restart_random_seed(None, 100) is None
        assert _restart_random_seed(42, 0) == 43
        assert _restart_random_seed(42, 100) == 142

    def test_atomic_checkpoint_save_cleans_temp_on_failure(self, tmp_path):
        from mdclaw.simulation.restart import _save_checkpoint_atomic

        out = tmp_path / "checkpoint.chk"

        class FakeSimulation:
            def saveCheckpoint(self, path):
                Path(path).write_text("partial")
                raise RuntimeError("disk full")

        with pytest.raises(RuntimeError, match="disk full"):
            _save_checkpoint_atomic(FakeSimulation(), out)

        assert not out.exists()
        assert list(tmp_path.glob(".*.tmp.*")) == []


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
        # parents stubbed via an empty-but-completed chain. The early
        # return for restart_from_error fires before any artifact is
        # actually opened, so the placeholder paths only need to look
        # like the schema-v3 XML triple.
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
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb",
                                 "state_xml": "artifacts/state.xml"})
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


# ----------------------------------------------------------------------------
# XML system contract (HMR / implicit-solvent baked into system.xml).
# ----------------------------------------------------------------------------


class TestRunEquilibrationFailNodeCoverage:
    """run_equilibration must mirror run_production: every early-return
    after the resolver call must transit through fail_node so the eq
    node ends up ``failed`` rather than perpetually ``pending``. This
    covers the ``input_resolution_error`` path (no XML triple on the
    topo ancestor), the ``restart_from_error`` path (a continue_from
    pointing at an unfinished eq/prod), and the explicit ``--restart-from``
    file-missing path."""

    def _seed_minimal_dag(self, jd, *, complete_topo: bool = True):
        # ``complete_node`` (alias for the placeholder-writing helper at
        # the top of this file) tolerates missing artifact files and only
        # touches the relative paths so node validation passes.
        from mdclaw._node import (
            create_node as _create,
            init_progress_v3,
        )
        init_progress_v3(str(jd))
        _create(str(jd), "source")
        complete_node(str(jd), "source_001", {"structure_file": "x.cif"})
        _create(str(jd), "prep", parent_node_ids=["source_001"])
        complete_node(str(jd), "prep_001", {"merged_pdb": "x.pdb"})
        _create(str(jd), "solv", parent_node_ids=["prep_001"])
        complete_node(str(jd), "solv_001",
                      {"solvated_pdb": "x.pdb", "box_dimensions": "x.json"})
        _create(str(jd), "topo", parent_node_ids=["solv_001"])
        if complete_topo:
            complete_node(str(jd), "topo_001",
                          {"system_xml": "artifacts/system.xml",
                           "topology_pdb": "artifacts/topology.pdb",
                           "state_xml": "artifacts/state.xml"})

    def test_input_resolution_error_marks_eq_node_failed(self, tmp_path):
        """Topo ancestor never completed → ``input_resolution_error``.
        Before the fix the eq node stayed in ``pending``."""
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation.equilibrate import run_equilibration

        jd = tmp_path / "job"
        jd.mkdir()
        # Topo deliberately left incomplete.
        self._seed_minimal_dag(jd, complete_topo=False)
        create_node(str(jd), "eq", parent_node_ids=["topo_001"])

        result = run_equilibration(
            job_dir=str(jd), node_id="eq_001",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "input_resolution_blocked"
        eq_node = read_node(str(jd), "eq_001")
        assert eq_node["status"] == "failed", eq_node["status"]

    def test_eq_chain_unfinished_parent_marks_node_failed(self, tmp_path):
        """eq → eq chaining: when the new eq's direct eq parent is still
        pending (no ``state`` artifact yet), the resolver returns an
        ``input_resolution_error`` and the new eq node must be flipped
        to ``failed``."""
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation.equilibrate import run_equilibration

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_minimal_dag(jd, complete_topo=True)
        # eq_001 exists as an anchor parent but never finished.
        create_node(str(jd), "eq", parent_node_ids=["topo_001"])
        create_node(str(jd), "eq", parent_node_ids=["eq_001"])

        result = run_equilibration(
            job_dir=str(jd), node_id="eq_002",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "input_resolution_blocked"
        eq_node = read_node(str(jd), "eq_002")
        assert eq_node["status"] == "failed", eq_node["status"]

    def test_explicit_restart_from_missing_marks_eq_node_failed(
        self, tmp_path
    ):
        """A literal ``--restart-from /nope.xml`` that does not exist on
        disk must flip the eq node to ``failed`` (and propagate the
        structured error)."""
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation.equilibrate import run_equilibration

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_minimal_dag(jd, complete_topo=True)
        # Place placeholder XML triple files on disk so the file-existence
        # checks in run_equilibration do not short-circuit before the
        # restart_path check fires.
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / "system.xml").write_text("<placeholder/>")
        (topo_artifacts / "topology.pdb").write_text("REMARK\nEND\n")
        (topo_artifacts / "state.xml").write_text("<placeholder/>")
        create_node(str(jd), "eq", parent_node_ids=["topo_001"])

        result = run_equilibration(
            job_dir=str(jd), node_id="eq_001",
            restart_from=str(jd / "definitely_not_a_state.xml"),
        )
        assert result.get("success", False) is False
        assert any(
            "Restart file not found" in e
            for e in result.get("errors", [])
        )
        eq_node = read_node(str(jd), "eq_001")
        assert eq_node["status"] == "failed", eq_node["status"]

    def test_eq_chain_completed_empty_eq_parent_marks_node_failed(
        self, tmp_path,
    ):
        """eq → eq chaining: when the eq parent is *completed* but
        registered no ``state`` / ``checkpoint`` artifact, the resolver
        surfaces ``restart_from_error`` (not ``input_resolution_error``)
        and the new eq node must be flipped to ``failed`` rather than
        silently rolling back to the topo state.xml."""
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation.equilibrate import run_equilibration

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_minimal_dag(jd, complete_topo=True)
        # eq_001 completed without writing state / checkpoint — only a
        # final_structure landed on the node. This is a broken-DAG
        # signal the resolver must surface, not paper over.
        create_node(str(jd), "eq", parent_node_ids=["topo_001"])
        complete_node(str(jd), "eq_001",
                      {"final_structure": "artifacts/equilibrated.pdb"})
        create_node(str(jd), "eq", parent_node_ids=["eq_001"])

        result = run_equilibration(
            job_dir=str(jd), node_id="eq_002",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "restart_from_unavailable"
        eq_node = read_node(str(jd), "eq_002")
        assert eq_node["status"] == "failed", eq_node["status"]


class TestRunEquilibrationTimeFlags:
    """Public API guards for weak-agent-safe equilibration durations."""

    def test_rejects_time_and_steps_for_same_stage_before_openmm(self):
        from mdclaw.simulation.equilibrate import run_equilibration

        result = run_equilibration(
            system_xml_file="missing.xml",
            topology_pdb_file="missing.pdb",
            nvt_time_ns=0.1,
            nvt_steps=50_000,
        )
        assert result["success"] is False
        assert result["code"] == "equilibration_time_step_conflict"
        assert "nvt_time_ns or nvt_steps" in result["message"]

    def test_declared_time_conditions_validate_against_resolved_steps(
        self, tmp_path,
    ):
        """Time conditions are checked against resolved runtime values.

        The call stops at XML parsing, after node context validation, so the
        test stays light while still covering the DAG condition contract.
        """
        from mdclaw._node import create_node
        from mdclaw.simulation.equilibrate import run_equilibration

        jd = tmp_path / "job"
        jd.mkdir()
        TestRunEquilibrationFailNodeCoverage()._seed_minimal_dag(
            jd, complete_topo=True,
        )
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / "system.xml").write_text("<placeholder/>")
        (topo_artifacts / "topology.pdb").write_text("REMARK\nEND\n")
        (topo_artifacts / "state.xml").write_text("<placeholder/>")
        create_node(
            str(jd),
            "eq",
            parent_node_ids=["topo_001"],
            conditions={
                "temperature_kelvin": 300.0,
                "pressure_bar": 1.0,
                "nvt_time_ns": 0.1,
                "npt_time_ns": 0.1,
                "nvt_steps": 25_000,
                "npt_steps": 25_000,
            },
        )

        result = run_equilibration(
            job_dir=str(jd),
            node_id="eq_001",
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_time_ns=0.1,
            npt_time_ns=0.1,
        )
        assert result["success"] is False
        assert result.get("code") != "node_execution_context_invalid"
        assert not any("condition" in error for error in result.get("errors", []))


class TestXMLTriplePartialOutputs:
    """Run-side consumers must not treat partial XML triples as success."""

    def test_equilibration_rejects_missing_state_xml_when_path_is_provided(
        self, tmp_path
    ):
        system_xml = tmp_path / "system.xml"
        topology_pdb = tmp_path / "topology.pdb"
        state_xml = tmp_path / "state.xml"
        system_xml.write_text("<placeholder/>")
        topology_pdb.write_text("REMARK fake\nEND\n")

        result = run_equilibration(
            system_xml_file=str(system_xml),
            topology_pdb_file=str(topology_pdb),
            state_xml_file=str(state_xml),
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert any("state.xml not found" in error for error in result["errors"])

    def test_production_rejects_missing_state_xml_when_path_is_provided(
        self, tmp_path
    ):
        system_xml = tmp_path / "system.xml"
        topology_pdb = tmp_path / "topology.pdb"
        state_xml = tmp_path / "state.xml"
        system_xml.write_text("<placeholder/>")
        topology_pdb.write_text("REMARK fake\nEND\n")

        result = run_production(
            system_xml_file=str(system_xml),
            topology_pdb_file=str(topology_pdb),
            state_xml_file=str(state_xml),
            simulation_time_ns=0.001,
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert any("state.xml not found" in error for error in result["errors"])


class TestXMLSystemContractValidation:
    """``_validate_xml_system_contract`` is the run-side validator for the
    XML triple emitted by ``build_amber_system`` / ``build_openmm_system``:
    each NVT / NPT / clean-handoff / production stage deserializes
    ``system.xml`` into a fresh ``openmm.System`` via
    ``_deserialize_xml_system`` and runs this validator before mutating
    the System or starting the integrator. Stable failure codes
    (``modern_system_hmr_mismatch``,
    ``modern_system_implicit_solvent_unsupported``)."""

    def _build_minimal_system(self, *, hmr: bool, implicit: bool, tmp_path):
        """Create a 1-residue ALA topology + a System optionally with HMR /
        a GBSA-OBC force. The serialized system.xml is the moral
        equivalent of what ``build_amber_system`` would emit; we only
        need it to exercise the contract check."""
        pytest.importorskip("openmm")
        from openmm import (
            HarmonicBondForce,
            NonbondedForce,
            System,
            XmlSerializer,
            CustomGBForce,
        )
        from openmm.app import Element, Topology
        from openmm.unit import dalton, nanometer

        top = Topology()
        chain = top.addChain("A")
        res = top.addResidue("ALA", chain, "1")
        n = top.addAtom("N", Element.getBySymbol("N"), res)
        ca = top.addAtom("CA", Element.getBySymbol("C"), res)
        h = top.addAtom("H", Element.getBySymbol("H"), res)
        top.addBond(n, ca)
        top.addBond(n, h)

        system = System()
        # N
        system.addParticle(14.003 * dalton)
        # CA
        system.addParticle(12.000 * dalton)
        # H — 4 amu under HMR, 1.008 amu otherwise.
        system.addParticle((4.0 if hmr else 1.008) * dalton)
        # Bond / NB forces are required only to make the deserialize round trip
        # exercise a non-trivial XML.
        system.addForce(HarmonicBondForce())
        nb = NonbondedForce()
        for _ in range(3):
            nb.addParticle(0.0, 0.1 * nanometer, 0.0)
        system.addForce(nb)
        if implicit:
            system.addForce(CustomGBForce())

        xml_path = tmp_path / "system.xml"
        xml_path.write_text(XmlSerializer.serialize(system))
        return top, system

    def test_validator_accepts_matching_hmr(self, tmp_path):
        from mdclaw.simulation.xml_contract import _validate_xml_system_contract

        topology, system = self._build_minimal_system(
            hmr=True, implicit=False, tmp_path=tmp_path,
        )
        # No raise expected — the deserialized System has H=4 amu, which
        # is what the run-time ``hmr=True`` request asked for.
        _validate_xml_system_contract(
            system, topology,
            hmr_request=True, implicit_solvent_request=None,
        )

    def test_validator_rejects_hmr_request_against_non_hmr_system(self, tmp_path):
        from mdclaw.simulation.xml_contract import (
            _ModernSystemContractError,
            _validate_xml_system_contract,
        )

        topology, system = self._build_minimal_system(
            hmr=False, implicit=False, tmp_path=tmp_path,
        )
        with pytest.raises(_ModernSystemContractError) as exc_info:
            _validate_xml_system_contract(
                system, topology,
                hmr_request=True, implicit_solvent_request=None,
            )
        assert exc_info.value.code == "modern_system_hmr_mismatch"

    def test_validator_rejects_no_hmr_request_against_hmr_system(self, tmp_path):
        """The reverse direction: requesting standard hydrogen masses
        against a System whose H atoms were already repartitioned to
        4 amu would silently mis-simulate at 2 fs without this guard."""
        from mdclaw.simulation.xml_contract import (
            _ModernSystemContractError,
            _validate_xml_system_contract,
        )

        topology, system = self._build_minimal_system(
            hmr=True, implicit=False, tmp_path=tmp_path,
        )
        with pytest.raises(_ModernSystemContractError) as exc_info:
            _validate_xml_system_contract(
                system, topology,
                hmr_request=False, implicit_solvent_request=None,
            )
        assert exc_info.value.code == "modern_system_hmr_mismatch"

    def test_validator_rejects_implicit_request_against_non_gb_system(self, tmp_path):
        from mdclaw.simulation.xml_contract import (
            _ModernSystemContractError,
            _validate_xml_system_contract,
        )

        topology, system = self._build_minimal_system(
            hmr=False, implicit=False, tmp_path=tmp_path,
        )
        with pytest.raises(_ModernSystemContractError) as exc_info:
            _validate_xml_system_contract(
                system, topology,
                hmr_request=None, implicit_solvent_request="OBC2",
            )
        assert exc_info.value.code == "modern_system_implicit_solvent_unsupported"

    def test_validator_accepts_implicit_request_when_gb_force_present(self, tmp_path):
        from mdclaw.simulation.xml_contract import _validate_xml_system_contract

        topology, system = self._build_minimal_system(
            hmr=False, implicit=True, tmp_path=tmp_path,
        )
        # No raise expected: the deserialized System carries a GB force,
        # so ``--implicit-solvent OBC2`` is satisfiable.
        _validate_xml_system_contract(
            system, topology,
            hmr_request=None, implicit_solvent_request="OBC2",
        )

    def test_validator_default_requests_are_pass_through(self, tmp_path):
        """When neither HMR nor implicit-solvent is being requested at
        run time, the validator must not consult the System."""
        from mdclaw.simulation.xml_contract import _validate_xml_system_contract

        topology, system = self._build_minimal_system(
            hmr=False, implicit=False, tmp_path=tmp_path,
        )
        _validate_xml_system_contract(
            system, topology,
            hmr_request=None, implicit_solvent_request=None,
        )


class TestExplicitRestartFromFinalStepAlignment:
    """An explicit ``--restart-from <path>`` must not silently inherit
    the resolver's auto-resolved ``restart_from_node_id``: that would
    pin ``simulation.currentStep`` to the *DAG-resolved* ancestor's
    ``final_step`` while loading state from a *user-supplied* file. The
    helper ``_resolve_restart_node_id_for_run`` enforces "the artifact
    we load and the metadata we trust must come from the same node":

      - DAG resolver's path → use the resolver's node id (auto-pick).
      - Explicit path that matches a DAG ancestor's
        ``state``/``checkpoint`` artifact → use that ancestor's id.
      - Explicit path with no DAG match → return ``None`` (external
        file; ``read_ancestor_final_step`` will return ``None``,
        ``simulation.currentStep`` stays at whatever the loader sets).
    """

    def _seed_dag(self, jd):
        """Topo → eq → prod DAG with both ``state`` and ``checkpoint``
        written on eq_001. ``prod_001`` is left pending so the helpers
        can be exercised against ``prod_001`` as the run's node id."""
        from mdclaw._node import (
            create_node as _create,
            init_progress_v3,
        )
        init_progress_v3(str(jd))
        _create(str(jd), "source")
        complete_node(str(jd), "source_001", {"structure_file": "x.cif"})
        _create(str(jd), "prep", parent_node_ids=["source_001"])
        complete_node(str(jd), "prep_001", {"merged_pdb": "x.pdb"})
        _create(str(jd), "topo", parent_node_ids=["prep_001"])
        complete_node(
            str(jd), "topo_001",
            {"system_xml": "artifacts/system.xml",
             "topology_pdb": "artifacts/topology.pdb",
             "state_xml": "artifacts/state.xml"},
        )
        _create(str(jd), "eq", parent_node_ids=["topo_001"])
        complete_node(
            str(jd), "eq_001",
            {"state": "artifacts/equilibrated.xml",
             "checkpoint": "artifacts/equilibrated.chk"},
            metadata={"final_step": 250000},
        )
        _create(str(jd), "prod", parent_node_ids=["eq_001"])

    def test_auto_resolved_path_uses_resolver_node_id(self, tmp_path):
        """Sanity: when the user does NOT pass ``restart_from`` and the
        resolver picks eq_001, the helper picks up the resolver's
        ``restart_from_node_id``."""
        from mdclaw.simulation.restart import _resolve_restart_node_id_for_run

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        # Simulated resolver inputs.
        resolver_inputs = {
            "restart_from": str(
                jd / "nodes" / "eq_001" / "artifacts" / "equilibrated.xml"
            ),
            "restart_from_node_id": "eq_001",
        }
        node_id = _resolve_restart_node_id_for_run(
            job_dir=str(jd), node_id="prod_001",
            restart_from=resolver_inputs["restart_from"],
            explicit_restart_from=False,
            inputs=resolver_inputs,
        )
        assert node_id == "eq_001"

    def test_explicit_path_matching_ancestor_artifact_resolves_node_id(
        self, tmp_path,
    ):
        """The explicit path equals eq_001's ``state`` artifact, so the
        helper still binds the step counter to eq_001."""
        from mdclaw.simulation.restart import _resolve_restart_node_id_for_run

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        explicit = str(
            jd / "nodes" / "eq_001" / "artifacts" / "equilibrated.xml"
        )
        node_id = _resolve_restart_node_id_for_run(
            job_dir=str(jd), node_id="prod_001",
            restart_from=explicit,
            explicit_restart_from=True,
            # Resolver inputs intentionally lie (different node) — the
            # helper must ignore them when an explicit path is passed.
            inputs={"restart_from_node_id": "different_node"},
        )
        assert node_id == "eq_001"

    def test_explicit_path_with_no_dag_match_returns_none(self, tmp_path):
        """An external file (e.g. user copied a state.xml from another
        job) does not match any ancestor's artifact. The helper returns
        ``None`` so ``read_ancestor_final_step`` will not bind
        ``simulation.currentStep`` to a DAG ancestor whose state is
        not the one we loaded."""
        from mdclaw.simulation.restart import _resolve_restart_node_id_for_run

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        external_state = jd / "external.xml"
        external_state.write_text("<placeholder/>")
        node_id = _resolve_restart_node_id_for_run(
            job_dir=str(jd), node_id="prod_001",
            restart_from=str(external_state),
            explicit_restart_from=True,
            # The resolver auto-resolved eq_001 — the helper must
            # NOT inherit that node id since the path is external.
            inputs={
                "restart_from": str(
                    jd / "nodes" / "eq_001"
                       / "artifacts" / "equilibrated.xml"
                ),
                "restart_from_node_id": "eq_001",
            },
        )
        assert node_id is None

    def test_helper_picks_resolver_node_id_when_user_omits_restart_from(
        self, tmp_path,
    ):
        """Pair to the explicit-path tests: when the user passes no
        ``restart_from``, the helper trusts the resolver's chosen node
        id verbatim. This is the auto-resolve path used by
        ``eq → prod`` in node mode."""
        from mdclaw.simulation.restart import _resolve_restart_node_id_for_run

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        node_id = _resolve_restart_node_id_for_run(
            job_dir=str(jd), node_id="prod_001",
            restart_from=str(
                jd / "nodes" / "eq_001" / "artifacts" / "equilibrated.xml"
            ),
            explicit_restart_from=False,
            inputs={
                "restart_from": str(
                    jd / "nodes" / "eq_001"
                       / "artifacts" / "equilibrated.xml"
                ),
                "restart_from_node_id": "eq_001",
            },
        )
        assert node_id == "eq_001"

    # -------- Run-function-level (routing wiring) -------------------------
    #
    # The tests above pin the helper's contract. The pair below confirms
    # the wiring inside ``run_production`` / ``run_equilibration``: the
    # ``explicit_restart_from`` flag is computed BEFORE the resolver
    # auto-resolve fallback, and ``_resolve_restart_node_id_for_run`` is
    # invoked with the right combination of (restart_from,
    # explicit_restart_from, inputs). We patch the helper itself with a
    # recorder and let the run abort on the next downstream OpenMM step
    # — that's enough to confirm the routing layer without needing a
    # real System build or trajectory.

    def _capture_helper_call(self):
        captured: dict = {}

        def _wrapper(**kwargs):
            captured.setdefault("calls", []).append(kwargs)
            return None  # safe: real run_* code accepts None
        return captured, _wrapper

    def test_run_production_explicit_external_path_routes_explicit_true(
        self, tmp_path,
    ):
        """``run_production(restart_from=<external>, ...)`` calls
        ``_resolve_restart_node_id_for_run`` with
        ``explicit_restart_from=True`` and the user's path verbatim,
        even though the DAG resolver would have auto-resolved
        eq_001's state."""
        from unittest.mock import patch
        from mdclaw._node import create_node
        from mdclaw.simulation.production import run_production
        from mdclaw.simulation import production as md_mod

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        # Drop a placeholder XML triple on disk so file-existence
        # checks pass before the run aborts at the OpenMM import stage.
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / "system.xml").write_text("<placeholder/>")
        (topo_artifacts / "topology.pdb").write_text("REMARK\nEND\n")
        (topo_artifacts / "state.xml").write_text("<placeholder/>")
        # External restart file (not in the DAG).
        external = jd / "external.xml"
        external.write_text("<placeholder/>")
        # Re-create prod_001 (seed already created it; we just need
        # node_id resolution to find an eq parent for the resolver).
        _ = create_node

        captured, wrapper = self._capture_helper_call()
        with patch.object(
            md_mod, "_resolve_restart_node_id_for_run", side_effect=wrapper,
        ):
            run_production(
                simulation_time_ns=0.001,
                pressure_bar=1.0,
                restart_from=str(external),
                job_dir=str(jd),
                node_id="prod_001",
                platform="CPU",
            )

        # The helper was invoked exactly once (from run_production), with
        # the user's external path and explicit_restart_from=True.
        calls = captured.get("calls") or []
        assert len(calls) == 1, calls
        kwargs = calls[0]
        assert kwargs["explicit_restart_from"] is True
        assert kwargs["restart_from"] == str(external)
        # The resolver did auto-resolve eq_001 — but the helper's
        # ``inputs`` is what gets fed in; the helper's contract (covered
        # in the explicit-path tests above) is to ignore those when
        # explicit_restart_from is True.
        assert kwargs["inputs"].get("restart_from_node_id") == "eq_001"

    def test_run_production_no_restart_from_routes_explicit_false(
        self, tmp_path,
    ):
        """No explicit ``restart_from`` → ``_resolve_restart_node_id_for_run``
        is called with ``explicit_restart_from=False`` so the
        resolver's ``restart_from_node_id`` is trusted."""
        from unittest.mock import patch
        from mdclaw.simulation.production import run_production
        from mdclaw.simulation import production as md_mod

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / "system.xml").write_text("<placeholder/>")
        (topo_artifacts / "topology.pdb").write_text("REMARK\nEND\n")
        (topo_artifacts / "state.xml").write_text("<placeholder/>")
        eq_artifacts = jd / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / "equilibrated.xml").write_text("<placeholder/>")
        (eq_artifacts / "equilibrated.chk").write_text("placeholder")

        captured, wrapper = self._capture_helper_call()
        with patch.object(
            md_mod, "_resolve_restart_node_id_for_run", side_effect=wrapper,
        ):
            run_production(
                simulation_time_ns=0.001,
                pressure_bar=1.0,
                # No restart_from: the resolver's auto-pick is what the
                # run side sees.
                job_dir=str(jd),
                node_id="prod_001",
                platform="CPU",
            )

        calls = captured.get("calls") or []
        assert len(calls) == 1, calls
        kwargs = calls[0]
        assert kwargs["explicit_restart_from"] is False
        # The resolver-supplied path will then be substituted in by the
        # run code (we returned None from the wrapper, so the helper's
        # actual contract is exercised separately above).
        assert kwargs["inputs"].get("restart_from_node_id") == "eq_001"

    def test_run_production_external_path_invokes_final_step_with_none(
        self, tmp_path,
    ):
        """Deepest wiring test: run all the way past
        ``_load_state_into_simulation`` so we observe the actual
        ``read_ancestor_final_step`` call. With an external
        ``--restart-from``, the helper *must* be invoked with
        ``restart_node_id=None`` — passing the resolver's eq_001 id
        through would trigger the very rollback the sentinel was
        introduced to prevent."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from mdclaw.simulation.production import run_production
        from mdclaw.simulation import production as md_mod

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        for f in ("system.xml", "topology.pdb", "state.xml"):
            (topo_artifacts / f).write_text("<placeholder/>")
        eq_artifacts = jd / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        # eq_001 has both state and a checkpoint so the resolver picks
        # eq_001 in its auto-resolve attempt — the test asserts that
        # the *external* path overrides this.
        (eq_artifacts / "equilibrated.xml").write_text(
            "<State><MonteCarloPressure>1.0</MonteCarloPressure></State>"
        )
        (eq_artifacts / "equilibrated.chk").write_text("placeholder")
        external = jd / "external.xml"
        external.write_text(
            "<State><MonteCarloPressure>1.0</MonteCarloPressure></State>"
        )

        # Stub the heavy OpenMM machinery so the run reaches
        # read_ancestor_final_step without needing a real System.
        fake_topology = MagicMock(name="topology")
        fake_topology.atoms.return_value = []
        fake_topology.residues.return_value = []
        fake_xml_inputs = SimpleNamespace(
            topology=fake_topology,
            positions=None,
            box_vectors=[(1, 0, 0), (0, 1, 0), (0, 0, 1)],
            is_periodic=True,
            system_xml_path=topo_artifacts / "system.xml",
            topology_pdb_path=topo_artifacts / "topology.pdb",
            state_xml_path=topo_artifacts / "state.xml",
        )
        fake_system = MagicMock(name="system")
        fake_system.getForces.return_value = []  # no barostat
        fake_simulation = MagicMock(name="simulation")
        fake_simulation.context.getPlatform.return_value.getName.return_value = "CPU"
        fake_simulation.currentStep = 0

        captured: dict = {}

        def _record_then_stop(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = dict(kwargs)
            # Aborting via a typed sentinel exception keeps the run
            # from continuing into reporters / step loops we have not
            # mocked. The outer try/except in run_production marks the
            # node failed cleanly.
            raise RuntimeError("__phase21_recorder__")

        with patch.object(
            md_mod, "_load_xml_topology_inputs",
            return_value=fake_xml_inputs,
        ), patch.object(
            md_mod, "_deserialize_xml_system",
            return_value=fake_system,
        ), patch.object(
            md_mod, "_validate_xml_system_contract",
            return_value=None,
        ), patch(
            "openmm.app.Simulation", return_value=fake_simulation,
        ), patch.object(
            md_mod, "_load_state_into_simulation",
            return_value={"format": "xml"},
        ), patch(
            "mdclaw._node.read_ancestor_final_step",
            side_effect=_record_then_stop,
        ):
            run_production(
                simulation_time_ns=0.001,
                pressure_bar=1.0,
                restart_from=str(external),
                job_dir=str(jd),
                node_id="prod_001",
                platform="CPU",
            )

        # The outer try caught our sentinel, so the test never reached
        # the reporters. The captured kwargs prove the contract:
        # restart_node_id=None for an external file, *not* the
        # resolver's auto-resolved "eq_001".
        assert "kwargs" in captured, (
            "read_ancestor_final_step was never reached — the wiring "
            "to the helper has regressed."
        )
        assert "restart_node_id" in captured["kwargs"]
        assert captured["kwargs"]["restart_node_id"] is None, (
            f"Expected restart_node_id=None for external file; got "
            f"{captured['kwargs'].get('restart_node_id')!r}. The run "
            f"side is silently re-introducing the resolver's "
            f"auto-resolved node id for an external --restart-from."
        )

    def test_run_equilibration_external_path_invokes_final_step_with_none(
        self, tmp_path,
    ):
        """Symmetric counterpart to the run_production deep test:
        ``run_equilibration`` (eq → eq chain) with an external
        ``--restart-from`` must reach
        ``read_ancestor_final_step(..., restart_node_id=None)``. Without
        the sentinel fix the eq side would inherit the resolver's
        eq_001 id and roll the new eq's currentStep onto the DAG
        timeline."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from mdclaw._node import create_node
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation import equilibrate as md_mod

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        # Chain a second eq node off eq_001 — the resolver auto-resolves
        # eq_001's state for eq_002, but we override with an external
        # path below.
        create_node(str(jd), "eq", parent_node_ids=["eq_001"])
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        for f in ("system.xml", "topology.pdb", "state.xml"):
            (topo_artifacts / f).write_text("<placeholder/>")
        eq_artifacts = jd / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / "equilibrated.xml").write_text(
            "<State><MonteCarloPressure>1.0</MonteCarloPressure></State>"
        )
        external = jd / "external_eq.xml"
        external.write_text(
            "<State><MonteCarloPressure>1.0</MonteCarloPressure></State>"
        )

        fake_topology = MagicMock(name="topology")
        fake_topology.atoms.return_value = []
        fake_topology.residues.return_value = []
        fake_xml_inputs = SimpleNamespace(
            topology=fake_topology,
            positions=None,
            box_vectors=[(1, 0, 0), (0, 1, 0), (0, 0, 1)],
            is_periodic=True,
            system_xml_path=topo_artifacts / "system.xml",
            topology_pdb_path=topo_artifacts / "topology.pdb",
            state_xml_path=topo_artifacts / "state.xml",
        )
        fake_system = MagicMock(name="system")
        fake_system.getForces.return_value = []
        fake_simulation = MagicMock(name="simulation")
        fake_simulation.context.getPlatform.return_value.getName.return_value = "CPU"
        fake_simulation.currentStep = 0

        captured: dict = {}

        def _record_then_stop(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = dict(kwargs)
            raise RuntimeError("__phase22_recorder__")

        with patch.object(
            md_mod, "_load_xml_topology_inputs",
            return_value=fake_xml_inputs,
        ), patch.object(
            md_mod, "_deserialize_xml_system",
            return_value=fake_system,
        ), patch.object(
            md_mod, "_validate_xml_system_contract",
            return_value=None,
        ), patch(
            "openmm.app.Simulation", return_value=fake_simulation,
        ), patch.object(
            md_mod, "_load_state_into_simulation",
            return_value={"format": "xml"},
        ), patch(
            "mdclaw._node.read_ancestor_final_step",
            side_effect=_record_then_stop,
        ):
            run_equilibration(
                pressure_bar=1.0,
                # Skip restraints / NPT step counts — keep the run
                # focused on the restart path.
                nvt_steps=0,
                npt_steps=0,
                restart_from=str(external),
                job_dir=str(jd),
                node_id="eq_002",
                platform="CPU",
            )

        assert "kwargs" in captured, (
            "read_ancestor_final_step was never reached in "
            "run_equilibration — the wiring to the helper has regressed."
        )
        assert captured["kwargs"].get("restart_node_id") is None, (
            f"Expected restart_node_id=None for an external eq restart; "
            f"got {captured['kwargs'].get('restart_node_id')!r}. The eq "
            f"side is silently re-introducing the resolver's "
            f"auto-resolved node id."
        )

    def test_run_equilibration_explicit_external_path_routes_explicit_true(
        self, tmp_path,
    ):
        """Same wiring on the eq side: an explicit external path lands
        in the helper with ``explicit_restart_from=True``."""
        from unittest.mock import patch
        from mdclaw._node import create_node
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation import equilibrate as md_mod

        jd = tmp_path / "job"
        jd.mkdir()
        self._seed_dag(jd)
        # New eq_002 chained off eq_001 — that's the call that exposes
        # the resolver's eq → eq auto-pick.
        create_node(str(jd), "eq", parent_node_ids=["eq_001"])
        topo_artifacts = jd / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / "system.xml").write_text("<placeholder/>")
        (topo_artifacts / "topology.pdb").write_text("REMARK\nEND\n")
        (topo_artifacts / "state.xml").write_text("<placeholder/>")
        eq_artifacts = jd / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / "equilibrated.xml").write_text("<placeholder/>")
        external = jd / "ext_eq.xml"
        external.write_text("<placeholder/>")

        captured, wrapper = self._capture_helper_call()
        with patch.object(
            md_mod, "_resolve_restart_node_id_for_run", side_effect=wrapper,
        ):
            run_equilibration(
                pressure_bar=1.0,
                restart_from=str(external),
                job_dir=str(jd),
                node_id="eq_002",
                platform="CPU",
            )

        calls = captured.get("calls") or []
        assert len(calls) == 1, calls
        kwargs = calls[0]
        assert kwargs["explicit_restart_from"] is True
        assert kwargs["restart_from"] == str(external)
        # Resolver still auto-resolved eq_001 — but the helper must
        # decide based on path equality, not on this resolver hint.
        assert kwargs["inputs"].get("restart_from_node_id") == "eq_001"


# ----------------------------------------------------------------------------
# Bug 5: every early-return path after begin_node() must mark the node failed
# ----------------------------------------------------------------------------


class TestRunProductionFailNodeCoverage:
    """run_production must never leave a node stuck in ``running`` after
    ``begin_node()``. Validation failures (missing artifacts, invalid
    platform, missing restart file) must transit through ``fail_node`` so
    the DAG never sees a perpetually in-flight node."""

    def _dag_with_fake_modern_artifacts(self, tmp_path):
        """Build a (topo -> eq -> ... ) DAG so a fresh ``prod`` node can be
        validated through ``validate_node_execution_context`` (which requires
        prod's parent to be ``eq`` or ``prod``). Topo lists modern triple
        artifacts that exist on disk but are not parseable XML — the goal is
        to drive past begin_node() and let the OpenMM deserialize step fail."""
        from mdclaw._node import complete_node, create_node

        job_dir = tmp_path / "job"
        create_node(str(job_dir), "topo")
        topo_artifacts = job_dir / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        # Write minimal placeholder files — exist for path checks but not
        # parseable; deserialize / minimize will raise after begin_node().
        (topo_artifacts / "system.xml").write_text("<bogus />")
        (topo_artifacts / "topology.pdb").write_text("REMARK fake\nEND\n")
        (topo_artifacts / "state.xml").write_text("<bogus />")
        complete_node(
            str(job_dir),
            "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
        )
        # eq_001 placeholder so a prod node downstream is execution-context
        # valid. We don't actually run eq — only the existence + completed
        # status matters for validate_node_execution_context.
        create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])
        eq_artifacts = job_dir / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / "equilibrated.xml").write_text("<bogus />")
        complete_node(
            str(job_dir),
            "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        return job_dir

    def test_node_modern_triple_present_in_dag_but_missing_on_disk_marks_failed(
        self, tmp_path
    ):
        """When the DAG resolver finds a modern triple on the topo node but
        the actual files have been deleted before run_production opens
        them, validation fails *after* ``begin_node()`` — the post-begin_node
        artifact-existence check must mark the node ``failed`` rather than
        leave it ``running``.

        We exercise that by completing the topo with the triple recorded,
        then deleting the files on disk."""
        from mdclaw._node import create_node, read_node

        job_dir = self._dag_with_fake_modern_artifacts(tmp_path)
        topo_artifacts = job_dir / "nodes" / "topo_001" / "artifacts"
        # Delete only the system.xml — the resolver still surfaces the path
        # from node.json, but the post-begin_node existence check fires.
        (topo_artifacts / "system.xml").unlink()

        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
        result = run_production(
            simulation_time_ns=0.001,
            platform="CPU",
            job_dir=str(job_dir),
            node_id="prod_001",
        )
        assert result.get("success", False) is False
        prod_node = read_node(str(job_dir), "prod_001")
        assert prod_node["status"] == "failed", (
            f"Expected failed, got {prod_node['status']!r}"
        )

    def test_node_invalid_platform_after_begin_node_marks_failed(self, tmp_path):
        """An unknown OpenMM platform name fails inside the inner try block
        — well past begin_node() — so the helper must catch and fail_node
        the result. The test points run_production at fake-but-existent
        modern artifacts so we get past the existence checks before the
        platform error fires."""
        from mdclaw._node import create_node, read_node

        job_dir = self._dag_with_fake_modern_artifacts(tmp_path)
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])

        result = run_production(
            simulation_time_ns=0.001,
            platform="DefinitelyNotAPlatform",
            job_dir=str(job_dir),
            node_id="prod_001",
        )
        assert result.get("success", False) is False
        prod_node = read_node(str(job_dir), "prod_001")
        assert prod_node["status"] == "failed", (
            f"Expected failed, got {prod_node['status']!r}"
        )

    def test_fail_node_helper_is_idempotent_outside_node_mode(self):
        """When called without job_dir/node_id, the helper must be a no-op
        on the result dict (it just returns ``result`` so call sites can
        ``return _fail_node_if_running(...)`` regardless of mode)."""
        from mdclaw.simulation._base import _fail_node_if_running

        result = {"success": False, "errors": ["x"], "warnings": []}
        out = _fail_node_if_running(None, None, result)
        assert out is result

    def test_run_equilibration_vacuum_rejection_marks_node_failed(self, tmp_path):
        """Same fail-node coverage applies to run_equilibration: a
        non-periodic topology with no implicit_solvent must mark the node
        ``failed``, never leave it ``running``. (Review fix 2 for
        openmmforcefields-unification.)

        We exercise the rejection path by handing run_equilibration a fake
        modern triple whose system.xml deserializes to a non-periodic
        System (no box vectors). The vacuum guardrail fires after
        ``begin_node()`` and must transit through ``_fail_node_if_running``."""
        from openmm import (
            HarmonicBondForce, NonbondedForce, System, XmlSerializer,
        )
        from openmm.app import Element, PDBFile, Topology
        from openmm.unit import dalton
        from mdclaw._node import complete_node, create_node, read_node
        from mdclaw.simulation.equilibrate import run_equilibration

        # 1) Build a minimal non-periodic OpenMM System and serialize to
        #    system.xml. Topology gets a single ALA residue's heavy atoms.
        top = Topology()
        chain = top.addChain("A")
        res = top.addResidue("ALA", chain, "1")
        n = top.addAtom("N", Element.getBySymbol("N"), res)
        ca = top.addAtom("CA", Element.getBySymbol("C"), res)
        c = top.addAtom("C", Element.getBySymbol("C"), res)
        top.addBond(n, ca)
        top.addBond(ca, c)

        sys_obj = System()
        for _ in range(3):
            sys_obj.addParticle(12.0 * dalton)
        sys_obj.addForce(HarmonicBondForce())
        sys_obj.addForce(NonbondedForce())

        topo_dir = tmp_path / "topo_artifacts"
        topo_dir.mkdir()
        sys_xml = topo_dir / "system.xml"
        sys_xml.write_text(XmlSerializer.serialize(sys_obj))

        # topology.pdb derived from the same Topology — no CRYST1, so
        # the loaded XML topology inputs carry no box vectors → vacuum
        # guardrail fires on the run side.
        topology_pdb = topo_dir / "topology.pdb"
        positions = [(0.0, 0.0, 0.0), (0.15, 0.0, 0.0), (0.30, 0.0, 0.0)]
        from openmm import Vec3
        positions_q = [Vec3(*p) for p in positions]
        with topology_pdb.open("w") as fh:
            PDBFile.writeFile(top, positions_q, fh, keepIds=False)

        # 2) Build a (topo -> eq) DAG that points at those artifacts.
        job_dir = tmp_path / "job_eq_vacuum"
        create_node(str(job_dir), "topo")
        ta = job_dir / "nodes" / "topo_001" / "artifacts"
        ta.mkdir(parents=True, exist_ok=True)
        (ta / "system.xml").write_bytes(sys_xml.read_bytes())
        (ta / "topology.pdb").write_bytes(topology_pdb.read_bytes())
        complete_node(
            str(job_dir),
            "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
            },
        )
        create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])

        # 3) Run equilibration without implicit_solvent. The non-periodic
        #    topology + no implicit_solvent guardrail must mark the node failed.
        result = run_equilibration(
            temperature_kelvin=300.0,
            nvt_steps=1,
            npt_steps=0,
            platform="CPU",
            job_dir=str(job_dir),
            node_id="eq_001",
        )
        assert result.get("success", False) is False
        eq_node = read_node(str(job_dir), "eq_001")
        assert eq_node["status"] == "failed", (
            f"Expected eq_001 status=failed after vacuum rejection, "
            f"got {eq_node['status']!r}"
        )

    def test_fail_node_helper_skips_when_success(self, tmp_path):
        """A successful run dict must NOT trigger fail_node — the caller
        sometimes passes through this helper from non-fatal cleanup
        branches."""
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation._base import _fail_node_if_running

        job_dir = tmp_path / "job_ok"
        create_node(str(job_dir), "prod")
        out = _fail_node_if_running(
            str(job_dir),
            "prod_001",
            {"success": True, "errors": [], "warnings": []},
        )
        assert out["success"] is True
        prod_node = read_node(str(job_dir), "prod_001")
        # Node should still be ``pending`` because we never began it; the
        # important invariant is that it is NOT now ``failed``.
        assert prod_node["status"] != "failed"


# ----------------------------------------------------------------------------
# Bug 1 end-to-end: build_amber_system bakes HMR / blocks implicit_solvent
# ----------------------------------------------------------------------------


class TestBuildAmberSystemHmrAndImplicitContract:
    """Sanity smokes for the Bug 1 fix at the public ``build_amber_system``
    surface. Lighter than the slow eq/prod smoke; runs in the default lane."""

    def _fake_om_build_capturing_kwargs(self):
        """Helper that records the ``hmr`` / ``implicit_solvent`` kwargs the
        outer build_amber_system passes into _run_openmmforcefields_build."""
        captured: dict = {}

        def _fake(**kwargs):
            captured["hmr"] = kwargs.get("hmr")
            captured["implicit_solvent"] = kwargs.get("implicit_solvent")
            captured["water_model"] = kwargs.get("water_model")
            captured["pdb_path"] = kwargs.get("pdb_path")
            captured["pablo_auto_download"] = kwargs.get("pablo_auto_download")
            kwargs["system_xml_file"].write_text("<System/>")
            kwargs["topology_pdb_file"].write_text("REMARK fake\nEND\n")
            kwargs["state_xml_file"].write_text("<State/>")
            return {
                "success": True,
                "errors": [],
                "warnings": [],
                "system_xml": str(kwargs["system_xml_file"]),
                "topology_pdb": str(kwargs["topology_pdb_file"]),
                "state_xml": str(kwargs["state_xml_file"]),
                "num_atoms": 1,
                "num_residues": 1,
                "forcefield_provenance": {
                    "kind": "amber_via_openmmforcefields",
                    "openmm_xml": [],
                    "method": {
                        "hmr": kwargs.get("hmr", False),
                        "implicit_solvent": kwargs.get("implicit_solvent"),
                    },
                },
            }

        return captured, _fake

    @staticmethod
    def _pdb_with_single_atom_ion(resname: str, element: str) -> str:
        ion_line = (
            f"{'HETATM':<6}{2:5d} {element:^4s} {resname:>3s} {'A'}{101:4d}"
            f"    {12.000:8.3f}{13.000:8.3f}{12.000:8.3f}"
            f"{1.00:6.2f}{20.00:6.2f}          {element:>2s}\n"
        )
        return (
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
            f"{ion_line}"
            "END\n"
        )

    def test_build_amber_system_does_not_pdbfixer_rewrite_prepared_input(
        self, tmp_path
    ):
        """Topology build must validate the prep artifact, not repair/rewrite it."""
        from pathlib import Path
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "glh_input.pdb"
        pdb.write_text(
            "ATOM      1  N   GLH A  11      11.104  13.207  12.011  1.00 20.00           N\n"
            "ATOM      2  CA  GLH A  11      12.104  13.207  12.011  1.00 20.00           C\n"
            "ATOM      3  HE2 GLH A  11      13.104  13.207  12.011  1.00 20.00           H\n"
            "END\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                output_dir=str(tmp_path / "topo"),
            )

        assert result["success"] is True
        assert " GLH A  11" in Path(captured["pdb_path"]).read_text()
        assert not list(Path(result["output_dir"]).glob("*.hydrogenated.pdb"))
        assert not any("PDBFixer" in w for w in result.get("warnings", []))

    def test_build_amber_system_passes_hmr_through_to_helper(self, tmp_path):
        """``build_amber_system(hmr=True)`` must reach the helper. The helper
        is mocked so we can assert the kwarg propagation without running
        SystemGenerator."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                hmr=True,
                output_dir=str(tmp_path / "topo"),
            )
        assert result["success"] is True
        assert captured["hmr"] is True
        assert captured["implicit_solvent"] is None

    def test_build_amber_system_passes_pablo_auto_download_to_helper(
        self, tmp_path
    ):
        """Offline/local topology builds can disable Pablo CCD fetches."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                pablo_auto_download=False,
                output_dir=str(tmp_path / "topo"),
            )
        assert result["success"] is True
        assert captured["pablo_auto_download"] is False
        assert result["parameters"]["pablo_auto_download"] is False

    def test_build_amber_system_rejects_unknown_implicit_model(self, tmp_path):
        """Unknown implicit-solvent names fail-fast with a structured code so
        agents can offer the supported set rather than letting a typo slip
        into ``SystemGenerator(forcefields=...)`` and crash with an opaque
        XML-not-found error.

        Calling ``_run_openmmforcefields_build`` directly keeps the test in
        the fast lane (no SystemGenerator import). The wider
        ``build_amber_system`` test in tests/test_guardrails.py covers the
        public-API path and the matching ``box_dimensions`` conflict guard.
        """
        from mdclaw.amber.openmm_build import _run_openmmforcefields_build

        result = _run_openmmforcefields_build(
            pdb_path=tmp_path / "x.pdb",
            output_name="system",
            out_dir=tmp_path,
            system_xml_file=tmp_path / "system.xml",
            topology_pdb_file=tmp_path / "topology.pdb",
            state_xml_file=tmp_path / "state.xml",
            forcefield="ff14SB",
            water_model=None,
            phosaa_library=None,
            nucleic_libraries=[],
            glycan_library=None,
            is_membrane=False,
            box_dimensions=None,
            valid_ligands=[],
            valid_modxna_params=[],
            disulfide_bonds=None,
            implicit_solvent="MAGIC_GB",
        )
        assert result["success"] is False
        assert result.get("code") == "implicit_solvent_model_unsupported"

    def test_build_amber_system_default_hmr_matches_run_defaults(self, tmp_path):
        """The default ``build_amber_system()`` call (no hmr= override) must
        bake HMR into system.xml. This is the contract that lets the
        zero-kwarg workflow (build_amber_system → run_equilibration →
        run_production with no overrides) succeed: run_equilibration's
        default ``hmr=True`` would otherwise trip
        ``modern_system_hmr_mismatch`` against a non-HMR build."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                # No hmr=! Verifying the default propagates.
                output_dir=str(tmp_path / "topo"),
            )
        assert result["success"] is True
        assert captured["hmr"] is True, (
            "build_amber_system default hmr must be True so run_equilibration "
            "/ run_production defaults (also hmr=True) line up under the "
            "modern-system contract."
        )
        prov = result.get("forcefield_provenance") or {}
        assert (prov.get("method") or {}).get("hmr") is True

    def test_implicit_crystallographic_ions_are_rejected(self, tmp_path):
        """Explicit ion residues should not sneak into implicit builds."""
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "ion_input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
            "HETATM    2 CA    CA A 101      12.000  13.000  12.000  1.00 20.00          CA\n"
            "END\n"
        )

        result = build_amber_system(
            pdb_file=str(pdb),
            forcefield="ff14SBonlysc",
            implicit_solvent="GBn2",
            output_dir=str(tmp_path / "topo"),
        )

        assert result["success"] is False
        assert result["code"] == "explicit_ions_in_implicit_solvent"
        assert result["parameters"]["retained_ion_residue_names"] == ["CA"]

    def test_vacuum_crystallographic_ions_are_allowed(self, tmp_path):
        """Vacuum/no-solvent topology is distinct from implicit solvent and may
        intentionally contain explicit ion particles."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "ion_input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
            "HETATM    2 CA    CA A 101      12.000  13.000  12.000  1.00 20.00          CA\n"
            "END\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff14SB",
                water_model="opc",
                output_dir=str(tmp_path / "topo"),
            )

        assert result["success"] is True
        assert result["solvent_type"] == "vacuum"
        assert captured["water_model"] == "opc"
        assert result["parameters"]["retained_ion_residue_names"] == ["CA"]
        assert result["parameters"]["ion_parameter_water_model"] == "opc"
        assert result["parameters"]["water_model_status"] == "used_for_vacuum_ion_templates"

    def test_explicit_crystallographic_ions_keep_water_ion_xml(self, tmp_path):
        """Explicit-solvent builds keep supported crystallographic ions and
        load the selected water/ion XML."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "ion_input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
            "HETATM    2 CA    CA A 101      12.000  13.000  12.000  1.00 20.00          CA\n"
            "END\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                box_dimensions={"box_a": 40.0, "box_b": 40.0, "box_c": 40.0},
                output_dir=str(tmp_path / "topo"),
            )

        assert result["success"] is True
        assert captured["water_model"] == "opc"
        assert result["parameters"]["retained_ion_residue_names"] == ["CA"]

    def test_explicit_iodine_ion_is_supported_by_opc_xml(self, tmp_path):
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "iodine_input.pdb"
        pdb.write_text(self._pdb_with_single_atom_ion("I", "I"))

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                box_dimensions={"box_a": 40.0, "box_b": 40.0, "box_c": 40.0},
                output_dir=str(tmp_path / "topo_opc_i"),
            )

        assert result["success"] is True
        assert captured["water_model"] == "opc"
        assert result["parameters"]["retained_ion_residue_names"] == ["I"]

    def test_explicit_iodine_ion_is_rejected_for_tip3p_xml(self, tmp_path):
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "iodine_input.pdb"
        pdb.write_text(self._pdb_with_single_atom_ion("I", "I"))

        result = build_amber_system(
            pdb_file=str(pdb),
            forcefield="ff14SB",
            water_model="tip3p",
            box_dimensions={"box_a": 40.0, "box_b": 40.0, "box_c": 40.0},
            output_dir=str(tmp_path / "topo_tip3p_i"),
        )

        assert result["success"] is False
        assert result["code"] == "unsupported_ion_for_water_model"
        assert result["context"]["water_model"] == "tip3p"
        assert result["context"]["unsupported_ion_residue_names"] == ["I"]
        assert result["parameters"]["retained_ion_residue_names"] == ["I"]

    def test_explicit_iodide_ion_is_supported_by_tip3p_xml(self, tmp_path):
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "iodide_input.pdb"
        pdb.write_text(self._pdb_with_single_atom_ion("IOD", "I"))

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff14SB",
                water_model="tip3p",
                box_dimensions={"box_a": 40.0, "box_b": 40.0, "box_c": 40.0},
                output_dir=str(tmp_path / "topo_tip3p_iod"),
            )

        assert result["success"] is True
        assert captured["water_model"] == "tip3p"
        assert result["parameters"]["retained_ion_residue_names"] == ["IOD"]

    def test_build_amber_system_provenance_records_hmr(self, tmp_path):
        """When ``hmr=True`` builds successfully via openmmforcefields, the
        topo node's ``forcefield_provenance.method.hmr`` must reflect that
        choice so evidence_server / run_* can read the source of truth."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                water_model="opc",
                hmr=True,
                output_dir=str(tmp_path / "topo"),
            )
        prov = result.get("forcefield_provenance") or {}
        method = prov.get("method") or {}
        assert method.get("hmr") is True

    def test_build_amber_system_rejects_box_and_implicit_conflict(self, tmp_path):
        """``build_amber_system(implicit_solvent=..., box_dimensions=...)``
        must fail-fast with a structured ``implicit_solvent_explicit_box_conflict``
        code so callers cannot accidentally produce a periodic GB system."""
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        result = build_amber_system(
            pdb_file=str(pdb),
            forcefield="ff14SB",
            implicit_solvent="OBC2",
            box_dimensions={"box_a": 50.0, "box_b": 50.0, "box_c": 50.0},
            output_dir=str(tmp_path / "topo"),
        )
        assert result["success"] is False
        assert result["code"] == "implicit_solvent_explicit_box_conflict"

    def test_build_amber_system_rejects_unknown_implicit_model_at_public_api(
        self, tmp_path
    ):
        """Unknown GB model names are rejected by the public ``build_amber_system``
        surface, not just the internal helper."""
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        result = build_amber_system(
            pdb_file=str(pdb),
            forcefield="ff14SB",
            implicit_solvent="MAGIC_GB",
            output_dir=str(tmp_path / "topo"),
        )
        assert result["success"] is False
        assert result["code"] == "implicit_solvent_model_unsupported"
        # The error message must surface the supported set so agents can
        # recover without re-reading the source.
        assert "OBC2" in result["message"]
        assert "GBn2" in result["message"]

    def test_build_amber_system_auto_switches_ff14sb_to_ff14sbonlysc_for_implicit(
        self, tmp_path
    ):
        """``build_amber_system(forcefield="ff14SB", implicit_solvent="OBC2")``
        must reach the helper with the GBneck2-tuned ``ff14SBonlysc`` variant
        (Amber25 implicit recipe). The auto-switch is surfaced via a
        warning so users keep the ability to opt back to plain ff14SB by
        passing ``ff14SBonlysc`` explicitly."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()

        # Augment the capture closure to also record ``forcefield``.
        original_fake = fake

        def _fake(**kwargs):
            captured["forcefield"] = kwargs.get("forcefield")
            return original_fake(**kwargs)

        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=_fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff14SB",
                implicit_solvent="OBC2",
                output_dir=str(tmp_path / "topo"),
            )
        assert result["success"] is True
        assert captured["forcefield"] == "ff14SBonlysc"
        assert captured["implicit_solvent"] == "OBC2"
        assert result["parameters"]["effective_forcefield"] == "ff14SBonlysc"
        assert result["parameters"]["implicit_solvent"] == "OBC2"
        assert any(
            "auto-switched protein force field ff14SB -> ff14SBonlysc" in w
            for w in result.get("warnings", [])
        ), result.get("warnings")

    def test_build_amber_system_warns_on_ff19sb_with_implicit(self, tmp_path):
        """ff19SB is OPC-tuned; pairing it with GB raises a warning but is
        not blocked outright (research workflows may still want it)."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        _captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff19SB",
                implicit_solvent="OBC2",
                output_dir=str(tmp_path / "topo"),
            )
        assert result["success"] is True
        assert any(
            "ff19SB was parameterized for OPC" in w
            for w in result.get("warnings", [])
        )

    def test_build_amber_system_normalizes_implicit_alias(self, tmp_path):
        """Case-insensitive aliases (``obc2``, ``gbneck2``) reach the helper
        as the canonical key (``OBC2``, ``GBn2``) so provenance / metadata
        are stable across user inputs."""
        from unittest.mock import patch
        from mdclaw.amber.build_system import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber.build_system._run_openmmforcefields_build",
            side_effect=fake,
        ):
            result = build_amber_system(
                pdb_file=str(pdb),
                forcefield="ff14SBonlysc",
                implicit_solvent="gbneck2",
                output_dir=str(tmp_path / "topo"),
            )
        assert result["success"] is True
        assert captured["implicit_solvent"] == "GBn2"
        assert result["parameters"]["implicit_solvent"] == "GBn2"


@pytest.mark.slow
class TestBuildAmberSystemImplicitEndToEnd:
    """End-to-end smoke for the openmmforcefields implicit-solvent path.

    Runs the real ``_run_openmmforcefields_build`` with a small peptide,
    so the test exercises SystemGenerator + Pablo + GB XML loading and
    asserts the resulting ``system.xml`` actually carries a Generalized-Born
    force. Marked slow because it imports the full openmmforcefields stack.

    Uses the project's ``small_pdb`` fixture (5-residue ALA-GLY pentapeptide)
    so terminal residue templates resolve cleanly under PDBFixer's default
    cap-handling.
    """

    def test_implicit_obc2_build_attaches_gb_force_to_system_xml(
        self, small_pdb, tmp_path
    ):
        """``build_amber_system(forcefield="ff14SBonlysc", implicit_solvent="OBC2")``
        must produce a ``system.xml`` whose deserialized System carries a
        ``CustomGBForce``/``GBSAOBCForce``. This is the contract the run-side
        shim relies on when honoring an ``implicitSolvent`` request."""
        pytest.importorskip("openmm")
        pytest.importorskip("openmmforcefields")
        pytest.importorskip("openff.pablo")
        pytest.importorskip("pdbfixer")

        from openmm import XmlSerializer
        from mdclaw.amber.build_system import build_amber_system

        result = build_amber_system(
            pdb_file=str(small_pdb),
            forcefield="ff14SBonlysc",
            implicit_solvent="OBC2",
            output_dir=str(tmp_path / "topo"),
        )
        assert result["success"] is True, result.get("errors")
        prov = result["forcefield_provenance"]
        # Provenance reflects the implicit choice + bundle.
        assert prov["method"]["solvent_type"] == "implicit"
        assert prov["method"]["implicit_solvent"] == "OBC2"
        assert prov["method"]["nonbonded"] == "NoCutoff"
        assert prov["method"]["hmr"] is True
        assert prov["method"]["hydrogen_mass_amu"] == 4.0
        assert "implicit/obc2.xml" in prov["openmm_xml"]
        # GB force is actually present in the saved system.xml.
        with open(result["system_xml"]) as fh:
            system = XmlSerializer.deserialize(fh.read())
        gb_classes = {
            "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
        }
        present = {type(f).__name__ for f in system.getForces()}
        assert present & gb_classes, (
            f"system.xml has no GB force; forces={present}"
        )

    def test_implicit_obc2_validator_accepts_implicitsolvent_request(
        self, small_pdb, tmp_path
    ):
        """End-to-end against the run-side contract: the native validator
        ``_validate_xml_system_contract`` must NOT raise
        ``modern_system_implicit_solvent_unsupported`` against an
        OBC2-built system.xml. Guards against future regressions where
        the build path stops baking the GB force."""
        pytest.importorskip("openmm")
        pytest.importorskip("openmmforcefields")
        pytest.importorskip("openff.pablo")
        pytest.importorskip("pdbfixer")

        from openmm.app import PDBFile
        from mdclaw.amber.build_system import build_amber_system
        from mdclaw.simulation.xml_contract import (
            _deserialize_xml_system,
            _load_xml_topology_inputs,
            _validate_xml_system_contract,
        )

        result = build_amber_system(
            pdb_file=str(small_pdb),
            forcefield="ff14SBonlysc",
            implicit_solvent="OBC2",
            output_dir=str(tmp_path / "topo"),
        )
        assert result["success"] is True, result.get("errors")
        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=result["system_xml"],
            topology_pdb_file=result["topology_pdb"],
            state_xml_file=result.get("state_xml"),
        )
        system = _deserialize_xml_system(xml_inputs)
        # No raise expected: the System has a GB force, the validator's
        # implicit-solvent contract is satisfied.
        _validate_xml_system_contract(
            system, xml_inputs.topology,
            hmr_request=True,
            implicit_solvent_request="OBC2",
        )
        topology = PDBFile(result["topology_pdb"]).topology
        assert topology.getNumAtoms() == xml_inputs.topology.getNumAtoms()
        assert any(
            type(f).__name__ in {
                "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
            }
            for f in system.getForces()
        )


class TestResolveImplicitSolventModel:
    """Unit tests for ``_resolve_implicit_solvent_model``.

    The helper guards against the historical ``.upper()``-vs-mixed-case bug
    that silently mapped ``"GBn2"`` → ``"GBN2"`` → OBC2 fallback. It must
    canonicalize aliases and fail-fast on unknown names rather than silently
    selecting OBC2.
    """

    def _stub_models(self):
        """Build an OpenMM-symbol stand-in keyed by canonical names."""
        # Sentinel objects stand in for the real OpenMM symbols
        # (``app.HCT`` etc.) — the helper only does dict lookup, so any
        # distinguishable values work and the test stays fast.
        return {
            "HCT":  object(),
            "OBC1": object(),
            "OBC2": object(),
            "GBn":  object(),
            "GBn2": object(),
        }

    def test_resolves_canonical_gbn2_to_distinct_object_not_obc2(self):
        """The headline regression: ``"GBn2"`` must resolve to the GBn2
        symbol, not silently fall back to OBC2 the way the old
        ``IMPLICIT_MODELS.get(name.upper(), OBC2)`` lookup did."""
        from mdclaw.simulation._base import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model("GBn2", models)
        assert err is None
        assert model is models["GBn2"]
        assert model is not models["OBC2"]

    def test_resolves_canonical_gbn_to_distinct_object_not_obc2(self):
        from mdclaw.simulation._base import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model("GBn", models)
        assert err is None
        assert model is models["GBn"]
        assert model is not models["OBC2"]

    @pytest.mark.parametrize(
        ("alias", "expected_canonical"),
        [
            ("OBC2",    "OBC2"),
            ("obc2",    "OBC2"),
            ("OBC",     "OBC2"),     # bare OBC defaults to OBC2
            ("gbneck2", "GBn2"),
            ("igb8",    "GBn2"),
            ("igb5",    "OBC2"),
            ("HCT",     "HCT"),
            ("hct",     "HCT"),
            ("igb1",    "HCT"),
        ],
    )
    def test_aliases_resolve_to_canonical_openmm_symbol(
        self, alias, expected_canonical
    ):
        """Aliases (``gbneck2``, ``igb8``, case variants) reach the OpenMM
        symbol associated with the canonical key, never via OBC2 fallback."""
        from mdclaw.simulation._base import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model(alias, models)
        assert err is None
        assert model is models[expected_canonical]

    def test_unknown_model_returns_structured_error_not_obc2(self):
        """A typo / unknown GB model must surface the structured failure
        code rather than silently selecting OBC2 — the silent fallback was
        precisely the regression this helper exists to fix."""
        from mdclaw.simulation._base import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model("MAGIC_GB", models)
        assert model is None
        assert err is not None
        assert err["code"] == "implicit_solvent_model_unsupported"
        assert any("MAGIC_GB" in e for e in err["errors"])
        # Supported set surfaces in the message so agents can recover
        # without consulting the source.
        assert any("OBC2" in e and "GBn2" in e for e in err["errors"])

    def test_blank_input_is_unknown(self):
        from mdclaw.simulation._base import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model("   ", models)
        assert model is None
        assert err["code"] == "implicit_solvent_model_unsupported"

    def test_catalog_known_but_openmm_map_missing_is_explicit_failure(self):
        """If the catalog lists a model that the run-side OpenMM map has
        not been updated for, the helper must report a structured failure
        rather than silently OBC2-fallback. Models a future drift between
        catalog and runtime."""
        from mdclaw.simulation._base import _resolve_implicit_solvent_model

        # Pretend the OpenMM symbol map is out of date and missing GBn2.
        partial = {
            "HCT":  object(),
            "OBC1": object(),
            "OBC2": object(),
            "GBn":  object(),
            # GBn2 deliberately absent
        }
        model, err = _resolve_implicit_solvent_model("GBn2", partial)
        assert model is None
        assert err["code"] == "implicit_solvent_model_unsupported"


class TestRunProductionImplicitSolventLookup:
    """``run_production`` must resolve canonical / aliased implicit-solvent
    names through ``_resolve_implicit_solvent_model`` instead of the old
    ``.upper()``-vs-mixed-case lookup that silently fell back to OBC2.
    The helper-level guarantees are covered exhaustively by
    ``TestResolveImplicitSolventModel``; here we exercise the public
    ``run_production`` surface end-to-end against the structured failure
    code so a regression in the routing layer surfaces without depending
    on the (now-removed) shim mock.
    """

    def test_unknown_implicit_model_fails_with_structured_code(
        self, tmp_path
    ):
        """run_production with an unknown GB model name must fail-fast
        with the structured ``implicit_solvent_model_unsupported`` code,
        not silently fall back to OBC2.

        Stub ``_load_xml_topology_inputs`` so we never hit the
        XML deserializer (the placeholder XML files would fail to parse
        well before the GB lookup), and stub ``_deserialize_xml_system``
        so a regression that *did* reach the System build raises loudly.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        sysxml = tmp_path / "system.xml"
        topo = tmp_path / "topology.pdb"
        state = tmp_path / "state.xml"
        for p in (sysxml, topo, state):
            p.write_text("<placeholder/>")

        fake_topology = MagicMock(name="topology")
        fake_topology.atoms.return_value = []
        fake_topology.residues.return_value = []
        fake_xml_inputs = SimpleNamespace(
            topology=fake_topology,
            positions=None,
            box_vectors=None,
            is_periodic=False,
            system_xml_path=sysxml,
            topology_pdb_path=topo,
            state_xml_path=state,
        )

        from mdclaw.simulation import production as md_mod

        with patch.object(
            md_mod, "_load_xml_topology_inputs",
            return_value=fake_xml_inputs,
        ), patch.object(
            md_mod, "_deserialize_xml_system",
            side_effect=AssertionError(
                "_deserialize_xml_system must not be reached for an unknown GB model."
            ),
        ):
            result = md_mod.run_production(
                system_xml_file=str(sysxml),
                topology_pdb_file=str(topo),
                state_xml_file=str(state),
                simulation_time_ns=0.001,
                implicit_solvent="MAGIC_GB",
                pressure_bar=0,
                output_dir=str(tmp_path / "out"),
            )
        assert result.get("success") is False
        assert result.get("code") == "implicit_solvent_model_unsupported"
        assert any("MAGIC_GB" in e for e in result.get("errors", []))


class TestCheckTopologyImplicitSolventMatch:
    """Unit tests for ``_check_topology_implicit_solvent_match``.

    The shim's GB-force presence check (``modern_system_implicit_solvent_unsupported``)
    catches vacuum-vs-GB but cannot distinguish OBC2-built from GBn2-built
    Systems (both carry a ``CustomGBForce``). This guard reads the topo
    node's build-time ``metadata.implicit_solvent`` and compares it
    against the runtime flag, with alias canonicalization, before any
    System is built.
    """

    def test_matching_canonical_returns_none(self):
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent="OBC2",
        ) is None

    @pytest.mark.parametrize(
        ("build", "runtime"),
        [
            ("GBn2", "gbneck2"),
            ("gbneck2", "GBn2"),
            ("OBC2", "obc2"),
            ("OBC2", "igb5"),
            ("HCT", "igb1"),
            ("GBn2", "igb8"),
        ],
    )
    def test_alias_pair_canonicalizes_to_match(self, build, runtime):
        """Aliases must canonicalize so users typing ``gbneck2`` against a
        node built with ``GBn2`` (or vice versa) do not trip the guard."""
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent=build,
            runtime_implicit_solvent=runtime,
        ) is None

    def test_obc2_topo_with_gbn2_runtime_is_mismatch(self):
        """The headline regression: build-time OBC2 + runtime GBn2 must
        surface ``implicit_solvent_topology_mismatch``, not silently run
        the wrong GB model."""
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent="GBn2",
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"
        joined = " ".join(err["errors"])
        assert "OBC2" in joined and "GBn2" in joined

    def test_implicit_topo_with_explicit_runtime_is_mismatch(self):
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent=None,
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"
        assert "OBC2" in " ".join(err["errors"])

    def test_explicit_topo_with_implicit_runtime_is_mismatch(self):
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent=None,
            runtime_implicit_solvent="GBn2",
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"
        assert "GBn2" in " ".join(err["errors"])

    def test_custom_implicit_topo_requires_custom_runtime(self):
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent="custom",
            runtime_implicit_solvent="custom",
        ) is None

        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="custom",
            runtime_implicit_solvent=None,
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"

    def test_unknown_runtime_implicit_model_is_not_reported_as_mismatch(self):
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent="MAGIC_GB",
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_model_unsupported"

    def test_corrupt_topo_metadata_returns_distinct_code(self):
        """A garbage value in ``node.json`` ``metadata.implicit_solvent``
        surfaces as ``implicit_solvent_topology_metadata_invalid`` so it
        is not confused with a runtime typo."""
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="MAGIC_GB",
            runtime_implicit_solvent="OBC2",
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_metadata_invalid"

    def test_both_none_skips_guard(self):
        """Explicit-solvent topo + explicit-solvent run is the most common
        case; the guard must not fire."""
        from mdclaw.simulation._base import _check_topology_implicit_solvent_match
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent=None,
            runtime_implicit_solvent=None,
        ) is None


class TestImplicitSolventTopologyMismatchInRunFunctions:
    """Integration-y guard tests against the public ``run_equilibration`` /
    ``run_production`` surfaces. The guard fires before any OpenMM System
    is built, so these tests stay in the fast lane.
    """

    def _dag_with_modern_topo(self, tmp_path, topo_implicit_solvent):
        """Build a topo -> eq DAG where topo carries the modern triple plus
        ``metadata.implicit_solvent``. ``topo_implicit_solvent`` may be
        ``None`` (explicit / vacuum) or a string like ``"OBC2"``."""
        from mdclaw._node import create_node as _create_node
        from mdclaw._node import complete_node as _complete_node

        job_dir = tmp_path / "job"
        _create_node(str(job_dir), "topo")
        topo_artifacts = job_dir / "nodes" / "topo_001" / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / "system.xml").write_text("<placeholder/>")
        (topo_artifacts / "topology.pdb").write_text("REMARK fake\nEND\n")
        (topo_artifacts / "state.xml").write_text("<placeholder/>")
        meta = {
            "hmr": True,
            "solvent_type": "implicit" if topo_implicit_solvent else "vacuum",
        }
        if topo_implicit_solvent is not None:
            meta["implicit_solvent"] = topo_implicit_solvent
        _complete_node(
            str(job_dir),
            "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
            metadata=meta,
        )
        _create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])
        eq_artifacts = job_dir / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / "equilibrated.xml").write_text("<placeholder/>")
        _complete_node(
            str(job_dir),
            "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        return job_dir

    def test_run_equilibration_obc2_topo_gbn2_runtime_fails(self, tmp_path):
        """Topo metadata says OBC2; user passes ``--implicit-solvent GBn2``.
        Must fail with ``implicit_solvent_topology_mismatch`` and never
        reach SystemGenerator."""
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation.equilibrate import run_equilibration

        job_dir = self._dag_with_modern_topo(tmp_path, "OBC2")
        create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])

        result = run_equilibration(
            implicit_solvent="GBn2",
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="eq_002",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_topology_mismatch"
        joined = (
            " ".join(result.get("errors", [])) + str(result.get("message", ""))
        )
        assert "OBC2" in joined and "GBn2" in joined
        eq_node = read_node(str(job_dir), "eq_002")
        assert eq_node["status"] == "failed", eq_node["status"]

    def test_run_production_obc2_topo_gbn2_runtime_fails(self, tmp_path):
        from mdclaw._node import create_node, read_node
        from mdclaw.simulation.production import run_production

        job_dir = self._dag_with_modern_topo(tmp_path, "OBC2")
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])

        result = run_production(
            simulation_time_ns=0.001,
            implicit_solvent="GBn2",
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="prod_001",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_topology_mismatch"
        prod_node = read_node(str(job_dir), "prod_001")
        assert prod_node["status"] == "failed", prod_node["status"]

    def test_run_production_alias_match_passes_guard(self, tmp_path):
        """Topo metadata says ``GBn2`` and the runtime arg is ``gbneck2``.
        The guard must not fire — the run will eventually fail later
        because ``system.xml`` is a placeholder, but specifically NOT
        with the topology-mismatch code."""
        from mdclaw._node import create_node
        from mdclaw.simulation.production import run_production

        job_dir = self._dag_with_modern_topo(tmp_path, "GBn2")
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])

        result = run_production(
            simulation_time_ns=0.001,
            implicit_solvent="gbneck2",
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="prod_001",
        )
        assert result.get("code") != "implicit_solvent_topology_mismatch"

    def test_run_equilibration_explicit_topo_with_runtime_implicit_fails(
        self, tmp_path,
    ):
        """Topo built without GB; runtime passes ``--implicit-solvent``."""
        from mdclaw._node import create_node
        from mdclaw.simulation.equilibrate import run_equilibration

        job_dir = self._dag_with_modern_topo(tmp_path, None)
        create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])

        result = run_equilibration(
            implicit_solvent="OBC2",
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="eq_002",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_topology_mismatch"

    def test_run_equilibration_implicit_topo_without_runtime_implicit_fails(
        self, tmp_path,
    ):
        """Topo built with GB; runtime omits ``--implicit-solvent``."""
        from mdclaw._node import create_node
        from mdclaw.simulation.equilibrate import run_equilibration

        job_dir = self._dag_with_modern_topo(tmp_path, "OBC2")
        create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])

        result = run_equilibration(
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="eq_002",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_topology_mismatch"
