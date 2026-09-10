"""Argument typing and node state around tool failures.

Three things went wrong at once on 2026-09-10 when ``modeller_from_alignment``
was called with ``--disulfide-patches '[[34,77]]'``: the parameter was a bare
``list``, which the CLI neither treats as ``nargs`` nor as JSON, so the raw
string reached the tool; the tool had already marked its source node
``running`` when it choked on that string; and the CLI failed only nodes of
node-required tools, so the node stayed ``running`` with no event. Each is
pinned here.
"""
import importlib
import inspect
from typing import List, Optional

import pytest

cli = importlib.import_module("mdclaw._cli")
gm = importlib.import_module("mdclaw.genesis.modeller")
lifecycle = importlib.import_module("mdclaw.node.lifecycle")


def test_bare_list_annotation_is_refused():
    def tool(x: Optional[list] = None):
        return x

    with pytest.raises(TypeError, match="bare list"):
        cli._tool_param_specs(tool)

    def tool_typing(x: List = None):  # noqa: UP006 - the bare typing alias is the point
        return x

    with pytest.raises(TypeError, match="bare list"):
        cli._tool_param_specs(tool_typing)


def test_element_typed_lists_are_accepted_and_json_lists_parse():
    def tool(names: list[str], pairs: Optional[list[list[int]]] = None, sites: Optional[list[dict]] = None):
        return names, pairs, sites

    specs = {spec.name: spec for spec in cli._tool_param_specs(tool)}
    assert not cli._takes_json(specs["names"].hint)
    assert cli._takes_json(specs["pairs"].hint) and cli._takes_json(specs["sites"].hint)
    assert cli._coerce_value("[[3, 40], [5, 9]]", specs["pairs"].hint) == [[3, 40], [5, 9]]


def test_no_registered_tool_carries_a_bare_list():
    tools = cli._discover_tools()
    for name, info in tools.items():
        fn = info["fn"] if isinstance(info, dict) else info
        cli._tool_param_specs(fn)   # raises TypeError on a bare list


def test_modeller_patch_parameters_are_json_typed():
    hints = inspect.get_annotations(gm.modeller_from_alignment, eval_str=True)
    assert cli._takes_json(hints["disulfide_patches"])
    assert cli._takes_json(hints["target_residue_sites"])


def test_running_node_is_failed_by_the_cli(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    created = lifecycle.create_node(str(job_dir), "source")
    node_id = created["node_id"]
    assert lifecycle.read_node(str(job_dir), node_id)["status"] == "pending"
    # A pending node is not the CLI's to fail: an argument error before the
    # tool started is a corrected invocation of the same node.
    assert cli._fail_node_if_running(str(job_dir), node_id, ["boom"]) is False
    assert lifecycle.read_node(str(job_dir), node_id)["status"] == "pending"

    lifecycle.begin_node(str(job_dir), node_id)
    assert lifecycle.read_node(str(job_dir), node_id)["status"] == "running"
    assert cli._fail_node_if_running(str(job_dir), node_id, ["boom"]) is True
    node = lifecycle.read_node(str(job_dir), node_id)
    assert node["status"] == "failed"
    events = sorted(p.name for p in (job_dir / "events").iterdir())
    assert any("tool_failed" in name for name in events)


def test_modeller_rejects_bad_patches_before_touching_the_node(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_MODELLER10v8", "not-a-real-key")
    template = tmp_path / "tmpl.pdb"
    template.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  GLY A   2       3.800   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    alignment = tmp_path / "aln.ali"
    alignment.write_text(
        ">P1;tmpl\nstructureX:tmpl:FIRST:A:LAST:A:::-1.00:-1.00\nAG*\n"
        ">P1;target\nsequence:target:::::::0.00:0.00\nAG*\n"
    )
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    node_id = lifecycle.create_node(str(job_dir), "source")["node_id"]

    result = gm.modeller_from_alignment(
        template_pdb=str(template),
        alignment_file=str(alignment),
        template_code="tmpl",
        target_code="target",
        disulfide_patches="[[0,1]]",      # the raw string the old CLI handed over
        output_dir=str(tmp_path / "out"),
        job_dir=str(job_dir),
        node_id=node_id,
    )
    assert result["success"] is False
    assert result["code"] == "invalid_disulfide_patches"
    # The argument was refused before begin_node: the node is still pending,
    # so the corrected invocation can run on the same node.
    assert lifecycle.read_node(str(job_dir), node_id)["status"] == "pending"
