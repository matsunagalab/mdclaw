#!/usr/bin/env python
"""Timeseries, joint distribution and correlation for the two TAS1R CVs.

Takes the per-state CSVs written by cv_compute.py and produces one figure plus
a JSON summary. Correlation errors come from a moving-block bootstrap, because
consecutive MD frames are not independent and the naive error on a Pearson r
over 2000 correlated frames is meaningless.
"""
import argparse
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

NM_TO_A = 10.0


def block_bootstrap_corr(x, y, block, n_boot, rng):
    n = len(x)
    n_blocks = max(1, n // block)
    pearson, spearman = [], []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])
        pearson.append(stats.pearsonr(x[idx], y[idx])[0])
        spearman.append(stats.spearmanr(x[idx], y[idx])[0])
    return np.array(pearson), np.array(spearman)


def autocorr_time(series):
    """Integrated autocorrelation time in frames (initial positive sequence)."""
    x = series - series.mean()
    n = len(x)
    acf = np.correlate(x, x, mode="full")[n - 1:]
    acf /= acf[0]
    tau, k = 1.0, 1
    while k < n and acf[k] > 0:
        tau += 2.0 * acf[k] * (1 - k / n)
        k += 1
    return float(tau)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="append", nargs=2, metavar=("LABEL", "PATH"),
                    required=True)
    ap.add_argument("--equilibration-ps", type=float, default=0.0,
                    help="discard this much from the start of each series")
    ap.add_argument("--out-png", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--seed", type=int, default=20260826)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    colors = {"apo": "#1b6ca8", "holo": "#d1495b"}
    summary = {}

    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5))
    for label, path in args.csv:
        df = pd.read_csv(path)
        df = df[df["time_ps"] >= args.equilibration_ps]
        t = df["time_ps"].to_numpy() / 1000.0
        cv1 = df["cv1_lb2_lb2_nm"].to_numpy() * NM_TO_A
        cv2 = df["cv2_crd_depth_nm"].to_numpy() * NM_TO_A
        color = colors.get(label, None)

        axes[0, 0].plot(t, cv1, lw=0.8, color=color, label=label)
        axes[1, 0].plot(t, cv2, lw=0.8, color=color, label=label)
        axes[0, 1].scatter(cv1, cv2, s=3, alpha=0.35, color=color, label=label)

        frames_per_ps = 1.0 / (df["time_ps"].diff().median())
        tau1 = autocorr_time(cv1)
        tau2 = autocorr_time(cv2)
        block = int(max(10, min(len(cv1) // 10, 4 * max(tau1, tau2))))
        pear, spear = block_bootstrap_corr(cv1, cv2, block, 500, rng)

        summary[label] = {
            "frames": int(len(cv1)),
            "cv1_lb2_lb2_angstrom": {
                "mean": float(cv1.mean()), "std": float(cv1.std(ddof=1)),
                "p1": float(np.percentile(cv1, 1)), "p99": float(np.percentile(cv1, 99)),
                "min": float(cv1.min()), "max": float(cv1.max()),
                "autocorr_time_frames": tau1,
            },
            "cv2_crd_depth_angstrom": {
                "mean": float(cv2.mean()), "std": float(cv2.std(ddof=1)),
                "p1": float(np.percentile(cv2, 1)), "p99": float(np.percentile(cv2, 99)),
                "min": float(cv2.min()), "max": float(cv2.max()),
                "autocorr_time_frames": tau2,
            },
            "correlation": {
                "pearson_r": float(stats.pearsonr(cv1, cv2)[0]),
                "pearson_ci95": [float(np.percentile(pear, 2.5)),
                                 float(np.percentile(pear, 97.5))],
                "spearman_rho": float(stats.spearmanr(cv1, cv2)[0]),
                "spearman_ci95": [float(np.percentile(spear, 2.5)),
                                  float(np.percentile(spear, 97.5))],
                "block_frames": block,
                "bootstrap_replicates": 500,
            },
            "frames_per_ps": float(frames_per_ps),
        }

        axes[1, 1].hist(cv1, bins=40, histtype="step", density=True,
                        color=color, label=f"{label} CV1")

    axes[0, 0].set_xlabel("time (ns)")
    axes[0, 0].set_ylabel(r"CV1  LB2-LB2 COM ($\AA$)")
    axes[0, 0].set_title("LB2-LB2 distance")
    axes[0, 0].legend(frameon=False)
    axes[1, 0].set_xlabel("time (ns)")
    axes[1, 0].set_ylabel(r"CV2  CRD-loop depth ($\AA$)")
    axes[1, 0].set_title("CRD-loop insertion depth (smaller = deeper)")
    axes[1, 0].legend(frameon=False)
    axes[0, 1].set_xlabel(r"CV1 ($\AA$)")
    axes[0, 1].set_ylabel(r"CV2 ($\AA$)")
    axes[0, 1].set_title("joint distribution")
    axes[0, 1].legend(frameon=False, markerscale=3)
    axes[1, 1].set_xlabel(r"CV1 ($\AA$)")
    axes[1, 1].set_ylabel("density")
    axes[1, 1].set_title("CV1 marginal")
    axes[1, 1].legend(frameon=False)
    for ax in axes.flat:
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("TAS1R2-TAS1R3 extracellular region: unbiased CV behaviour", y=0.98)
    fig.tight_layout()
    fig.savefig(args.out_png, dpi=150)

    json.dump(summary, open(args.out_json, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
