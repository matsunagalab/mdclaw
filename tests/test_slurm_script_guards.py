"""Submission-time guards on the script, the node's conditions, the Slurm
dependency and the paths a job needs (MDDataBench v5, 2026-10).

Every case here used to pass ``submit_job`` and fail inside the job seconds
after it started, when the agent had already left and ``afterok`` cancelled
the rest of the chain. Nothing reaches sbatch: ``run_command`` is mocked and
answers squeue/sacct where a test needs a job state.
"""

import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mdclaw.slurm.mps import submit_mps_job
from mdclaw.slurm.submit import submit_array_job, submit_job


def _proc(stdout=""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout, proc.stderr, proc.returncode = stdout, "", 0
    return proc


def _slurm(squeue="", sacct="", sbatch="Submitted batch job 9000\n"):
    """A run_command stand-in answering each Slurm client."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return _proc({"squeue": squeue, "sacct": sacct, "sbatch": sbatch}.get(cmd[0], ""))

    run.calls = calls
    return run


def _dag(tmp_path, *, hmr=True, implicit_solvent=None):
    """topo_001 (completed) -> min_001 -> eq_001, both pending."""
    from mdclaw._node import create_node
    from tests.pipeline_helpers import complete_node_with_placeholders as complete

    jd = tmp_path / "job"
    jd.mkdir()
    create_node(str(jd), "topo")
    complete(str(jd), "topo_001", {"system_xml": "artifacts/system.xml",
                                  "topology_pdb": "artifacts/topology.pdb",
                                  "state_xml": "artifacts/state.xml"},
             metadata={"hmr": hmr, "implicit_solvent": implicit_solvent})
    create_node(str(jd), "min", parent_node_ids=["topo_001"])
    create_node(str(jd), "eq", parent_node_ids=["min_001"])
    return jd


def _queue(jd, node_id, job_id):
    from mdclaw.slurm.node_sync import _stamp_slurm_on_node

    _stamp_slurm_on_node(str(jd), node_id, str(job_id), script_file="x.sbatch",
                         stdout_log="x.out", stderr_log="x.err", parent_job_id=str(job_id))


def _set_conditions(jd, node_id, conditions):
    path = Path(jd) / "nodes" / node_id / "node.json"
    node = json.loads(path.read_text())
    node["conditions"] = conditions
    path.write_text(json.dumps(node))


def _cmd(jd, node_id, tool, flags=""):
    return f"mdclaw --job-dir {shlex.quote(str(jd))} --node-id {node_id} {tool} {flags}".strip()


def _submit(command, jd=None, node_id=None, slurm=None, **kwargs):
    slurm = slurm or _slurm()
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command", side_effect=slurm):
        result = submit_job(command, job_dir=str(jd) if jd else None, node_id=node_id,
                            output_dir=kwargs.pop("output_dir", str(jd) if jd else None), **kwargs)
    return result, slurm.calls


def _sbatch_calls(calls):
    return [c for c in calls if c[0] == "sbatch"]


# ---------------------------------------------------------------------------
# 1. The script's arguments, parsed by the CLI's own parser
# ---------------------------------------------------------------------------


def test_underscore_flags_are_refused_with_the_corrected_command(tmp_path):
    """kimi 001_membrane_5yc8 r1: --nvt_time_ns died in the job."""
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 101)
    before = (jd / "nodes/eq_001/node.json").read_bytes()
    result, calls = _submit(_cmd(jd, "eq_001", "run_equilibration",
                                 "--nvt_time_ns 0.5 --npt_time_ns 0.7 --platform CUDA"), jd, "eq_001")
    assert result["code"] == "script_arguments_invalid"
    assert "unrecognized arguments: --nvt_time_ns 0.5 --npt_time_ns 0.7" in result["message"]
    assert "Cannot validate" not in result["message"]
    corrected = result["corrected_command"]
    assert "--nvt-time-ns 0.5 --npt-time-ns 0.7" in corrected
    assert result["next_action"] == f"Resubmit with --script {shlex.quote(corrected)}"
    assert not _sbatch_calls(calls)
    assert (jd / "nodes/eq_001/node.json").read_bytes() == before


@pytest.mark.parametrize("flags, fixed", [
    ("--duration-ns 2", "--simulation-time-ns 2"),
    ("--duration-ps 1.0", "--simulation-time-ns 0.001"),
    ("--duration 100ps", "--simulation-time-ns 0.1"),
    ("--simulation-time-ns 100ps", "--simulation-time-ns 0.1"),
    ("--simulation-time-ns 1 --gpus 1", "--simulation-time-ns 1"),
    ("--simulation-time-ns 1 --continue-from prod_001", "--simulation-time-ns 1"),
    ("--simulation-time-ns 1 --device_index 0", "--simulation-time-ns 1 --device-index 0"),
])
def test_near_misses_of_production_flags(tmp_path, flags, fixed):
    from mdclaw.slurm.script_check import check_script

    command = _cmd(tmp_path, "prod_001", "run_production", flags)
    error = check_script(command, job_dir=str(tmp_path), node_id="prod_001").error
    assert error["code"] == "script_arguments_invalid"
    assert error["corrected_command"].endswith(fixed)


def test_ambiguous_flag_gets_candidates_but_no_correction(tmp_path):
    from mdclaw.slurm.script_check import check_script

    error = check_script(_cmd(tmp_path, "eq_001", "run_equilibration", "--equilibration-time-ns 0.5"),
                         job_dir=str(tmp_path), node_id="eq_001").error
    assert "corrected_command" not in error
    assert error["suggestions"][0]["candidates"] == ["--nvt-time-ns", "--npt-time-ns"]


def test_minimization_drops_dynamics_flags_and_renames_restraint_selection(tmp_path):
    from mdclaw.slurm.script_check import check_script

    error = check_script(_cmd(tmp_path, "min_001", "run_minimization",
                              "--timestep-fs 2.0 --restraint-atom-selection backbone"),
                         job_dir=str(tmp_path), node_id="min_001").error
    assert error["corrected_command"].endswith("run_minimization --restraint-atoms backbone")


def test_unknown_tool_and_missing_node_context(tmp_path):
    from mdclaw.slurm.script_check import check_script

    error = check_script(f"mdclaw slurm run_production --job-dir {tmp_path} --node-id prod_001",
                         job_dir=str(tmp_path), node_id="prod_001").error
    assert "Unknown MDClaw tool 'slurm'" in error["message"]
    assert error["corrected_command"] == f"mdclaw run_production --job-dir {tmp_path} --node-id prod_001"
    error = check_script("mdclaw run_minimization --platform CUDA",
                         job_dir=str(tmp_path), node_id="min_001").error
    assert "workflow tool" in error["message"]
    assert error["corrected_command"] == (f"mdclaw --job-dir {tmp_path} --node-id min_001 "
                                          "run_minimization --platform CUDA")
    # without a node to name, the refusal stands without a guess
    error = check_script("mdclaw run_minimization").error
    assert error["code"] == "script_arguments_invalid" and "corrected_command" not in error


def test_script_job_dir_typo_is_replaced_by_the_submitted_job(tmp_path):
    """qwen 068_soluble_1ail r1: --job-dir /data1/rkp00079/rkp00079/... in
    the script, the right job dir given to submit_job."""
    from mdclaw.slurm.script_check import check_script

    jd = _dag(tmp_path)
    typo = str(jd).replace("/job", "/jobb")
    error = check_script(_cmd(typo, "min_001", "run_minimization"), job_dir=str(jd), node_id="min_001").error
    assert "but this submission is for" in error["message"]
    assert error["corrected_command"] == _cmd(jd, "min_001", "run_minimization").replace(
        shlex.quote(str(jd)), str(jd))


def test_script_file_lines_are_checked_too(tmp_path):
    jd = _dag(tmp_path)
    script = tmp_path / "eq.sh"
    script.write_text("#!/bin/bash\nset -e\n"
                      + _cmd(jd, "min_001", "run_minimization", "--max_iterations 100") + "\n")
    result, calls = _submit(str(script), jd, "min_001")
    assert result["code"] == "script_arguments_invalid"
    assert str(script) in result["next_action"] and "--max-iterations 100" in result["next_action"]
    assert not _sbatch_calls(calls)


def test_array_and_mps_tasks_get_the_same_refusal(tmp_path):
    jd = _dag(tmp_path)
    task = {"job_dir": str(jd), "node_id": "min_001",
            "command": _cmd(jd, "min_001", "run_minimization", "--platform CUDA --gpus 1")}
    slurm = _slurm()
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command", side_effect=slurm):
        array = submit_array_job([task], output_dir=str(jd))
        mps = submit_mps_job([task], output_dir=str(jd))
    for result in (array, mps):
        assert result["code"] == "script_arguments_invalid"
        assert result["message"].startswith("tasks[0]: ")
        assert "--gpus" not in result["corrected_command"]
    assert not _sbatch_calls(slurm.calls)


# ---------------------------------------------------------------------------
# 2. Placeholders left from an example command
# ---------------------------------------------------------------------------


def test_ellipsis_from_next_run_command_is_refused(tmp_path):
    """qwen 008/048/054: 'run_minimization ...' pasted from next.run_command."""
    jd = _dag(tmp_path)
    result, calls = _submit(_cmd(jd, "min_001", "run_minimization", "..."), jd, "min_001")
    assert result["code"] == "script_placeholder_unfilled"
    assert result["corrected_command"] == f"mdclaw --job-dir {jd} --node-id min_001 run_minimization"
    assert not _sbatch_calls(calls)


def test_angle_placeholders_are_filled_from_the_submission(tmp_path):
    jd = _dag(tmp_path)
    result, _ = _submit("mdclaw --job-dir <job_dir> --node-id <node_id> run_minimization", jd, "min_001")
    assert result["code"] == "script_placeholder_unfilled"
    assert result["corrected_command"] == f"mdclaw --job-dir {jd} --node-id min_001 run_minimization"


def test_placeholder_dependency_is_refused(tmp_path):
    result, calls = _submit("echo hi", dependency="afterok:<job>", output_dir=str(tmp_path))
    assert result["code"] == "script_placeholder_unfilled"
    assert "<job>" in result["message"]
    assert not calls


# ---------------------------------------------------------------------------
# 3. Declared conditions of min / eq / prod nodes
# ---------------------------------------------------------------------------


def test_eq_temperature_declared_but_omitted_is_refused_with_the_flag_added(tmp_path):
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 101)
    _set_conditions(jd, "eq_001", {"temperature_kelvin": 310})
    result, calls = _submit(_cmd(jd, "eq_001", "run_equilibration", "--platform CUDA"), jd, "eq_001")
    assert result["code"] == "node_execution_context_invalid"
    assert result["blocking_codes"] == ["condition_mismatch"]
    assert result["corrected_command"].endswith("--platform CUDA --temperature-kelvin 310")
    assert not _sbatch_calls(calls)


@pytest.mark.parametrize("hmr, flags, effective", [
    (True, "", 1.0),                      # 250000 default steps at 4 fs
    (False, "--nvt-steps 25000", 0.05),   # 2 fs without HMR
])
def test_eq_stage_length_is_predicted_from_the_topology(tmp_path, hmr, flags, effective):
    from mdclaw.slurm.preflight import submission_preflight

    jd = _dag(tmp_path, hmr=hmr)
    _queue(jd, "min_001", 101)
    _set_conditions(jd, "eq_001", {"nvt_time_ns": 0.1})
    with patch("mdclaw.slurm._base.check_external_tool", return_value=False):
        report = submission_preflight(_cmd(jd, "eq_001", "run_equilibration", flags), str(jd), "eq_001")
    error = report["error"]
    assert f"declared 0.1, actual {effective}" in error["errors"][0]
    assert error["corrected_command"].endswith("--nvt-time-ns 0.1")
    assert "--nvt-steps" not in error["corrected_command"]


def test_matching_eq_and_min_commands_pass(tmp_path):
    from mdclaw.slurm.preflight import submission_preflight

    jd = _dag(tmp_path)
    _set_conditions(jd, "min_001", {"max_iterations": 5000, "platform": "CUDA"})
    _set_conditions(jd, "eq_001", {"temperature_kelvin": 300, "pressure_bar": 1.0, "nvt_time_ns": 0.1,
                                   "ensemble": "NPT", "is_membrane": False})
    _queue(jd, "min_001", 101)
    with patch("mdclaw.slurm._base.check_external_tool", return_value=False):
        mn = submission_preflight(_cmd(jd, "min_001", "run_minimization", "--platform CUDA"), str(jd), "min_001")
        eq = submission_preflight(_cmd(jd, "eq_001", "run_equilibration", "--nvt-time-ns 0.1"),
                                  str(jd), "eq_001", dependency="afterok:101")
    assert mn["error"] is None and mn["condition_preflight"]["checked_conditions"] == ["max_iterations", "platform"]
    assert eq["error"] is None
    assert eq["condition_preflight"]["checked_conditions"] == ["ensemble", "nvt_time_ns", "pressure_bar",
                                                               "temperature_kelvin"]
    assert eq["condition_preflight"]["deferred_conditions"] == ["is_membrane"]


def test_min_condition_mismatch(tmp_path):
    jd = _dag(tmp_path)
    _set_conditions(jd, "min_001", {"max_iterations": 5000})
    result, _ = _submit(_cmd(jd, "min_001", "run_minimization", "--max-iterations 1000"), jd, "min_001")
    assert result["blocking_codes"] == ["condition_mismatch"]
    assert result["corrected_command"].endswith("run_minimization --max-iterations 5000")


def test_condition_key_the_tool_never_reports_is_refused_with_the_reported_key(tmp_path):
    """total_time_ps passed preflight as deferred and always ended the job
    with condition_missing."""
    from mdclaw._node import create_node

    jd = _dag(tmp_path)
    create_node(str(jd), "prod", parent_node_ids=["eq_001"], conditions={"total_time_ps": 100000})
    _queue(jd, "min_001", 101)
    _queue(jd, "eq_001", 102)
    result, calls = _submit(_cmd(jd, "prod_001", "run_production", "--simulation-time-ns 100"), jd, "prod_001")
    assert result["code"] == "node_execution_context_invalid"
    assert "condition_missing" in result["blocking_codes"]
    assert result["condition_preflight"]["unreported_conditions"] == ["total_time_ps"]
    assert "--abandon" in result["next_action"]
    assert '{"simulation_time_ns": 100.0}' in result["next_action"]
    assert "--parent-node-ids eq_001" in result["next_action"]
    assert not _sbatch_calls(calls)


def test_reported_key_unit_conversion():
    from mdclaw.slurm.condition_check import reported_key_for

    prod = {"simulation_time_ns", "output_frequency_ps", "timestep_fs", "temperature_kelvin"}
    assert reported_key_for("production_time_ps", 5000, prod) == ("simulation_time_ns", 5.0)
    assert reported_key_for("simulation_time_us", 1, prod) == ("simulation_time_ns", 1000.0)
    assert reported_key_for("save_interval_ns", 0.01, prod) == ("output_frequency_ps", 10.0)
    assert reported_key_for("temperature_c", 37, prod) == ("temperature_kelvin", 310.15)
    eq = {"nvt_time_ns", "npt_time_ns"}
    assert reported_key_for("nvt_time_ps", 100, eq) == ("nvt_time_ns", 0.1)
    assert reported_key_for("equilibration_time_ns", 1, eq) is None


def test_reported_vocabulary_is_read_from_the_tool_source():
    from mdclaw.simulation.equilibrate import run_equilibration
    from mdclaw.simulation.minimize import run_minimization
    from mdclaw.simulation.production import run_production
    from mdclaw.slurm.condition_check import reported_condition_keys

    assert {"simulation_time_ns", "pressure_bar", "random_seed"} <= reported_condition_keys(run_production)
    assert {"nvt_time_ns", "ensemble", "timestep_fs"} <= reported_condition_keys(run_equilibration)
    assert reported_condition_keys(run_minimization) == {
        "max_iterations", "restraint_atoms", "restraint_force_constant", "is_membrane",
        "implicit_solvent", "platform", "device_index", "hmr"}


def test_a_unit_changed_key_is_not_read_as_the_reported_one(tmp_path):
    """simulation_time_ps would be fuzzy-read as simulation_time_ns and its
    value compared as nanoseconds; it is refused with the value converted,
    never 'corrected' to --simulation-time-ns 100000."""
    from mdclaw._node import create_node

    jd = _dag(tmp_path)
    create_node(str(jd), "prod", parent_node_ids=["eq_001"], conditions={"simulation_time_ps": 100000})
    _queue(jd, "min_001", 1)
    _queue(jd, "eq_001", 2)
    result, _ = _submit(_cmd(jd, "prod_001", "run_production", "--simulation-time-ns 100"), jd, "prod_001")
    assert result["condition_preflight"]["unreported_conditions"] == ["simulation_time_ps"]
    assert "corrected_command" not in result
    assert '{"simulation_time_ns": 100.0}' in result["next_action"]


def test_declared_nvt_ensemble_is_met_with_zero_pressure(tmp_path):
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 101)
    _set_conditions(jd, "eq_001", {"ensemble": "NVT"})
    result, _ = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001")
    assert result["blocking_codes"] == ["condition_mismatch"]
    assert result["corrected_command"].endswith("run_equilibration --pressure-bar 0")


# ---------------------------------------------------------------------------
# 4. The Slurm dependency on the parents' jobs
# ---------------------------------------------------------------------------


def test_missing_afterok_on_a_queued_parent_is_added(tmp_path):
    """glm 026_complex_1ava r1: eq submitted without afterok on min's job."""
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 203943)
    result, calls = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001",
                            slurm=_slurm(squeue="203943|RUNNING|203943\n"))
    assert result["success"] is True, result
    assert result["dependency"] == "afterok:203943"
    assert result["dependency_added"] == [{"node_id": "min_001", "slurm_job_id": "203943"}]
    assert any("added afterok:203943" in w for w in result["warnings"])
    script = Path(result["script_file"]).read_text()
    assert "#SBATCH --dependency=afterok:203943" in script


