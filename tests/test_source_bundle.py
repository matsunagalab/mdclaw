"""Tests for structural source bundle helpers."""

from __future__ import annotations

import textwrap

import pytest

from mdclaw.source_bundle import (
    build_source_bundle,
    materialize_source_selection,
    select_source_structure,
    write_source_bundle,
)


MULTI_MODEL_PDB = textwrap.dedent("""\
MODEL        1
ATOM      1  N   GLY A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  GLY A   1       2.000   1.000   1.000  1.00 10.00           C
TER
ENDMDL
MODEL        2
ATOM      1  N   GLY A   1      11.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  GLY A   1      12.000   1.000   1.000  1.00 10.00           C
TER
ENDMDL
END
""")


def test_source_bundle_normalizes_nmr_models_to_candidate_files(tmp_path):
    pytest.importorskip("gemmi")

    source_node_dir = tmp_path / "job" / "nodes" / "source_001"
    source_artifacts = source_node_dir / "artifacts"
    source_artifacts.mkdir(parents=True)
    source_file = source_artifacts / "nmr.pdb"
    source_file.write_text(MULTI_MODEL_PDB)

    bundle = build_source_bundle(
        source_type="pdb",
        source_id="NMR",
        structure_paths=[source_file],
        source_node_dir=source_node_dir,
    )
    assert [s["structure_id"] for s in bundle["structures"]] == [
        "candidate_001",
        "candidate_002",
    ]
    assert bundle["storage_contract"] == "candidate_files"
    assert bundle["structures"][1]["requires_materialization"] is False
    assert bundle["structures"][1]["origin"]["model_rank"] == 2
    candidate_file = source_node_dir / bundle["structures"][1]["candidate_file"]
    assert candidate_file.is_file()

    rel_bundle = write_source_bundle(source_node_dir, bundle)
    prep_artifacts = tmp_path / "job" / "nodes" / "prep_001" / "artifacts"
    selected = materialize_source_selection(
        bundle_file=source_node_dir / rel_bundle,
        selection={"model_index": 2},
        prep_artifacts_dir=prep_artifacts,
    )

    assert selected["materialized"] is False
    assert selected["structure_file"] == str(candidate_file)
    assert "  11.000" in candidate_file.read_text()
    assert (prep_artifacts / "source_selection.json").is_file()


def test_source_bundle_applies_per_model_candidate_metadata(tmp_path):
    pytest.importorskip("gemmi")

    source_node_dir = tmp_path / "job" / "nodes" / "source_001"
    source_artifacts = source_node_dir / "artifacts"
    source_artifacts.mkdir(parents=True)
    source_file = source_artifacts / "ensemble.pdb"
    source_file.write_text(MULTI_MODEL_PDB)

    bundle = build_source_bundle(
        source_type="boltz2",
        source_id="boltz2_ensemble",
        structure_paths=[source_file],
        source_node_dir=source_node_dir,
        candidate_metadata=[{
            "origin": {"generator": "boltz2"},
            "models": [
                {
                    "label": "Boltz-2 candidate 1",
                    "metrics": {"confidence_score": 0.92},
                    "origin": {"boltz_model_index": 0},
                },
                {
                    "label": "Boltz-2 candidate 2",
                    "metrics": {"confidence_score": 0.81},
                    "origin": {
                        "boltz_model_index": 1,
                        "confidence_file": "confidence_model_1.json",
                    },
                },
            ],
        }],
    )

    second = bundle["structures"][1]
    assert second["label"] == "Boltz-2 candidate 2"
    assert second["origin"]["generator"] == "boltz2"
    assert second["origin"]["model_rank"] == 2
    assert second["origin"]["boltz_model_index"] == 1
    assert second["origin"]["confidence_file"] == "confidence_model_1.json"
    assert second["metrics"]["confidence_score"] == pytest.approx(0.81)


def test_multi_file_bundle_requires_explicit_structure_selection(tmp_path):
    source_node_dir = tmp_path / "job" / "nodes" / "source_001"
    source_artifacts = source_node_dir / "artifacts"
    source_artifacts.mkdir(parents=True)
    first = source_artifacts / "candidate_a.pdb"
    second = source_artifacts / "candidate_b.pdb"
    first.write_text("HEADER A\n")
    second.write_text("HEADER B\n")

    bundle = build_source_bundle(
        source_type="boltz2",
        source_id="boltz2_test",
        structure_paths=[first, second],
        source_node_dir=source_node_dir,
    )

    with pytest.raises(ValueError, match="multiple candidate structures"):
        select_source_structure(bundle)

    selected = select_source_structure(bundle, {"structure_id": "candidate_002"})
    assert selected["candidate_file"].endswith("candidates/candidate_002.pdb")
    assert selected["raw_file"].endswith("candidate_b.pdb")


def test_source_model_index_selection_prefers_exact_index_before_rank():
    bundle = {
        "schema_version": 1,
        "structures": [
            {
                "structure_id": "rank_two",
                "origin": {"model_index": 1, "model_rank": 2},
            },
            {
                "structure_id": "index_two",
                "origin": {"model_index": 2, "model_rank": 3},
            },
        ],
    }

    selected = select_source_structure(bundle, {"model_index": 2})
    assert selected["structure_id"] == "index_two"


def test_source_bundle_records_candidate_metadata_length_warning(tmp_path):
    source_node_dir = tmp_path / "job" / "nodes" / "source_001"
    source_artifacts = source_node_dir / "artifacts"
    source_artifacts.mkdir(parents=True)
    first = source_artifacts / "candidate_a.pdb"
    second = source_artifacts / "candidate_b.pdb"
    first.write_text("HEADER A\n")
    second.write_text("HEADER B\n")

    bundle = build_source_bundle(
        source_type="boltz2",
        source_id="boltz2_test",
        structure_paths=[first, second],
        source_node_dir=source_node_dir,
        candidate_metadata=[{"label": "first only"}],
    )

    assert "candidate_metadata length does not match" in bundle["metadata"]["warnings"][0]


def test_write_source_bundle_ends_with_newline(tmp_path):
    source_node_dir = tmp_path / "job" / "nodes" / "source_001"
    bundle = {
        "schema_version": 1,
        "source_type": "test",
        "source_id": "x",
        "storage_contract": "candidate_files",
        "structures": [{"structure_id": "candidate_001"}],
        "metadata": {},
    }

    rel = write_source_bundle(source_node_dir, bundle)

    assert (source_node_dir / rel).read_text().endswith("\n")
