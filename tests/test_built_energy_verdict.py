"""A relaxed built state with overlapping atoms is refused, not shipped.

006_membrane_6a94 cli_sif r3 (campaign v2): the topology build relaxed a cell
whose water overlapped across the periodic seam from 1.435e11 to 1.434e11
kJ/mol, wrote the artifact triple, and reported validation passed; the scorer's
potential_energy_is_physical check failed the attempt on that state.
"""

import math

from mdclaw.amber.openmm_build import _built_energy_verdict


def test_the_006_state_is_refused():
    ok, per_particle, message = _built_energy_verdict(143388166482.7, 83920)
    assert ok is False and per_particle > 1.0e6
    assert "per particle" in message and "solvation" in message


def test_a_normal_relaxed_cell_passes():
    ok, per_particle, message = _built_energy_verdict(-755347.0, 82970)
    assert ok is True and -10 < per_particle < -8 and message == ""


def test_non_finite_and_empty_cases():
    assert _built_energy_verdict(math.nan, 100)[0] is False
    assert _built_energy_verdict(math.inf, 100)[0] is False
    assert _built_energy_verdict(1.0e12, 0) == (True, None, "")
