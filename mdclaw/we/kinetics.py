"""Rates from a weighted ensemble: steady-state flux, two-state relaxation,
block bootstrap. Pure numpy / scipy; ``analyze_we`` wires them to the DAG.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np


def _relaxation(t, f_ss, tau):
    return f_ss * (1.0 - np.exp(-t / tau))


def flux_relaxation_fit(t: np.ndarray, flux: np.ndarray) -> dict:
    """Fit ``F(t) = F_ss (1 - exp(-t / tau))`` to the per-round flux.

    The flux of a recycling weighted ensemble rises from zero towards its
    steady state; the fit gives the plateau and the relaxation time. A fit
    that fails (too few points, no events, singular covariance) is reported
    as ``fitted: False`` rather than guessed.
    """
    t = np.asarray(t, dtype=float)
    flux = np.asarray(flux, dtype=float)
    out = {"fitted": False, "f_ss": None, "tau": None, "f_ss_err": None, "tau_err": None, "rmse": None}
    if t.size < 4 or not np.any(flux > 0):
        return out
    try:
        from scipy.optimize import curve_fit

        f0 = float(np.mean(flux[-max(2, t.size // 4):]))
        tau0 = float(max(t[-1] / 4.0, t[1] - t[0]))
        params, cov = curve_fit(_relaxation, t, flux, p0=[max(f0, 1e-12), tau0],
                                bounds=([0.0, 1e-12], [np.inf, np.inf]), maxfev=10000)
    except Exception:  # noqa: BLE001 - any optimizer failure is "not fitted"
        return out
    f_ss, tau = (float(v) for v in params)
    errors = np.sqrt(np.diag(cov)) if np.all(np.isfinite(cov)) else np.array([np.nan, np.nan])
    residual = flux - _relaxation(t, f_ss, tau)
    out.update({
        "fitted": True, "f_ss": f_ss, "tau": tau,
        "f_ss_err": float(errors[0]) if math.isfinite(errors[0]) else None,
        "tau_err": float(errors[1]) if math.isfinite(errors[1]) else None,
        "rmse": float(np.sqrt(np.mean(residual ** 2))),
    })
    return out


MIN_BOOTSTRAP_BLOCKS = 5


def _bootstrap_block(block: int, n: int) -> tuple[int, bool]:
    """Block length for a moving-block bootstrap of ``n`` values: the
    requested one, capped so that at least ``MIN_BOOTSTRAP_BLOCKS`` blocks
    fit. A block as long as the window (a degenerate fit with a huge
    relaxation time) would resample the same series every time and report
    an interval of width zero; ``capped`` says the interval is optimistic
    because the correlation time exceeds a fifth of the window."""
    cap = max(1, n // MIN_BOOTSTRAP_BLOCKS)
    wanted = max(1, int(block))
    return min(wanted, cap), wanted > cap


def block_bootstrap_mean(values: np.ndarray, *, block: int, n_boot: int = 200,
                         seed: int = 0) -> dict:
    """Mean of a correlated series with a moving-block bootstrap interval."""
    values = np.asarray(values, dtype=float)
    n = values.size
    if n == 0:
        return {"mean": None, "low": None, "high": None, "n": 0, "block": block, "block_capped": False}
    block, capped = _bootstrap_block(block, n)
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block))
    starts = np.arange(0, n - block + 1)
    means = np.empty(n_boot)
    for b in range(n_boot):
        picks = rng.choice(starts, size=n_blocks, replace=True)
        sample = np.concatenate([values[s:s + block] for s in picks])[:n]
        means[b] = sample.mean()
    return {
        "mean": float(values.mean()),
        "low": float(np.percentile(means, 2.5)),
        "high": float(np.percentile(means, 97.5)),
        "n": int(n),
        "block": block,
        "block_capped": capped,
    }


def mann_kendall(values: np.ndarray) -> dict:
    """Mann-Kendall trend test (ties corrected, normal approximation):
    ``p_value`` is the two-sided probability of the observed monotone trend
    under no trend. Distribution-free, so it suits a spiky per-round flux."""
    x = np.asarray(values, dtype=float)
    n = int(x.size)
    if n < 4:
        return {"n": n, "s": 0, "z": 0.0, "p_value": 1.0}
    diff = np.sign(x[None, :] - x[:, None])
    s = float(np.triu(diff, k=1).sum())
    _, counts = np.unique(x, return_counts=True)
    ties = counts[counts > 1]
    var = (n * (n - 1) * (2 * n + 5) - float(np.sum(ties * (ties - 1) * (2 * ties + 5)))) / 18.0
    if var <= 0:
        return {"n": n, "s": int(s), "z": 0.0, "p_value": 1.0}
    z = (s - 1) / math.sqrt(var) if s > 0 else (s + 1) / math.sqrt(var) if s < 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return {"n": n, "s": int(s), "z": float(z), "p_value": float(p)}


def integrated_autocorrelation_rounds(values: np.ndarray) -> float:
    """Integrated autocorrelation time of a series in samples (1 + 2 sum of
    the autocorrelation up to its first negative lag): the bootstrap block
    a correlated flux needs when no relaxation time is fitted."""
    x = np.asarray(values, dtype=float)
    n = x.size
    if n < 4 or np.allclose(x, x[0]):
        return 1.0
    x = x - x.mean()
    denom = float(np.dot(x, x))
    tau = 1.0
    for lag in range(1, n // 2):
        rho = float(np.dot(x[:-lag], x[lag:])) / denom
        if rho <= 0:
            break
        tau += 2.0 * rho
    return float(tau)


def window_level_check(window: np.ndarray, *, n_boot: int = 200, seed: int = 0) -> dict:
    """Is the window level? Its first quarter against the rest, each with a
    block-bootstrap interval (block = the window's integrated autocorrelation
    time); compatible when either mean lies inside the other's interval.
    Catches what a monotone-trend test misses on a spiky flux: the
    overshoot right after the first arrivals and a rise that is still going
    on inside the window (WE-24)."""
    w = np.asarray(window, dtype=float)
    n = int(w.size)
    q = max(2, n // 4)
    if n < 8:
        return {"compatible": True, "head_rounds": 0, "rest_rounds": n,
                "note": "window too short to split"}
    block = max(1, int(math.ceil(integrated_autocorrelation_rounds(w))))
    head = block_bootstrap_mean(w[:q], block=block, n_boot=n_boot, seed=seed)
    rest = block_bootstrap_mean(w[q:], block=block, n_boot=n_boot, seed=seed + 1)
    # Compatible when either mean lies inside the other's interval, or the
    # two differ by less than 20 % of the larger (a smooth, low-noise
    # residual rise is detectable but immaterial at that level).
    scale = max(abs(head["mean"]), abs(rest["mean"]), 1e-300)
    compatible = bool((rest["low"] <= head["mean"] <= rest["high"])
                      or (head["low"] <= rest["mean"] <= head["high"])
                      or abs(head["mean"] - rest["mean"]) <= 0.2 * scale)
    return {"compatible": compatible, "head_rounds": int(q), "head_mean": head["mean"],
            "head_low": head["low"], "head_high": head["high"], "rest_rounds": int(n - q),
            "rest_mean": rest["mean"], "rest_low": rest["low"], "rest_high": rest["high"],
            "block": block}


def window_sensitivity(flux: np.ndarray, events: Optional[np.ndarray], start: int) -> list[dict]:
    """The window mean when its start is pushed later by quarters of its
    length: a reader sees at a glance whether the estimate depends on where
    the window begins."""
    flux = np.asarray(flux, dtype=float)
    n = int(flux.size)
    width = n - int(start)
    rows = []
    for k in range(4):
        s = int(start) + (k * width) // 4
        if n - s < 2:
            break
        w = flux[s:]
        n_events = int(np.asarray(events)[s:].sum()) if events is not None else int(np.count_nonzero(w > 0))
        rows.append({"start_round_index": int(s), "rounds": int(n - s), "events": n_events,
                     "mean": float(w.mean())})
    return rows


def stationary_suffix(flux: np.ndarray, events: Optional[np.ndarray], *, first_index: int,
                      min_rounds: int, min_events: int, alpha: float = 0.05,
                      max_candidates: int = 24, level_check: bool = True,
                      n_boot: int = 200, seed: int = 0) -> Optional[dict]:
    """The longest stretch at the end of the run with no monotone trend
    (Mann-Kendall, ``p >= alpha``) that holds at least ``min_events``
    recycling events: the earliest candidate start, never before
    ``first_index`` (the round after the first recycling event, so the
    empty rounds before any event are not read as a rise), whose stretch
    passes. A rise fails the test for every start inside it and passes only
    after it, so the earliest pass sits after the rise; a burst of events in
    the middle of a flat flux fails the test only for the starts right at
    the burst, and the earliest pass keeps the long window around it (a
    walk from the end that stopped at the first failure cut such windows
    short: WE-23b). With ``level_check`` the stretch must also be level
    (``window_level_check``: its first quarter agrees with the rest), which
    a monotone-trend test alone does not guarantee on a spiky flux (WE-24).
    None when no stretch passes."""
    flux = np.asarray(flux, dtype=float)
    n = int(flux.size)
    latest = n - int(min_rounds)
    first = max(0, int(first_index))
    if latest < first:
        return None
    count = min(int(max_candidates), latest - first + 1)
    candidates = np.unique(np.linspace(first, latest, num=count).round().astype(int))
    rejected = 0
    for start in candidates:
        window = flux[start:]
        mk = mann_kendall(window)
        n_events = (int(np.asarray(events)[start:].sum()) if events is not None
                    else int(np.count_nonzero(window > 0)))
        if mk["p_value"] >= alpha and n_events >= int(min_events):
            level = window_level_check(window, n_boot=n_boot, seed=seed) if level_check else None
            if level is None or level["compatible"]:
                return {"test": "mann_kendall", "start_round_index": int(start), "rounds": int(n - start),
                        "events": n_events, "p_value": round(mk["p_value"], 4), "z": round(mk["z"], 3),
                        "alpha": alpha, "candidates_tested": rejected + 1, "level_check": level}
        rejected += 1
    return None


def steady_state_rate(t: np.ndarray, flux: np.ndarray, *, events: Optional[np.ndarray] = None,
                      n_boot: int = 200, seed: int = 0, min_events: int = 10) -> dict:
    """Rate from the recycled flux: the mean over the rounds after the
    relaxation (burn-in of twice the fitted relaxation time, at most three
    quarters of the run; the last quarter when nothing can be fitted),
    judged steady when it agrees with the fitted plateau.

    ``verdict``: ``no_target_events`` (nothing recycled);
    ``flux_undersampled`` when the window holds fewer than ``min_events``
    recycling events (rounds with events when ``events`` is not given) — the
    window mean is then a fluctuation, not an estimate, and ``rate`` is None;
    ``flux_steady`` when the fit succeeded with a determined plateau
    (``f_ss_err / f_ss < 0.5``), the relaxation time is shorter than half
    the run and the window mean lies within the larger of its bootstrap
    interval and 20 % of the fitted plateau — or, when the fit cannot pin
    the relaxation down (a spiky fast flux fits an exponential badly), when
    the flux has a stationary stretch at the end of the run: no monotone
    trend by the Mann-Kendall test (``p >= 0.05``) over at least a quarter
    of the run with ``min_events`` events (``window.stationarity``); the
    window is then that whole stretch. Else ``flux_transient``: enough
    events, but the flux is still rising, so the window mean is a lower bound.
    The bootstrap block is the fitted relaxation time or, without one, the
    window's integrated autocorrelation time.
    """
    t = np.asarray(t, dtype=float)
    flux = np.asarray(flux, dtype=float)
    reasons: list[str] = []
    if t.size == 0 or not np.any(flux > 0):
        return {"verdict": "no_target_events", "verdict_reasons": ["no walker reached the target"],
                "rate": None, "window": None, "fit": flux_relaxation_fit(t, flux)}
    fit = flux_relaxation_fit(t, flux)
    dt = float(t[1] - t[0]) if t.size > 1 else 1.0
    tau_rounds = 1
    if fit["fitted"] and fit["tau"] and t.size > 1:
        tau_rounds = max(1, int(round(fit["tau"] / dt)))
    half_run = 0.5 * float(t[-1] - t[0]) if t.size > 1 else 0.0
    fit_reasons: list[str] = []
    if not fit["fitted"]:
        fit_reasons.append("the flux relaxation could not be fitted")
    else:
        if fit["tau"] > half_run:
            fit_reasons.append(f"fitted relaxation time {fit['tau']:.3g} exceeds half the run ({half_run:.3g})")
        # A plateau fitted through a handful of rounds has an error larger
        # than itself; the flux is then still rising. The relaxation time is
        # often ill-determined when the rise takes only a few rounds, so it
        # is not a criterion beyond the half-run bound above.
        if fit["f_ss_err"] is None or fit["f_ss"] <= 0 or fit["f_ss_err"] / fit["f_ss"] >= 0.5:
            fit_reasons.append("the fitted plateau is not determined (f_ss_err / f_ss >= 0.5)")
    fit_determined = not fit_reasons
    # Window: everything after the burn-in, never less than the last quarter.
    quarter = max(2, t.size // 4)
    first_event = int(np.argmax(flux > 0))
    path: Optional[str] = None
    start = t.size - quarter
    stationarity = None
    if fit_determined:
        # Two relaxation times of burn-in, counted from the first recycling
        # event: the lag before any walker arrives is not part of the
        # relaxation (WE-24). The window must then be level — the flux
        # often overshoots right after the first arrivals, which the
        # exponential rise cannot represent.
        burn_in = first_event + int(np.count_nonzero(t - t[0] < 2.0 * fit["tau"]))
        start_fit = max(0, min(burn_in, t.size - quarter))
        level = window_level_check(flux[start_fit:], n_boot=n_boot, seed=seed)
        if level["compatible"]:
            start, path = start_fit, "fit"
        else:
            fit_reasons.append(
                f"the window after the burn-in (round {start_fit + 1} on) is not level: its first quarter "
                f"averages {level['head_mean']:.3g} against {level['rest_mean']:.3g} for the rest "
                "(an overshoot after the first arrivals, or a rise inside the window)")
    if path is None:
        # A stationary, level stretch of the flux (no trend, first quarter
        # like the rest) is the window instead of the last quarter (WE-23: a
        # spiky fast flux fits an exponential badly although it is flat).
        stationarity = stationary_suffix(flux, events, first_index=first_event + 1,
                                         min_rounds=max(quarter, 6), min_events=min_events,
                                         n_boot=n_boot, seed=seed)
        if stationarity:
            start, path = stationarity["start_round_index"], "stationary"
    window = flux[start:]
    n_window = int(window.size)
    block = tau_rounds if path == "fit" else max(tau_rounds, int(math.ceil(integrated_autocorrelation_rounds(window))))
    boot = block_bootstrap_mean(window, block=min(block, n_window), n_boot=n_boot, seed=seed)
    rate = boot["mean"]
    window_events = (int(np.asarray(events)[start:].sum()) if events is not None
                     else int(np.count_nonzero(window > 0)))
    window_info = {"rounds": n_window, "first_round_index": int(start), "events": window_events,
                   "path": path or "last_quarter", "first_event_round_index": first_event,
                   "block": boot["block"], "block_capped": boot["block_capped"],
                   "min_events": int(min_events), "mean": rate,
                   "low": boot["low"], "high": boot["high"], "stationarity": stationarity,
                   "level_check": window_level_check(window, n_boot=n_boot, seed=seed),
                   "sensitivity": window_sensitivity(flux, events, start)}
    if window_events < min_events:
        # Two heavy walkers arriving in the same round make a window mean
        # several times the true rate; with this few events the number is a
        # fluctuation in either direction, not a bound.
        return {"verdict": "flux_undersampled",
                "verdict_reasons": [f"only {window_events} recycling events in the averaging window "
                                    f"(need {min_events}); the window mean is not an estimate"],
                "rate": None, "rate_low": None, "rate_high": None, "window": window_info, "fit": fit}
    verdict = "flux_steady"
    if boot["block_capped"]:
        reasons.append(f"bootstrap block capped to {boot['block']} rounds (a fifth of the window; the "
                       "correlation is longer): the interval is optimistic")
    if path == "fit":
        tolerance = max(boot["high"] - boot["mean"], boot["mean"] - boot["low"], 0.2 * fit["f_ss"])
        if abs(rate - fit["f_ss"]) > tolerance:
            # The exponential rise cannot represent an overshoot, so its
            # plateau may sit off a window that is itself level: a level,
            # trend-free window wins over the fitted plateau (WE-24).
            mk = mann_kendall(window)
            if mk["p_value"] >= 0.05:
                reasons.append(
                    f"window mean {rate:.3g} differs from the fitted plateau {fit['f_ss']:.3g}, but the "
                    f"window is level and trend-free (Mann-Kendall p = {mk['p_value']:.2f}): the window mean "
                    "is reported, the plateau is an artefact of the rise model")
            else:
                verdict = "flux_transient"
                reasons.append(f"window mean {rate:.3g} differs from the fitted plateau {fit['f_ss']:.3g} "
                               f"and the window still trends (Mann-Kendall p = {mk['p_value']:.2f})")
    elif path == "stationary":
        reasons.append(
            f"the relaxation fit does not give the window ({'; '.join(fit_reasons)}), but the flux from "
            f"round {stationarity['start_round_index'] + 1} on shows no trend (Mann-Kendall p = "
            f"{stationarity['p_value']:.2f}) and is level (first quarter "
            f"{stationarity['level_check']['head_mean']:.3g} vs rest {stationarity['level_check']['rest_mean']:.3g}; "
            f"{stationarity['events']} events over {stationarity['rounds']} rounds): the window is that stretch")
    else:
        verdict = "flux_transient"
        reasons.extend(fit_reasons)
        reasons.append(f"no stationary, level stretch with at least {min_events} events at the end of the "
                       "run (Mann-Kendall trend test and first-quarter check); the last quarter is reported")
    return {"verdict": verdict, "verdict_reasons": reasons, "rate": rate,
            "rate_low": boot["low"], "rate_high": boot["high"], "window": window_info, "fit": fit}


def _two_state(t, p_inf, k_tot):
    return p_inf * (1.0 - np.exp(-k_tot * t))


def two_state_fit(t: np.ndarray, p_target: np.ndarray) -> dict:
    """Fit ``P_B(t) = P_inf (1 - exp(-(k_AB + k_BA) t))`` to the target
    population of a non-recycling ensemble started in A: ``k_AB = k_tot
    P_inf`` and ``k_BA = k_tot (1 - P_inf)``."""
    t = np.asarray(t, dtype=float)
    p = np.asarray(p_target, dtype=float)
    out = {"fitted": False, "p_inf": None, "k_tot": None, "k_ab": None, "k_ba": None,
           "p_inf_err": None, "k_tot_err": None}
    if t.size < 4 or not np.any(p > 0):
        return out
    try:
        from scipy.optimize import curve_fit

        params, cov = curve_fit(_two_state, t, p, p0=[max(float(p[-1]), 1e-6), 1.0 / max(float(t[-1]), 1e-12)],
                                bounds=([0.0, 0.0], [1.0, np.inf]), maxfev=10000)
    except Exception:  # noqa: BLE001
        return out
    p_inf, k_tot = (float(v) for v in params)
    errors = np.sqrt(np.diag(cov)) if np.all(np.isfinite(cov)) else np.array([np.nan, np.nan])
    out.update({
        "fitted": True, "p_inf": p_inf, "k_tot": k_tot,
        "k_ab": k_tot * p_inf, "k_ba": k_tot * (1.0 - p_inf),
        "p_inf_err": float(errors[0]) if math.isfinite(errors[0]) else None,
        "k_tot_err": float(errors[1]) if math.isfinite(errors[1]) else None,
    })
    return out


def bootstrap_statistic(values: np.ndarray, statistic: Callable[[np.ndarray], Optional[float]], *,
                        block: int, n_boot: int = 200, seed: int = 0) -> dict:
    """Moving-block bootstrap interval of an arbitrary statistic of a series."""
    values = np.asarray(values, dtype=float)
    n = values.size
    if n == 0:
        return {"low": None, "high": None}
    block, _capped = _bootstrap_block(block, n)
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block))
    starts = np.arange(0, n - block + 1)
    samples = []
    for _ in range(n_boot):
        picks = rng.choice(starts, size=n_blocks, replace=True)
        sample = np.concatenate([values[s:s + block] for s in picks])[:n]
        value = statistic(sample)
        if value is not None and math.isfinite(value):
            samples.append(value)
    if not samples:
        return {"low": None, "high": None}
    return {"low": float(np.percentile(samples, 2.5)), "high": float(np.percentile(samples, 97.5))}
