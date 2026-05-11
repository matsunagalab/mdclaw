"""Unit tests for the artifact integrity helpers added in v1.0.x.

These cover the failure modes that string-equality JSON checks miss: template
stubs left in place, PNG figures that are actually text, citations that are
not anchored to the curator's allowed pool, and manifest.status="completed"
on a submission whose artifacts are still 50-byte scaffolds.
"""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark import integrity


# ---------------------------------------------------------------------------
# artifact_min_bytes


def test_artifact_min_bytes_missing(tmp_path: Path):
    warn = integrity.check_artifact_min_bytes(tmp_path, "missing.json", 100)
    assert warn is not None
    assert "file not found" in warn


def test_artifact_min_bytes_too_small(tmp_path: Path):
    (tmp_path / "evidence_report.json").write_text("{}")
    warn = integrity.check_artifact_min_bytes(
        tmp_path, "evidence_report.json", 100,
    )
    assert warn is not None
    assert "template stub" in warn


def test_artifact_min_bytes_pass(tmp_path: Path):
    (tmp_path / "evidence_report.json").write_text("a" * 500)
    warn = integrity.check_artifact_min_bytes(
        tmp_path, "evidence_report.json", 100,
    )
    assert warn is None


# ---------------------------------------------------------------------------
# template_markers


def test_template_markers_detects_placeholder(tmp_path: Path):
    (tmp_path / "methods.md").write_text(
        "# Methods\n\nTemplate placeholder. Replace before scoring.\n"
    )
    warn = integrity.check_template_markers(
        tmp_path, "methods.md",
        ["Template placeholder", "Replace before scoring"],
    )
    assert warn is not None
    assert "Template placeholder" in warn


def test_template_markers_pass_when_real_content(tmp_path: Path):
    (tmp_path / "methods.md").write_text(
        "# Methods\n\nWe equilibrated T4L for 100 ps in NPT.\n"
    )
    warn = integrity.check_template_markers(
        tmp_path, "methods.md", ["Replace with", "Template placeholder"],
    )
    assert warn is None


# ---------------------------------------------------------------------------
# markdown_structure


def test_markdown_structure_flags_missing_section(tmp_path: Path):
    (tmp_path / "methods.md").write_text(
        "# Title\n\n## Methods\n\nDetails here.\n"
    )
    warnings = integrity.check_markdown_structure(
        tmp_path, "methods.md", min_h2=2,
        required_sections=["Methods", "Limitations"],
    )
    # one H2 missing, one section missing
    assert any("H2" in w for w in warnings)
    assert any("Limitations" in w for w in warnings)


def test_markdown_structure_pass(tmp_path: Path):
    (tmp_path / "methods.md").write_text(
        "# Title\n\n## Methods\n\n...\n\n## Limitations\n\n...\n"
    )
    warnings = integrity.check_markdown_structure(
        tmp_path, "methods.md", min_h2=2,
        required_sections=["Methods", "Limitations"],
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# evidence_completeness


def test_evidence_completeness_flags_missing_keys():
    evidence = {"effect": {"direction": "destabilizing"}}
    warnings = integrity.check_evidence_completeness(
        evidence, ["effect.direction", "limitations", "evidence.citations"],
    )
    assert any("limitations" in w for w in warnings)
    assert any("evidence.citations" in w for w in warnings)
    assert not any("effect.direction" in w for w in warnings)


def test_evidence_completeness_flags_empty_lists():
    evidence = {
        "effect": {"direction": "destabilizing"},
        "limitations": [],
        "evidence": {"citations": []},
    }
    warnings = integrity.check_evidence_completeness(
        evidence, ["limitations", "evidence.citations"],
    )
    assert len(warnings) == 2
    assert all("empty" in w for w in warnings)


# ---------------------------------------------------------------------------
# citation_pool


def test_citation_pool_flags_fabricated_doi(tmp_path: Path):
    pool = {
        "allowed_source_pools": ["FireProtDB", "S669"],
        "primary_reference": {"doi": "10.1126/science.1553543"},
    }
    pool_file = tmp_path / "refs.json"
    pool_file.write_text(json.dumps(pool))

    evidence = {"evidence": {"citations": [
        {"doi": "10.9999/fabricated.doi", "source": "Made-Up Journal"},
    ]}}
    warnings = integrity.check_citation_pool(evidence, pool_file)
    assert len(warnings) == 1
    assert "not anchored" in warnings[0]


def test_citation_pool_pass_for_primary_doi(tmp_path: Path):
    pool = {
        "allowed_source_pools": ["FireProtDB"],
        "primary_reference": {"doi": "10.1126/science.1553543"},
    }
    pool_file = tmp_path / "refs.json"
    pool_file.write_text(json.dumps(pool))

    evidence = {"evidence": {"citations": [
        "Eriksson 1992, doi:10.1126/science.1553543",
    ]}}
    warnings = integrity.check_citation_pool(evidence, pool_file)
    assert warnings == []


def test_citation_pool_pass_for_allowed_pool(tmp_path: Path):
    pool = {
        "allowed_source_pools": ["FireProtDB", "S669"],
        "primary_reference": {"doi": "10.x/y"},
    }
    pool_file = tmp_path / "refs.json"
    pool_file.write_text(json.dumps(pool))

    evidence = {"evidence": {"citations": [
        {
            "source": "FireProtDB",
            "pmid": "12345678",
            "note": "single-mutation ΔΔG records",
        },
    ]}}
    warnings = integrity.check_citation_pool(evidence, pool_file)
    assert warnings == []


def test_citation_pool_rejects_pool_name_without_anchor(tmp_path: Path):
    pool = {
        "allowed_source_pools": ["FireProtDB", "S669"],
        "primary_reference": {"doi": "10.x/y"},
    }
    pool_file = tmp_path / "refs.json"
    pool_file.write_text(json.dumps(pool))

    evidence = {"evidence": {"citations": [
        {"source": "FireProtDB", "note": "single-mutation ΔΔG records"},
    ]}}
    warnings = integrity.check_citation_pool(evidence, pool_file)
    assert len(warnings) == 1
    assert "lacks DOI" in warnings[0]


def test_citation_pool_rejects_pool_name_in_free_text_note(tmp_path: Path):
    pool = {
        "allowed_source_pools": ["FireProtDB", "S669"],
        "primary_reference": {"doi": "10.x/y"},
    }
    pool_file = tmp_path / "refs.json"
    pool_file.write_text(json.dumps(pool))

    evidence = {"evidence": {"citations": [
        {"note": "this is similar to FireProtDB"},
    ]}}
    warnings = integrity.check_citation_pool(evidence, pool_file)
    assert len(warnings) == 1
    assert "not anchored" in warnings[0]


def test_citation_pool_flags_no_citations(tmp_path: Path):
    pool_file = tmp_path / "refs.json"
    pool_file.write_text(json.dumps(
        {"allowed_source_pools": ["X"], "primary_reference": {"doi": "10.x/y"}}
    ))

    evidence: dict = {"evidence": {"citations": []}}
    warnings = integrity.check_citation_pool(evidence, pool_file)
    assert any("no citations" in w for w in warnings)


# ---------------------------------------------------------------------------
# figures_are_png


def test_png_magic_rejects_text_file(tmp_path: Path):
    fig = tmp_path / "figure.png"
    fig.write_text("This is actually a text caption, not a PNG.\n")
    warn = integrity.check_png_magic(fig)
    assert warn is not None
    assert "not a PNG" in warn


def test_png_magic_accepts_real_png_header(tmp_path: Path):
    fig = tmp_path / "figure.png"
    # 8-byte PNG magic + minimal padding so size > 1024
    fig.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048)
    warn = integrity.check_png_magic(fig)
    assert warn is None


