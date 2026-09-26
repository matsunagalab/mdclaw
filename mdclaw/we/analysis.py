"""``analyze_we``: rates and distributions from the rounds of a weighted ensemble.

Parents are ``we_resample`` policy nodes; each is followed back through
``metadata.scheme.previous_policy_node_id`` so one parent (the latest round)
yields the whole scheme. Per scheme the round ledgers give the recycled
flux, the target population, the bin weights and every walker's weight.

- recycling on: the flux ``F(t)`` is fitted with ``F_ss (1 - exp(-t/tau))``,
  the rate is the last-quarter mean (Hill relation, ``k = F_ss``), judged
  ``flux_steady`` or ``flux_transient`` (then a lower bound), with a
  moving-block bootstrap interval; ``MFPT = 1/k``. When the box volume is
  known the rate is also given per molar (``k_on`` if the target is a bound
  state: one ligand in the box).
- recycling off: the target population ``P_B(t)`` is fitted with the
  two-state relaxation ``P_inf (1 - exp(-(k_AB + k_BA) t))``.

Outputs: ``we_kinetics.json``, ``we_iterations.csv`` (per round), ``we_bins.csv``
(weighted bin populations after burn-in, -kT ln P), ``we_frames.csv`` (every
frame with its walker weight: the input of any weighted observable) and
``we_flux.png``.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import ensure_directory, setup_logger
from mdclaw._tool_meta import node_tool
from mdclaw.analyze.inputs import _rel_to_node_root
from mdclaw.node.io import _read_artifact_from_node, _read_node_json
from mdclaw.rounds.scheme import RoundsError, read_scheme
from mdclaw.we.kinetics import DRIFT_TOLERANCE_KT, steady_state_rate, two_state_fit
from mdclaw.we.policy import WEError, _recorded_temperature

logger = setup_logger(__name__)

GAS_CONSTANT_KJ_MOL_K = 8.314462618e-3
AVOGADRO = 6.02214076e23
DEFAULT_TEMPERATURE_K = 300.0


def _chain_of_policy_nodes(job_dir: str, leaf_id: str) -> list[dict]:
    """The policy nodes of one scheme, oldest round first, from a leaf."""
    chain: list[dict] = []
    current: Optional[str] = leaf_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        node = _read_node_json(job_dir, current) or {}
        meta = node.get("metadata") or {}
        if meta.get("analysis") != "we_resample":
            raise WEError(code="we_inputs_missing",
                          message=f"{current} is not a we_resample policy node (analysis={meta.get('analysis')!r})")
        if node.get("status") != "completed":
            raise WEError(code="we_inputs_missing", message=f"policy node {current} is {node.get('status')!r}")
        ledger_file = _read_artifact_from_node(job_dir, current, "we_round")
        if not ledger_file or not Path(ledger_file).is_file():
            raise WEError(code="we_inputs_missing", message=f"policy node {current} has no we_round artifact")
        chain.append({
            "node_id": current,
            "round": meta.get("round"),
            "scheme_id": meta.get("scheme_id"),
            "ledger": json.loads(Path(ledger_file).read_text()),
            "pcoords_file": _read_artifact_from_node(job_dir, current, "we_pcoords"),
        })
        scheme = meta.get("scheme") or {}
        current = scheme.get("previous_policy_node_id")
        if not current:
            deps = node.get("dependency_node_ids") or []
            current = deps[0] if deps else None
    chain.reverse()
    return chain


def _collect_schemes(job_dir: str, node: dict) -> dict[str, list[dict]]:
    parents = node.get("parent_node_ids") or []
    if not parents:
        raise WEError(code="we_inputs_missing", message="analyze_we needs we_resample policy nodes as parents")
    schemes: dict[str, dict[int, dict]] = {}
    for pid in parents:
        for entry in _chain_of_policy_nodes(job_dir, pid):
            key = entry["scheme_id"] or f"manual:{pid}"
            rounds = schemes.setdefault(key, {})
            round_index = entry["round"] if entry["round"] is not None else len(rounds) + 1
            rounds.setdefault(round_index, entry)
    return {key: [rounds[r] for r in sorted(rounds)] for key, rounds in schemes.items()}


def _tau_ns(job_dir: str, scheme_id: str, tau_ns: Optional[float]) -> float:
    if tau_ns is not None:
        return float(tau_ns)
    if scheme_id and not scheme_id.startswith("manual:"):
        try:
            scheme = read_scheme(job_dir, scheme_id)
        except RoundsError:
            scheme = None
        value = ((scheme or {}).get("stage_args") or {}).get("simulation_time_ns")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    raise WEError(code="we_inputs_missing",
                  message="the segment length is unknown; pass --tau-ns (the scheme's stage_args has no "
                          "simulation_time_ns)")


def _segment_temperatures(job_dir: str, rounds: list[dict]) -> set[float]:
    """Temperatures a scheme's segments ran at: from each round's ledger
    (``segment_temperatures_kelvin``, written by we_resample), or — for a
    ledger written before it recorded them — from the segments' node.json."""
    found: set[float] = set()
    for entry in rounds:
        ledger = entry["ledger"]
        recorded = ledger.get("segment_temperatures_kelvin")
        if isinstance(recorded, list):
            found.update(round(float(t), 6) for t in recorded
                         if not isinstance(t, bool) and isinstance(t, (int, float)) and t > 0)
            continue
        for walker in ledger.get("walkers") or []:
            if not isinstance(walker.get("id"), str):
                continue
            meta = (_read_node_json(job_dir, walker["id"]) or {}).get("metadata") or {}
            value = _recorded_temperature(meta)
            if value is not None:
                found.add(round(value, 6))
    return found