def test_existing_dependency_is_widened_not_replaced(tmp_path):
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 500)
    result, _ = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001", dependency="afterany:77")
    assert result["dependency"] == "afterany:77,afterok:500"


def test_parent_running_outside_slurm_is_refused(tmp_path):
    from mdclaw._node import update_node_status

    jd = _dag(tmp_path)
    update_node_status(str(jd), "min_001", "running")
    result, calls = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001")
    assert result["code"] == "parent_not_submitted"
    assert "wait_node" in result["next_action"]
    assert not _sbatch_calls(calls)


def test_dependency_on_a_cancelled_parent_job_is_unsatisfiable(tmp_path):
    """kimi 023_antibody_3wd5 r3 cancelled min's job, then chained eq on it."""
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 231825)
    result, calls = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001",
                            dependency="afterok:231825",
                            slurm=_slurm(sacct="231825|CANCELLED by 100160\n"))
    assert result["code"] == "dependency_unsatisfiable"
    assert "ended CANCELLED" in result["message"]
    assert "--clear-slurm-metadata" in result["next_action"] and "--node-id min_001" in result["next_action"]
    assert not _sbatch_calls(calls)


def test_dependency_on_a_non_ancestor_job_is_refused(tmp_path):
    """qwen 005_membrane_6a93 r1: eq_001 chained on min_002's job."""
    from mdclaw._node import create_node

    jd = _dag(tmp_path)
    create_node(str(jd), "min", parent_node_ids=["topo_001"])  # min_002
    _queue(jd, "min_001", 600)
    _queue(jd, "min_002", 601)
    result, _ = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001", dependency="afterok:601")
    assert result["code"] == "dependency_not_parent_job"
    assert "min_002" in result["message"]
    assert result["next_action"] == "Resubmit with --dependency afterok:600"
    # the rest of the caller's spec is kept
    result, _ = _submit(_cmd(jd, "eq_001", "run_equilibration"), jd, "eq_001",
                        dependency="afterany:55,afterok:601")
    assert result["next_action"] == "Resubmit with --dependency afterany:55,afterok:600"


