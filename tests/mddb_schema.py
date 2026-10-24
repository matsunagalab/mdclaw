"""Run the pinned official MDDB schema without importing the MDDB workflow."""

from copy import deepcopy
from functools import cache
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


_SNAPSHOT = Path(__file__).parent / "data" / "mddb_schema"


@cache
def official_mddb_schema():
    provenance = json.loads((_SNAPSHOT / "provenance.json").read_text())
    for filename, expected in provenance["sha256"].items():
        actual = hashlib.sha256((_SNAPSHOT / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"MDDB schema snapshot checksum mismatch: {filename}")

    constants = ModuleType("mddb_workflow.utils.constants")
    constants.__dict__.update(json.loads((_SNAPSHOT / "constants.json").read_text()))
    auxiliar = ModuleType("mddb_workflow.utils.auxiliar")
    auxiliar.InputError = ValueError

    def unexpected_warning(message):
        raise ValueError(f"Unexpected MDDB schema warning: {message}")

    auxiliar.warn = unexpected_warning
    schema = ModuleType("_mdclaw_test_official_mddb_schema")
    source = _SNAPSHOT / "inputs_schema.py.txt"
    # The upstream validator stays byte-for-byte intact. Only its logging/error
    # adapter and four constants are supplied, avoiding GROMACS/RDKit/BSON imports.
    # Restore sys.modules even if compilation or model construction fails.
    with pytest.MonkeyPatch.context() as patch:
        for module in (constants, auxiliar, schema):
            patch.setitem(sys.modules, module.__name__, module)
        exec(compile(source.read_bytes(), str(source), "exec"), schema.__dict__)
    return schema


def validate_mddb_inputs(inputs):
    """Validate project metadata and each effective MD override, without mutation."""
    schema = official_mddb_schema()
    schema.validate_inputs(deepcopy(inputs), strict_unknown=True)
    for index, md in enumerate(inputs.get("mds") or []):
        # Upstream permits project fields on each MD without declaring their
        # types there. Validate their effective values through WorkflowInputs.
        effective = {**inputs, **{k: v for k, v in md.items() if k not in ("mdir", "removed")}}
        try:
            schema.validate_inputs(deepcopy(effective), strict_unknown=True)
        except ValueError as exc:
            raise ValueError(f"mds[{index}]: {exc}") from exc
