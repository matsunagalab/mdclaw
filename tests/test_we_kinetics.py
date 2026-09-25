"""Rate estimators of analyze_we, and the whole WE machinery on a lattice.

The lattice test runs the real resampler with recycling on a discrete
double-well chain whose mean first-passage time is exact (linear solve), so
the steady-state flux is checked against a known rate without OpenMM.
"""

import math

import numpy as np
import pytest

from mdclaw.we import kinetics as kinetics_module
from mdclaw.we.kinetics import (
    block_bootstrap_mean,
    flux_relaxation_fit,
    integrated_autocorrelation_rounds,
    mann_kendall,
    stationary_suffix,
    steady_state_rate,
    two_state_fit,
    window_level_check,
    window_sensitivity,
)
from mdclaw.we.resample import normalize_edges, normalize_target, resample


class TestFits:
    def test_flux_relaxation_fit_recovers_the_plateau(self):
        rng = np.random.default_rng(0)
        t = np.arange(1, 121) * 0.05
        flux = 2.0 * (1 - np.exp(-t / 0.8)) + rng.normal(0, 0.05, t.size)
        fit = flux_relaxation_fit(t, flux)
        assert fit["fitted"] is True
        assert fit["f_ss"] == pytest.approx(2.0, rel=0.05)
        assert fit["tau"] == pytest.approx(0.8, rel=0.15)
        assert fit["rmse"] < 0.1

    def test_steady_state_verdicts(self):
        rng = np.random.default_rng(1)
        t = np.arange(1, 201) * 0.1
        steady = 1.0 * (1 - np.exp(-t / 1.0)) + rng.normal(0, 0.03, t.size)
        result = steady_state_rate(t, steady)
        assert result["verdict"] == "flux_steady", result["verdict_reasons"]
        assert result["rate"] == pytest.approx(1.0, rel=0.05)
        assert result["rate_low"] <= result["rate"] <= result["rate_high"]
        # the window starts after two relaxation times, not at the last quarter
        assert 10 <= result["window"]["first_round_index"] <= 30
        assert result["window"]["rounds"] == t.size - result["window"]["first_round_index"]

        # too few recycling events in the window: no estimate at all
        few = steady_state_rate(t, steady, events=np.zeros(t.size, dtype=int))
        assert few["verdict"] == "flux_undersampled" and few["rate"] is None
        assert any("recycling events" in reason for reason in few["verdict_reasons"])
        assert few["window"]["mean"] == pytest.approx(1.0, rel=0.05)
        enough = steady_state_rate(t, steady, events=np.ones(t.size, dtype=int), min_events=5)
        assert enough["verdict"] == "flux_steady", enough["verdict_reasons"]

        slow = 1.0 * (1 - np.exp(-t / 40.0)) + rng.normal(0, 0.01, t.size)
        result = steady_state_rate(t, slow)
        assert result["verdict"] == "flux_transient"
        assert any("relaxation time" in reason for reason in result["verdict_reasons"])

        assert steady_state_rate(t, np.zeros_like(t))["verdict"] == "no_target_events"
        assert steady_state_rate(np.array([]), np.array([]))["verdict"] == "no_target_events"

    def test_two_state_fit(self):
        t = np.arange(1, 101) * 0.2
        k_ab, k_ba = 0.3, 0.7
        p = k_ab / (k_ab + k_ba) * (1 - np.exp(-(k_ab + k_ba) * t))
        fit = two_state_fit(t, p)
        assert fit["fitted"] is True
        assert fit["k_ab"] == pytest.approx(k_ab, rel=1e-3) and fit["k_ba"] == pytest.approx(k_ba, rel=1e-3)
        assert two_state_fit(t, np.zeros_like(t))["fitted"] is False

    def test_block_bootstrap_interval_brackets_the_mean(self):
        rng = np.random.default_rng(2)
        values = 3.0 + rng.normal(0, 1.0, 400)
        boot = block_bootstrap_mean(values, block=5, n_boot=300, seed=3)
        assert boot["low"] < boot["mean"] < boot["high"]
        assert boot["mean"] == pytest.approx(3.0, abs=0.2)
        assert block_bootstrap_mean(np.array([]), block=3)["mean"] is None

    def test_block_longer_than_the_window_is_capped_not_degenerate(self):
        rng = np.random.default_rng(4)
        values = 1.0 + rng.normal(0, 0.2, 60)
        boot = block_bootstrap_mean(values, block=10_000, n_boot=200, seed=5)
        assert boot["block"] == 12 and boot["block_capped"] is True
        assert boot["low"] < boot["mean"] < boot["high"] and boot["high"] - boot["low"] > 0.0
        plain = block_bootstrap_mean(values, block=4, n_boot=200, seed=5)
        assert plain["block"] == 4 and plain["block_capped"] is False


