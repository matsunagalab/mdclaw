"""Target selection, replica identity, provenance and read-only report contracts."""

import json
from pathlib import Path

import pytest

from mdclaw.evidence import generate_md_report


def node(job, nid, kind="prod", parents=(), metadata=None, status="completed", **extra):
    path = job / "nodes" / nid / "node.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(node_id=nid, node_type=kind, parent_node_ids=list(parents),
                  dependency_node_ids=[], metadata=metadata or {}, conditions={},
                  artifacts={}, status=status, **extra)
    path.write_text(json.dumps(record))
    return path


def target(job, nid, label):
    return dict(job_dir=str(job), node_id=nid, label=label)


def test_single_leaf_and_read_only(tmp_path):
    node(tmp_path, "source", "source")
    node(tmp_path, "prod", parents=["source"], metadata={"temperature_kelvin": 300})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = generate_md_report(job_dir=str(tmp_path))
    assert result["success"]
    history = result["report"]["subjects"][0]["history"]
    assert [r["node_id"] for r in history] == ["source", "prod"]
    assert history[-1]["source"]["metadata_pointer"] == "/metadata"
    assert generate_md_report(job_dir=str(tmp_path)) == result
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_multiple_leaves_require_selection_including_failed(tmp_path):
    node(tmp_path, "a")
    node(tmp_path, "b", status="failed")
    out = tmp_path / "report"
    result = generate_md_report(job_dir=str(tmp_path), grouping="replicas", output_dir=str(out))
    assert result["code"] == "report_selection_required"
    assert {t["status"] for t in result["candidates"]} == {"completed", "failed"}
    assert not out.exists()


def test_same_job_replicas_preserve_shared_eq_and_differences(tmp_path):
    node(tmp_path, "eq", "eq")
    node(tmp_path, "a", parents=["eq"], metadata={"temperature_kelvin": 300, "random_seed": 1})
    node(tmp_path, "b", parents=["eq"], metadata={"temperature_kelvin": 300, "random_seed": 2})
    node(tmp_path, "ignored", metadata={"temperature_kelvin": 999})
    targets = [target(tmp_path, "a", "r1"), target(tmp_path, "b", "r2")]
    assert generate_md_report(targets=targets)["code"] == "report_selection_required"
    result = generate_md_report(targets=targets, grouping="replicas")
    assert result["success"]
    report = result["report"]
    assert report["relationships"][0]["shared_ancestors"] == ["eq"]
    assert report["relationships"][0]["shared_production"] == []
    assert report["comparison"]["common_recorded_settings"]["prod/1/recorded/temperature_kelvin"] == 300
    assert "prod/1/recorded/random_seed" in report["comparison"]["differences"]
    assert "ignored" not in json.dumps(report)