def test_siblings_may_be_chained_to_serialise_them(tmp_path):
    """The MDDataBench harness chains replicas prod_002 afterok prod_001."""
    from mdclaw._node import create_node

    jd = _dag(tmp_path)
    for _ in range(2):
        create_node(str(jd), "prod", parent_node_ids=["eq_001"])
    _queue(jd, "min_001", 700)
    _queue(jd, "eq_001", 701)
    _queue(jd, "prod_001", 702)
    result, _ = _submit(_cmd(jd, "prod_002", "run_production"), jd, "prod_002", dependency="afterok:702")
    assert result["success"] is True, result
    assert result["dependency"] == "afterok:702,afterok:701"


def test_unlinked_submission_on_a_failed_job_is_unsatisfiable(tmp_path):
    result, calls = _submit("echo hi", dependency="afterok:42", output_dir=str(tmp_path),
                            slurm=_slurm(sacct="42|FAILED\n"))
    assert result["code"] == "dependency_unsatisfiable"
    assert not _sbatch_calls(calls)


def test_dependency_parsing():
    from mdclaw.slurm.dependency import parse_dependency

    assert parse_dependency("afterok:1:2,afterany:3") == ([("afterok", ["1", "2"]), ("afterany", ["3"])], ",")
    assert parse_dependency("after:5+10?afterok:6") == ([("after", ["5"]), ("afterok", ["6"])], "?")
    assert parse_dependency("singleton") == ([("singleton", [])], ",")
    assert parse_dependency("afterok:abc") is None


