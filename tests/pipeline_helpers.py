"""Helpers shared by Level-3 node-DAG pipeline tests."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest


def fetch_pdb_node(job_dir: Path, pdb_id: str, label: str | None = None) -> str:
    """Create a source node and populate it with an RCSB PDB structure."""
    from mdclaw._node import create_node, read_node
    from mdclaw.research_server import fetch_structure

    node = create_node(str(job_dir), "source", label=label or f"PDB {pdb_id}")
    assert node["success"], node
    node_id = node["node_id"]
    result = asyncio.run(
        fetch_structure(
            source="pdb",
            pdb_id=pdb_id,
            format="pdb",
            job_dir=str(job_dir),
            node_id=node_id,
        )
    )
    assert result["success"], result.get("errors")
    assert Path(result["file_path"]).parent.name == "artifacts"
    assert read_node(str(job_dir), node_id)["status"] == "completed"
    return node_id


def node_artifact(job_dir: Path, node_id: str, artifact_key: str) -> Path:
    """Return the absolute path for an artifact recorded on a node."""
    from mdclaw._node import read_node

    node = read_node(str(job_dir), node_id)
    rel_path = node["artifacts"][artifact_key]
    assert isinstance(rel_path, str), f"{artifact_key} is not a path artifact"
    path = job_dir / "nodes" / node_id / rel_path
    assert path.exists(), f"Missing artifact {artifact_key}: {path}"
    return path


def require_tleap() -> None:
    from mdclaw.amber_server import tleap_wrapper

    if not tleap_wrapper.is_available():
        pytest.skip("tleap is required for this integration test")


def require_packmol_memgen() -> None:
    from mdclaw.solvation_server import packmol_memgen_wrapper

    if not packmol_memgen_wrapper.is_available():
        pytest.skip("packmol-memgen is required for this integration test")


def require_metalpdb2mol2() -> None:
    if shutil.which("metalpdb2mol2.py") is None:
        pytest.skip("metalpdb2mol2.py is required for this integration test")