def _resolve_temperature(job_dir: str, schemes: dict[str, list[dict]],
                         explicit: Optional[float]) -> tuple[float, str, list[str]]:
    """kT's temperature for ``-kT ln P``: the one the segments ran at, so a
    WE at 340 K analysed with the defaults is not weighed at 300 K."""
    per_scheme = {key: sorted(_segment_temperatures(job_dir, rounds)) for key, rounds in schemes.items()}
    distinct = sorted({t for temps in per_scheme.values() for t in temps})
    listing = "; ".join(f"{key}: {', '.join(f'{t:g} K' for t in temps) or 'none recorded'}"
                        for key, temps in per_scheme.items())
    # A mix is refused whatever --temperature-kelvin says: the flag only sets
    # kT, and a rate from two ensembles is not a rate at either temperature.
    if distinct and distinct[-1] - distinct[0] > 1e-6:
        raise WEError(
            code="rounds_start_temperature_mismatch",
            message=(f"the segments ran at different temperatures ({listing}): their weights and flux mix "
                     "ensembles, and no single kT describes the bin populations. Analyse only policy nodes of "
                     "schemes whose segments ran at one temperature, or rerun the scheme from start and basis "
                     "nodes at one temperature; --temperature-kelvin only sets kT and does not repair the mix."),
        )
    if explicit is not None:
        warnings = []
        if any(abs(t - float(explicit)) > 1e-6 for t in distinct):
            warnings.append(f"temperature_kelvin={float(explicit):g} given, but the segments ran at {listing}; "
                            f"-kT ln P uses {float(explicit):g} K")
        return float(explicit), "explicit", warnings
    if distinct:
        return distinct[0], "segments", []
    return DEFAULT_TEMPERATURE_K, "default", [
        f"the segments record no temperature; -kT ln P uses {DEFAULT_TEMPERATURE_K:g} K "
        "(pass --temperature-kelvin if they ran elsewhere)"]


def _box_volume_nm3(job_dir: str, ledger: dict) -> Optional[float]:
    from mdtraj.formats import DCDTrajectoryFile

    for record in ledger.get("walkers") or []:
        trajectory = _read_artifact_from_node(job_dir, record["id"], "trajectory")
        if not trajectory or not Path(trajectory).is_file():
            continue
        try:
            with DCDTrajectoryFile(str(trajectory), "r") as infile:
                _xyz, lengths, angles = infile.read(n_frames=1)
        except Exception:  # noqa: BLE001
            return None
        if lengths is None or lengths.size == 0:
            return None
        a, b, c = (float(v) / 10.0 for v in lengths[0])
        alpha, beta, gamma = (math.radians(float(v)) for v in angles[0])
        cos_a, cos_b, cos_g = math.cos(alpha), math.cos(beta), math.cos(gamma)
        volume = a * b * c * math.sqrt(max(1.0 - cos_a ** 2 - cos_b ** 2 - cos_g ** 2
                                           + 2.0 * cos_a * cos_b * cos_g, 0.0))
        return volume
    return None


def _bin_edges(ledger: dict) -> list[list]:
    """Per-dimension boundary lists of the ledger's bins (None = infinite)."""
    edges = ((ledger.get("policy_args") or {}).get("bins") or {}).get("edges") or []
    return [list(dim) for dim in edges]


def _bin_bounds(bin_indices: list[int], edges: list[list]) -> list[list]:
    """``[[lo, hi], ...]`` of one bin from its per-dimension indices."""
    bounds = []
    for dim, index in enumerate(bin_indices):
        arr = edges[dim] if dim < len(edges) else []
        lo = arr[index] if index < len(arr) else None
        hi = arr[index + 1] if index + 1 < len(arr) else None
        bounds.append([lo, hi])
    return bounds


