"""Analyze server: equilibration helpers.

Split out of the original ``analyze_server`` monolith. Behavior unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import (
    create_validation_error,
    ensure_directory,
    setup_logger,
)
from mdclaw.analyze.inputs import _load_scalar_timeseries, _rel_to_node_root

logger = setup_logger(__name__)


def detect_equilibration(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    timeseries_file: Optional[str] = None,
    column: Any = None,
    fast: bool = True,
    nskip: int = 1,
    output_name: str = "equilibration",
    _out_dir_override: Optional[str] = None,
) -> dict:
    """Run PyMBAR's automated equilibration detection on a scalar timeseries."""
    result: dict[str, Any] = {
        "success": False,
        "equilibration_json": None,
        "equilibration_csv": None,
        "errors": [],
        "warnings": [],
    }
    node_mode = bool(job_dir and node_id)

    def _validation_failure(field: str, message: str) -> dict:
        validation = create_validation_error(field, message)
        if node_mode:
            from mdclaw._node import begin_node, fail_node

            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=validation["errors"])
        return validation

    if not timeseries_file:
        return _validation_failure(
            "timeseries_file",
            "timeseries_file is required and must point to a .npy or .csv scalar timeseries",
        )
    if nskip <= 0:
        return _validation_failure("nskip", "nskip must be a positive integer")

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
        from mdclaw._node import begin_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "equilibration_output")

    try:
        from pymbar import timeseries

        series, source_metadata = _load_scalar_timeseries(timeseries_file, column)
        t0, g, neff_max = timeseries.detect_equilibration(series, fast=fast, nskip=int(nskip))
        t0 = int(t0)
        g = float(g)
        neff_max = float(neff_max)
        equilibrated = series[t0:]
        std = float(equilibrated.std(ddof=1)) if equilibrated.size > 1 else 0.0
        stderr = float(std / np.sqrt(neff_max)) if neff_max > 0 else None

        summary = {
            **source_metadata,
            "method": "pymbar.timeseries.detect_equilibration",
            "pymbar_function": "detect_equilibration",
            "fast": bool(fast),
            "nskip": int(nskip),
            "t0": t0,
            "g": g,
            "Neff_max": neff_max,
            "n_equilibrated_samples": int(equilibrated.size),
            "equilibrated_fraction": float(equilibrated.size / series.size),
            "mean": float(equilibrated.mean()),
            "std": std,
            "stderr": stderr,
        }

        json_path = out_dir / f"{output_name}.json"
        json_path.write_text(json.dumps(summary, indent=2, default=str))
        csv_path = out_dir / f"{output_name}.csv"
        csv_path.write_text(
            "metric,value\n"
            f"t0,{t0}\n"
            f"g,{g:.12g}\n"
            f"Neff_max,{neff_max:.12g}\n"
            f"n_samples,{series.size}\n"
            f"n_equilibrated_samples,{equilibrated.size}\n"
            f"equilibrated_fraction,{summary['equilibrated_fraction']:.12g}\n"
            f"mean,{summary['mean']:.12g}\n"
            f"std,{std:.12g}\n"
            f"stderr,{'' if stderr is None else f'{stderr:.12g}'}\n"
        )

        result.update({
            "success": True,
            "equilibration_json": str(json_path),
            "equilibration_csv": str(csv_path),
            **summary,
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"detect_equilibration failed: {e}")
        result["errors"].append(f"detect_equilibration failed: {type(e).__name__}: {e}")

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "equilibration_json": _rel_to_node_root(result["equilibration_json"], out_dir),
                    "equilibration_csv": _rel_to_node_root(result["equilibration_csv"], out_dir),
                },
                metadata={
                    "tool": "detect_equilibration",
                    "timeseries_file": timeseries_file,
                    "column": column,
                    "method": result.get("method"),
                    "fast": fast,
                    "nskip": int(nskip),
                    "t0": result.get("t0"),
                    "g": result.get("g"),
                    "Neff_max": result.get("Neff_max"),
                    "n_samples": result.get("n_samples"),
                    "n_equilibrated_samples": result.get("n_equilibrated_samples"),
                    "equilibrated_fraction": result.get("equilibrated_fraction"),
                    "mean": result.get("mean"),
                    "std": result.get("std"),
                    "stderr": result.get("stderr"),
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result