# ---------------------------------------------------------------------------
# lattice weighted ensemble against the exact mean first-passage time
# ---------------------------------------------------------------------------


def _lattice(n_states=12, barrier_kt=4.0):
    """Metropolis nearest-neighbour walk on a symmetric double well."""
    x = np.arange(n_states)
    energy = barrier_kt * np.sin(math.pi * x / (n_states - 1)) ** 2
    T = np.zeros((n_states, n_states))
    for i in range(n_states):
        for j in (i - 1, i + 1):
            if 0 <= j < n_states:
                T[i, j] = 0.5 * min(1.0, math.exp(-(energy[j] - energy[i])))
        T[i, i] = 1.0 - T[i].sum()
    return T


def _exact_mfpt(T, source, target):
    n = T.shape[0]
    transient = [i for i in range(n) if i != target]
    Q = T[np.ix_(transient, transient)]
    m = np.linalg.solve(np.eye(len(transient)) - Q, np.ones(len(transient)))
    return float(m[transient.index(source)])


def _propagate(states, T, steps, rng):
    cumulative = np.cumsum(T, axis=1)
    out = np.array(states, dtype=int)
    for _ in range(steps):
        draws = rng.random(out.size)
        out = np.array([int(np.searchsorted(cumulative[s], d)) for s, d in zip(out, draws)])
    return np.minimum(out, T.shape[0] - 1)


def test_lattice_weighted_ensemble_reproduces_the_exact_rate():
    n_states, tau, walkers_per_bin, rounds = 12, 10, 8, 320
    T = _lattice(n_states)
    k_exact = 1.0 / _exact_mfpt(T, 0, n_states - 1)
    rng = np.random.default_rng(7)

    edges = normalize_edges([[i + 0.5 for i in range(n_states - 1)]], 1)
    target = normalize_target({"pcoord_ranges": [[n_states - 1.5, None]]}, 1)
    walkers = [{"id": f"r0w{i}", "replica": i + 1, "weight": 1.0 / walkers_per_bin, "state": 0}
               for i in range(walkers_per_bin)]
    flux = []
    for round_index in range(1, rounds + 1):
        states = _propagate([w["state"] for w in walkers], T, tau, rng)
        for walker, state in zip(walkers, states):
            walker["state"] = int(state)
            walker["pcoord"] = [float(state)]
        step = resample(walkers, walkers_per_bin=walkers_per_bin, edges=edges, target=target,
                        recycle=True, basis_node_ids=["A"], seed=round_index)
        flux.append(step["flux"]["weight_recycled"] / tau)
        by_id = {w["id"]: w for w in walkers}
        walkers = []
        for child in step["children"]:
            state = 0 if child.get("start_node_id") else by_id[child["parent_node_id"]]["state"]
            walkers.append({"id": f"r{round_index}w{child['replica']}", "replica": child["replica"],
                            "weight": child["weight"], "state": state})
        assert sum(w["weight"] for w in walkers) == pytest.approx(1.0)
        # recycled walkers restart at the basis on top of the binned ones
        assert len(walkers) <= walkers_per_bin * (n_states - 1) + step["flux"]["events"]

    t = np.arange(1, rounds + 1) * float(tau)
    result = steady_state_rate(t, np.asarray(flux), n_boot=100)
    assert result["verdict"] == "flux_steady", result["verdict_reasons"]
    ratio = result["rate"] / k_exact
    assert 0.7 < ratio < 1.3, f"k_WE / k_exact = {ratio:.3f} (k_exact = {k_exact:.3e} per step)"