def _analyze_scheme(job_dir: str, key: str, rounds: list[dict], *, tau_ns: float, burn_in_rounds: Optional[int],
                    temperature_kelvin: float, n_bootstrap: int, min_events: int,
                    drift_tolerance_kt: float = DRIFT_TOLERANCE_KT) -> dict:
    ledgers = [entry["ledger"] for entry in rounds]
    round_numbers = np.asarray([entry["round"] if entry["round"] is not None else i + 1
                                for i, entry in enumerate(rounds)], dtype=float)
    t = round_numbers * tau_ns
    flux_weight = np.asarray([lg["flux"]["weight_recycled"] for lg in ledgers], dtype=float)
    events = np.asarray([lg["flux"]["events"] for lg in ledgers], dtype=int)
    flux_per_ns = flux_weight / tau_ns
    target_weight = np.asarray([lg.get("target_weight", 0.0) for lg in ledgers], dtype=float)
    recycle = bool((ledgers[-1].get("policy_args") or {}).get("recycle"))
    kt = GAS_CONSTANT_KJ_MOL_K * temperature_kelvin

    result: dict[str, Any] = {
        "scheme_id": key, "n_rounds": len(rounds), "tau_ns": tau_ns, "recycle": recycle,
        "time_ns": float(t[-1]),
        "aggregate_ns": float(sum(lg["n_in"] for lg in ledgers) * tau_ns),
        "rounds": [
            {"round": int(round_numbers[i]), "time_ns": float(t[i]), "n_walkers": lg["n_in"],
             "flux_per_ns": float(flux_per_ns[i]), "flux_weight": float(flux_weight[i]),
             "events": int(events[i]), "cumulative_flux_weight": float(flux_weight[:i + 1].sum()),
             "target_weight": float(target_weight[i]), "weight_min": lg.get("weight_min"),
             "weight_max": lg.get("weight_max"), "bins_occupied": len(lg.get("bins") or [])}
            for i, lg in enumerate(ledgers)
        ],
    }
    if recycle:
        rate = steady_state_rate(t, flux_per_ns, events=events, n_boot=n_bootstrap, min_events=min_events,
                                 drift_tolerance_kt=drift_tolerance_kt)
        result["kinetics"] = {"mode": "steady_state_flux", **rate}
        convergence = rate.get("convergence")
        if convergence:
            # Rounds and per-second rates for readers of the JSON; the round
            # index of the ledgers is 0-based.
            for point in convergence["history"]:
                point["round"] = int(round_numbers[point["round_index"]])
                for key_ns, key_s in (("rate", "rate_per_s"), ("low", "low_per_s"), ("high", "high_per_s")):
                    point[key_s] = None if point.get(key_ns) is None else point[key_ns] * 1e9
            convergence["second_half_start_round"] = int(round_numbers[min(convergence["second_half_start_index"],
                                                                           len(round_numbers) - 1)])
            for extreme in ("lowest", "highest"):
                convergence[extreme]["round"] = int(round_numbers[convergence[extreme]["round_index"]])
        if rate["rate"]:
            result["kinetics"]["rate_per_s"] = rate["rate"] * 1e9
            result["kinetics"]["rate_low_per_s"] = (rate["rate_low"] or 0.0) * 1e9
            result["kinetics"]["rate_high_per_s"] = (rate["rate_high"] or 0.0) * 1e9
            result["kinetics"]["mfpt_ns"] = 1.0 / rate["rate"]
        # A rate per molar only means something for a transition between
        # molecules (the pcoord holds an intermolecular distance).
        if ledgers[-1].get("pcoord_minimum_image"):
            volume = _box_volume_nm3(job_dir, ledgers[0])
            if volume:
                concentration = 1.0 / (AVOGADRO * volume * 1e-24)
                result["kinetics"]["box_volume_nm3"] = volume
                result["kinetics"]["concentration_molar"] = concentration
                if rate["rate"]:
                    result["kinetics"]["rate_per_molar_per_s"] = rate["rate"] * 1e9 / concentration
                    result["kinetics"]["rate_per_molar_note"] = (
                        "k_on if the target is the bound state: one ligand in the box; a small box "
                        "overestimates k_on")
        # How much longer to run before the verdict can change. A determined
        # fit says two relaxation times; otherwise the observed event rate
        # says how many rounds fill the averaging window with min_events.
        # Capped at 50: a degenerate fit once suggested 200 rounds (100 GPU-h
        # on a 0.5 GPU-h round), so the budget stays with --max-aggregate-ns.
        fit = rate.get("fit") or {}
        if rate["verdict"] == "rate_not_converged":
            # The window is steady but the answer still moved: half the run
            # again lets the second half of the longer run test it.
            result["kinetics"]["next_rounds_suggested"] = max(10, min(int(math.ceil(len(rounds) / 2)), 50))
            result["kinetics"]["next_rounds_basis"] = ("the reported rate still moved by more than the drift "
                                                       "tolerance over the second half: half the run again")
        elif rate["verdict"] != "flux_steady":
            determined = (fit.get("fitted") and fit.get("tau") and fit.get("tau_err") is not None
                          and fit.get("f_ss_err") is not None and fit["f_ss"] > 0
                          and fit["tau_err"] / fit["tau"] < 0.5 and fit["f_ss_err"] / fit["f_ss"] < 0.5)
            window_events = int((rate.get("window") or {}).get("events") or 0)
            events_per_round = float(events.sum()) / max(len(rounds), 1)
            if rate["verdict"] != "flux_undersampled" and determined:
                more = int(math.ceil(2.0 * fit["tau"] / tau_ns))
                basis = "two fitted relaxation times"
            elif events_per_round > 0:
                more = int(math.ceil(max(min_events - window_events, 1) / events_per_round))
                basis = f"{min_events} events at the observed {events_per_round:.2f} events per round"
            else:
                more = 20
                basis = "no events yet"
            result["kinetics"]["next_rounds_suggested"] = max(10, min(more, 50))
            result["kinetics"]["next_rounds_basis"] = basis
        result["kinetics"]["rate_note"] = {
            "flux_steady": "steady-state flux: the rate constant (Hill relation)",
            "rate_not_converged": ("steady window, but the reported rate still moves by more than the drift "
                                   "tolerance over the second half of the run: quote it only with that drift, "
                                   "or extend the scheme"),
            "flux_transient": "the flux is still rising: the window mean is a lower bound of the rate",
            "flux_undersampled": "too few recycling events: no rate; run more rounds",
            "no_target_events": "no walker reached the target: no rate",
        }.get(rate["verdict"])
    else:
        fit = two_state_fit(t, target_weight)
        verdict = "two_state_fitted" if fit["fitted"] else "two_state_unfitted"
        reasons = [] if fit["fitted"] else ["the target population could not be fitted (no target visits or too few rounds)"]
        result["kinetics"] = {"mode": "two_state_population", "verdict": verdict, "verdict_reasons": reasons, **fit}
        if fit["fitted"]:
            result["kinetics"]["k_ab_per_s"] = fit["k_ab"] * 1e9
            result["kinetics"]["k_ba_per_s"] = fit["k_ba"] * 1e9
        else:
            result["kinetics"]["next_rounds_suggested"] = 20
    # burn-in and bin populations
    if burn_in_rounds is None:
        tau_fit = (result["kinetics"].get("fit") or {}).get("tau") if recycle else None
        if tau_fit:
            burn_in_rounds = min(int(math.ceil(2.0 * tau_fit / tau_ns)), len(rounds) // 2)
        else:
            burn_in_rounds = 0
    burn_in_rounds = max(0, min(int(burn_in_rounds), max(len(rounds) - 1, 0)))
    result["burn_in_rounds"] = burn_in_rounds
    kept = ledgers[burn_in_rounds:]
    edges = _bin_edges(ledgers[-1])
    bin_totals: dict[int, dict] = {}
    for lg in kept:
        for entry in lg.get("bins") or []:
            slot = bin_totals.setdefault(entry["bin"], {"bin": entry["bin"], "bin_indices": entry["bin_indices"],
                                                        "weight_sum": 0.0, "rounds_occupied": 0})
            slot["weight_sum"] += float(entry["weight"])
            slot["rounds_occupied"] += 1
    bins = []
    for bin_id in sorted(bin_totals):
        slot = bin_totals[bin_id]
        mean_weight = slot["weight_sum"] / len(kept)
        bins.append({"bin": bin_id, "bin_indices": slot["bin_indices"],
                     "bounds": _bin_bounds(slot["bin_indices"], edges), "mean_weight": mean_weight,
                     "rounds_occupied": slot["rounds_occupied"],
                     "free_energy_kj_mol": (-kt * math.log(mean_weight)) if mean_weight > 0 else None})
    if bins:
        reference = min(b["free_energy_kj_mol"] for b in bins if b["free_energy_kj_mol"] is not None)
        for b in bins:
            if b["free_energy_kj_mol"] is not None:
                b["free_energy_kj_mol"] -= reference
    result["bins"] = bins
    result["bin_shape"] = ledgers[-1].get("bin_shape") or []
    result["pcoord_names"] = ledgers[-1].get("pcoord_names") or []
    result["distribution_note"] = ("non-equilibrium steady-state distribution (recycling on)" if recycle
                                   else "weighted distribution of a relaxing ensemble")
    return result


def _write_frames(job_dir: str, out_dir: Path, schemes: dict[str, list[dict]], taus: dict[str, float]) -> Optional[Path]:
    path = out_dir / "we_frames.csv"
    wrote = False
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        header_written = False
        for key, rounds in schemes.items():
            tau = taus[key]
            for entry in rounds:
                pcoords_file = entry["pcoords_file"]
                if not pcoords_file or not Path(pcoords_file).is_file():
                    continue
                weights = {w["id"]: w["weight"] for w in entry["ledger"].get("walkers") or []}
                frames = {w["id"]: w.get("n_frames") for w in entry["ledger"].get("walkers") or []}
                round_index = entry["round"] if entry["round"] is not None else 0
                with Path(pcoords_file).open() as src:
                    reader = csv.reader(src)
                    names = next(reader)[3:]
                    if not header_written:
                        writer.writerow(["scheme_id", "round", "replica", "node_id", "frame", "time_ns",
                                         "weight", *names])
                        header_written = True
                    for row in reader:
                        replica, node_id, frame = row[0], row[1], int(row[2])
                        n_frames = frames.get(node_id) or 1
                        time_ns = (round_index - 1) * tau + (frame + 1) * tau / n_frames
                        writer.writerow([key, round_index, replica, node_id, frame, f"{time_ns:.6f}",
                                         f"{weights.get(node_id, float('nan')):.6e}", *row[3:]])
                        wrote = True
    if not wrote:
        path.unlink(missing_ok=True)
        return None
    return path


def _plot(out_dir: Path, analyses: dict[str, dict]) -> Optional[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return None
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for key, analysis in analyses.items():
        t = [r["time_ns"] for r in analysis["rounds"]]
        if analysis["recycle"]:
            axes[0].plot(t, [r["flux_per_ns"] for r in analysis["rounds"]], ".-", label=f"{key} flux")
            fit = (analysis["kinetics"].get("fit") or {})
            if fit.get("fitted"):
                tt = np.linspace(0, max(t), 200)
                axes[0].plot(tt, fit["f_ss"] * (1 - np.exp(-tt / fit["tau"])), "--", label=f"{key} fit")
            axes[0].set_ylabel("flux into target (1/ns)")
        else:
            axes[0].plot(t, [r["target_weight"] for r in analysis["rounds"]], ".-", label=f"{key} P_target")
            axes[0].set_ylabel("target population")
    axes[0].set_xlabel("time (ns)")
    axes[0].legend(fontsize=8)
    # The distribution: a line over the bin index in one dimension, a map over
    # the two bin indices in two (a flattened index has no neighbourhood).
    first = next(iter(analyses.values()))
    names = first.get("pcoord_names") or []
    shape = first.get("bin_shape") or []
    bins = first["bins"]
    if bins and len(shape) == 2:
        grid = np.full(tuple(shape), np.nan)
        for b in bins:
            if b["free_energy_kj_mol"] is not None:
                grid[tuple(b["bin_indices"])] = b["free_energy_kj_mol"]
        image = axes[1].imshow(grid.T, origin="lower", aspect="auto", cmap="viridis")
        axes[1].set_xlabel(f"{names[0] if names else 'pcoord 1'} bin")
        axes[1].set_ylabel(f"{names[1] if len(names) > 1 else 'pcoord 2'} bin")
        fig.colorbar(image, ax=axes[1], label="-kT ln P (kJ/mol)")
    else:
        for key, analysis in analyses.items():
            if analysis["bins"]:
                axes[1].plot([b["bin"] for b in analysis["bins"]],
                             [b["free_energy_kj_mol"] if b["free_energy_kj_mol"] is not None else np.nan
                              for b in analysis["bins"]], "o-", label=key)
        axes[1].set_xlabel(f"{names[0] if names else 'pcoord'} bin")
        axes[1].set_ylabel("-kT ln P (kJ/mol)")
        axes[1].legend(fontsize=8)
    fig.tight_layout()
    path = out_dir / "we_flux.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def _write_convergence_csv(out_dir: Path, analyses: dict[str, dict]) -> Optional[Path]:
    rows = []
    for key, analysis in analyses.items():
        conv = (analysis.get("kinetics") or {}).get("convergence") or {}
        for point in conv.get("history") or []:
            rows.append([key, point["round"], f"{point['time']:.6f}",
                         "" if point.get("rate_per_s") is None else f"{point['rate_per_s']:.6e}",
                         "" if point.get("low_per_s") is None else f"{point['low_per_s']:.6e}",
                         "" if point.get("high_per_s") is None else f"{point['high_per_s']:.6e}",
                         point.get("verdict") or "",
                         "" if point.get("window_start_index") is None
                         else analysis["rounds"][point["window_start_index"]]["round"]])
    if not rows:
        return None
    path = out_dir / "we_convergence.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scheme_id", "round", "time_ns", "rate_per_s", "low_per_s", "high_per_s",
                         "verdict_at_stop", "window_start_round"])
        writer.writerows(rows)
    return path


