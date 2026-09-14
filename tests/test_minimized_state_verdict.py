"""A minimization that ends on superposed atoms does not complete the node.

011_membrane_6kuy cli_sif r3 (campaign v2): 5,000 steps ended at 1.88e10
kJ/mol with a max force of 3.7e9 kJ/mol/nm, min_001 completed, and the
equilibration NaN'd at 2, 1 and 0.5 fs.
"""

import math

from mdclaw.simulation.minimize import _minimized_state_verdict
from mdclaw.simulation.nan_retry import is_nan_failure


def test_the_011_state_is_refused():
    ok, message = _minimized_state_verdict(3.7e9, 1.88e10, 118494)
    assert ok is False and "on top of each other" in message


def test_a_relaxed_cell_passes():
    ok, message = _minimized_state_verdict(2896.0, -1190459.0, 83920)
    assert ok is True and message == ""


def test_non_finite_is_refused():
    assert _minimized_state_verdict(math.nan, -1.0, 10)[0] is False
    assert _minimized_state_verdict(10.0, math.inf, 10)[0] is False


def test_the_exhausted_nan_has_its_own_code():
    from mdclaw.guardrail_codes import GUARDRAIL_CODES

    assert is_nan_failure(Exception("Particle coordinate is NaN.  For more information, see ..."))
    assert "equilibration_nan_unrecoverable" in GUARDRAIL_CODES
    assert "minimized_state_implausible" in GUARDRAIL_CODES
