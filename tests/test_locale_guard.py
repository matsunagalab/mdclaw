"""Guards for the process locale that OpenMM platform drivers reset.

Creating an OpenMM Context loads the platform driver, and Apple's OpenCL driver
calls ``setlocale(LC_ALL, "C")`` while initialising. That is process-global, so
after one topology build every later ``read_text()``/``write_text()`` without an
explicit encoding becomes ASCII, and the run starts failing on its own UTF-8
files — em dashes in a generated report, in the case that surfaced this.
"""

import locale
import re
from pathlib import Path

import pytest

from mdclaw._common import preserve_locale

REPO_ROOT = Path(__file__).resolve().parents[1]
MDCLAW = REPO_ROOT / "mdclaw"


def test_preserve_locale_restores_what_a_driver_changed():
    """The driver's setlocale is what this simulates; the guard must undo it."""
    before = locale.setlocale(locale.LC_ALL)
    try:
        with preserve_locale():
            locale.setlocale(locale.LC_ALL, "C")
            assert locale.setlocale(locale.LC_ALL) == "C"
        assert locale.setlocale(locale.LC_ALL) == before
    finally:
        locale.setlocale(locale.LC_ALL, before)


def test_preserve_locale_restores_after_an_exception():
    before = locale.setlocale(locale.LC_ALL)
    try:
        with pytest.raises(RuntimeError):
            with preserve_locale():
                locale.setlocale(locale.LC_ALL, "C")
                raise RuntimeError("boom")
        assert locale.setlocale(locale.LC_ALL) == before
    finally:
        locale.setlocale(locale.LC_ALL, before)


def test_no_module_constructs_a_simulation_directly():
    """Simulation construction must go through _common.new_simulation.

    A direct ``Simulation(...)`` leaves the process in the C locale on any
    platform whose driver resets it, and the damage shows up much later.
    """
    offenders = []
    for path in sorted(MDCLAW.rglob("*.py")):
        if path.name == "_common.py":
            continue  # the one deliberate construction, inside the guard
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])Simulation\(", line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "construct these through mdclaw._common.new_simulation:\n" + "\n".join(offenders)
    )
