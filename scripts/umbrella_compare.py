#!/usr/bin/env python
"""Compare the apo and holo 2D PMFs and locate where sucralose moves the
barrier to CRD-loop insertion.

The insertion barrier is read along CV2 at fixed CV1: for each CV1 column the
profile G(CV2) is scanned for the deepest minimum on the inserted side, the
deepest minimum on the withdrawn side, and the highest point between them.
Columns where either basin is unsampled are reported as such rather than
silently skipped.

Both surfaces are shifted to a common zero -- the mean of the two PMFs over the
bins that are well sampled in both -- so the colour scales are comparable. A 2D
PMF has no absolute zero, so any single-point reference would make the
comparison depend on that point's noise.
"""
import argparse
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def load(path):
    d = np.load(path)
    return {k: d[k] for k in d.files}


def common_mask(a, b, max_err):
    finite = np.isfinite(a["pmf_kcal"]) & np.isfinite(b["pmf_kcal"])
    return finite & (a["err_kcal"] < max_err) & (b["err_kcal"] < max_err)


def column_barrier(cv2, profile, err, split_A):
    """Barrier between the inserted (CV2 < split) and withdrawn side."""
    ok = np.isfinite(profile)
    inserted = ok & (cv2 < split_A)
    withdrawn = ok & (cv2 >= split_A)
    if inserted.sum() < 2 or withdrawn.sum() < 2:
        return None
    i_min = np.where(inserted)[0][np.argmin(profile[inserted])]
    w_min = np.where(withdrawn)[0][np.argmin(profile[withdrawn])]
    lo, hi = sorted((i_min, w_min))
    if hi - lo < 2:
        return None
    between = np.arange(lo, hi + 1)
    top = between[np.argmax(profile[between])]
    return {
        "inserted_min_cv2_A": float(cv2[i_min]),
        "inserted_min_kcal": float(profile[i_min]),
        "withdrawn_min_cv2_A": float(cv2[w_min]),
        "withdrawn_min_kcal": float(profile[w_min]),
        "barrier_cv2_A": float(cv2[top]),
        "barrier_top_kcal": float(profile[top]),
        # Barrier for going from withdrawn into the inserted state.
        "barrier_insertion_kcal": float(profile[top] - profile[w_min]),
        "barrier_insertion_err_kcal": float(
            np.hypot(err[top], err[w_min])
        ),
        "delta_g_inserted_minus_withdrawn_kcal": float(
            profile[i_min] - profile[w_min]
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apo", required=True)
    ap.add_argument("--holo", required=True)
    ap.add_argument("--split-cv2-A", type=float, required=True,
                    help="CV2 value separating inserted from withdrawn")
    ap.add_argument("--max-err-kcal", type=float, default=1.5)
    ap.add_argument("--out-png", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args()

    apo, holo = load(args.apo), load(args.holo)
    if apo["pmf_kcal"].shape != holo["pmf_kcal"].shape:
        raise SystemExit("the two surfaces were binned differently")

    mask = common_mask(apo, holo, args.max_err_kcal)
    if mask.sum() < 10:
        raise SystemExit(f"only {mask.sum()} bins are well sampled in both")
    for surface in (apo, holo):
        surface["pmf_kcal"] = surface["pmf_kcal"] - surface["pmf_kcal"][mask].mean()
        surface["pmf_kcal"][~np.isfinite(surface["pmf_kcal"])] = np.nan

    cv1 = apo["cv1_centers_A"]
    cv2 = apo["cv2_centers_A"]

    columns = []
    for i, x in enumerate(cv1):
        entry = {"cv1_A": float(x)}
        for name, s in (("apo", apo), ("holo", holo)):
            col = np.where(mask[i], s["pmf_kcal"][i], np.nan)
            entry[name] = column_barrier(cv2, col, s["err_kcal"][i],
                                         args.split_cv2_A)
        if entry.get("apo") and entry.get("holo"):
            d = entry["holo"]["barrier_insertion_kcal"] - entry["apo"]["barrier_insertion_kcal"]
            sd = np.hypot(entry["holo"]["barrier_insertion_err_kcal"],
                          entry["apo"]["barrier_insertion_err_kcal"])
            entry["delta_barrier_holo_minus_apo_kcal"] = float(d)
            entry["delta_barrier_err_kcal"] = float(sd)
            entry["significant_2sigma"] = bool(abs(d) > 2 * sd)
        columns.append(entry)

    vmax = float(np.nanpercentile(
        np.concatenate([apo["pmf_kcal"][mask], holo["pmf_kcal"][mask]]), 99))
    vmin = float(np.nanmin(
        np.concatenate([apo["pmf_kcal"][mask], holo["pmf_kcal"][mask]])))

    fig, axes = plt.subplots(2, 3, figsize=(16, 9),
                             gridspec_kw={"height_ratios": [1.35, 1]})
    for ax, (name, s) in zip(axes[0, :2], (("apo (9UT9)", apo), ("holo + sucralose (9UTC)", holo))):
        z = np.where(mask, s["pmf_kcal"], np.nan)
        pcm = ax.pcolormesh(cv1, cv2, z.T, shading="nearest", cmap="viridis",
                            vmin=vmin, vmax=vmax)
        cs = ax.contour(cv1, cv2, z.T, levels=np.arange(np.floor(vmin), vmax, 1.0),
                        colors="white", linewidths=0.5)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%.0f")
        ax.axhline(args.split_cv2_A, color="w", ls="--", lw=1)
        ax.set_title(name)
        ax.set_xlabel(r"CV1  LB2-LB2 COM ($\AA$)")
        ax.set_ylabel(r"CV2  CRD-loop depth ($\AA$)")
    fig.colorbar(pcm, ax=axes[0, :2].tolist(), label="PMF (kcal/mol)", shrink=0.85)

    diff = np.where(mask, holo["pmf_kcal"] - apo["pmf_kcal"], np.nan)
    lim = float(np.nanpercentile(np.abs(diff), 98))
    pcm2 = axes[0, 2].pcolormesh(cv1, cv2, diff.T, shading="nearest",
                                 cmap="RdBu_r", vmin=-lim, vmax=lim)
    axes[0, 2].axhline(args.split_cv2_A, color="k", ls="--", lw=1)
    axes[0, 2].set_title("holo - apo  (blue = sucralose stabilises)")
    axes[0, 2].set_xlabel(r"CV1 ($\AA$)")
    axes[0, 2].set_ylabel(r"CV2 ($\AA$)")
    fig.colorbar(pcm2, ax=axes[0, 2], label=r"$\Delta$PMF (kcal/mol)")

    # 1D slices through the best-sampled CV1 column
    counts = mask.sum(axis=1)
    best = int(np.argmax(counts))
    for name, s, color in (("apo", apo, "#1b6ca8"), ("holo", holo, "#d1495b")):
        prof = np.where(mask[best], s["pmf_kcal"][best], np.nan)
        err = s["err_kcal"][best]
        axes[1, 0].plot(cv2, prof, color=color, label=name)
        axes[1, 0].fill_between(cv2, prof - err, prof + err, color=color, alpha=0.2)
    axes[1, 0].axvline(args.split_cv2_A, color="k", ls="--", lw=1)
    axes[1, 0].set_xlabel(r"CV2  CRD-loop depth ($\AA$)")
    axes[1, 0].set_ylabel("PMF (kcal/mol)")
    axes[1, 0].set_title(f"CRD insertion profile at CV1 = {cv1[best]:.1f} " + r"$\AA$")
    axes[1, 0].legend(frameon=False)

    xs = [c["cv1_A"] for c in columns if "delta_barrier_holo_minus_apo_kcal" in c]
    ys = [c["delta_barrier_holo_minus_apo_kcal"] for c in columns
          if "delta_barrier_holo_minus_apo_kcal" in c]
    es = [c["delta_barrier_err_kcal"] for c in columns
          if "delta_barrier_holo_minus_apo_kcal" in c]
    if xs:
        axes[1, 1].errorbar(xs, ys, yerr=es, fmt="o-", color="#6a4c93", capsize=3)
    axes[1, 1].axhline(0, color="k", lw=0.8)
    axes[1, 1].set_xlabel(r"CV1 ($\AA$)")
    axes[1, 1].set_ylabel(r"$\Delta G^{\ddag}_{holo}-\Delta G^{\ddag}_{apo}$ (kcal/mol)")
    axes[1, 1].set_title("change in the insertion barrier")

    om = apo["overlap_matrix"]
    axes[1, 2].imshow(np.log10(np.maximum(om, 1e-6)), cmap="magma", vmin=-4, vmax=0)
    axes[1, 2].set_title("apo window overlap  ($\\log_{10}$)")
    axes[1, 2].set_xlabel("window")
    axes[1, 2].set_ylabel("window")

    for ax in axes.flat:
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("TAS1R2-TAS1R3: CRD-loop insertion vs LB2-LB2 interface", y=0.99)
    fig.savefig(args.out_png, dpi=150, bbox_inches="tight")

    summary = {
        "reference": "both surfaces shifted to the mean over bins well sampled in both",
        "max_err_kcal": args.max_err_kcal,
        "split_cv2_A": args.split_cv2_A,
        "n_common_bins": int(mask.sum()),
        "best_sampled_cv1_A": float(cv1[best]),
        "columns": columns,
        "max_abs_delta_pmf_kcal": float(np.nanmax(np.abs(diff))),
    }
    json.dump(summary, open(args.out_json, "w"), indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "columns"}, indent=2))


if __name__ == "__main__":
    main()