# ---------------------------------------------------------------------------
# 5. Paths a compute node cannot see
# ---------------------------------------------------------------------------


def test_output_dir_under_tmp_is_refused(tmp_path, monkeypatch):
    """qwen 040_ligand_3n2u r3: --output-dir /tmp/mdclaw_min_001."""
    monkeypatch.delenv("MDCLAW_ALLOW_LOCAL_SUBMIT_PATHS")
    assert not str(Path.cwd()).startswith("/tmp")  # the checkout is the working directory
    result, calls = _submit("echo hi", output_dir="/tmp/mdclaw_min_001")
    assert result["code"] == "submission_path_not_shared"
    assert "/tmp" in result["message"] and "--output-dir" in result["next_action"]
    assert not calls
    assert not Path("/tmp/mdclaw_min_001").exists()


def test_typo_path_on_a_memory_filesystem_is_refused(tmp_path, monkeypatch):
    """The MDDataBench sandbox root is a tmpfs, so /data1/rku00161 (for
    /data1/rkp00079/rku00161) could be created there and looked real."""
    from mdclaw.slurm import paths

    monkeypatch.delenv("MDCLAW_ALLOW_LOCAL_SUBMIT_PATHS")
    table = [("/", "tmpfs"), ("/home", "lustre")]
    assert paths.unshared_reason("/home/someone/study", table=table) is None
    reason = paths.unshared_reason("/data1/rku00161/study", table=table)
    assert reason and "tmpfs" in reason and "mounted at /" in reason
    assert paths._mount_of("/data1/x", [("/", "xfs"), ("/data1", "lustre")]) == ("/data1", "lustre")


