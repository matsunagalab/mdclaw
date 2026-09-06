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


@pytest.mark.parametrize("kind, left, right, difference", [
    ("min", {"restraint_atoms": "solute_heavy", "restraint_force_constant": 100},
     {"restraint_atoms": "solute_heavy", "restraint_force_constant": 200},
     "restraint_force_constant"),
    ("eq", {"restraint_atoms": "solute_heavy", "restraint_count": 12},
     {"restraint_atoms": "backbone", "restraint_count": 8}, "restraint_atoms"),
    ("prod", {"distance_restraints": [{"name": "d", "selection_group1": "resid 0",
                                      "selection_group2": "resid 1", "target_distance_nm": 1,
                                      "force_constant_kj_mol_nm2": 100}]},
     {"distance_restraints": [{"name": "d", "selection_group1": "resid 0",
                              "selection_group2": "resid 1", "target_distance_nm": 2,
                              "force_constant_kj_mol_nm2": 100}]}, "distance_restraints"),
    ("prod", {"custom_force": {"kind": "torch_script_energy", "signature": {
        "sha256": "same_script", "parameters": {"k": 100}}}},
     {"custom_force": {"kind": "torch_script_energy", "signature": {
         "sha256": "same_script", "parameters": {"k": 200}}}},
     "custom_force/signature/parameters/k"),
    ("prod", {"custom_force_signature": {"sha256": "script_a", "parameters": {}}},
     {"custom_force_signature": {"sha256": "script_b", "parameters": {}}},
     "custom_force_signature/sha256"),
    ("prod", {"steering": {"duration_steps": 1000, "update_steps": 10}},
     {"steering": {"duration_steps": 2000, "update_steps": 10}}, "steering/duration_steps"),
    ("prod", {"plumed": {"protocol": {"signature": {"sha256": "input_a"}}}},
     {"plumed": {"protocol": {"signature": {"sha256": "input_b"}}}},
     "plumed/protocol/signature/sha256"),
])
def test_execution_argument_biases_are_compared(tmp_path, kind, left, right, difference):
    # No node conditions: these settings were supplied as tool execution arguments.
    node(tmp_path, "a", kind, metadata=left)
    node(tmp_path, "b", kind, metadata=right)
    result = generate_md_report(
        targets=[target(tmp_path, "a", "a"), target(tmp_path, "b", "b")], grouping="separate")
    assert result["success"]
    rows = result["report"]["comparison"]["differences"][f"{kind}/1/recorded/{difference}"]
    assert all(row["present"] for row in rows)
    assert rows[0]["value"] != rows[1]["value"]


def test_biased_and_unbiased_productions_are_distinguished(tmp_path):
    node(tmp_path, "a", metadata={"distance_restraints": [{"target_distance_nm": 1}]})
    node(tmp_path, "b")
    report = generate_md_report(
        targets=[target(tmp_path, "a", "a"), target(tmp_path, "b", "b")],
        grouping="replicas")["report"]
    rows = report["comparison"]["differences"]["prod/1/recorded/distance_restraints"]
    assert [row["present"] for row in rows] == [True, False]


def test_bias_progress_and_artifact_locations_remain_history_only(tmp_path):
    for nid, elapsed in (("a", 10), ("b", 20)):
        node(tmp_path, nid, metadata={
            "custom_force_signature": {"sha256": "same_script", "parameters": {"k": 100}},
            "steering": {"duration_steps": 20, "update_steps": 1, "initial_sha256": "same",
                         "initial_file": str(tmp_path / nid / "steering_initial.npz"),
                         "elapsed_steps": elapsed, "schedule_complete": elapsed == 20,
                         "progress": elapsed / 20},
            "plumed": {"protocol": {"signature": {"sha256": "same_input"}},
                       "elapsed_steps": elapsed, "schedule_complete": elapsed == 20},
            "final_step": elapsed,
        })
    report = generate_md_report(
        targets=[target(tmp_path, "a", "a"), target(tmp_path, "b", "b")],
        grouping="replicas")["report"]
    assert report["comparison"]["differences"] == {}
    assert [s["history"][0]["recorded_metadata"]["steering"]["elapsed_steps"]
            for s in report["subjects"]] == [10, 20]


