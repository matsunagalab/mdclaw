"""Analyze server: plots helpers.

Split out of the original ``analyze_server`` monolith. Behavior unchanged.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from mdclaw._common import (
    setup_logger,
)

logger = setup_logger(__name__)


def _save_overlay_plot(
    series_by_label: dict[str, np.ndarray],
    out_path: Path,
    xlabel: str = "frame",
    ylabel: str = "value",
    title: str = "",
) -> None:
    """Multi-branch overlay lineplot (one curve per label). Only
    called when ≥ 2 branches — a single-branch overlay would just
    duplicate the per-branch plot."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4))
    for label, arr in series_by_label.items():
        if arr.ndim == 1:
            ax.plot(arr, label=label)
        else:
            for k in range(arr.shape[1]):
                ax.plot(arr[:, k], label=f"{label}[{k}]")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _time_axis_ns(n_frames: int, dt_ps: float = 100.0) -> np.ndarray:
    """Frame index → time axis (ns) for CSV/plot output.

    ``dt_ps`` is the production run's ``output_frequency_ps``. Phase 1
    writes that on every prod node's metadata, but Phase 2 tools
    intentionally don't chase it across the DAG — they display the
    frame axis and record the default (100 ps) so the caller can
    rescale if needed.
    """
    return np.arange(n_frames, dtype=np.float64) * dt_ps / 1000.0


def _save_timeseries_plot(
    data: np.ndarray,
    out_path: Path,
    xlabel: str = "frame",
    ylabel: str = "value",
    title: str = "",
) -> None:
    """Minimal lineplot helper. Uses the Agg backend so it works
    headlessly inside the SIF without an X display."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 3.5))
    if data.ndim == 1:
        ax.plot(data)
    else:
        for i in range(data.shape[1]):
            ax.plot(data[:, i], label=f"series {i}")
        ax.legend()
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _save_matrix_plot(
    data: np.ndarray,
    out_path: Path,
    xlabel: str = "index",
    ylabel: str = "index",
    title: str = "",
    colorbar_label: str = "value",
) -> None:
    """Minimal heatmap helper for contact-frequency matrices."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    im = ax.imshow(data, origin="lower", aspect="auto", vmin=0.0, vmax=1.0)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    fig.colorbar(im, ax=ax, label=colorbar_label)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