def test_nonexistent_job_dir_in_the_script_is_refused(tmp_path):
    result, calls = _submit(f"mdclaw --job-dir {tmp_path}/nope --node-id min_001 run_minimization",
                            output_dir=str(tmp_path))
    assert result["code"] == "submission_path_not_shared"
    assert "does not exist" in result["message"]
    assert not _sbatch_calls(calls)


# ---------------------------------------------------------------------------
# 6. Container binds: quoted paths, missing sources, --nv
# ---------------------------------------------------------------------------


def _container(tmp_path, monkeypatch, **container):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SINGULARITY_CONTAINER", raising=False)
    monkeypatch.delenv("APPTAINER_CONTAINER", raising=False)
    (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({
        "partitions": [{"name": "gpu", "gpus_per_node": 4}],
        "container": {"image": "/opt/mdclaw.sif", "extra_flags": "--nv", "runtime": "singularity",
                      **container},
    }))


def test_quoted_job_dir_is_bound_without_its_quotes(tmp_path, monkeypatch):
    """qwen 070/086: --job-dir '/data1/...' became the bind <cwd>/'/data1/...'."""
    from mdclaw.slurm.config import _extract_bind_paths

    spaced = tmp_path / "with space"
    spaced.mkdir()
    assert _extract_bind_paths(f"mdclaw --job-dir '{tmp_path}' --node-id x run_minimization") == [str(tmp_path)]
    assert _extract_bind_paths(f'mdclaw --output-dir="{spaced}" x') == [str(spaced)]
    assert _extract_bind_paths("mdclaw --job-dir $JD --node-id x run_minimization") == []

    jd = _dag(tmp_path)
    _container(tmp_path, monkeypatch)
    result, _ = _submit(f"mdclaw --job-dir '{jd}' --node-id min_001 run_minimization --platform CUDA",
                        jd, "min_001")
    assert result["success"] is True, result
    line = next(x for x in Path(result["script_file"]).read_text().splitlines() if x.startswith("singularity exec"))
    assert f"--bind {tmp_path}," in line or f",{jd}" in line
    assert "'" + str(jd) not in line.split(" /opt/mdclaw.sif")[0]