# ---------------------------------------------------------------------------
# WE-23: a spiky fast flux is steady by its stationary stretch, not by the fit
# ---------------------------------------------------------------------------


def _unfitted(t, flux):
    return {"fitted": False, "tau": None, "tau_err": None, "f_ss": None, "f_ss_err": None, "rmse": None}


class TestStationarity:
    def test_mann_kendall_flags_a_trend_and_accepts_noise(self):
        rng = np.random.default_rng(3)
        rising = np.arange(60) * 0.02 + rng.normal(0, 0.1, 60)
        flat = 1.0 + rng.normal(0, 0.3, 60)
        assert mann_kendall(rising)["p_value"] < 0.01
        assert mann_kendall(flat)["p_value"] > 0.05
        assert mann_kendall(np.zeros(10))["p_value"] == 1.0 and mann_kendall(np.ones(3))["p_value"] == 1.0
        assert integrated_autocorrelation_rounds(np.repeat([1.0, 3.0], 20)) > 5.0
        assert integrated_autocorrelation_rounds(flat) < 3.0

    def test_spiky_flat_flux_is_steady_by_the_stationary_stretch(self, monkeypatch):
        rng = np.random.default_rng(11)
        n = 80
        flux = np.zeros(n)
        for i in range(8, n):           # empty rounds and heavy walkers, but no trend
            flux[i] = 0.0 if rng.random() < 0.35 else rng.lognormal(0.0, 0.8)
        events = (flux > 0).astype(int)
        t = np.arange(1, n + 1) * 0.1
        monkeypatch.setattr(kinetics_module, "flux_relaxation_fit", _unfitted)

        result = steady_state_rate(t, flux, events=events)
        assert result["verdict"] == "flux_steady", result["verdict_reasons"]
        stat = result["window"]["stationarity"]
        assert stat["test"] == "mann_kendall" and stat["p_value"] >= 0.05
        assert 8 <= stat["start_round_index"] <= 14
        assert result["window"]["rounds"] == n - stat["start_round_index"]
        assert result["window"]["events"] == int(events[stat["start_round_index"]:].sum())
        assert result["rate"] == pytest.approx(float(flux[stat["start_round_index"]:].mean()))
        assert result["rate_low"] < result["rate"] < result["rate_high"]
        assert any("Mann-Kendall" in reason for reason in result["verdict_reasons"])

        # the stretch itself, with too few events, is no estimate
        few = steady_state_rate(t, flux, events=events, min_events=1000)
        assert few["verdict"] == "flux_undersampled" and few["rate"] is None

    def test_a_burst_in_a_flat_flux_does_not_shorten_the_window(self, monkeypatch):
        rng = np.random.default_rng(23)
        n = 64
        flux = np.zeros(n)
        for i in range(6, n):
            flux[i] = 0.0 if rng.random() < 0.3 else rng.lognormal(0.0, 0.5)
        flux[40:54] *= 8.0                    # a burst of heavy arrivals (WE-23b)
        events = (flux > 0).astype(int)
        t = np.arange(1, n + 1) * 0.1
        monkeypatch.setattr(kinetics_module, "flux_relaxation_fit", _unfitted)
        # the starts right at the burst read as a falling trend ...
        assert any(mann_kendall(flux[s:])["p_value"] < 0.05 for s in range(38, 48))
        # ... but the earliest passing start keeps the long window (a walk from
        # the end that stopped at the first failure cut it at the burst)
        walk = stationary_suffix(flux, events, first_index=7, min_rounds=16, min_events=10, level_check=False)
        assert walk["start_round_index"] <= 10 and walk["rounds"] >= n - 10 and walk["candidates_tested"] == 1
        result = steady_state_rate(t, flux, events=events)
        assert result["verdict"] == "flux_steady", result["verdict_reasons"]
        stat = result["window"]["stationarity"]
        assert stat["p_value"] >= 0.05 and stat["rounds"] >= 0.6 * n and stat["level_check"]["compatible"]

    def test_rising_flux_stays_transient_without_a_fit(self, monkeypatch):
        rng = np.random.default_rng(5)
        n = 80
        t = np.arange(1, n + 1) * 0.1
        flux = np.clip(np.linspace(0, 2, n) + rng.normal(0, 0.05, n), 0, None)
        monkeypatch.setattr(kinetics_module, "flux_relaxation_fit", _unfitted)

        result = steady_state_rate(t, flux)
        assert result["verdict"] == "flux_transient"
        assert result["window"]["stationarity"] is None
        assert any("no stationary, level stretch" in reason for reason in result["verdict_reasons"])
        assert stationary_suffix(flux, None, first_index=1, min_rounds=20, min_events=5) is None


