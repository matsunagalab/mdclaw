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
# A protocol names its own global parameters (``global_parameters``); the five
# hybrid parameters are the default. ``fep_restraint`` scales the Boresch
# restraint of an absolute-binding complex leg (mdclaw.fep.boresch).
RESTRAINT_PARAMETER = "fep_restraint"
KNOWN_PARAMETERS: tuple[str, ...] = (*FEP_PARAMETERS, RESTRAINT_PARAMETER)


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


def parse_phase_bounds(value: Any) -> tuple[float, float]:
    """``"0.25,0.75"`` / ``[0.25, 0.75]`` / ``None`` (default) -> ``(p1, p2)``
    with ``0 < p1 < p2 < 1``: the lambda at which the old side chain is fully
    decharged and the lambda at which the steric swap is complete."""
    if value is None:
        return PHASE_BOUNDS
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProtocolError(code="fep_protocol_invalid", message=f"phase_bounds is not valid JSON: {exc}") from exc
        else:
            value = [tok for tok in text.replace(";", ",").split(",") if tok.strip()]
    try:
        bounds = tuple(float(x) for x in value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(code="fep_protocol_invalid",
                            message="phase_bounds must be two numbers, e.g. '0.25,0.75'") from exc
    if len(bounds) != 2 or not (0.0 < bounds[0] < bounds[1] < 1.0):
        raise ProtocolError(
            code="fep_protocol_invalid",
            message=f"phase_bounds must satisfy 0 < p1 < p2 < 1 (decharge end, steric swap end), got {value!r}")
    return bounds  # type: ignore[return-value]


def lambda_to_parameters(lam: float, phase_bounds: tuple[float, float] = PHASE_BOUNDS) -> dict[str, float]:
    """Map the scalar window coordinate to the five hybrid global parameters."""
    lam = float(lam)
    if not 0.0 <= lam <= 1.0:
        raise ProtocolError(code="fep_protocol_invalid", message=f"lambda must be within [0, 1], got {lam}")
    p1, p2 = phase_bounds
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


def protocol_parameter_names(protocol: dict) -> tuple[str, ...]:
    """The global parameters a protocol drives (default: the five hybrid ones)."""
    names = tuple(protocol.get("global_parameters") or FEP_PARAMETERS)
    unknown = sorted(set(names) - set(KNOWN_PARAMETERS))
    if unknown:
        raise ProtocolError(code="fep_protocol_invalid",
                            message=f"unknown global parameter(s) {unknown}; allowed: {list(KNOWN_PARAMETERS)}")
    return names


def _validate_parameter_dict(values: dict, index: int, names: Sequence[str] = FEP_PARAMETERS) -> dict[str, float]:
    unknown = sorted(set(values) - set(names))
    if unknown:
        raise ProtocolError(
            code="fep_protocol_invalid", message=f"window {index}: unknown global parameter(s) {unknown}; allowed: {list(names)}",
        )
    out = {}
    for name in names:
        if name not in values:
            raise ProtocolError(code="fep_protocol_invalid", message=f"window {index}: missing parameter {name!r}")
        v = float(values[name])
        if not 0.0 <= v <= 1.0:
            raise ProtocolError(code="fep_protocol_invalid", message=f"window {index}: {name}={v} is outside [0, 1]")
        out[name] = v
    return out


def _parse_lambda_list(schedule: Any) -> list[float]:
    """``"0,0.1,1"`` / ``"[0, 0.1, 1]"`` / ``[0, 0.1, 1]`` -> list of floats."""
    if isinstance(schedule, str):
        text = schedule.strip()
        if text.startswith("["):
            try:
                schedule = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProtocolError(code="fep_protocol_invalid", message=f"lambda_schedule is not valid JSON: {exc}") from exc
        else:
            schedule = [tok for tok in text.replace(";", ",").split(",") if tok.strip()]
    if not isinstance(schedule, (list, tuple)):
        raise ProtocolError(code="fep_protocol_invalid", message="lambda_schedule must be a list of lambdas")
    try:
        lambdas = [float(x) for x in schedule]
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            code="fep_protocol_invalid",
            message="lambda_schedule must be scalar lambdas in [0, 1] (e.g. '0,0.1,...,1' or a JSON list of numbers)",
        ) from exc
    return lambdas