def test_missing_configured_bind_is_refused(tmp_path, monkeypatch):
    """qwen 040_ligand_3n2u r2: configured bind /data2 did not exist."""
    kept = tmp_path / "kept"
    kept.mkdir()
    _container(tmp_path, monkeypatch, bind_paths=[str(kept), "/data2-does-not-exist"])
    result, calls = _submit("echo hi", output_dir=str(tmp_path), gpus=1)
    assert result["code"] == "container_bind_missing"
    assert result["next_action"] == f"mdclaw configure_container --bind-paths {kept}"
    assert not _sbatch_calls(calls)


def test_missing_output_dir_argument_is_created_for_the_bind(tmp_path, monkeypatch):
    """qwen 064_soluble_16pk r2: --output-dir <new dir> made the bind fail."""
    _container(tmp_path, monkeypatch)
    target = tmp_path / "exports"
    result, _ = _submit(f"mdclaw clean_ligand --ligand-pdb {tmp_path}/x.pdb --ligand-id LIG --output-dir {target}",
                        output_dir=str(tmp_path))
    assert result["success"] is True, result
    assert target.is_dir()
    assert any("so the container can bind it" in w for w in result["warnings"])


def test_gpu_job_gets_nv_when_the_container_flags_lack_it(tmp_path, monkeypatch):
    """qwen 092_soluble_1ah9 r2: configure_container --extra-flags "" and the
    job saw no CUDA platform."""
    _container(tmp_path, monkeypatch, extra_flags="")
    result, _ = _submit("echo gpu", output_dir=str(tmp_path), gpus=1)
    assert result["success"] is True, result
    assert "singularity exec --nv" in Path(result["script_file"]).read_text()
    assert any(w.startswith("container_gpu_flag_added:") for w in result["warnings"])
    cpu, _ = _submit("echo cpu", output_dir=str(tmp_path), gpus=0, job_name="cpu")
    assert "--nv" not in Path(cpu["script_file"]).read_text()
    assert not any("container_gpu_flag_added" in w for w in cpu["warnings"])