# ---------------------------------------------------------------------------
# WE-24: the burn-in counts from the first arrival, the window must be level
# ---------------------------------------------------------------------------


class TestLevelWindow:
    def test_level_check_flags_a_pulse_and_a_rise_but_not_noise(self):
        rng = np.random.default_rng(24)
        flat = 1.0 + rng.normal(0, 0.3, 80)
        assert window_level_check(flat)["compatible"] is True
        pulse = flat.copy()
        pulse[:15] *= 6.0
        check = window_level_check(pulse)
        assert check["compatible"] is False and check["head_mean"] > 3 * check["rest_mean"]
        rise = np.concatenate([0.5 + rng.normal(0, 0.1, 30), 2.5 + rng.normal(0, 0.3, 50)])
        assert window_level_check(rise)["compatible"] is False
        # a smooth residual rise of a few per cent is detectable but immaterial
        smooth = 1.0 - 0.05 * np.exp(-np.arange(80) / 20.0) + rng.normal(0, 0.005, 80)
        assert window_level_check(smooth)["compatible"] is True
        assert window_level_check(np.ones(5))["compatible"] is True
        rows = window_sensitivity(flat, None, 20)
        assert [r["start_round_index"] for r in rows] == [20, 35, 50, 65] and rows[0]["rounds"] == 60

    def test_overshoot_after_the_first_arrivals_is_left_out(self):
        # unf1-like: no arrivals for 19 rounds, a pulse of six times the
        # plateau for ten rounds, then a spiky plateau of 1.0
        rng = np.random.default_rng(1)
        n = 100
        t = np.arange(1, n + 1) * 0.2
        flux = np.zeros(n)
        for i in range(19, n):
            base = 6.0 if i < 30 else (2.2 if i < 45 else 1.0)
            flux[i] = 0.0 if rng.random() < 0.2 else base * rng.lognormal(0, 0.5)
        events = (flux > 0).astype(int) * rng.integers(1, 4, n)
        result = steady_state_rate(t, flux, events=events)
        assert result["verdict"] == "flux_steady", result["verdict_reasons"]
        window = result["window"]
        assert window["first_event_round_index"] == 19 and window["first_round_index"] >= 30
        assert window["level_check"]["compatible"] is True
        assert result["rate"] == pytest.approx(1.0, rel=0.15)
        means = [row["mean"] for row in window["sensitivity"]]
        assert max(means) / min(means) < 1.3
        # the plateau of the exponential rise is off (it cannot represent the
        # pulse) but the level window wins, and the reasons say so
        assert any("plateau is an artefact" in reason for reason in result["verdict_reasons"]) or \
            abs(result["fit"]["f_ss"] - result["rate"]) <= 0.2 * result["fit"]["f_ss"]

    def test_rise_inside_a_trend_free_window_moves_the_start(self):
        # fold1-like: nothing for 24 rounds, a low plateau, then a higher one
        rng = np.random.default_rng(1)
        n = 100
        t = np.arange(1, n + 1) * 0.2
        flux = np.zeros(n)
        for i in range(24, n):
            base = 0.6 if i < 55 else (2.5 if i < 85 else 1.5)
            flux[i] = 0.0 if rng.random() < 0.3 else base * rng.lognormal(0, 0.5)
        events = (flux > 0).astype(int) * rng.integers(1, 4, n)
        result = steady_state_rate(t, flux, events=events)
        window = result["window"]
        # either the window starts after the low stretch, or the run is called transient
        assert result["verdict"] == "flux_transient" or window["first_round_index"] >= 40, window
        if result["verdict"] == "flux_steady":
            assert window["level_check"]["compatible"] is True
            assert result["rate"] > 0.9   # not the 0.5 of the low stretch
