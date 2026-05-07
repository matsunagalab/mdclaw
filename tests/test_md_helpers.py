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
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node


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
        box_nm = 2.0

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

        from mdclaw.md_simulation_server import _load_state_into_simulation

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
        from mdclaw.md_simulation_server import _load_state_into_simulation

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

    def test_chk_path_takes_load_checkpoint_branch(self, tmp_path):
        """Binary checkpoint route requires identical System layout but
        is the fast same-GPU bit-exact path. Verify the loader returns
        ``"checkpoint"`` and the target context advances after the call."""
        from mdclaw.md_simulation_server import _load_state_into_simulation

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


# ----------------------------------------------------------------------------
# Modern-system shim contract (HMR / implicit-solvent baked into system.xml).
# ----------------------------------------------------------------------------


class TestModernPrmtopShimContract:
    """The shim must validate run-time createSystem kwargs against the saved
    system.xml so the user cannot silently ask for HMR or implicit solvent
    that build_amber_system did not bake in."""

    def _build_minimal_system(self, *, hmr: bool, implicit: bool, tmp_path):
        """Create a 1-residue ALA topology + a System optionally with HMR /
        a GBSA-OBC force, and serialize a system.xml ready for the shim."""
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
        return top, xml_path

    def test_shim_accepts_matching_hmr(self, tmp_path):
        from openmm.unit import amu
        from mdclaw.md_simulation_server import _ModernPrmtopShim

        topology, xml_path = self._build_minimal_system(
            hmr=True, implicit=False, tmp_path=tmp_path,
        )
        shim = _ModernPrmtopShim(topology, xml_path)
        # Asking for hydrogenMass=4 amu against an HMR system must succeed.
        sys_obj = shim.createSystem(hydrogenMass=4.0 * amu)
        assert sys_obj.getNumParticles() == 3

    def test_shim_rejects_hmr_request_against_non_hmr_system(self, tmp_path):
        from openmm.unit import amu
        from mdclaw.md_simulation_server import (
            _ModernPrmtopShim,
            _ModernSystemContractError,
        )

        topology, xml_path = self._build_minimal_system(
            hmr=False, implicit=False, tmp_path=tmp_path,
        )
        shim = _ModernPrmtopShim(topology, xml_path)
        with pytest.raises(_ModernSystemContractError) as exc_info:
            shim.createSystem(hydrogenMass=4.0 * amu)
        assert exc_info.value.code == "modern_system_hmr_mismatch"

    def test_shim_rejects_implicit_request_against_non_gb_system(self, tmp_path):
        from mdclaw.md_simulation_server import (
            _ModernPrmtopShim,
            _ModernSystemContractError,
        )

        topology, xml_path = self._build_minimal_system(
            hmr=False, implicit=False, tmp_path=tmp_path,
        )
        shim = _ModernPrmtopShim(topology, xml_path)
        with pytest.raises(_ModernSystemContractError) as exc_info:
            shim.createSystem(implicitSolvent="OBC2")
        assert exc_info.value.code == "modern_system_implicit_solvent_unsupported"

    def test_shim_accepts_implicit_request_when_gb_force_present(self, tmp_path):
        from mdclaw.md_simulation_server import _ModernPrmtopShim

        topology, xml_path = self._build_minimal_system(
            hmr=False, implicit=True, tmp_path=tmp_path,
        )
        shim = _ModernPrmtopShim(topology, xml_path)
        # With a GB force already on the System, the request is satisfiable.
        sys_obj = shim.createSystem(implicitSolvent="OBC2")
        assert any(
            type(f).__name__ in {"GBSAOBCForce", "CustomGBForce"}
            for f in sys_obj.getForces()
        )

    def test_shim_default_kwargs_are_pass_through(self, tmp_path):
        """No hydrogenMass / implicitSolvent in kwargs -> no validation."""
        from mdclaw.md_simulation_server import _ModernPrmtopShim
        topology, xml_path = self._build_minimal_system(
            hmr=False, implicit=False, tmp_path=tmp_path,
        )
        shim = _ModernPrmtopShim(topology, xml_path)
        sys_obj = shim.createSystem()
        assert sys_obj.getNumParticles() == 3


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
        from mdclaw.md_simulation_server import _fail_node_if_running

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
        from mdclaw.md_simulation_server import run_equilibration

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
        # ``inpcrd.boxVectors`` will be None → vacuum guardrail.
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
        from mdclaw.md_simulation_server import _fail_node_if_running

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

    def test_build_amber_system_passes_hmr_through_to_helper(self, tmp_path):
        """``build_amber_system(hmr=True)`` must reach the helper. The helper
        is mocked so we can assert the kwarg propagation without running
        SystemGenerator."""
        from unittest.mock import patch
        from mdclaw.amber_server import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber_server._run_openmmforcefields_build",
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
        from mdclaw.amber_server import _run_openmmforcefields_build

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
            valid_metal_params=[],
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
        from mdclaw.amber_server import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )

        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber_server._run_openmmforcefields_build",
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

    def test_build_amber_system_provenance_records_hmr(self, tmp_path):
        """When ``hmr=True`` builds successfully via openmmforcefields, the
        topo node's ``forcefield_provenance.method.hmr`` must reflect that
        choice so evidence_server / run_* can read the source of truth."""
        from unittest.mock import patch
        from mdclaw.amber_server import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber_server._run_openmmforcefields_build",
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
        from mdclaw.amber_server import build_amber_system

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
        from mdclaw.amber_server import build_amber_system

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
        from mdclaw.amber_server import build_amber_system

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
            "mdclaw.amber_server._run_openmmforcefields_build",
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
        from mdclaw.amber_server import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        _captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber_server._run_openmmforcefields_build",
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
        from mdclaw.amber_server import build_amber_system

        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\nEND\n"
        )
        captured, fake = self._fake_om_build_capturing_kwargs()
        with patch(
            "mdclaw.amber_server._run_openmmforcefields_build",
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
        from mdclaw.amber_server import build_amber_system

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

    def test_implicit_obc2_shim_accepts_implicitsolvent_request(
        self, small_pdb, tmp_path
    ):
        """End-to-end against the run-side contract: the ``_ModernPrmtopShim``
        must NOT raise ``modern_system_implicit_solvent_unsupported`` against
        an OBC2-built system.xml. This guards against future regressions
        where the build path stops baking the GB force."""
        pytest.importorskip("openmm")
        pytest.importorskip("openmmforcefields")
        pytest.importorskip("openff.pablo")
        pytest.importorskip("pdbfixer")

        from openmm import app
        from mdclaw.amber_server import build_amber_system
        from mdclaw.md_simulation_server import _ModernPrmtopShim

        result = build_amber_system(
            pdb_file=str(small_pdb),
            forcefield="ff14SBonlysc",
            implicit_solvent="OBC2",
            output_dir=str(tmp_path / "topo"),
        )
        assert result["success"] is True, result.get("errors")
        topology = app.PDBFile(result["topology_pdb"]).topology
        shim = _ModernPrmtopShim(topology, result["system_xml"])
        # No raise expected: the System has a GB force, the shim's
        # implicit-solvent contract is satisfied.
        system = shim.createSystem(implicitSolvent=app.OBC2)
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
        from mdclaw.md_simulation_server import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model("GBn2", models)
        assert err is None
        assert model is models["GBn2"]
        assert model is not models["OBC2"]

    def test_resolves_canonical_gbn_to_distinct_object_not_obc2(self):
        from mdclaw.md_simulation_server import _resolve_implicit_solvent_model

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
        from mdclaw.md_simulation_server import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model(alias, models)
        assert err is None
        assert model is models[expected_canonical]

    def test_unknown_model_returns_structured_error_not_obc2(self):
        """A typo / unknown GB model must surface the structured failure
        code rather than silently selecting OBC2 — the silent fallback was
        precisely the regression this helper exists to fix."""
        from mdclaw.md_simulation_server import _resolve_implicit_solvent_model

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
        from mdclaw.md_simulation_server import _resolve_implicit_solvent_model

        models = self._stub_models()
        model, err = _resolve_implicit_solvent_model("   ", models)
        assert model is None
        assert err["code"] == "implicit_solvent_model_unsupported"

    def test_catalog_known_but_openmm_map_missing_is_explicit_failure(self):
        """If the catalog lists a model that the run-side OpenMM map has
        not been updated for, the helper must report a structured failure
        rather than silently OBC2-fallback. Models a future drift between
        catalog and runtime."""
        from mdclaw.md_simulation_server import _resolve_implicit_solvent_model

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
    """Mock-level smoke verifying ``run_production`` honors GBn2 (not OBC2).

    The bug was that ``IMPLICIT_MODELS.get(implicit_solvent.upper(), OBC2)``
    silently mapped ``"GBn2"`` → ``"GBN2"`` → ``OBC2``. After the fix,
    ``run_production(implicit_solvent="GBn2")`` must reach
    ``prmtop.createSystem`` with ``implicitSolvent=app.GBn2``.
    """

    def test_gbn2_request_calls_createsystem_with_gbn2(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from openmm import app

        # Stand-in artifact triple — ``run_production`` short-circuits on
        # missing files before reaching createSystem, so we just need
        # paths that exist (their content is not parsed by the mock).
        sysxml = tmp_path / "system.xml"
        topo = tmp_path / "topology.pdb"
        state = tmp_path / "state.xml"
        for p in (sysxml, topo, state):
            p.write_text("<placeholder/>")

        # Mock prmtop / inpcrd loaders (the modern triple wrapper) so the
        # only path we care about — implicit-solvent createSystem — runs
        # against a captured MagicMock.
        fake_topology = MagicMock(name="topology")
        fake_topology.atoms.return_value = []
        fake_topology.residues.return_value = []
        fake_prmtop = MagicMock(name="prmtop")
        fake_prmtop.topology = fake_topology
        fake_prmtop.createSystem = MagicMock()
        # Sentinel system to surface as the createSystem return value.
        fake_system = MagicMock(name="system")
        fake_prmtop.createSystem.return_value = fake_system

        fake_inpcrd = MagicMock(name="inpcrd")
        fake_inpcrd.boxVectors = None  # non-periodic = implicit/vacuum

        from mdclaw import md_simulation_server as md_mod

        # Catch the createSystem call; we don't need a full simulation run,
        # just to confirm the implicit-solvent symbol matches GBn2 instead
        # of falling back to OBC2.
        captured: dict = {}

        original_create = fake_prmtop.createSystem

        def _capture_createsystem(**kwargs):
            captured["implicitSolvent"] = kwargs.get("implicitSolvent")
            captured["nonbondedMethod"] = kwargs.get("nonbondedMethod")
            # Surface OBC2 as a poison sentinel: if the lookup regressed
            # to OBC2, we'd see it here.
            return fake_system

        fake_prmtop.createSystem = _capture_createsystem
        # Restore reference for assertion below.
        _ = original_create

        with patch.object(
            md_mod, "_maybe_load_modern_topology",
            return_value=(fake_prmtop, fake_inpcrd),
        ), patch(
            "openmm.app.Simulation",
            side_effect=RuntimeError("__stop_after_createSystem__"),
        ):
            result = md_mod.run_production(
                system_xml_file=str(sysxml),
                topology_pdb_file=str(topo),
                state_xml_file=str(state),
                simulation_time_ns=0.001,
                implicit_solvent="GBn2",
                pressure_bar=0,
                output_dir=str(tmp_path / "out"),
            )

        # The simulation set-up was aborted on purpose; we only need to
        # confirm that createSystem received the GBn2 symbol (not OBC2).
        assert captured.get("implicitSolvent") is app.GBn2, (
            f"Expected app.GBn2; got {captured.get('implicitSolvent')!r} "
            "(silent OBC2 fallback regression)."
        )
        assert captured.get("nonbondedMethod") is app.NoCutoff
        # The function aborted via the planted RuntimeError; result will
        # carry an error message but the key invariant — the GB symbol
        # we routed in — is satisfied above.
        assert isinstance(result, dict)

    def test_unknown_implicit_model_fails_node_with_structured_code(
        self, tmp_path
    ):
        """run_production with an unknown GB model name must fail-fast
        with the structured ``implicit_solvent_model_unsupported`` code,
        not silently fall back to OBC2."""
        from unittest.mock import MagicMock, patch

        sysxml = tmp_path / "system.xml"
        topo = tmp_path / "topology.pdb"
        state = tmp_path / "state.xml"
        for p in (sysxml, topo, state):
            p.write_text("<placeholder/>")

        fake_topology = MagicMock(name="topology")
        fake_topology.atoms.return_value = []
        fake_topology.residues.return_value = []
        fake_prmtop = MagicMock(name="prmtop")
        fake_prmtop.topology = fake_topology
        # createSystem MUST NOT be called when the GB lookup fails — the
        # fail-fast path skips it. Wire it to raise so a regression is
        # impossible to miss.
        fake_prmtop.createSystem = MagicMock(
            side_effect=AssertionError(
                "createSystem must not be reached for an unknown GB model."
            )
        )
        fake_inpcrd = MagicMock(name="inpcrd")
        fake_inpcrd.boxVectors = None

        from mdclaw import md_simulation_server as md_mod

        with patch.object(
            md_mod, "_maybe_load_modern_topology",
            return_value=(fake_prmtop, fake_inpcrd),
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
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent="OBC2",
            is_modern_topology=True,
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
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent=build,
            runtime_implicit_solvent=runtime,
            is_modern_topology=True,
        ) is None

    def test_obc2_topo_with_gbn2_runtime_is_mismatch(self):
        """The headline regression: build-time OBC2 + runtime GBn2 must
        surface ``implicit_solvent_topology_mismatch``, not silently run
        the wrong GB model."""
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent="GBn2",
            is_modern_topology=True,
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"
        joined = " ".join(err["errors"])
        assert "OBC2" in joined and "GBn2" in joined

    def test_implicit_topo_with_explicit_runtime_is_mismatch(self):
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="OBC2",
            runtime_implicit_solvent=None,
            is_modern_topology=True,
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"
        assert "OBC2" in " ".join(err["errors"])

    def test_explicit_topo_with_implicit_runtime_is_mismatch(self):
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent=None,
            runtime_implicit_solvent="GBn2",
            is_modern_topology=True,
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_mismatch"
        assert "GBn2" in " ".join(err["errors"])

    def test_legacy_topology_skips_guard(self):
        """Legacy parm7/rst7 topo nodes carry no
        ``metadata.implicit_solvent`` and must not trip this guard."""
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent=None,
            runtime_implicit_solvent="OBC2",
            is_modern_topology=False,
        ) is None

    def test_corrupt_topo_metadata_returns_distinct_code(self):
        """A garbage value in ``node.json`` ``metadata.implicit_solvent``
        surfaces as ``implicit_solvent_topology_metadata_invalid`` so it
        is not confused with a runtime typo."""
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        err = _check_topology_implicit_solvent_match(
            topology_implicit_solvent="MAGIC_GB",
            runtime_implicit_solvent="OBC2",
            is_modern_topology=True,
        )
        assert err is not None
        assert err["code"] == "implicit_solvent_topology_metadata_invalid"

    def test_both_none_skips_guard(self):
        """Explicit-solvent topo + explicit-solvent run is the most common
        case; the guard must not fire."""
        from mdclaw.md_simulation_server import (
            _check_topology_implicit_solvent_match,
        )
        assert _check_topology_implicit_solvent_match(
            topology_implicit_solvent=None,
            runtime_implicit_solvent=None,
            is_modern_topology=True,
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
        from mdclaw.md_simulation_server import run_equilibration

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
        from mdclaw.md_simulation_server import run_production

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
        from mdclaw.md_simulation_server import run_production

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
        from mdclaw.md_simulation_server import run_equilibration

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
        from mdclaw.md_simulation_server import run_equilibration

        job_dir = self._dag_with_modern_topo(tmp_path, "OBC2")
        create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])

        result = run_equilibration(
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="eq_002",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_topology_mismatch"
