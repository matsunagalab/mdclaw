"""Helpers shared by Level-3 node-DAG pipeline tests."""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import pytest


def require_protein_preparation_stack() -> None:
    """Skip a real preparation pipeline when its optional tools are absent."""
    try:
        __import__("pdbfixer")
    except ImportError:
        pytest.skip("PDBFixer is required for this preparation integration test")
    if not any(shutil.which(name) for name in ("pdb2pqr", "pdb4amber")):
        pytest.skip(
            "pdb2pqr or pdb4amber is required for this preparation integration test"
        )


def require_executable(name: str, purpose: str) -> None:
    """Skip an integration test that cannot exercise its external-tool path."""
    if shutil.which(name) is None:
        pytest.skip(f"{name} is required for {purpose}")


def skip_if_rcsb_unavailable(result: dict, pdb_id: str) -> None:
    """Skip only failures attributable to connectivity or RCSB availability."""
    if result.get("success"):
        return
    errors = " ".join(str(error) for error in result.get("errors", []))
    lower = errors.lower()
    connectivity_markers = (
        "connection timeout",
        "connection error:",
        "connecterror",
        "networkerror",
        "proxyerror",
        "name or service not known",
        "temporary failure in name resolution",
    )
    status_match = re.search(r"\bHTTP (\d{3})\b", errors)
    service_unavailable = bool(
        status_match
        and (
            int(status_match.group(1)) == 429
            or int(status_match.group(1)) >= 500
        )
    )
    if any(marker in lower for marker in connectivity_markers) or service_unavailable:
        pytest.skip(f"RCSB is unavailable while fetching {pdb_id}: {errors}")


def skip_if_pubchem_unavailable(result: dict) -> None:
    """Skip a live PubChem smoke test only for transport/service failures."""
    if result.get("success"):
        return
    errors = " ".join(str(error) for error in result.get("errors", []))
    lower = errors.lower()
    connectivity_markers = (
        "connection refused",
        "connection reset",
        "connection aborted",
        "name or service not known",
        "temporary failure in name resolution",
        "timed out",
        "remote end closed connection",
        "remotedisconnected",
        "urlerror",
    )
    status_match = re.search(r"\b(?:HTTP(?: Error)?[ :]*)?(\d{3})\b", errors)
    service_unavailable = bool(
        status_match
        and (
            int(status_match.group(1)) == 429
            or int(status_match.group(1)) >= 500
        )
    )
    if any(marker in lower for marker in connectivity_markers) or service_unavailable:
        pytest.skip(f"PubChem is unavailable: {errors}")


def fetch_pdb_node(job_dir: Path, pdb_id: str, label: str | None = None) -> str:
    """Create a source node and populate it with an RCSB PDB structure."""
    from mdclaw._node import create_node, read_node
    from mdclaw.research.fetch import fetch_structure

    require_protein_preparation_stack()
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
    skip_if_rcsb_unavailable(result, pdb_id)
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


def complete_node_with_placeholders(job_dir, node_id, artifacts, **kwargs):
    """Complete a node after creating placeholder files for string artifacts.

    The production ``complete_node`` deliberately rejects missing artifact
    paths. Many lifecycle tests care about DAG wiring rather than file contents,
    so they use this helper and reserve the real function for strict-guard tests.
    """
    from mdclaw._node import complete_node

    node_dir = Path(job_dir) / "nodes" / node_id
    for rel_path in artifacts.values():
        if not isinstance(rel_path, str) or not rel_path:
            continue
        full = node_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if not full.exists():
            full.touch()
    return complete_node(job_dir, node_id, artifacts, **kwargs)


def require_topology_builder_stack() -> None:
    """Skip the test unless the openmmforcefields topology build stack is importable.

    The curated build path needs ``openmm``, ``openmmforcefields``, and
    ``openff.pablo``. Any one of these missing means the integration test
    cannot exercise the real build path.
    """
    missing: list[str] = []
    for module_name, friendly in (
        ("openmm", "openmm"),
        ("openmmforcefields", "openmmforcefields"),
        ("openff.pablo", "openff-pablo"),
    ):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(friendly)
    if missing:
        pytest.skip(
            "openmmforcefields topology build stack is required for this "
            f"integration test; missing: {', '.join(missing)}"
        )


def require_packmol_memgen() -> None:
    from mdclaw.solvation._base import packmol_memgen_wrapper

    if not packmol_memgen_wrapper.is_available():
        pytest.skip("packmol-memgen is required for this integration test")