def test_figures_are_png_flags_text_disguised_as_png(tmp_path: Path):
    figs_dir = tmp_path / "figures"
    figs_dir.mkdir()
    (figs_dir / "rmsf.png").write_text("Caption text only.\n")
    warnings = integrity.check_figures_are_png(
        tmp_path, ["figures/rmsf.png"], min_figure_bytes=1024,
    )
    # too small AND wrong magic — but min_bytes is checked first and short-circuits
    assert len(warnings) == 1
    assert ("template stub" in warnings[0]) or ("not a PNG" in warnings[0])


# ---------------------------------------------------------------------------
# status_artifact_floor


def test_status_floor_blocks_completed_with_template(tmp_path: Path):
    (tmp_path / "methods.md").write_text("# Methods\n\nTemplate.\n")
    manifest = {"status": "completed"}
    warnings = integrity.check_status_artifact_floor(
        manifest, tmp_path, {"methods.md": 500},
    )
    assert len(warnings) == 1
    assert "completed" in warnings[0]


def test_status_floor_waives_when_partial(tmp_path: Path):
    (tmp_path / "methods.md").write_text("# Methods\n\nTemplate.\n")
    manifest = {"status": "partial"}
    warnings = integrity.check_status_artifact_floor(
        manifest, tmp_path, {"methods.md": 500},
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# run_artifact_integrity dispatch


def _make_integrity_check(**kwargs):
    from mdclaw.benchmark.models import IntegrityCheck
    return IntegrityCheck(**kwargs)


def test_run_artifact_integrity_routes_template_markers(tmp_path: Path):
    (tmp_path / "methods.md").write_text("Template placeholder.\n")
    checks = [_make_integrity_check(
        check_id="m1", check_type="template_markers",
        path="methods.md", forbid_markers=["Template placeholder"],
    )]
    warnings = integrity.run_artifact_integrity(
        tmp_path, checks, manifest={}, evidence={},
    )
    assert len(warnings) == 1
    assert "[m1]" in warnings[0]


def test_run_artifact_integrity_routes_unknown_type_to_warning(tmp_path: Path):
    # Build an IntegrityCheck object whose check_type the dispatcher does not
    # know. We sidestep pydantic Literal validation by constructing it with
    # the Literal-matching type and then mutating the attribute, since we
    # want to exercise the unknown-type branch defensively.
    check = _make_integrity_check(
        check_id="m2", check_type="template_markers", path="x", forbid_markers=[],
    )
    object.__setattr__(check, "check_type", "future_check_v2")
    warnings = integrity.run_artifact_integrity(
        tmp_path, [check], manifest={}, evidence={},
    )
    assert len(warnings) == 1
    assert "unknown integrity check_type" in warnings[0]