def _plot_convergence(out_dir: Path, analyses: dict[str, dict], pooled: Optional[dict]) -> Optional[str]:
    """One panel per scheme: the rate the analysis would have reported had
    the run stopped after each round (with its bootstrap interval), the
    final estimate, and the second half of the run that the drift verdict
    looks at (green converged, red not) — the weighted-ensemble counterpart
    of dF(t) in ``analyze_metadynamics``. The per-round flux is in
    ``we_flux.png``."""
    panels = [(key, a) for key, a in analyses.items()
              if ((a.get("kinetics") or {}).get("convergence") or {}).get("history")]
    if not panels:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return None
    fig, axes = plt.subplots(len(panels), 1, figsize=(6.4, 3.2 * len(panels)), squeeze=False)
    for ax, (key, analysis) in zip(axes[:, 0], panels):
        kin = analysis["kinetics"]
        conv = kin["convergence"]
        tau = analysis["tau_ns"]
        history = [h for h in conv["history"] if h.get("rate_per_s")]
        ht = np.asarray([h["time"] for h in history])
        hk = np.asarray([h["rate_per_s"] for h in history])
        lo = np.asarray([h["low_per_s"] if h.get("low_per_s") else h["rate_per_s"] for h in history])
        hi = np.asarray([h["high_per_s"] if h.get("high_per_s") else h["rate_per_s"] for h in history])
        ax.fill_between(ht, lo, hi, color="tab:blue", alpha=0.18, lw=0, label="95 % interval at that stop")
        ax.plot(ht, hk, "-", color="tab:blue", lw=1.6, label="rate reported had the run stopped here")
        under = [h for h in history if h.get("verdict") == "flux_undersampled"]
        if under:
            ax.plot([h["time"] for h in under], [h["rate_per_s"] for h in under], "o", mfc="white",
                    mec="tab:blue", ms=3.5, label="that stop had too few events")
        final = kin.get("rate_per_s") or (float(hk[-1]) if hk.size else None)
        band = (kin.get("rate_low_per_s"), kin.get("rate_high_per_s"))
        if final:
            ax.axhline(final, color="0.3", lw=0.9, ls="--", label="final estimate")
            if band[0] and band[1]:
                ax.axhspan(band[0], band[1], color="0.5", alpha=0.10, lw=0)
        window = kin.get("window") or {}
        if window.get("first_round_index") is not None:
            ax.axvline(analysis["rounds"][window["first_round_index"]]["time_ns"] - tau, color="0.4", lw=0.8,
                       ls=":", label="final averaging window starts")
        t_end = analysis["rounds"][-1]["time_ns"]
        t_half = analysis["rounds"][conv["second_half_start_index"]]["time_ns"] - tau
        ok = bool(conv.get("converged"))
        ax.axvspan(t_half, t_end, color="tab:green" if ok else "tab:red", alpha=0.08, lw=0)
        ax.set_yscale("log")
        values = [v for v in [*lo.tolist(), *hi.tolist(), *hk.tolist(), final, *band] if v and v > 0]
        if values:
            ax.set_ylim(min(values) / 1.6, max(values) * 1.6)
        ax.set_xlim(0, t_end)
        ax.set_xlabel("molecular time (rounds x segment length) / ns")
        ax.set_ylabel("rate / s$^{-1}$")
        rate_text = "no rate" if not final else f"k = {final:.3g} s$^{{-1}}$"
        if band[0] and band[1]:
            rate_text += f" [{band[0]:.3g}, {band[1]:.3g}]"
        moved = ("no estimate yet at the middle of the run" if conv.get("drift_factor") is None
                 else f"moved x{conv['drift_factor']:.2f} ({conv['drift_kt']:.2f} kT) over the second half")
        state = "converged" if ok else "not converged"
        extra = "" if kin.get("verdict") in ("flux_steady", "rate_not_converged") else f"; {kin.get('verdict')}"
        ax.set_title(f"{key}: {rate_text}\n{moved}: {state} (tolerance {conv['tolerance_kt']:g} kT = "
                     f"x{conv['tolerance_factor']:.2f}){extra}", fontsize=8.5)
        ax.grid(alpha=0.3, which="major")
        ax.legend(fontsize=6.5, loc="best", framealpha=0.85)
    if pooled:
        fig.suptitle(f"pooled over {pooled['n_schemes']} schemes: {pooled['rate_mean_per_s']:.3g} "
                     f"+- {pooled['rate_sem_per_ns'] * 1e9:.2g} s$^{{-1}}$ (SEM)", fontsize=9)
    fig.tight_layout()
    path = out_dir / "we_convergence.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return str(path)