def test_cross_job_replicas_missing_not_equal_null(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    node(a, "prod", metadata={"random_seed": None})
    node(b, "prod")
    result = generate_md_report(targets=[target(a, "prod", "a"), target(b, "prod", "b")], grouping="replicas")
    assert result["success"]
    rows = result["report"]["comparison"]["differences"]["prod/1/recorded/random_seed"]
    assert [r["present"] for r in rows] == [True, False]


def test_continuations_and_duplicate_analyses_are_not_replicas(tmp_path):
    node(tmp_path, "p1")
    node(tmp_path, "p2", parents=["p1"])
    node(tmp_path, "a1", "analyze", ["p2"])
    node(tmp_path, "a2", "analyze", ["p2"])
    for left, right in (("p1", "p2"), ("a1", "a2")):
        targets = [target(tmp_path, left, "a"), target(tmp_path, right, "b")]
        assert not generate_md_report(targets=targets, grouping="replicas")["success"]
        assert generate_md_report(targets=targets, grouping="separate")["success"]


@pytest.mark.parametrize("parents", [["missing"], ["a"]])
def test_broken_lineage_fails(tmp_path, parents):
    node(tmp_path, "a", parents=parents)
    assert not generate_md_report(targets=[target(tmp_path, "a", "a")])["success"]


def test_dependencies_are_recorded_but_not_production_ancestors(tmp_path):
    node(tmp_path, "dep")
    path = node(tmp_path, "a")
    data = json.loads(path.read_text())
    data["dependency_node_ids"] = ["dep"]
    path.write_text(json.dumps(data))
    subject = generate_md_report(targets=[target(tmp_path, "a", "a")])["report"]["subjects"][0]
    assert len(subject["history"]) == 2
    assert subject["production_node_ids"] == ["a"]


def test_production_forks_retain_shared_prefix_without_pooling(tmp_path):
    node(tmp_path, "shared")
    node(tmp_path, "a", parents=["shared"])
    node(tmp_path, "b", parents=["shared"])
    result = generate_md_report(targets=[target(tmp_path, "a", "a"), target(tmp_path, "b", "b")], grouping="replicas")
    assert result["success"]
    assert result["report"]["relationships"][0]["shared_production"] == ["shared"]
    assert [s["production_frontier"] for s in result["report"]["subjects"]] == [["a"], ["b"]]


def test_runtime_citations_not_from_declarations(tmp_path):
    path = node(tmp_path, "p", metadata={"integrator_signature": {"integrator": "LangevinMiddleIntegrator"}})
    data = json.loads(path.read_text())
    data["conditions"] = {"method": "SHAKE WHAM MBAR"}
    data["artifacts"] = {"runtime_system": "system.xml", "integrator": "integrator.xml"}
    path.write_text(json.dumps(data))
    (path.parent / "system.xml").write_text('<System openmmVersion="8.5.1"><Constraints><Constraint/></Constraints><Forces><Force type="MonteCarloMembraneBarostat" pressure="1"/></Forces></System>')
    (path.parent / "integrator.xml").write_text('<Integrator type="LangevinMiddleIntegrator" temperature="300"/>')
    result = generate_md_report(job_dir=str(tmp_path), output_dir=str(tmp_path / "report"))
    assert result["success"]
    citations = result["report"]["citations"]
    assert len(citations["selected"]) == 5
    middle = next(c for c in citations["selected"] if c["key"] == "Zhang2019LFMiddle")
    assert middle["reasons"][0]["evidence_file"] == str(path.parent / "integrator.xml")
    assert middle["reasons"][0]["evidence_field"] == "/Integrator/@type"
    assert citations["documentation"][0]["dedicated_paper"] is None
    assert any(x["method"] == "constraint_solver" for x in citations["unresolved"])
    assert "SHAKE" not in citations["bibtex"]
    assert Path(result["files"]["bibtex"]).read_text() == citations["bibtex"]
    assert not generate_md_report(job_dir=str(tmp_path), output_dir=str(tmp_path / "report"))["success"]
    assert not generate_md_report(job_dir=str(tmp_path), output_dir=str(path.parent / "report"))["success"]


def test_study_named_plan_and_missing_jobs(tmp_path):
    (tmp_path / "study.json").write_text(json.dumps({"jobs": [{"job_id": "a", "job_dir": "jobs/a"}, {"job_id": "b", "job_dir": "jobs/b"}]}))
    (tmp_path / "plans").mkdir()
    plan = tmp_path / "plans" / "selected.json"
    plan.write_text(json.dumps({"plan": {"jobs": [{"job_id": "a"}]}}))
    node(tmp_path / "jobs/a", "p")
    result = generate_md_report(study_dir=str(tmp_path), plan_id="selected")
    assert result["success"]
    assert len(result["report"]["subjects"]) == 1
    assert result["report"]["study"]["plan_file"] == str(plan)
    plan.write_text(json.dumps({"plan": {"jobs": [{"job_id": "missing"}]}}))
    assert not generate_md_report(study_dir=str(tmp_path), plan_id="selected")["success"]
    assert not generate_md_report(study_dir=str(tmp_path), plan_id="absent")["success"]


def test_duplicate_and_invalid_inputs(tmp_path):
    node(tmp_path, "a")
    t = target(tmp_path, "a", "a")
    for kwargs in ({}, {"targets": []}, {"targets": [t, t], "grouping": "separate"},
                   {"job_dir": str(tmp_path), "targets": [t]}, {"job_dir": str(tmp_path), "plan_id": "x"}):
        assert not generate_md_report(**kwargs)["success"]


def test_corrupt_node_and_study_plan_fail_closed(tmp_path):
    path = node(tmp_path, "p")
    path.write_text("{")
    assert not generate_md_report(job_dir=str(tmp_path))["success"]
    (tmp_path / "study.json").write_text('{"jobs": []}')
    (tmp_path / "study_plan.json").write_text("{")
    assert not generate_md_report(study_dir=str(tmp_path))["success"]


def test_cli_discovery_and_retirement():
    from mdclaw._cli import _discover_tools, _missing_tool_error
    tools = _discover_tools()
    assert "generate_md_report" in tools
    for old in ("generate_md_evidence_report", "generate_study_evidence_report"):
        assert old not in tools
        assert "generate_md_report" in _missing_tool_error(old, tools)["message"]


def test_missing_runtime_and_failed_analysis_stay_explicit(tmp_path):
    path = node(tmp_path, "a", "analyze", status="failed")
    data = json.loads(path.read_text())
    data["artifacts"] = {"integrator": "absent.xml"}
    data["metadata"] = {"integrator_signature": {"integrator": "LangevinMiddleIntegrator"}}
    path.write_text(json.dumps(data))
    report = generate_md_report(job_dir=str(tmp_path))["report"]
    assert report["subjects"][0]["analysis_results"] == []
    assert report["subjects"][0]["history"][0]["warnings"]
    assert report["citations"]["selected"] == []


def test_recorded_parameters_not_inferred_from_label(tmp_path):
    node(tmp_path, "p", label="ff19SB OPC HMR", metadata={"effective_forcefield": "ff14SB", "water_model": "tip3p", "hmr": True})
    refs = generate_md_report(job_dir=str(tmp_path))["report"]["citations"]["selected"]
    assert {r["key"] for r in refs} == {"Maier2015ff14SB", "Jorgensen1983TIP3P", "Hopkins2015HMR"}


def test_packaged_bibliography_matches_verified_audit():
    import re
    from mdclaw.evidence import citations
    audit = Path(__file__).parents[1] / "docs/research/citation-audit-2026-09-06.bib"
    source = audit.read_text()
    packaged = Path(citations.__file__).with_name("references.bib").read_text()
    entries = list(re.finditer(r"@\w+\{([^,]+),\n.*?^\}", packaged, re.M | re.S))
    assert len(entries) == len({m[1] for m in entries}) == 13
    assert all(m[0] in source for m in entries)


def test_cli_execution_with_json_targets(tmp_path, capsys):
    from mdclaw._cli import main
    node(tmp_path, "a")
    targets = [target(tmp_path, "a", "r1")]
    with pytest.raises(SystemExit) as exc:
        main(["generate_md_report", "--targets", json.dumps(targets)])
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["success"]
    assert result["report"]["subjects"][0]["label"] == "r1"