def windows_from_schedule(schedule: Optional[Sequence[Any]] = None, n_windows: Optional[int] = None,
                          phase_bounds: Any = None) -> list[dict]:
    """Resolve the window list from ``--lambda-schedule`` / ``--n-windows``.

    ``schedule`` is a strictly increasing list of scalar lambdas from 0 to 1
    (CSV string, JSON string, or a Python sequence); ``None`` gives the evenly
    spaced default of ``n_windows`` windows. ``phase_bounds`` moves the
    decharge / steric-swap / recharge boundaries (default 0.25 / 0.75; a
    charge-changing mutation may want a longer decharge phase). Every window
    carries the five resolved global parameters so the file, not this
    function, is the contract downstream.
    """
    bounds = parse_phase_bounds(phase_bounds)
    if schedule is None:
        lambdas = default_lambdas(n_windows or DEFAULT_N_WINDOWS)
    else:
        lambdas = _parse_lambda_list(schedule)
    if len(lambdas) < 2:
        raise ProtocolError(code="fep_protocol_invalid", message="lambda_schedule must have at least two windows")
    if abs(lambdas[0]) > 1e-9 or abs(lambdas[-1] - 1.0) > 1e-9:
        raise ProtocolError(
            code="fep_protocol_invalid",
            message=f"lambda_schedule must start at 0 (wild type) and end at 1 (mutant), got {lambdas[0]}..{lambdas[-1]}")
    for prev, cur in zip(lambdas, lambdas[1:]):
        if cur <= prev:
            raise ProtocolError(
                code="fep_protocol_invalid",
                message=f"lambda_schedule must be strictly increasing (found {prev} followed by {cur}); "
                "neighbour overlap and phase sums assume index order = lambda order")
    return [
        {"index": i, "lambda": lam, "parameters": lambda_to_parameters(lam, bounds)}
        for i, lam in enumerate(lambdas)
    ]


def build_protocol(
    *,
    mutation: dict,
    windows: list[dict],
    softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA,
    phase_bounds: Any = None,
    extra: Optional[dict] = None,
) -> dict:
    protocol = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "mutation": mutation,
        "softcore_alpha": float(softcore_alpha),
        "global_parameters": list(FEP_PARAMETERS),
        "state_a": dict(STATE_A),
        "state_b": dict(STATE_B),
        "phase_bounds": list(parse_phase_bounds(phase_bounds)),
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
    names = protocol_parameter_names(data)
    for i, w in enumerate(windows):
        w["parameters"] = _validate_parameter_dict(w.get("parameters", {}), i, names)
        w.setdefault("index", i)
        if not isinstance(w.get("lambda"), (int, float)):
            raise ProtocolError(code="fep_protocol_invalid", message=f"{p}: window {i} has no scalar lambda")
        w["lambda"] = float(w["lambda"])
    data["phase_bounds"] = list(parse_phase_bounds(data.get("phase_bounds")))
    return data


def protocols_equivalent(a: dict, b: dict) -> bool:
    """Same windows (lambda + parameters) and mutation label: the samples of
    two fep nodes may be pooled by MBAR."""
    wa, wb = a.get("windows") or [], b.get("windows") or []
    if len(wa) != len(wb):
        return False
    for x, y in zip(wa, wb):
        if abs(float(x["lambda"]) - float(y["lambda"])) > 1e-9:
            return False
        if set(x["parameters"]) != set(y["parameters"]):
            return False
        if any(abs(x["parameters"][k] - y["parameters"][k]) > 1e-9 for k in x["parameters"]):
            return False
    return (a.get("mutation") or {}).get("label") == (b.get("mutation") or {}).get("label")


def parse_lambda_indices(spec: Any, n_windows: int) -> list[int]:
    """``None`` -> all windows; list/int/"0-4,7" -> explicit sorted subset.

    Raises ``ProtocolError(code="fep_lambda_index_invalid")`` for anything
    that is not a non-negative index or ``lo-hi`` range inside the protocol.
    """
    if spec is None or spec == "" or spec == "all":
        return list(range(n_windows))
    try:
        if isinstance(spec, bool):
            raise ValueError(spec)
        if isinstance(spec, int):
            items = [spec]
        elif isinstance(spec, str):
            items = []
            for tok in spec.replace(" ", "").split(","):
                if not tok:
                    continue
                if "-" in tok:
                    lo, hi = tok.split("-", 1)
                    if not (lo.isdigit() and hi.isdigit()):
                        raise ValueError(tok)
                    items.extend(range(int(lo), int(hi) + 1))
                else:
                    if not tok.isdigit():
                        raise ValueError(tok)
                    items.append(int(tok))
        else:
            items = [int(x) for x in spec]
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            code="fep_lambda_index_invalid",
            message=f"lambda_indices {spec!r} is not a comma-separated list of indices / 'lo-hi' ranges",
        ) from exc
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
    "parse_lambda_indices", "parse_phase_bounds", "protocols_equivalent", "windows_from_schedule",
]
