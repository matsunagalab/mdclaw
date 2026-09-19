"""Lambda schedule for the hybrid topology (``fep_protocol.json``).

A window is described by one scalar ``lambda`` in [0, 1] that drives the
five hybrid global parameters through three consecutive phases:

======================  ==========================  ======================
phase (lambda range)    what changes                parameters moving
======================  ==========================  ======================
0.00 - 0.25             switch off old charges       ``fep_elec_old`` 1 -> 0
0.25 - 0.75             swap sterics + shared terms  ``fep_sterics_old`` 1 -> 0,
                                                     ``fep_core`` 0 -> 1,
                                                     ``fep_sterics_new`` 0 -> 1
0.75 - 1.00             switch on new charges        ``fep_elec_new`` 0 -> 1
======================  ==========================  ======================

Charges are never on while the matching soft-core sterics are off, so the
end-point catastrophe cannot occur. The resolved parameter values of every
window are written to ``fep_protocol.json``; ``run_fep`` and ``analyze_fep``
read those explicit values and never recompute the mapping, so the file is
the contract even if this function changes later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Sequence

from mdclaw.fep.hybrid import DEFAULT_SOFTCORE_ALPHA, FEP_PARAMETERS, STATE_A, STATE_B

PROTOCOL_SCHEMA_VERSION = 1
DEFAULT_N_WINDOWS = 21
PHASE_BOUNDS = (0.25, 0.75)


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ramp(x: float, lo: float, hi: float) -> float:
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def lambda_to_parameters(lam: float) -> dict[str, float]:
    """Map the scalar window coordinate to the five hybrid global parameters."""
    lam = float(lam)
    if not 0.0 <= lam <= 1.0:
        raise ProtocolError(code="fep_protocol_invalid", message=f"lambda must be within [0, 1], got {lam}")
    p1, p2 = PHASE_BOUNDS
    swap = _ramp(lam, p1, p2)
    return {
        "fep_elec_old": 1.0 - _ramp(lam, 0.0, p1),
        "fep_sterics_old": 1.0 - swap,
        "fep_core": swap,
        "fep_sterics_new": swap,
        "fep_elec_new": _ramp(lam, p2, 1.0),
    }


def default_lambdas(n_windows: int = DEFAULT_N_WINDOWS) -> list[float]:
    n = int(n_windows)
    if n < 3:
        raise ProtocolError(code="fep_protocol_invalid", message=f"n_windows must be >= 3, got {n}")
    return [round(i / (n - 1), 6) for i in range(n)]


def _validate_parameter_dict(values: dict, index: int) -> dict[str, float]:
    unknown = sorted(set(values) - set(FEP_PARAMETERS))
    if unknown:
        raise ProtocolError(
            code="fep_protocol_invalid", message=f"window {index}: unknown global parameter(s) {unknown}; allowed: {list(FEP_PARAMETERS)}",
        )
    out = {}
    for name in FEP_PARAMETERS:
        if name not in values:
            raise ProtocolError(code="fep_protocol_invalid", message=f"window {index}: missing parameter {name!r}")
        v = float(values[name])
        if not 0.0 <= v <= 1.0:
            raise ProtocolError(code="fep_protocol_invalid", message=f"window {index}: {name}={v} is outside [0, 1]")
        out[name] = v
    return out


def windows_from_schedule(schedule: Optional[Sequence[Any]] = None, n_windows: Optional[int] = None) -> list[dict]:
    """Resolve the window list from ``--lambda-schedule`` / ``--n-windows``.

    ``schedule`` may be a list of scalar lambdas or a list of explicit
    parameter dicts (optionally carrying ``lambda``). ``None`` gives the
    evenly spaced default of ``n_windows`` windows.
    """
    if schedule is None:
        lambdas = default_lambdas(n_windows or DEFAULT_N_WINDOWS)
        return [
            {"index": i, "lambda": lam, "parameters": lambda_to_parameters(lam)}
            for i, lam in enumerate(lambdas)
        ]
    if isinstance(schedule, str):
        text = schedule.strip()
        if text.startswith("["):
            try:
                schedule = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProtocolError(code="fep_protocol_invalid", message=f"lambda_schedule is not valid JSON: {exc}") from exc
        else:
            try:
                schedule = [float(tok) for tok in text.replace(";", ",").split(",") if tok.strip()]
            except ValueError as exc:
                raise ProtocolError(
                    code="fep_protocol_invalid", message="lambda_schedule must be comma-separated lambdas (e.g. '0,0.1,...,1') or a JSON list",
                ) from exc
    if not isinstance(schedule, (list, tuple)) or len(schedule) < 2:
        raise ProtocolError(code="fep_protocol_invalid", message="lambda_schedule must be a list with at least two windows")
    windows = []
    for i, item in enumerate(schedule):
        if isinstance(item, dict):
            lam = item.get("lambda")
            params = {k: v for k, v in item.items() if k != "lambda"}
            if not params:
                if lam is None:
                    raise ProtocolError(code="fep_protocol_invalid", message=f"window {i}: empty schedule entry")
                params = lambda_to_parameters(lam)
            params = _validate_parameter_dict(params, i)
            windows.append({"index": i, "lambda": None if lam is None else float(lam), "parameters": params})
        else:
            lam = float(item)
            windows.append({"index": i, "lambda": lam, "parameters": lambda_to_parameters(lam)})
    first, last = windows[0]["parameters"], windows[-1]["parameters"]
    if any(abs(first[k] - STATE_A[k]) > 1e-9 for k in FEP_PARAMETERS):
        raise ProtocolError(code="fep_protocol_invalid", message="the first window must be state A (wild type)")
    if any(abs(last[k] - STATE_B[k]) > 1e-9 for k in FEP_PARAMETERS):
        raise ProtocolError(code="fep_protocol_invalid", message="the last window must be state B (mutant)")
    return windows


def build_protocol(
    *,
    mutation: dict,
    windows: list[dict],
    softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA,
    extra: Optional[dict] = None,
) -> dict:
    protocol = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "mutation": mutation,
        "softcore_alpha": float(softcore_alpha),
        "global_parameters": list(FEP_PARAMETERS),
        "state_a": dict(STATE_A),
        "state_b": dict(STATE_B),
        "phase_bounds": list(PHASE_BOUNDS),
        "n_windows": len(windows),
        "windows": windows,
    }
    if extra:
        protocol.update(extra)
    return protocol


def load_protocol(path: str | Path) -> dict:
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(code="fep_protocol_invalid", message=f"cannot read {p}: {exc}") from exc
    windows = data.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ProtocolError(code="fep_protocol_invalid", message=f"{p} has no windows")
    for i, w in enumerate(windows):
        w["parameters"] = _validate_parameter_dict(w.get("parameters", {}), i)
        w.setdefault("index", i)
    return data


def parse_lambda_indices(spec: Any, n_windows: int) -> list[int]:
    """``None`` -> all windows; list/int/"0-4,7" -> explicit sorted subset."""
    if spec is None or spec == "" or spec == "all":
        return list(range(n_windows))
    if isinstance(spec, int):
        items = [spec]
    elif isinstance(spec, str):
        items = []
        for tok in spec.replace(" ", "").split(","):
            if not tok:
                continue
            if "-" in tok:
                lo, hi = tok.split("-", 1)
                items.extend(range(int(lo), int(hi) + 1))
            else:
                items.append(int(tok))
    else:
        items = [int(x) for x in spec]
    out = sorted(set(items))
    bad = [i for i in out if i < 0 or i >= n_windows]
    if bad or not out:
        raise ProtocolError(
            code="fep_lambda_index_invalid", message=f"lambda_indices {spec!r} outside 0..{n_windows - 1}" if bad else "lambda_indices is empty",
        )
    return out


__all__ = [
    "DEFAULT_N_WINDOWS", "PHASE_BOUNDS", "PROTOCOL_SCHEMA_VERSION", "ProtocolError",
    "build_protocol", "default_lambdas", "lambda_to_parameters", "load_protocol",
    "parse_lambda_indices", "windows_from_schedule",
]