def test_mps_job_gets_nv_too(tmp_path, monkeypatch):
    jd = _dag(tmp_path)
    _container(tmp_path, monkeypatch, extra_flags="")
    task = {"job_dir": str(jd), "node_id": "min_001",
            "command": _cmd(jd, "min_001", "run_minimization", "--platform CUDA")}
    slurm = _slurm()
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command", side_effect=slurm):
        result = submit_mps_job([task], output_dir=str(jd))
    assert result["success"] is True, result
    assert "exec --nv" in Path(result["script_file"]).read_text()


# ---------------------------------------------------------------------------
# 7. next_action and small UX fixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("squeue, sacct, expected", [
    ("", "9100|FAILED\n", "--clear-slurm-metadata"),
    ("9100|RUNNING|9100\n", "", "do not submit it again"),
    ("", "", "if it is gone"),
])
def test_already_submitted_next_action_follows_the_job_state(tmp_path, squeue, sacct, expected):
    jd = _dag(tmp_path)
    _queue(jd, "min_001", 9100)
    result, calls = _submit(_cmd(jd, "min_001", "run_minimization"), jd, "min_001",
                            slurm=_slurm(squeue=squeue, sacct=sacct))
    assert result["code"] == "slurm_node_already_submitted"
    assert expected in result["next_action"]
    assert str(jd) in result["next_action"] and "<job_dir>" not in result["next_action"]
    assert not _sbatch_calls(calls)


def test_partition_star_from_sinfo_is_dropped(tmp_path):
    result, _ = _submit("echo hi", output_dir=str(tmp_path), partition="gpu*")
    assert result["success"] is True, result
    assert "#SBATCH --partition=gpu\n" in Path(result["script_file"]).read_text()
    assert any("sinfo's mark of the default partition" in w for w in result["warnings"])


@pytest.mark.parametrize("label", ["B200", "NVIDIA B200", "GB200", "nvidia_gb200", "Blackwell"])
def test_blackwell_aliases_map_to_gb200(label):
    from mdclaw.throughput.estimate import estimate_md_throughput

    out = estimate_md_throughput(atom_count=30000, gpu_type=label)
    assert out["success"] is True and out["gpu_type_normalized"] == "gb200"
