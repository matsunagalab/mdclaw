"""Offline validation with the pinned, unmodified official MDDB schema."""

import builtins
from copy import deepcopy
from functools import cache
import hashlib
import json
from pathlib import Path
from types import ModuleType

_SNAPSHOT = Path(__file__).with_name("mddb_schema_data")


@cache
def official_mddb_schema():
    provenance = json.loads((_SNAPSHOT / "provenance.json").read_text())
    for filename in ("inputs_schema.py.txt", "constants.json", "LICENSE"):
        expected = provenance["sha256"][filename]
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
    adapters = {module.__name__: module for module in (constants, auxiliar)}

    def schema_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in adapters:
            return adapters[name]
        return builtins.__import__(name, globals, locals, fromlist, level)

    schema = ModuleType("mdclaw.evidence._official_mddb_schema")
    schema.__dict__["__builtins__"] = {**vars(builtins), "__import__": schema_import}
    source = _SNAPSHOT / "inputs_schema.py.txt"
    # The upstream validator stays byte-for-byte intact. Only its logging/error
    # adapter and four constants are supplied, avoiding GROMACS/RDKit/BSON imports.
    # Imports are scoped to this module's execution; never replace sys.modules
    # entries or process-wide builtins (including during concurrent exports).
    exec(compile(source.read_bytes(), str(source), "exec"), schema.__dict__)
    schema.provenance = provenance
    return schema


def validate_mddb_inputs(inputs):
    """Validate project metadata and each effective MD override, without mutation."""
    try:
        schema = official_mddb_schema()
    except (OSError, ImportError, SyntaxError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot load official MDDB schema: {exc}") from exc
    schema.validate_inputs(deepcopy(inputs), strict_unknown=True)
    for index, md in enumerate(inputs.get("mds") or []):
        # Upstream permits project fields on each MD without declaring their
        # types there. Validate their effective values through WorkflowInputs.
        effective = {**inputs, **{k: v for k, v in md.items() if k not in ("mdir", "removed")}}
        try:
            schema.validate_inputs(deepcopy(effective), strict_unknown=True)
        except ValueError as exc:
            raise ValueError(f"mds[{index}]: {exc}") from exc
    return {"repository": schema.provenance["repository"], "commit": schema.provenance["commit"],
            "sha256": schema.provenance["sha256"]["inputs_schema.py.txt"]}
