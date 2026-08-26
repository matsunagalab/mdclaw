#!/usr/bin/env python
"""2D MBAR free-energy surface from a TAS1R umbrella grid.

Reads one collective_variables.csv per window plus a grid manifest describing
each window's umbrella centres and force constants, and writes:

  <out>.fes.npz    PMF, its uncertainty, and the bin centres
  <out>.json       overlap diagnostics, per-window statistics, convergence
  <out>.png        the surface

Samples are decorrelated per window before MBAR: umbrella frames 10 ps apart
are not independent, and feeding correlated samples to MBAR makes the reported
uncertainty far smaller than the real one.
"""
import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pymbar import FES, MBAR, timeseries

KB_KJ = 0.008314462618  # kJ/mol/K
KJ_PER_KCAL = 4.184
NM_TO_A = 10.0


def load_window(window, equilibration_ps, subsample, half="all"):
    df = pd.read_csv(window["cv_csv"])
    df = df[df["time_ps"] >= equilibration_ps]
    # Splitting each window in half and rebuilding the surface from each is the
    # convergence check: two halves that disagree by more than the reported
    # error mean the windows have not equilibrated, whatever MBAR's own
    # uncertainty says.
    if half in ("first", "second") and len(df) >= 2:
        mid = len(df) // 2
        df = df.iloc[:mid] if half == "first" else df.iloc[mid:]
    cv = np.column_stack([df["cv1_lb2_lb2_nm"].to_numpy(),
                          df["cv2_crd_depth_nm"].to_numpy()])
    if len(cv) == 0:
        return cv, {"frames_raw": 0, "frames_used": 0, "g": None}
    if subsample:
        # Decorrelate on the coordinate that mixes slowest of the two.
        g = max(timeseries.statistical_inefficiency(cv[:, 0]),
                timeseries.statistical_inefficiency(cv[:, 1]))
        keep = timeseries.subsample_correlated_data(cv[:, 0], g=g)
        cv_used = cv[keep]
    else:
        g = 1.0
        cv_used = cv
    return cv_used, {"frames_raw": int(len(cv)), "frames_used": int(len(cv_used)),
                     "g": float(g)}


def bias_energy(cv, window):
    d1 = cv[:, 0] - window["cv1_center_nm"]
    d2 = cv[:, 1] - window["cv2_center_nm"]
    return 0.5 * window["k1"] * d1 ** 2 + 0.5 * window["k2"] * d2 ** 2


