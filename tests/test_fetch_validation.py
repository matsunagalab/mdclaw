"""Unit tests for structure-acquisition tool node validation.

Verifies that ``fetch_structure`` and its compatibility wrappers reject:
  - a node_id that doesn't exist
  - a node_id whose node_type is not ``source``

These checks must happen **before** ``begin_node`` so the wrong node never
receives source metadata or a status mutation. Tests don't hit the network
because the validation short-circuits before any HTTP call.
"""

import asyncio
import json
import textwrap

import pytest

from mdclaw._node import create_node, read_node

pytest.importorskip("httpx")

from mdclaw.research.fetch import (
    download_structure,
    fetch_structure,
    get_alphafold_structure,
)
from mdclaw.research.source_node import (
    list_source_candidates,
    register_local_structure,
)


BIOMT_PDB = textwrap.dedent("""\
HEADER    TEST BIOLOGICAL ASSEMBLY
REMARK 350 BIOMOLECULE: 1
REMARK 350 APPLY THE FOLLOWING TO CHAINS: A
REMARK 350   BIOMT1   1  1.000000 0.000000 0.000000        0.00000
REMARK 350   BIOMT2   1  0.000000 1.000000 0.000000        0.00000
REMARK 350   BIOMT3   1  0.000000 0.000000 1.000000        0.00000
REMARK 350   BIOMT1   2  1.000000 0.000000 0.000000       10.00000
REMARK 350   BIOMT2   2  0.000000 1.000000 0.000000        0.00000
REMARK 350   BIOMT3   2  0.000000 0.000000 1.000000        0.00000
ATOM      1  N   GLY A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  GLY A   1       2.000   1.000   1.000  1.00 10.00           C
TER
END
""")


@pytest.fixture
def job_dir(tmp_path):
    jd = tmp_path / "job_validate"
    jd.mkdir()
    return jd


@pytest.fixture
def prep_node(job_dir):
    """A non-source node that structure-acquisition tools must refuse to write under."""
    result = create_node(str(job_dir), "prep")
    assert result["success"]
    return result["node_id"]


# ── fetch_structure ────────────────────────────────────────────────────────


