"""The halved-timestep retry policy and the restart integrator verdict."""

import pytest

from mdclaw.simulation.nan_retry import is_nan_failure, run_with_halved_timestep
from mdclaw.simulation.restart import _integrator_restart_verdict


class TestNanRetry:
    def test_a_nan_at_4fs_is_retried_at_2fs_from_the_same_state(self):
        calls = []

        def run(timestep):
            calls.append(timestep)
            if timestep > 2.0:
                raise RuntimeError("Particle coordinate is NaN. For more information, see ...")

        outcome = run_with_halved_timestep("low_temperature_warmup", 4.0, run)
        assert calls == [4.0, 2.0]
        assert outcome["timestep_fs"] == 2.0
        assert outcome["requested_timestep_fs"] == 4.0
        assert outcome["retried"] is True
        assert [a["outcome"] for a in outcome["attempts"]] == ["nan", "ok"]

    def test_a_clean_run_is_not_retried(self):
        outcome = run_with_halved_timestep("nvt_heating", 4.0, lambda dt: None)
        assert outcome["retried"] is False and outcome["timestep_fs"] == 4.0

    def test_the_floor_stops_the_halving(self):
        def always_nan(timestep):
            raise RuntimeError("Non-finite energy/force detected during warmup")

        with pytest.raises(RuntimeError, match="Non-finite"):
            run_with_halved_timestep("warmup", 4.0, always_nan, floor_fs=1.0)

    def test_other_errors_propagate_at_once(self):
        calls = []

        def broken(timestep):
            calls.append(timestep)
            raise ValueError("platform CUDA is not available")

        with pytest.raises(ValueError):
            run_with_halved_timestep("warmup", 4.0, broken)
        assert calls == [4.0]

    def test_nan_detection(self):
        assert is_nan_failure(RuntimeError("Particle coordinate is NaN"))
        assert is_nan_failure(RuntimeError("Non-finite energy/force detected during x"))
        assert not is_nan_failure(RuntimeError("Restart file not found"))


class TestIntegratorRestartVerdict:
    MISMATCH = ["timestep_fs: restart=2.0, current=4.0"]

    def test_eq_to_prod_timestep_change_is_a_warning(self):
        hard, soft = _integrator_restart_verdict(self.MISMATCH, restart_is_xml=True,
                                                 source_node_type="eq")
        assert hard == [] and soft == self.MISMATCH

    def test_prod_to_prod_continuation_keeps_it_hard(self):
        hard, soft = _integrator_restart_verdict(self.MISMATCH, restart_is_xml=True,
                                                 source_node_type="prod")
        assert hard == self.MISMATCH and soft == []

    def test_binary_checkpoint_keeps_everything_hard(self):
        hard, soft = _integrator_restart_verdict(self.MISMATCH, restart_is_xml=False,
                                                 source_node_type="eq")
        assert hard == self.MISMATCH

    def test_a_different_integrator_kind_stays_hard_even_from_eq(self):
        mismatches = ["integrator: restart='LangevinMiddle', current='Verlet'",
                      "temperature_kelvin: restart=300.0, current=310.0"]
        hard, soft = _integrator_restart_verdict(mismatches, restart_is_xml=True,
                                                 source_node_type="eq")
        assert hard == [mismatches[0]] and soft == [mismatches[1]]