def run_mbar(windows, temperature, equilibration_ps, subsample, bins, seed,
             half="all", edges=None):
    beta = 1.0 / (KB_KJ * temperature)
    samples, stats_per_window = [], []
    for w in windows:
        cv, st = load_window(w, equilibration_ps, subsample, half)
        st["window_id"] = w["window_id"]
        st["cv1_center_nm"] = w["cv1_center_nm"]
        st["cv2_center_nm"] = w["cv2_center_nm"]
        if len(cv):
            st["cv1_mean_nm"] = float(cv[:, 0].mean())
            st["cv2_mean_nm"] = float(cv[:, 1].mean())
            st["cv1_std_nm"] = float(cv[:, 0].std(ddof=1)) if len(cv) > 1 else 0.0
            st["cv2_std_nm"] = float(cv[:, 1].std(ddof=1)) if len(cv) > 1 else 0.0
        samples.append(cv)
        stats_per_window.append(st)

    keep = [i for i, s in enumerate(samples) if len(s) > 1]
    if len(keep) < 2:
        raise SystemExit("fewer than two windows have usable samples")
    dropped = [windows[i]["window_id"] for i in range(len(windows)) if i not in keep]

    windows_used = [windows[i] for i in keep]
    samples = [samples[i] for i in keep]
    N_k = np.array([len(s) for s in samples])
    x_n = np.concatenate(samples)

    u_kn = np.zeros((len(windows_used), int(N_k.sum())))
    for k, w in enumerate(windows_used):
        u_kn[k] = beta * bias_energy(x_n, w)

    mbar = MBAR(u_kn, N_k, solver_protocol="robust")
    overlap = mbar.compute_overlap()

    fes = FES(u_kn, N_k, mbar_options={"solver_protocol": "robust"})
    # Both axes get the same bin count: pymbar runs np.shape() over the
    # bin_edges list, which raises on ragged input, so unequal counts are
    # rejected by the library rather than by us. Widths still differ per axis.
    # Pad the outer edges: pymbar bins with np.digitize(x, edges) - 1, so a
    # sample sitting exactly on the top edge lands in bin index nbins, one past
    # the end. A hair of padding keeps every sample strictly inside.
    # Passing edges in keeps the halves on the same grid as the full surface,
    # so the convergence check compares like with like.
    clipped = 0
    if edges is None:
        edges = []
        for d in range(2):
            lo, hi = x_n[:, d].min(), x_n[:, d].max()
            pad = max((hi - lo) * 1e-6, 1e-9)
            edges.append(np.linspace(lo - pad, hi + pad, bins + 1))
    else:
        # Reusing the full surface's edges: each half subsamples independently
        # and can keep an extreme frame the full run dropped, which would land
        # past the last edge. Clip those few back inside rather than let the
        # convergence check die on them.
        for d in range(2):
            lo = edges[d][0] + 1e-12
            hi = edges[d][-1] - 1e-12
            clipped += int(((x_n[:, d] < lo) | (x_n[:, d] > hi)).sum())
            x_n[:, d] = np.clip(x_n[:, d], lo, hi)
    fes.generate_fes(u_kn, x_n, fes_type="histogram",
                     histogram_parameters={"bin_edges": edges})
    centers = [0.5 * (e[1:] + e[:-1]) for e in edges]
    # Only bins that actually hold samples can be queried: pymbar looks each
    # grid point up in its bin_label dict and raises KeyError on an empty bin.
    # Everything else stays NaN, which is the honest answer for a bin no window
    # visited.
    occupied = sorted(fes.histogram_data["bin_label"].keys())
    grid = np.array([[centers[0][i], centers[1][j]] for i, j in occupied])
    result = fes.get_fes(grid, reference_point="from-lowest",
                         uncertainty_method="analytical")

    shape = (bins, bins)
    pmf = np.full(shape, np.nan)
    err = np.full(shape, np.nan)
    f_i = np.asarray(result["f_i"], dtype=float) / beta / KJ_PER_KCAL
    df_i = np.asarray(result.get("df_i"), dtype=float) / beta / KJ_PER_KCAL
    for n, (i, j) in enumerate(occupied):
        pmf[i, j] = f_i[n]
        err[i, j] = df_i[n]

    return {
        "pmf_kcal": pmf,
        "err_kcal": err,
        "cv1_centers_A": centers[0] * NM_TO_A,
        "cv2_centers_A": centers[1] * NM_TO_A,
        "overlap_matrix": np.asarray(overlap["matrix"], dtype=float),
        "overlap_scalar": float(overlap["scalar"]),
        "window_stats": stats_per_window,
        "windows_used": [w["window_id"] for w in windows_used],
        "windows_dropped": dropped,
        "n_samples_total": int(N_k.sum()),
        "occupied_bins": len(occupied),
        "edges": edges,
        "samples_clipped_to_edges": clipped,
    }