@pytest.mark.parametrize("change", ["measurement", "progress", "target", "force_constant"])
def test_distance_steering_summary_separates_results_from_protocol(tmp_path, change):
    from mdclaw.simulation.steering import DistanceSteering

    summaries = []
    for nid in ("a", "b"):
        # Exercise the real summary serializer without starting a simulation.
        steering = object.__new__(DistanceSteering)
        restraint = {"name": "d", "target_distance_nm": 3 if nid == "b" and change == "target" else 2,
                     "force_constant_kj_mol_nm2": 200 if nid == "b" and change == "force_constant" else 100}
        steering.protocol = {"duration_steps": 100, "update_steps": 10,
                             "signature": {"restraints": [restraint]},
                             "initial_distances_nm": {"d": 1}}
        steering.loaded = {"restraints": [restraint]}
        steering.fixed = False
        steering.elapsed = 50 if nid == "b" and change == "progress" else 100
        steering.centers = {"d": 1.5 if steering.elapsed == 50 else restraint["target_distance_nm"]}
        distance = 2.1 if nid == "b" and change == "measurement" else 1.9
        steering.distances = lambda distance=distance: {"d": distance}
        summaries.append(steering.summary())
        node(tmp_path, nid, metadata={"steering": summaries[-1]})
    result = generate_md_report(
        targets=[target(tmp_path, "a", "a"), target(tmp_path, "b", "b")], grouping="replicas")
    assert result["success"]
    report = result["report"]
    differences = report["comparison"]["differences"]
    if change in ("measurement", "progress"):
        assert differences == {}
    else:
        assert set(differences) == {"prod/1/recorded/steering/signature/restraints"}
    assert [s["history"][0]["recorded_metadata"]["steering"] for s in report["subjects"]] == summaries


@pytest.mark.parametrize("parameter", ["center", "force_constant", "group_weight"])
def test_nested_runtime_force_parameters_are_compared(tmp_path, parameter):
    openmm = pytest.importorskip("openmm")
    for nid, value in (("a", 1.0), ("b", 2.0)):
        system = openmm.System()
        for _ in range(3):
            system.addParticle(12)
        force = openmm.CustomCentroidBondForce(2, "0.5*k*(distance(g1,g2)-r0)^2")
        force.addPerBondParameter("k")
        force.addPerBondParameter("r0")
        force.addGroup([0, 1], [1, value if parameter == "group_weight" else 1])
        force.addGroup([2])
        force.addBond([0, 1], [value if parameter == "force_constant" else 100,
                               value if parameter == "center" else 1])
        system.addForce(force)
        path = node(tmp_path, nid)
        data = json.loads(path.read_text())
        data["artifacts"] = {"runtime_system": "runtime.xml"}
        path.write_text(json.dumps(data))
        (path.parent / "runtime.xml").write_text(openmm.XmlSerializer.serialize(system))
    report = generate_md_report(
        targets=[target(tmp_path, "a", "a"), target(tmp_path, "b", "b")],
        grouping="replicas")["report"]
    rows = report["comparison"]["differences"]["prod/1/runtime/runtime_system/forces"]
    left, right = (row["value"][0] for row in rows)
    assert left["definition_sha256"] != right["definition_sha256"]
    assert {k: v for k, v in left.items() if k != "definition_sha256"} == {
        k: v for k, v in right.items() if k != "definition_sha256"}


def test_runtime_force_comparison_ignores_xml_format_and_source_location(tmp_path):
    definitions = [
        '<Force type="CustomBondForce" name="bias"><Bonds><Bond p1="0" p2="1" param1="1"/></Bonds></Force>',
        '<Force name="bias" type="CustomBondForce">\n  <Bonds>\n    <Bond param1="1" p2="1" p1="0"/>\n  </Bonds>\n</Force>',
    ]
    for nid, definition in zip(("a", "b"), definitions):
        path = node(tmp_path / nid, "prod")
        data = json.loads(path.read_text())
        data["artifacts"] = {"runtime_system": f"{nid}.xml"}
        path.write_text(json.dumps(data))
        (path.parent / f"{nid}.xml").write_text(f"<System><Forces>{definition}</Forces></System>")
    report = generate_md_report(
        targets=[target(tmp_path / nid, "prod", nid) for nid in ("a", "b")],
        grouping="replicas")["report"]
    assert report["comparison"]["differences"] == {}
    sources = [s["history"][0]["runtime_sources"]["runtime_system"] for s in report["subjects"]]
    assert sources[0]["file"] != sources[1]["file"]
    assert sources[0]["sha256"] != sources[1]["sha256"]


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