_VERDICT_ORDER = ("no_target_events", "flux_undersampled", "flux_transient", "rate_not_converged",
                  "two_state_unfitted", "flux_steady", "two_state_fitted")


@node_tool(node_type="analyze")
def analyze_we(
    job_dir: str,
    node_id: str,
    tau_ns: Optional[float] = None,
    burn_in_rounds: Optional[int] = None,
    temperature_kelvin: Optional[float] = None,
    n_bootstrap: int = 200,
    min_events: int = 10,
    drift_tolerance_kt: float = DRIFT_TOLERANCE_KT,
) -> dict:
    """Rates, verdicts and weighted distributions of a weighted ensemble.

    Parents: ``we_resample`` policy nodes (the latest round is enough; the
    chain of rounds is followed back). Several schemes as parents are
    analysed separately and their rates pooled (mean, SEM).

    Args:
        tau_ns: Segment length per round; defaults to the scheme's
            ``stage_args.simulation_time_ns``.
        burn_in_rounds: Rounds dropped before averaging bin populations;
            default 2 x the fitted flux relaxation time (at most half the rounds).
        temperature_kelvin: For ``-kT ln P``. Omitted: the temperature the
            segments ran at (recorded per round by we_resample); refused when
            they ran at different temperatures
            (``rounds_start_temperature_mismatch``); 300 K when they record
            none. The rate does not depend on it.
        n_bootstrap: Moving-block bootstrap samples for the rate interval.
        min_events: Recycling events the averaging window must hold for
            ``flux_steady`` (default 10).
        drift_tolerance_kt: Converged when the rate this analysis would have
            reported, had the run stopped after any round of its second half,
            stays within this many kT of barrier of the final rate (default
            1 kT: a factor of e). A larger drift makes a steady window
            ``rate_not_converged``. ``we_convergence.png`` draws it.
    """
    node = _read_node_json(job_dir, node_id) or {}
    if (node.get("conditions") or {}).get("analysis_data_scope") == "comparison":
        return _fail(job_dir, node_id, WEError(code="we_scope_unsupported",
                                                message="analyze_we pools rounds itself; do not use a comparison scope"),
                     pending=True)
    try:
        schemes = _collect_schemes(job_dir, node)
        taus = {key: _tau_ns(job_dir, key, tau_ns) for key in schemes}
        temperature_kelvin, temperature_source, temperature_warnings = _resolve_temperature(
            job_dir, schemes, temperature_kelvin)
    except WEError as exc:
        return _fail(job_dir, node_id, exc, pending=True)

    from mdclaw._node import begin_node, complete_node

    out_dir = ensure_directory(Path(job_dir) / "nodes" / node_id / "artifacts")
    begin_node(job_dir, node_id)
    try:
        analyses = {
            key: _analyze_scheme(job_dir, key, rounds, tau_ns=taus[key], burn_in_rounds=burn_in_rounds,
                                 temperature_kelvin=temperature_kelvin, n_bootstrap=n_bootstrap,
                                 min_events=min_events, drift_tolerance_kt=drift_tolerance_kt)
            for key, rounds in schemes.items()
        }
        warnings: list[str] = list(temperature_warnings)
        rates = [a["kinetics"].get("rate") for a in analyses.values()
                 if a["recycle"] and a["kinetics"].get("rate") is not None]
        pooled: Optional[dict] = None
        if len(rates) >= 2:
            arr = np.asarray(rates, dtype=float)
            pooled = {"n_schemes": int(arr.size), "rate_mean_per_ns": float(arr.mean()),
                      "rate_sem_per_ns": float(arr.std(ddof=1) / math.sqrt(arr.size)),
                      "rate_mean_per_s": float(arr.mean() * 1e9)}
            spread = float(arr.max() / arr.min()) if arr.min() > 0 else math.inf
            pooled["max_over_min"] = None if not math.isfinite(spread) else round(spread, 3)
            if spread >= math.exp(drift_tolerance_kt):
                warnings.append(
                    f"schemes_disagree: the independent schemes' rates differ by a factor of {spread:.2f} "
                    f"(more than the {drift_tolerance_kt:g} kT drift tolerance); quote the pooled mean with its "
                    "SEM, which carries that spread, and consider another scheme")
        iterations_csv = out_dir / "we_iterations.csv"
        with iterations_csv.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["scheme_id", "round", "time_ns", "n_walkers", "flux_per_ns", "flux_weight", "events",
                             "cumulative_flux_weight", "target_weight", "weight_min", "weight_max", "bins_occupied"])
            for key, analysis in analyses.items():
                for r in analysis["rounds"]:
                    writer.writerow([key, r["round"], f"{r['time_ns']:.6f}", r["n_walkers"], f"{r['flux_per_ns']:.6e}",
                                     f"{r['flux_weight']:.6e}", r["events"], f"{r['cumulative_flux_weight']:.6e}",
                                     f"{r['target_weight']:.6e}", r["weight_min"], r["weight_max"], r["bins_occupied"]])
        bins_csv = out_dir / "we_bins.csv"
        with bins_csv.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["scheme_id", "bin", "bin_indices", "lo", "hi", "mean_weight", "rounds_occupied",
                             "free_energy_kj_mol"])
            for key, analysis in analyses.items():
                for b in analysis["bins"]:
                    lo = " ".join("-inf" if v is None else f"{v:g}" for v, _ in b["bounds"])
                    hi = " ".join("inf" if v is None else f"{v:g}" for _, v in b["bounds"])
                    writer.writerow([key, b["bin"], " ".join(str(i) for i in b["bin_indices"]), lo, hi,
                                     f"{b['mean_weight']:.6e}", b["rounds_occupied"],
                                     "" if b["free_energy_kj_mol"] is None else f"{b['free_energy_kj_mol']:.4f}"])
        frames_csv = _write_frames(job_dir, out_dir, schemes, taus)
        plot = _plot(out_dir, analyses)
        convergence_csv = _write_convergence_csv(out_dir, analyses)
        try:
            convergence_plot = _plot_convergence(out_dir, analyses, pooled)
        except Exception as exc:  # noqa: BLE001 - the numbers stand without the figure
            convergence_plot = None
            warnings.append(f"convergence plot skipped: {type(exc).__name__}: {exc}")
        summary = {"schemes": analyses, "pooled": pooled, "temperature_kelvin": temperature_kelvin,
                   "temperature_kelvin_source": temperature_source,
                   "parent_node_ids": node.get("parent_node_ids") or []}
        kinetics_json = out_dir / "we_kinetics.json"
        kinetics_json.write_text(json.dumps(summary, indent=2))
    except WEError as exc:
        return _fail(job_dir, node_id, exc, pending=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("analyze_we failed: %s", exc)
        return _fail(job_dir, node_id, exc, pending=False, code="unhandled_exception")

    artifacts = {
        "we_kinetics": _rel_to_node_root(str(kinetics_json), out_dir),
        "we_iterations": _rel_to_node_root(str(iterations_csv), out_dir),
        "we_bins": _rel_to_node_root(str(bins_csv), out_dir),
    }
    if frames_csv:
        artifacts["we_frames"] = _rel_to_node_root(str(frames_csv), out_dir)
    if plot:
        artifacts["we_plot"] = _rel_to_node_root(plot, out_dir)
    if convergence_csv:
        artifacts["we_convergence"] = _rel_to_node_root(str(convergence_csv), out_dir)
    if convergence_plot:
        artifacts["we_convergence_plot"] = _rel_to_node_root(convergence_plot, out_dir)
    # The node's verdict is the least settled scheme's: several independent
    # schemes are done only when every one of them is.
    ordered = sorted(analyses.items(),
                     key=lambda item: _VERDICT_ORDER.index(item[1]["kinetics"].get("verdict"))
                     if item[1]["kinetics"].get("verdict") in _VERDICT_ORDER else -1)
    lead_key, lead = ordered[0]
    first = next(iter(analyses.values()))
    kinetics = first["kinetics"]
    node_verdict = lead["kinetics"].get("verdict")
    convergence_summary = {
        key: {k: (a["kinetics"].get("convergence") or {}).get(k)
              for k in ("drift_kt", "drift_factor", "converged", "tolerance_kt", "second_half_start_round")}
        for key, a in analyses.items() if a["kinetics"].get("convergence")
    }
    metadata = {
        "analysis": "we_kinetics",
        "n_schemes": len(analyses),
        "scheme_ids": list(analyses),
        "verdict": node_verdict,
        "verdict_reasons": lead["kinetics"].get("verdict_reasons"),
        "verdict_scheme_id": lead_key,
        "next_scheme_id": lead_key if node_verdict not in ("flux_steady", "two_state_fitted") else None,
        "next_rounds_basis": lead["kinetics"].get("next_rounds_basis"),
        "convergence": convergence_summary,
        "mode": kinetics.get("mode"),
        "rate_per_s": kinetics.get("rate_per_s"),
        "rate_low_per_s": kinetics.get("rate_low_per_s"),
        "rate_high_per_s": kinetics.get("rate_high_per_s"),
        "mfpt_ns": kinetics.get("mfpt_ns"),
        "k_ab_per_s": kinetics.get("k_ab_per_s"),
        "k_ba_per_s": kinetics.get("k_ba_per_s"),
        "window": kinetics.get("window"),
        "next_rounds_suggested": lead["kinetics"].get("next_rounds_suggested"),
        "pooled": pooled,
        "burn_in_rounds": first["burn_in_rounds"],
        "temperature_kelvin": temperature_kelvin,
        "temperature_kelvin_source": temperature_source,
    }
    complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata)
    return {
        "success": True,
        "code": "ok",
        "job_dir": job_dir,
        "node_id": node_id,
        # The per-stop history stays in we_kinetics.json / we_convergence.csv.
        "schemes": {key: {"n_rounds": a["n_rounds"], "time_ns": a["time_ns"], "aggregate_ns": a["aggregate_ns"],
                          "recycle": a["recycle"],
                          "kinetics": {**a["kinetics"],
                                       "convergence": ({k: v for k, v in a["kinetics"]["convergence"].items()
                                                        if k != "history"}
                                                       if a["kinetics"].get("convergence") else None)},
                          "burn_in_rounds": a["burn_in_rounds"], "bins_occupied": len(a["bins"])}
                    for key, a in analyses.items()},
        "pooled": pooled,
        "verdict": node_verdict,
        "verdict_reasons": lead["kinetics"].get("verdict_reasons"),
        "convergence": convergence_summary,
        "temperature_kelvin": temperature_kelvin,
        "temperature_kelvin_source": temperature_source,
        "artifacts": artifacts,
        "warnings": warnings,
    }


def _fail(job_dir: str, node_id: str, exc: Exception, *, pending: bool, code: Optional[str] = None) -> dict:
    code = code or getattr(exc, "code", "unhandled_exception")
    result = {"success": False, "code": code, "message": str(exc), "errors": [str(exc)],
              "warnings": [], "job_dir": job_dir, "node_id": node_id}
    if pending:
        from mdclaw._node import fail_node_from_result

        return fail_node_from_result(job_dir, node_id, result, default_error=str(exc))
    from mdclaw._node import fail_node

    fail_node(job_dir, node_id, errors=[str(exc)], code=code)
    return result