def neighbour_overlaps(result, windows):
    """Smallest overlap between each window and any other window."""
    matrix = result["overlap_matrix"]
    np.fill_diagonal(matrix, 0.0)
    best = matrix.max(axis=1)
    return {wid: float(v) for wid, v in zip(result["windows_used"], best)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True,
                    help="JSON list of windows: window_id, cv1_center_nm, "
                         "cv2_center_nm, k1, k2, cv_csv")
    ap.add_argument("--temperature", type=float, default=300.0)
    ap.add_argument("--equilibration-ps", type=float, default=0.0)
    ap.add_argument("--bins", type=int, default=28,
                    help="bins per axis; pymbar requires the same count on both")
    ap.add_argument("--no-subsample", action="store_true")
    ap.add_argument("--out-prefix", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--convergence", action="store_true",
                    help="rebuild the surface from each half and compare")
    ap.add_argument("--seed", type=int, default=20260826)
    args = ap.parse_args()

    windows = json.load(open(args.manifest))
    windows = [w for w in windows if os.path.exists(w["cv_csv"])]
    if not windows:
        raise SystemExit("no window has written a collective_variables.csv yet")

    result = run_mbar(windows, args.temperature, args.equilibration_ps,
                      not args.no_subsample, args.bins, args.seed)

    # Convergence: rebuild the surface from each half of every window, on the
    # same bin edges, and report the largest disagreement over bins both halves
    # resolved.
    convergence = {"checked": False}
    if args.convergence:
        try:
            first = run_mbar(windows, args.temperature, args.equilibration_ps,
                             not args.no_subsample, args.bins, args.seed,
                             half="first", edges=result["edges"])
            second = run_mbar(windows, args.temperature, args.equilibration_ps,
                              not args.no_subsample, args.bins, args.seed,
                              half="second", edges=result["edges"])
            both = np.isfinite(first["pmf_kcal"]) & np.isfinite(second["pmf_kcal"])
            if both.sum() >= 5:
                a = first["pmf_kcal"][both] - first["pmf_kcal"][both].mean()
                b = second["pmf_kcal"][both] - second["pmf_kcal"][both].mean()
                convergence = {
                    "checked": True,
                    "bins_compared": int(both.sum()),
                    "max_abs_half_difference_kcal": float(np.max(np.abs(a - b))),
                    "rms_half_difference_kcal": float(np.sqrt(np.mean((a - b) ** 2))),
                    "criterion_kcal": 1.0,
                    "samples_clipped_to_edges": (
                        first["samples_clipped_to_edges"]
                        + second["samples_clipped_to_edges"]),
                }
                convergence["passes"] = (
                    convergence["max_abs_half_difference_kcal"] <= 1.0)
            else:
                convergence = {"checked": True, "bins_compared": int(both.sum()),
                               "passes": None,
                               "note": "too few bins resolved in both halves"}
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            convergence = {"checked": False, "error": f"{type(exc).__name__}: {exc}"}

    np.savez(f"{args.out_prefix}.fes.npz",
             pmf_kcal=result["pmf_kcal"], err_kcal=result["err_kcal"],
             cv1_centers_A=result["cv1_centers_A"],
             cv2_centers_A=result["cv2_centers_A"],
             overlap_matrix=result["overlap_matrix"])

    worst = neighbour_overlaps(result, windows)
    summary = {
        "label": args.label,
        "temperature_kelvin": args.temperature,
        "equilibration_discarded_ps": args.equilibration_ps,
        "subsampled": not args.no_subsample,
        "bins": [args.bins, args.bins],
        "n_windows_used": len(result["windows_used"]),
        "windows_dropped": result["windows_dropped"],
        "n_samples_total": result["n_samples_total"],
        "occupied_bins": result["occupied_bins"],
        "overlap_scalar": result["overlap_scalar"],
        "best_neighbour_overlap_per_window": worst,
        "min_best_neighbour_overlap": min(worst.values()) if worst else None,
        "pmf_max_kcal": float(np.nanmax(result["pmf_kcal"])),
        "bins_with_pmf": int(np.isfinite(result["pmf_kcal"]).sum()),
        "pmf_mean_error_kcal": float(np.nanmean(result["err_kcal"])),
        "convergence": convergence,
        "window_stats": result["window_stats"],
    }
    json.dump(summary, open(f"{args.out_prefix}.json", "w"), indent=2)

    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    pcm = ax.pcolormesh(result["cv1_centers_A"], result["cv2_centers_A"],
                        result["pmf_kcal"].T, shading="nearest", cmap="viridis")
    cs = ax.contour(result["cv1_centers_A"], result["cv2_centers_A"],
                    result["pmf_kcal"].T, levels=np.arange(0, 12, 1),
                    colors="white", linewidths=0.5)
    ax.clabel(cs, inline=True, fontsize=7, fmt="%.0f")
    fig.colorbar(pcm, ax=ax, label="PMF (kcal/mol)")
    ax.set_xlabel(r"CV1  LB2-LB2 COM distance ($\AA$)")
    ax.set_ylabel(r"CV2  CRD-loop depth ($\AA$)")
    ax.set_title(args.label or "2D PMF")
    fig.tight_layout()
    fig.savefig(f"{args.out_prefix}.png", dpi=150)

    print(json.dumps({k: v for k, v in summary.items() if k != "window_stats"},
                     indent=2))


if __name__ == "__main__":
    main()