class TestSourceStructureValidation:

    def test_missing_node_id(self, job_dir):
        result = asyncio.run(fetch_structure(
            source="pdb",
            pdb_id="1AKE",
            job_dir=str(job_dir),
            node_id="source_999",
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
        assert any("expected 'source'" in e for e in result["errors"])
        prep_data = read_node(str(job_dir), prep_node)
        assert prep_data["status"] == "pending"
        assert prep_data["artifacts"] == {}

    def test_local_fetch_generates_requested_assembly_candidate(self, job_dir, tmp_path):
        pytest.importorskip("gemmi")

        source_file = tmp_path / "biomt.pdb"
        source_file.write_text(BIOMT_PDB)
        source_node = create_node(str(job_dir), "source")["node_id"]

        result = asyncio.run(fetch_structure(
            source="local",
            file_path=str(source_file),
            job_dir=str(job_dir),
            node_id=source_node,
            assembly_ids=["1"],
        ))

        assert result["success"] is True
        assert result["assembly_generation"]["generated_count"] == 1

        candidates = list_source_candidates(str(job_dir), source_node)
        assert candidates["success"] is True
        assert [c["structure_id"] for c in candidates["candidates"]] == [
            "candidate_001",
            "candidate_002",
        ]
        assembly = candidates["candidates"][1]
        assert assembly["label"] == "biological assembly 1"
        assert assembly["origin"]["kind"] == "pdb_biological_assembly"
        assert assembly["origin"]["assembly_id"] == "1"
        assert assembly["metrics"]["chain_count"] == 2
        assert assembly["exists"] is True

    def test_invalid_assembly_mode_leaves_source_pending_for_cli_retry(
        self,
        job_dir,
        tmp_path,
        capsys,
    ):
        from mdclaw._cli import main

        source_file = tmp_path / "input.pdb"
        source_file.write_text(BIOMT_PDB)
        source_node = create_node(str(job_dir), "source")["node_id"]
        node_dir = job_dir / "nodes" / source_node

        with pytest.raises(SystemExit) as exc_info:
            main([
                "--job-dir", str(job_dir),
                "--node-id", source_node,
                "fetch_structure",
                "--source", "local",
                "--file-path", str(source_file),
                "--assembly-mode", "monomer",
            ])
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "invalid_assembly_mode"
        assert "none|preferred|all|ids" in payload["context"]["expected"]
        assert any("without truncating" in hint for hint in payload["hints"])

        pending = read_node(str(job_dir), source_node)
        assert pending["status"] == "pending"
        assert pending["artifacts"] == {}
        assert not (node_dir / "artifacts" / "failure").exists()

        with pytest.raises(SystemExit) as exc_info:
            main([
                "--job-dir", str(job_dir),
                "--node-id", source_node,
                "fetch_structure",
                "--source", "local",
                "--file-path", str(source_file),
                "--assembly-mode", "none",
            ])
        assert exc_info.value.code == 0
        capsys.readouterr()
        assert read_node(str(job_dir), source_node)["status"] == "completed"

    @pytest.mark.parametrize(
        ("source_args", "expected_code"),
        [
            (["--source", "pdb"], "missing_pdb_id"),
            (
                ["--source", "pdb", "--pdb-id", "1AKE", "--format", "mmcif"],
                "invalid_structure_format",
            ),
            (
                ["--source", "local", "--assembly-output-format", "mol2"],
                "invalid_assembly_output_format",
            ),
            (
                ["--source", "local", "--assembly-chain-naming", "invented"],
                "invalid_assembly_chain_naming",
            ),
        ],
    )
    def test_recoverable_fetch_inputs_leave_source_pending(
        self,
        source_args,
        expected_code,
        job_dir,
        tmp_path,
        capsys,
    ):
        from mdclaw._cli import main

        source_file = tmp_path / "input.pdb"
        source_file.write_text(BIOMT_PDB)
        source_node = create_node(str(job_dir), "source")["node_id"]
        args = [
            "--job-dir", str(job_dir),
            "--node-id", source_node,
            "fetch_structure",
            *source_args,
        ]
        if "local" in source_args:
            args.extend(["--file-path", str(source_file)])

        with pytest.raises(SystemExit) as exc_info:
            main(args)

        assert exc_info.value.code == 1
        assert json.loads(capsys.readouterr().out)["code"] == expected_code
        pending = read_node(str(job_dir), source_node)
        assert pending["status"] == "pending"
        assert pending["artifacts"] == {}
        assert not (job_dir / "nodes" / source_node / "artifacts" / "failure").exists()


# ── compatibility wrappers ─────────────────────────────────────────────────


# ── download_structure ─────────────────────────────────────────────────────


class TestDownloadStructureValidation:

    def test_missing_node_id(self, job_dir):
        result = asyncio.run(download_structure(
            pdb_id="1AKE",
            job_dir=str(job_dir),
            node_id="source_999",  # never created
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
        assert any("expected 'source'" in e for e in result["errors"])
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
            node_id="source_999",
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
        assert any("expected 'source'" in e for e in result["errors"])
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
            node_id="source_999",
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
        assert any("expected 'source'" in e for e in result["errors"])
        # prep node untouched
        prep_data = read_node(str(job_dir), prep_node)
        assert prep_data["status"] == "pending"
        assert prep_data["artifacts"] == {}


# ── create_node source-root invariant (smoke check at the API entrypoint) ──


class TestCreateNodeSourceInvariant:
    """Mirror the test_node.py coverage at the public API level."""

    def test_source_with_parent_rejected(self, job_dir):
        create_node(str(job_dir), "source")
        result = create_node(
            str(job_dir),
            "source",
            parent_node_ids=["source_001"],
        )
        assert result["success"] is False
        assert "DAG root" in result["error"]

    def test_source_with_dependency_rejected(self, job_dir):
        create_node(str(job_dir), "source")
        result = create_node(
            str(job_dir),
            "source",
            dependency_node_ids=["source_001"],
        )
        assert result["success"] is False
        assert "DAG root" in result["error"]

    def test_non_source_with_parent_still_allowed(self, job_dir):
        """Invariant only applies to source; other types may have parents."""
        create_node(str(job_dir), "source")
        result = create_node(
            str(job_dir),
            "prep",
            parent_node_ids=["source_001"],
        )
        assert result["success"] is True
