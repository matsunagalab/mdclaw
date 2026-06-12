"""Analyze server: registry helpers.

Split out of the original ``analyze_server`` monolith. Behavior unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


from mdclaw._common import (
    setup_logger,
)
from mdclaw.analyze.inputs import _rel_to_node_root

logger = setup_logger(__name__)


def _load_analysis_manifest(manifest_file: Optional[str]) -> dict[str, Any]:
    """Read a custom-analysis manifest JSON file."""
    if not manifest_file:
        return {}
    path = Path(manifest_file).expanduser()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"could not read manifest_file {manifest_file!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("analysis manifest must be a JSON object")
    return data


def _coerce_json_object(value: Any, field: str) -> dict[str, Any]:
    """Accept dicts or JSON object strings for CLI-friendly fields."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return dict(value)


def _coerce_warnings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    if not isinstance(value, list):
        raise ValueError("warnings must be a list of strings")
    return [str(item) for item in value]


def register_analysis_result(
    job_dir: str,
    node_id: str,
    manifest_file: Optional[str] = None,
    artifacts: Optional[dict] = None,
    metrics: Optional[dict] = None,
    summary: Optional[str] = None,
    method: Optional[dict] = None,
    provenance: Optional[dict] = None,
    analysis_type: str = "custom",
    name: Optional[str] = None,
    producer_agent: Optional[str] = None,
    warnings: Optional[list[str]] = None,
) -> dict:
    """Register externally generated analysis outputs on an analyze node.

    This tool intentionally does not execute arbitrary code. A coding agent or
    harness writes files first, then calls this tool to stamp artifacts,
    metrics, method details, and provenance onto the DAG.
    """
    from mdclaw._node import begin_node, complete_node, fail_node

    result: dict[str, Any] = {
        "success": False,
        "artifacts": {},
        "metadata": {},
        "errors": [],
        "warnings": [],
    }

    begin_node(job_dir, node_id)
    try:
        manifest = _load_analysis_manifest(manifest_file)

        merged_artifacts = {
            **_coerce_json_object(manifest.get("artifacts"), "manifest.artifacts"),
            **_coerce_json_object(artifacts, "artifacts"),
        }
        if manifest_file and "analysis_manifest" not in merged_artifacts:
            merged_artifacts["analysis_manifest"] = str(Path(manifest_file).expanduser())

        merged_metrics = {
            **_coerce_json_object(manifest.get("metrics"), "manifest.metrics"),
            **_coerce_json_object(metrics, "metrics"),
        }
        merged_method = {
            **_coerce_json_object(manifest.get("method"), "manifest.method"),
            **_coerce_json_object(method, "method"),
        }
        merged_provenance = {
            **_coerce_json_object(manifest.get("provenance"), "manifest.provenance"),
            **_coerce_json_object(provenance, "provenance"),
        }
        merged_warnings = [
            *_coerce_warnings(manifest.get("warnings")),
            *_coerce_warnings(warnings),
        ]

        metadata: dict[str, Any] = {
            "tool": "register_analysis_result",
            "analysis_type": str(
                (manifest.get("analysis_type") or analysis_type)
                if analysis_type == "custom"
                else analysis_type
            ),
        }
        analysis_name = name or manifest.get("name")
        if analysis_name:
            metadata["analysis_name"] = str(analysis_name)
        if summary or manifest.get("summary"):
            metadata["summary"] = str(summary or manifest["summary"])
        if merged_metrics:
            metadata["metrics"] = merged_metrics
        if merged_method:
            metadata["method"] = merged_method
        if merged_provenance:
            metadata["provenance"] = merged_provenance
        if producer_agent or manifest.get("producer_agent"):
            metadata["producer_agent"] = str(producer_agent or manifest["producer_agent"])

        complete_node(
            job_dir,
            node_id,
            artifacts=merged_artifacts,
            metadata=metadata,
            warnings=merged_warnings or None,
        )
        result.update({
            "success": True,
            "artifacts": merged_artifacts,
            "metadata": metadata,
            "warnings": merged_warnings,
        })
    except Exception as exc:  # noqa: BLE001
        msg = f"register_analysis_result failed: {type(exc).__name__}: {exc}"
        logger.error(msg)
        result["errors"].append(msg)
        fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _finalize_concat_node(
    job_dir: str,
    node_id: str,
    result: dict,
    out_dir: Path,
    selection: str,
    stride: int,
    chunk: int,
    topology_file: Optional[str],
) -> None:
    """Update node.json with the concat result. Single-branch uses the
    flat ``combined_trajectory`` keys; multi-branch uses a structured
    ``branches`` list artifact. Both shapes share ``reference_pdb``
    and ``selection_indices`` at the top level."""
    from mdclaw._node import complete_node, fail_node

    if not result["success"]:
        fail_node(job_dir, node_id, errors=result["errors"])
        return

    n_atoms_original = None
    if result.get("selection_indices"):
        try:
            n_atoms_original = int(
                json.loads(Path(result["selection_indices"]).read_text())[
                    "n_atoms_original"
                ]
            )
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass
    if n_atoms_original is None:
        n_atoms_original = result.get("n_atoms_selected", 0)

    if result.get("n_branches", 0) >= 1 and result.get("branches"):
        # Multi-branch artifact shape: a list of per-branch dicts with
        # label + trajectory + energy + metadata. reference_pdb /
        # selection_indices stay at the top level because every branch
        # shares one topology.
        arts: dict[str, Any] = {
            "reference_pdb": _rel_to_node_root(
                result["reference_pdb"], out_dir
            ),
            "selection_indices": _rel_to_node_root(
                result["selection_indices"], out_dir
            ),
            "branches": [
                {
                    "label": b["label"],
                    "leaf_prod_id": b.get("leaf_prod_id"),
                    "combined_trajectory": _rel_to_node_root(
                        b.get("combined_trajectory"), out_dir
                    ),
                    "combined_energy": _rel_to_node_root(
                        b.get("combined_energy"), out_dir
                    ),
                    "total_frames": b.get("total_frames"),
                    "frames_per_source": b.get("frames_per_source", []),
                    "source_trajectories": b.get("source_trajectories", []),
                    "source_energy_files": b.get("source_energy_files", []),
                    "total_energy_rows": b.get("total_energy_rows", 0),
                    "conditions": b.get("conditions", {}),
                }
                for b in result["branches"]
            ],
        }
        complete_node(
            job_dir,
            node_id,
            artifacts=arts,
            metadata={
                "selection": selection,
                "stride": stride,
                "chunk": chunk,
                "n_atoms_selected": result["n_atoms_selected"],
                "n_atoms_original": n_atoms_original,
                "n_branches": result["n_branches"],
                "total_frames": result["total_frames"],
                "topology_file": topology_file,
            },
        )
        return

    # Single-branch flat shape (back-compat)
    arts = {
        "combined_trajectory": _rel_to_node_root(
            result["combined_trajectory"], out_dir
        ),
        "reference_pdb": _rel_to_node_root(result["reference_pdb"], out_dir),
        "selection_indices": _rel_to_node_root(
            result["selection_indices"], out_dir
        ),
    }
    if result.get("combined_energy"):
        arts["combined_energy"] = _rel_to_node_root(
            result["combined_energy"], out_dir
        )
    complete_node(
        job_dir,
        node_id,
        artifacts=arts,
        metadata={
            "selection": selection,
            "stride": stride,
            "chunk": chunk,
            "n_atoms_selected": result["n_atoms_selected"],
            "n_atoms_original": n_atoms_original,
            "total_frames": result["total_frames"],
            "frames_per_source": result["frames_per_source"],
            "source_trajectories": result["source_trajectories"],
            "source_energy_files": result.get("source_energy_files", []),
            "energy_rows_per_source": result.get(
                "energy_rows_per_source", []
            ),
            "total_energy_rows": result.get("total_energy_rows", 0),
            "topology_file": topology_file,
        },
    )
