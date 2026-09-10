"""A tool with a broken signature is isolated; the rest of the CLI keeps working.

Since b01aa61 a bare ``list`` annotation is refused when parameter specs are
built. Refusing it by raising from ``_build_parser`` took every entry point
down with a traceback (``--list``, ``--workflow``, ``--list-json`` and every
other tool). Now the tool is listed as not callable with the reason and
answers with ``tool_contract_invalid``; nothing else changes.
"""

import inspect
import json

import pytest

import mdclaw.research.inspection as inspection


@pytest.fixture
def broken_inspect_molecules(monkeypatch):
    fn = inspection.inspect_molecules
    signature = inspect.signature(fn)
    extra = inspect.Parameter("bad_param", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=list)
    monkeypatch.setattr(fn, "__signature__", signature.replace(
        parameters=[*signature.parameters.values(), extra]), raising=False)
    annotations = dict(fn.__annotations__)
    annotations["bad_param"] = list
    monkeypatch.setattr(fn, "__annotations__", annotations)
    return fn


def _run(argv):
    from mdclaw._cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    return exc_info.value.code


def test_discovery_marks_the_tool_instead_of_raising(broken_inspect_molecules, capsys):
    from mdclaw._cli import _discover_tools

    tools = _discover_tools()
    assert "bare list" in tools["inspect_molecules"]["contract_error"]
    assert "contract_error" not in tools["fetch_structure"]
    assert "invalid CLI contract" in capsys.readouterr().err


def test_the_index_and_the_workflow_still_print(broken_inspect_molecules, capsys):
    assert _run(["--list"]) == 0
    out = capsys.readouterr().out
    assert "Not callable (invalid CLI contract" in out
    assert "inspect_molecules: inspect_molecules.bad_param: bare list annotation" in out
    assert "fetch_structure" in out
    assert _run(["--workflow"]) == 0
    assert "source > prep > solv" in capsys.readouterr().out


def test_calling_the_broken_tool_is_a_structured_refusal(broken_inspect_molecules, capsys):
    assert _run(["inspect_molecules", "--structure-file", "x.pdb"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "tool_contract_invalid"
    assert payload["recoverable"] is False
    assert "bare list" in payload["context"]["contract_error"]


def test_list_json_reports_the_defect(broken_inspect_molecules, capsys):
    assert _run(["--list-json", "inspect_molecules"]) == 1
    targeted = json.loads(capsys.readouterr().out)
    assert targeted["code"] == "tool_contract_invalid"
    assert _run(["--list-json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    entry = next(t for t in listing["tools"] if t["name"] == "inspect_molecules")
    assert entry["callable"] is False and entry["parameters"] == []
    assert "contract_error" in entry


def test_other_tools_run_unaffected(broken_inspect_molecules, tmp_path, capsys):
    from mdclaw._node import update_job_params

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    update_job_params(str(job_dir), {"solvent_regime": "explicit"})
    assert _run(["--output", "id", "create_node", "--job-dir", str(job_dir), "--node-type", "source"]) == 0
    assert capsys.readouterr().out.strip() == "source_001"
