"""CLI replies to the slips MDDataBench v5 agents made (2026-10, three models).

Each test pins one observed failure mode: commands pasted verbatim from
``next``, an envelope read through ``| tail``, an empty ``--output id``
capture, guessed tool names, an empty ``--node-id`` variable, argparse prefix
matching, ``--job-dir`` without ``--node-id`` on read-only tools, a stage name
as a parent id, and a study created in the wrong place.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from pathlib import Path

import pytest

from mdclaw._envelope import STATUS_LINE_KEY, next_step, status_line
from mdclaw._node import create_node, update_job_params
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node

TRIPLE = {"system_xml": "artifacts/sys.xml", "topology_pdb": "artifacts/t.pdb",
          "state_xml": "artifacts/st.xml"}
TOY_PDB = """\
ATOM      1  N   MET A   1      11.104   6.134  -6.504  1.00  0.00           N
ATOM      2  CA  MET A   1      11.639   6.071  -5.147  1.00  0.00           C
ATOM      3  C   MET A   1      10.465   6.064  -4.164  1.00  0.00           C
ATOM      4  O   MET A   1       9.307   6.182  -4.584  1.00  0.00           O
ATOM      5  N   GLY A   2      10.767   5.929  -2.876  1.00  0.00           N
ATOM      6  CA  GLY A   2       9.738   5.917  -1.835  1.00  0.00           C
ATOM      7  C   GLY A   2      10.366   5.773  -0.462  1.00  0.00           C
ATOM      8  O   GLY A   2      11.588   5.719  -0.341  1.00  0.00           O
END
"""


@pytest.fixture
def job_dir(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    update_job_params(str(jd), {"solvent_regime": "explicit"})
    return jd


def _tools():
    from mdclaw._cli import _discover_tools

    return _discover_tools()


def _main(argv, capsys):
    """Run the CLI in-process: (exit code, stdout JSON or text, stderr)."""
    from mdclaw._cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    captured = capsys.readouterr()
    try:
        out = json.loads(captured.out)
    except ValueError:
        out = captured.out
    return exc_info.value.code, out, captured.err


def _chain_to_topo(job_dir):
    ids = {}
    parent = None
    for node_type, artifacts in (("source", {"source_bundle": "artifacts/sb.json"}),
                                 ("prep", {"merged_pdb": "artifacts/m.pdb"}),
                                 ("solv", {"solvated_pdb": "artifacts/s.pdb"}),
                                 ("topo", TRIPLE)):
        created = create_node(str(job_dir), node_type, parent_node_ids=[parent] if parent else None)
        assert created["success"], created
        complete_node(str(job_dir), created["node_id"], artifacts=artifacts)
        ids[node_type] = parent = created["node_id"]
    return ids


def _commands(step: dict) -> list[str]:
    return [str(v) for k, v in step.items() if k.endswith("_command") or k == "node_id_capture"]


def _set_queued(job_dir, node_id, slurm_job_id):
    from mdclaw._node import update_workflow_state

    assert update_workflow_state(str(job_dir), node_id=node_id, status="queued")["success"]
    path = Path(job_dir) / "nodes" / node_id / "node.json"
    data = json.loads(path.read_text())
    data.setdefault("metadata", {})["slurm_job_id"] = slurm_job_id
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# 1. ``next`` commands run as written
# ---------------------------------------------------------------------------

class TestExecutableNextCommands:
    def test_no_placeholders_in_run_create_or_batch_commands(self, job_dir):
        tools = _tools()
        ids = _chain_to_topo(job_dir)
        create = next_step(str(job_dir), ids["topo"], tools)
        assert create["action"] == "create" and create["node_type"] == "min"
        pending = create_node(str(job_dir), "min")["node_id"]
        run = next_step(str(job_dir), pending, tools)
        for step in (create, run):
            for command in _commands(step):
                assert "..." not in command and "[--" not in command and "<new>" not in command, command
        assert run["run_command"].endswith(f"--node-id {pending} run_minimization")
        assert run["batch_command"].endswith("--gpus 1")
        assert f"--script 'mdclaw --job-dir {job_dir} --node-id {pending} run_minimization'" in run["batch_command"]
        # a node still to be created is $NODE_ID, which node_id_capture sets
        assert create["node_id_capture"].startswith("NODE_ID=$(mdclaw --output id create_node ")
        assert '--node-id "$NODE_ID" run_minimization' in create["run_command"]
        assert '--script "mdclaw --job-dir' in create["batch_command"]
        assert "--node-id $NODE_ID run_minimization" in create["batch_command"]
        assert create["options_from"] == "mdclaw --list-json run_minimization"

    def test_required_arguments_are_listed_not_written_as_placeholders(self, job_dir):
        step = {"action": "create"}
        from mdclaw._envelope import NEW_NODE, _command_fields

        step = _command_fields(str(job_dir), NEW_NODE, "extract_tripeptide", _tools())
        assert step["required_args"] == ["--mutation"]
        assert "<" not in step["run_command"]

    def test_queued_node_chains_the_next_stage_behind_its_slurm_job(self, job_dir):
        tools = _tools()
        ids = _chain_to_topo(job_dir)
        minimize = create_node(str(job_dir), "min")["node_id"]
        _set_queued(job_dir, minimize, "4242")
        step = next_step(str(job_dir), minimize, tools)
        assert step["action"] == "wait" and step["slurm_job_id"] == "4242"
        assert step["chain_command"].startswith("NODE_ID=$(mdclaw --output id create_node ")
        assert f"--node-type eq --parent-node-ids {minimize}" in step["chain_command"]
        assert step["chain_command"].endswith("--gpus 1 --dependency afterok:4242")
        # the pending eq created ahead is submitted behind the same job
        eq = create_node(str(job_dir), "eq")["node_id"]
        for node in (minimize, eq):
            step = next_step(str(job_dir), node, tools)
            assert step["chain_command"] == (
                f"mdclaw submit_job --job-dir {job_dir} --node-id {eq} --script "
                f"'mdclaw --job-dir {job_dir} --node-id {eq} run_equilibration' --gpus 1 "
                "--dependency afterok:4242"), step
        assert ids


# ---------------------------------------------------------------------------
# 2. the envelope survives ``| tail``; 4. ``--output id`` failures say why
# ---------------------------------------------------------------------------

class TestEnvelopeTail:
    def test_status_line_is_the_last_key(self, job_dir, capsys):
        code, out, _ = _main(["create_node", "--job-dir", str(job_dir), "--node-type", "source"], capsys)
        assert code == 0
        assert list(out)[-1] == STATUS_LINE_KEY
        assert out[STATUS_LINE_KEY].startswith("success=true node=source_001 status=pending next=")
        code, out, _ = _main(["inspect_job", "--job-dir", str(job_dir), "--bogus", "1"], capsys)
        assert list(out)[-1] == STATUS_LINE_KEY
        assert out[STATUS_LINE_KEY].startswith("success=false code=unknown_parameter")

    def test_status_line_prefers_the_fix_on_failure(self):
        line = status_line({"success": False, "code": "x", "next_action": "mdclaw do_it",
                            "next": {"action": "run", "run_command": "mdclaw other"}})
        assert line == "success=false code=x next=mdclaw do_it"

    def test_id_mode_failure_prints_nothing_on_stdout_and_the_reason_on_stderr(self, job_dir, capsys):
        code, out, err = _main(["--output", "id", "create_node", "--job-dir", str(job_dir),
                                "--node-type", "membrane_stage"], capsys)
        assert code != 0
        assert out == ""
        line = next(row for row in err.splitlines() if row.startswith("mdclaw error: "))
        brief = json.loads(line[len("mdclaw error: "):])
        assert brief["success"] is False and brief["code"] == "invalid_node_type"

    def test_repeated_python_warnings_are_summarised(self, monkeypatch, capsys):
        from mdclaw import _cli

        def noisy() -> dict:
            for i in range(40):
                warnings.warn(f"two consecutive residues with same number ({i})", UserWarning)
            return {"success": True}

        monkeypatch.setattr(_cli, "_discover_tools", lambda: {
            "noisy": {"fn": noisy, "is_async": False, "server": "fake", "description": "Noisy.",
                      "requires_node": False}})
        code, out, err = _main(["noisy"], capsys)
        assert code == 0 and out["success"] is True
        assert err.count("UserWarning: two consecutive residues") == 3
        assert "37 more UserWarning warnings from" in err


# ---------------------------------------------------------------------------
# 3. guessed tool names
# ---------------------------------------------------------------------------

class TestToolNames:
    def test_spelling_variants_and_server_prefixes_name_the_tool(self):
        from mdclaw._cli_names import canonical_tool_name

        tools = _tools()
        for name in ("slurm_submit_job", "slurm.submit_job", "slurm:submit_job", "Submit-Job"):
            assert canonical_tool_name(name, tools) == "submit_job", name
        assert canonical_tool_name("build_amber-system", tools) == "build_amber_system"
        assert canonical_tool_name("fetch_pdb", tools) is None

    def test_a_server_word_before_the_tool_is_dropped(self, tmp_path, capsys):
        code, out, _ = _main(["study", "init_study", "--study-dir", str(tmp_path / "s")], capsys)
        assert code == 0 and out["success"] is True
        assert any("read as 'init_study'" in w for w in out["warnings"])

    def test_list_json_reads_a_prefixed_name(self, capsys):
        code, out, _ = _main(["--list-json", "slurm.submit_job"], capsys)
        assert code == 0 and out["tools"][0]["name"] == "submit_job"

    def test_a_guessed_verb_gets_the_corrected_command(self, capsys):
        code, out, _ = _main(["fetch_pdb", "--pdb-id", "1abc"], capsys)
        assert code == 1 and out["code"] == "tool_not_available"
        assert out["next_action"] == "mdclaw fetch_structure --pdb-id 1abc"
        assert out["suggestions"][0] == "fetch_structure"
        code, out, _ = _main(["--job-dir", "/j", "--node-id", "prep_002", "remove_node"], capsys)
        assert out["next_action"] == "mdclaw --job-dir /j --node-id prep_002 update_workflow_state --abandon"

    def test_suggestions_are_never_empty_for_a_plausible_name(self, capsys):
        code, out, _ = _main(["inspect_structure"], capsys)
        assert out["suggestions"][0] == "inspect_molecules"
        for name in ("inspect_structure_file", "show_chains", "get_candidates"):
            code, out, _ = _main([name], capsys)
            assert out["suggestions"], name

    def test_a_node_id_in_the_tool_position(self, job_dir, capsys):
        code, out, _ = _main(["--job-dir", str(job_dir), "source_001", "inspect_molecules"], capsys)
        assert out["code"] == "tool_not_available" and "is a node id" in out["message"]
        assert out["next_action"] == f"mdclaw --job-dir {job_dir} --node-id source_001 inspect_molecules"
        # the same id twice (a capture that matched two lines)
        code, out, _ = _main(["--job-dir", str(job_dir), "--node-id", "min_003", "min_003",
                              "run_minimization"], capsys)
        assert out["next_action"] == f"mdclaw --job-dir {job_dir} --node-id min_003 run_minimization"

    def test_tool_options_before_the_tool_are_moved_after_it(self, job_dir, capsys):
        code, out, _ = _main(["--job-dir", str(job_dir), "--node-type", "source", "create_node"], capsys)
        assert code == 0 and out["node_id"] == "source_001"
        assert any("moved after 'create_node'" in w for w in out["warnings"])

    def test_a_tool_option_without_any_tool_names_the_tool(self, job_dir, capsys):
        code, out, _ = _main(["--job-dir", str(job_dir), "--node-type", "prep"], capsys)
        assert out["code"] == "tool_not_available"
        assert out["next_action"] == f"mdclaw --job-dir {job_dir} create_node --node-type prep"


# ---------------------------------------------------------------------------
# 3b. an empty ``--node-id`` variable
# ---------------------------------------------------------------------------

class TestNodeIdValueMissing:
    def test_the_tool_name_after_an_empty_node_id(self, job_dir, capsys):
        _chain_to_topo(job_dir)
        pending = create_node(str(job_dir), "min")["node_id"]
        code, out, _ = _main(["--job-dir", str(job_dir), "--node-id", "run_minimization",
                              "--platform", "CPU"], capsys)
        assert code == 2 and out["code"] == "node_id_value_missing"
        assert out["next_action"] == (f"mdclaw --job-dir {job_dir} --node-id {pending} "
                                      "run_minimization --platform CPU")

    @pytest.mark.parametrize("argv_tail", [["--node-id", "", "inspect_job"], ["inspect_job", "--node-id"],
                                           ["--node-id", "--platform", "CPU", "run_minimization"]])
    def test_other_shapes_of_an_empty_value(self, job_dir, capsys, argv_tail):
        code, out, _ = _main(["--job-dir", str(job_dir), *argv_tail], capsys)
        assert code == 2 and out["code"] == "node_id_value_missing", out

    def test_a_stage_tool_without_node_id_names_the_pending_node(self, job_dir, capsys):
        _chain_to_topo(job_dir)
        pending = create_node(str(job_dir), "min")["node_id"]
        code, out, _ = _main(["--job-dir", str(job_dir), "run_minimization", "--platform", "CPU"], capsys)
        assert code == 1 and out["code"] == "node_context_required"
        assert out["candidate_node_ids"] == [pending]
        assert out["next_action"] == (f"mdclaw --node-id {pending} --job-dir {job_dir} "
                                      "run_minimization --platform CPU")

    def test_no_pending_node_gives_a_capture_command(self, job_dir, capsys):
        _chain_to_topo(job_dir)
        code, out, _ = _main(["--job-dir", str(job_dir), "--node-id", "run_minimization"], capsys)
        assert out["next_action"].startswith(
            f"NODE_ID=$(mdclaw --output id create_node --job-dir {job_dir} --node-type min) && "
            'test -n "$NODE_ID" && ')
        assert out["next_action"].endswith('--node-id "$NODE_ID" run_minimization')


# ---------------------------------------------------------------------------
# 5. argparse refusals are JSON; abbreviations only complete a unit
# ---------------------------------------------------------------------------

class TestArgumentErrors:
    def test_a_flag_without_its_value(self, job_dir, capsys):
        code, out, err = _main(["create_node", "--job-dir", str(job_dir), "--node-type"], capsys)
        assert code == 2 and out["code"] == "invalid_parameter_value"
        assert "--node-type needs a value" in out["message"]
        assert "usage:" not in err

    def test_an_unknown_flag_is_named_never_substituted(self, job_dir, capsys):
        code, out, _ = _main(["inspect_job", "--job-dir", str(job_dir), "--include-progres"], capsys)
        assert code == 2 and out["code"] == "unknown_parameter"
        assert out["next_action"] == "mdclaw --list-json inspect_job"

    def test_a_pasted_placeholder_is_removed_in_next_action(self, job_dir, capsys):
        code, out, _ = _main(["inspect_job", "--job-dir", str(job_dir), "..."], capsys)
        assert out["code"] == "unknown_parameter"
        assert out["next_action"] == f"mdclaw inspect_job --job-dir {job_dir}"

    def test_unit_and_plural_endings_are_completed_other_prefixes_refused(self, monkeypatch, capsys):
        from mdclaw import _cli

        seen = {}

        def fake(temperature_kelvin: float = 300.0, residue_ranges: list[str] = None,
                 forcefield_xml: str = None) -> dict:
            seen.update(t=temperature_kelvin, r=residue_ranges, f=forcefield_xml)
            return {"success": True}

        monkeypatch.setattr(_cli, "_discover_tools", lambda: {
            "fake": {"fn": fake, "is_async": False, "server": "fake", "description": "Fake.",
                     "requires_node": False}})
        code, out, _ = _main(["fake", "--temperature", "310", "--residue-range", "A:1-5"], capsys)
        assert code == 0 and seen == {"t": 310.0, "r": ["A:1-5"], "f": None}
        code, out, _ = _main(["fake", "--forcefield", "ff14SB"], capsys)
        assert code == 2 and out["code"] == "unknown_parameter"
        assert any("--forcefield-xml" in hint for hint in out["hints"])


# ---------------------------------------------------------------------------
# 7. read-only tools pick the job's source node; stage names are not ids
# ---------------------------------------------------------------------------

class TestAutoResolution:
    @pytest.fixture
    def fetched_job(self, job_dir, tmp_path):
        pytest.importorskip("gemmi")
        from mdclaw.research.fetch import fetch_structure

        pdb = tmp_path / "toy.pdb"
        pdb.write_text(TOY_PDB)
        source = create_node(str(job_dir), "source")["node_id"]
        fetched = asyncio.run(fetch_structure(source="local", file_path=str(pdb),
                                              job_dir=str(job_dir), node_id=source))
        assert fetched["success"], fetched
        return job_dir, source

    def test_inspect_molecules_with_job_dir_only(self, fetched_job):
        from mdclaw.research.inspection import inspect_molecules

        job_dir, source = fetched_job
        result = inspect_molecules(job_dir=str(job_dir))
        assert result["success"], result
        assert result["node_id"] == source
        assert any("source node" in w for w in result["warnings"])
        assert (job_dir / "nodes" / source / "artifacts" / "inspection.json").is_file()

    def test_list_source_candidates_with_job_dir_only(self, fetched_job):
        from mdclaw.research.source_node import list_source_candidates

        job_dir, source = fetched_job
        result = list_source_candidates(str(job_dir))
        assert result["success"] and result["source_node_id"] == source
        assert result["candidates"]

    def test_a_job_without_a_source_node_says_node_id_required(self, job_dir):
        from mdclaw.research.inspection import inspect_molecules
        from mdclaw.research.source_node import list_source_candidates

        result = inspect_molecules(job_dir=str(job_dir))
        assert result["success"] is False and result["code"] == "node_id_required"
        result = list_source_candidates(str(job_dir))
        assert result["success"] is False and result["code"] == "node_id_required"

    def test_a_stage_name_as_parent_id_names_the_nodes_of_that_stage(self, job_dir):
        ids = _chain_to_topo(job_dir)
        result = create_node(str(job_dir), "min", parent_node_ids=["topo"])
        assert result["success"] is False and result["code"] == "referenced_node_missing"
        assert "is a node type" in result["message"]
        assert result["candidate_parent_node_ids"] == [ids["topo"]]
        assert result["next_action"] == (f"mdclaw create_node --job-dir {job_dir} --node-type min "
                                         f"--parent-node-ids {ids['topo']}")


# ---------------------------------------------------------------------------
# 8. the study goes where it will be looked for
# ---------------------------------------------------------------------------

class TestStudyPlacement:
    def _bootstrap(self, study_dir, **kwargs):
        from mdclaw.study import bootstrap_md_workflow

        return bootstrap_md_workflow(str(study_dir), "Simulate X", create_source_node=False, **kwargs)

    def test_a_non_empty_workspace_root_is_refused(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "task_prompt.md").write_text("task")
        (workspace / ".mdclaw_cluster.json").write_text("{}")
        result = self._bootstrap(workspace, solvent_regime="membrane")
        assert result["success"] is False and result["code"] == "study_dir_not_empty"
        assert not (workspace / "study.json").exists() and not (workspace / "jobs").exists()
        assert result["next_action"] == (f"mdclaw bootstrap_md_workflow --study-dir {workspace / 'study'} "
                                         "--question 'Simulate X' --solvent-regime membrane")
        # dot files alone do not make a directory foreign; an existing study is reused
        (workspace / "task_prompt.md").unlink()
        assert self._bootstrap(workspace)["success"]
        (workspace / "notes.txt").write_text("later")
        assert self._bootstrap(workspace)["success"]

    def test_a_study_inside_a_study_is_refused(self, tmp_path):
        outer = tmp_path / "study"
        assert self._bootstrap(outer)["success"]
        result = self._bootstrap(outer / "study")
        assert result["success"] is False and result["code"] == "study_dir_nested"
        assert f"--study-dir {outer}" in result["next_action"]
        result = self._bootstrap(outer / "jobs" / "main" / "study")
        assert result["code"] == "study_dir_nested"

    def test_init_study_shares_the_checks(self, tmp_path):
        from mdclaw.study import init_study

        (tmp_path / "file.pdb").write_text("x")
        result = init_study(str(tmp_path))
        assert result["success"] is False and result["code"] == "study_dir_not_empty"

    def test_relative_path_resolved_in_a_container_home_is_flagged(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SINGULARITY_CONTAINER", "/img.sif")
        monkeypatch.chdir(home)
        result = self._bootstrap("study")
        assert result["success"] is True
        assert result["warnings"][0].startswith("study_dir_under_home:")
        assert "--containall" in result["warnings"][0]
        assert "CHECK THE LOCATION" in result["message"]

    def test_an_expected_study_dir_mismatch_is_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MDCLAW_STUDY_DIR", str(tmp_path / "expected"))
        result = self._bootstrap(tmp_path / "elsewhere")
        assert result["success"] is True
        assert any(w.startswith("study_dir_mismatch:") for w in result["warnings"])
        monkeypatch.setenv("MDCLAW_STUDY_DIR", str(tmp_path / "elsewhere"))
        assert not any(w.startswith("study_dir_mismatch:")
                       for w in self._bootstrap(tmp_path / "elsewhere")["warnings"])
