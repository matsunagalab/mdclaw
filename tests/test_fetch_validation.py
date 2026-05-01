"""Unit tests for fetch-tool node validation.

Verifies that ``fetch_structure`` and its compatibility wrappers reject:
  - a node_id that doesn't exist
  - a node_id whose node_type is not ``fetch``

These checks must happen **before** ``begin_node`` so the wrong node never
receives fetch metadata or a status mutation. Tests don't hit the network
because the validation short-circuits before any HTTP call.
"""

import asyncio

import pytest

from mdclaw._node import create_node, read_node

pytest.importorskip("httpx")

from mdclaw.research_server import (
    download_structure,
    fetch_structure,
    get_alphafold_structure,
    register_local_structure,
)


@pytest.fixture
def job_dir(tmp_path):
    jd = tmp_path / "job_validate"
    jd.mkdir()
    return jd


@pytest.fixture
def prep_node(job_dir):
    """A non-fetch node that fetch tools must refuse to write under."""
    result = create_node(str(job_dir), "prep")
    assert result["success"]
    return result["node_id"]


# ── fetch_structure ────────────────────────────────────────────────────────


class TestFetchStructureValidation:

    def test_missing_node_id(self, job_dir):
        result = asyncio.run(fetch_structure(
            source="pdb",
            pdb_id="1AKE",
            job_dir=str(job_dir),
            node_id="fetch_999",
        ))
        assert result["success"] is False
        assert any("does not exist" in e for e in result["errors"])

    def test_wrong_node_type(self, job_dir, prep_node):
        result = asyncio.run(fetch_structure(
            source="alphafold",
            uniprot_id="P12345",
            job_dir=str(job_dir),
            node_id=prep_node,
        ))
        assert result["success"] is False
        assert any("expected 'fetch'" in e for e in result["errors"])
        prep_data = read_node(str(job_dir), prep_node)
        assert prep_data["status"] == "pending"
        assert prep_data["artifacts"] == {}


# ── compatibility wrappers ─────────────────────────────────────────────────


# ── download_structure ─────────────────────────────────────────────────────


class TestDownloadStructureValidation:

    def test_missing_node_id(self, job_dir):
        result = asyncio.run(download_structure(
            pdb_id="1AKE",
            job_dir=str(job_dir),
            node_id="fetch_999",  # never created
        ))
        assert result["success"] is False
        assert any("does not exist" in e for e in result["errors"])

    def test_wrong_node_type(self, job_dir, prep_node):
        result = asyncio.run(download_structure(
            pdb_id="1AKE",
            job_dir=str(job_dir),
            node_id=prep_node,
        ))
        assert result["success"] is False
        assert any("expected 'fetch'" in e for e in result["errors"])
        # Crucial: the prep node must NOT have been mutated.
        prep_data = read_node(str(job_dir), prep_node)
        assert prep_data["status"] == "pending"
        assert prep_data["artifacts"] == {}
        assert "source_type" not in prep_data["metadata"]


# ── get_alphafold_structure ────────────────────────────────────────────────


class TestAlphafoldStructureValidation:

    def test_missing_node_id(self, job_dir):
        result = asyncio.run(get_alphafold_structure(
            uniprot_id="P12345",
            job_dir=str(job_dir),
            node_id="fetch_999",
        ))
        assert result["success"] is False
        assert any("does not exist" in e for e in result["errors"])

    def test_wrong_node_type(self, job_dir, prep_node):
        result = asyncio.run(get_alphafold_structure(
            uniprot_id="P12345",
            job_dir=str(job_dir),
            node_id=prep_node,
        ))
        assert result["success"] is False
        assert any("expected 'fetch'" in e for e in result["errors"])
        prep_data = read_node(str(job_dir), prep_node)
        assert prep_data["status"] == "pending"


# ── register_local_structure ──────────────────────────────────────────────


class TestRegisterLocalStructureValidation:

    def test_missing_node_id(self, job_dir, tmp_path):
        # Create a real source file so the failure is from the missing
        # node, not from missing input.
        src = tmp_path / "in.pdb"
        src.write_text("HEADER\n")

        result = register_local_structure(
            file_path=str(src),
            job_dir=str(job_dir),
            node_id="fetch_999",
        )
        assert result["success"] is False
        assert any("does not exist" in e for e in result["errors"])

    def test_wrong_node_type(self, job_dir, prep_node, tmp_path):
        src = tmp_path / "in.pdb"
        src.write_text("HEADER\n")

        result = register_local_structure(
            file_path=str(src),
            job_dir=str(job_dir),
            node_id=prep_node,
        )
        assert result["success"] is False
        assert any("expected 'fetch'" in e for e in result["errors"])
        # prep node untouched
        prep_data = read_node(str(job_dir), prep_node)
        assert prep_data["status"] == "pending"
        assert prep_data["artifacts"] == {}


# ── create_node fetch-root invariant (smoke check at the API entrypoint) ──


class TestCreateNodeFetchInvariant:
    """Mirror the test_node.py coverage at the public API level."""

    def test_fetch_with_parent_rejected(self, job_dir):
        create_node(str(job_dir), "fetch")
        result = create_node(
            str(job_dir),
            "fetch",
            parent_node_ids=["fetch_001"],
        )
        assert result["success"] is False
        assert "DAG root" in result["error"]

    def test_fetch_with_dependency_rejected(self, job_dir):
        create_node(str(job_dir), "fetch")
        result = create_node(
            str(job_dir),
            "fetch",
            dependency_node_ids=["fetch_001"],
        )
        assert result["success"] is False
        assert "DAG root" in result["error"]

    def test_non_fetch_with_parent_still_allowed(self, job_dir):
        """Invariant only applies to fetch; other types may have parents."""
        create_node(str(job_dir), "fetch")
        result = create_node(
            str(job_dir),
            "prep",
            parent_node_ids=["fetch_001"],
        )
        assert result["success"] is True
