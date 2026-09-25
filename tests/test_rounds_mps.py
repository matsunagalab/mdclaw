"""``run_rounds --executor mps``: segments as ``submit_mps_job`` tasks, the
driver on the host.

Slurm is replaced by two seams. The fake submit stamps the nodes queued the
way ``submit_mps_job`` does; the fake ``check_job`` runs each task's command
through the real CLI in a subprocess (Reference platform in place of CUDA)
and then reflects COMPLETED onto the nodes with the real sync, so the
generated commands and the zombie handling are exercised end to end.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw._node import read_node
from mdclaw.rounds import driver as driver_module
from mdclaw.rounds import scheme as scheme_module
from mdclaw.rounds.driver import _stage_args_to_flags, run_rounds
from mdclaw.rounds.plan import first_round_plan
from mdclaw.rounds.scheme import read_scheme, segment_node_id, setup_rounds
from mdclaw.slurm.node_sync import _stamp_slurm_on_node, _sync_slurm_state_to_node
from tests.test_rounds import STAGE_ARGS, _job_with_eq, _scheme, _split_policy, periodic_triple  # noqa: F401


class FakeSlurm:
    """submit_mps_job / check_job stand-ins sharing one job table."""

    def __init__(self, log_dir, *, drop=None, refuse=None, vanish_first=False):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, list[dict]] = {}
        self.submissions: list[dict] = []
        self.checks: list[str] = []
        self.next_id = 700
        self.drop = drop or (lambda node_id: False)      # the task process never runs
        self.refuse = refuse                              # a result dict to return instead
        self.vanish_first = vanish_first                  # first check: no scheduler record
        self.script: list[dict] = []                      # observations served before the tasks run
        self.cancelled: set[str] = set()
        self.actions: list[tuple[str, str]] = []
        self.truncate_batches_once = False                # first job: batch tasks stop after one segment

    def action(self, kind, job_id):
        self.actions.append((kind, job_id))
        if kind == "cancel":
            self.cancelled.add(job_id)

    def submit(self, *, tasks, job_name, gpus, time_limit, output_dir, extra_sbatch=None):
        if self.refuse is not None:
            return self.refuse
        assert extra_sbatch == "--no-requeue", extra_sbatch
        job_id = str(self.next_id)
        self.next_id += 1
        for slot, task in enumerate(tasks):
            assert read_node(task["job_dir"], task["node_id"])["status"] == "pending"
            err = _stamp_slurm_on_node(
                task["job_dir"], task["node_id"], job_id,
                script_file=str(self.log_dir / f"{job_name}.sbatch"),
                stdout_log=str(self.log_dir / f"{job_name}_{job_id}.task{slot}.out"),
                stderr_log=str(self.log_dir / f"{job_name}_{job_id}.task{slot}.err"),
                parent_job_id=job_id, mps_slot=slot,
            )
            assert err is None, err
        self.jobs[job_id] = list(tasks)
        self.submissions.append({"job_id": job_id, "job_name": job_name, "gpus": gpus,
                                 "time_limit": time_limit, "output_dir": output_dir,
                                 "commands": [t["command"] for t in tasks]})
        return {"success": True, "slurm_job_id": job_id, "job_name": job_name,
                "tasks": [{"mps_slot": i, "slurm_job_id": job_id, **t} for i, t in enumerate(tasks)]}

    def check(self, job_id, job_dir=None, output_dir=None):
        self.checks.append(job_id)
        if job_id in self.cancelled:
            for task in self.jobs.pop(job_id, []):
                _sync_slurm_state_to_node(task["job_dir"], task["node_id"], "CANCELLED")
            return {"success": True, "state": "CANCELLED", "job_id": job_id}
        if self.script:
            return {"success": True, "job_id": job_id, **self.script.pop(0)}
        tasks = self.jobs.pop(job_id, None)
        if tasks is None:
            return {"success": True, "state": "COMPLETED", "job_id": job_id}
        if self.vanish_first:
            self.vanish_first = False
            return {"success": False, "code": "slurm_job_vanished", "state": None,
                    "stranded_nodes": [{"job_dir": t["job_dir"], "node_id": t["node_id"],
                                        "node_status": "queued"} for t in tasks]}
        truncated = False
        for task in tasks:
            if self.drop(task["node_id"]):
                continue
            argv = shlex.split(task["command"])
            assert argv[0] == "mdclaw"
            argv = ["Reference" if a == "CUDA" else a for a in argv[1:]]
            if self.truncate_batches_once and "--node-ids" in argv:
                # the job's time limit hits after the first segment of every task
                start = argv.index("--node-ids") + 1
                end = argv.index("--stage-tool")
                argv = argv[:start + 1] + argv[end:]
                truncated = True
            proc = subprocess.run([sys.executable, "-m", "mdclaw._cli", *argv],
                                  capture_output=True, text=True, env=dict(os.environ))
            assert proc.returncode == 0, (proc.stdout[-1500:], proc.stderr[-1500:])
        state = "TIMEOUT" if truncated else "COMPLETED"
        if truncated:
            self.truncate_batches_once = False
        for task in tasks:
            _sync_slurm_state_to_node(task["job_dir"], task["node_id"], state)
        return {"success": True, "state": state, "job_id": job_id}


@pytest.fixture
def fake_slurm(tmp_path, monkeypatch):
    fake = FakeSlurm(tmp_path / "slurm-fake")
    monkeypatch.setattr(driver_module, "_SUBMIT_MPS", fake.submit)
    monkeypatch.setattr(driver_module, "_CHECK_JOB", fake.check)
    monkeypatch.setattr(driver_module, "_SLEEP", lambda seconds: None)
    monkeypatch.setattr(driver_module, "_SLURM_ACTION", fake.action)
    return fake


@pytest.fixture(autouse=True)
def _clear_tool_overrides():
    scheme_module._TOOL_OVERRIDES.clear()
    yield
    scheme_module._TOOL_OVERRIDES.clear()


def test_stage_args_become_cli_flags():
    flags = _stage_args_to_flags({"simulation_time_ns": 0.1, "hmr": False, "npt": True,
                                  "distance_restraints": [{"k": 1}], "device_index": None,
                                  "trajectory_format": "dcd"})
    assert flags == ["--simulation-time-ns", "0.1", "--hmr", "false", "--npt", "true",
                     "--distance-restraints", '[{"k": 1}]', "--trajectory-format", "dcd"]


class TestMpsExecutor:
    def test_two_rounds_run_as_packed_jobs(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        assert setup_rounds(str(jd), _scheme(eq))["success"]

        result = run_rounds(str(jd), "rep", max_rounds=2, executor="mps", mps_tasks_per_gpu=2,
                            mps_time_limit="00:30:00")
        assert result["success"] is True, result
        assert result["rounds_completed"] == 2 and result["segments_run"] == 6
        assert result["stopped_because"] == "max_rounds" and result["failures"] == []
        assert "--executor mps" in result["next_action"]
        assert result["next"]["action"] == "run" and result["next"]["run_command"].endswith("--executor mps")
        # 3 replicas at 2 per GPU: two jobs per round
        assert [s["job_name"] for s in fake_slurm.submissions] == [
            "rep_r0001_1", "rep_r0001_2", "rep_r0002_1", "rep_r0002_2"]
        assert [len(s["commands"]) for s in fake_slurm.submissions] == [2, 1, 2, 1]
        assert all(s["time_limit"] == "00:30:00" and s["gpus"] == 1 for s in fake_slurm.submissions)
        assert result["slurm_jobs"][0]["segments"] == [segment_node_id("rep", 1, 1), segment_node_id("rep", 1, 2)]
        assert Path(fake_slurm.submissions[0]["output_dir"]) == jd / "slurm"

        # every command targets its node, carries the stage args as flags,
        # the node's seed, and CUDA in place of the scheme's platform
        for submission in fake_slurm.submissions:
            for command in submission["commands"]:
                argv = shlex.split(command)
                node_id = argv[argv.index("--node-id") + 1]
                node = read_node(str(jd), node_id)
                assert argv[argv.index("--job-dir") + 1] == str(jd)
                assert argv[argv.index("run_production") - 1] == node_id
                assert argv[argv.index("--random-seed") + 1] == str(node["conditions"]["random_seed"])
                assert argv[argv.index("--hmr") + 1] == "false"
                assert argv[argv.index("--simulation-time-ns") + 1] == "0.002"
                assert argv.count("--platform") == 1 and argv[argv.index("--platform") + 1] == "CUDA"
                assert node["status"] == "completed"
                assert node["metadata"]["slurm_job_id"] == submission["job_id"]
        # round 3 is created and pending; round 2 continued from round 1
        assert read_node(str(jd), segment_node_id("rep", 3, 1))["status"] == "pending"
        second = read_node(str(jd), segment_node_id("rep", 2, 2))
        assert second["metadata"]["continued_from"] == segment_node_id("rep", 1, 2)
        assert second["metadata"]["final_step"] == 2000

    def test_zombie_task_is_failed_and_retried(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        fake_slurm.drop = lambda node_id: node_id == segment_node_id("rep", 1, 2)

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        assert [f["node_id"] for f in result["failures"]] == [segment_node_id("rep", 1, 2)]
        assert result["failures"][0]["code"] == "slurm_completed_without_node_completion"
        zombie = read_node(str(jd), segment_node_id("rep", 1, 2))
        retry = read_node(str(jd), segment_node_id("rep", 1, 2, attempt=1))
        assert zombie["status"] == "failed" and retry["status"] == "completed"
        assert retry["metadata"]["scheme"]["retry_of"] == zombie["node_id"]
        # the retry went out as its own job
        assert [len(s["commands"]) for s in fake_slurm.submissions] == [2, 1]
        assert read_node(str(jd), segment_node_id("rep", 2, 2))["metadata"]["continued_from"] == retry["node_id"]

    def test_resume_waits_for_segments_a_previous_driver_submitted(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        scheme = read_scheme(str(jd), "rep")
        driver = driver_module._Driver(str(jd), scheme, executor="mps", platform=None, device_index=None,
                                       mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00",
                                       mps_poll_seconds=30, slurm_output_dir=None)
        created = driver.create_segments(1, first_round_plan(scheme), None)
        fake_slurm.submit(tasks=[{"job_dir": str(jd), "node_id": n, "command": driver._segment_command(n)}
                                 for n in created],
                          job_name="by_hand", gpus=1, time_limit="01:00:00", output_dir=str(jd / "slurm"),
                          extra_sbatch="--no-requeue")
        assert read_node(str(jd), created[0])["status"] == "queued"

        # the local executor refuses a round owned by a Slurm job
        local = run_rounds(str(jd), "rep", max_rounds=1)
        assert local["code"] == "rounds_round_in_progress"

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        assert result["segments_run"] == 0 and result["slurm_jobs"] == []      # nothing resubmitted
        assert fake_slurm.checks and all(n in {read_node(str(jd), c)["node_id"] for c in created} for n in created)
        assert all(read_node(str(jd), n)["status"] == "completed" for n in created)
        assert read_node(str(jd), segment_node_id("rep", 2, 1))["status"] == "pending"

    def test_vanished_job_fails_its_segments_for_retry(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        fake_slurm.vanish_first = True

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        assert sorted(f["node_id"] for f in result["failures"]) == [
            segment_node_id("rep", 1, 1), segment_node_id("rep", 1, 2)]
        assert all(f["code"] == "slurm_job_vanished" for f in result["failures"])
        assert len(fake_slurm.submissions) == 2
        for w in (1, 2):
            assert read_node(str(jd), segment_node_id("rep", 1, w))["status"] == "failed"
            assert read_node(str(jd), segment_node_id("rep", 1, w, attempt=1))["status"] == "completed"

    def test_submit_refusal_and_unreadable_queue_are_reported(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 1}))
        fake_slurm.refuse = {"success": False, "code": "mps_task_requires_cuda_platform", "message": "no CUDA"}
        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is False and result["code"] == "rounds_submit_failed"
        assert "mps_task_requires_cuda_platform" in result["message"]
        assert read_node(str(jd), segment_node_id("rep", 1, 1))["status"] == "pending"

        fake_slurm.refuse = None
        fake_slurm.check = lambda job_id, job_dir=None, output_dir=None: {"success": False, "errors": ["squeue down"]}
        driver_module._CHECK_JOB = fake_slurm.check
        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is False and result["code"] == "rounds_slurm_unavailable"
        assert read_node(str(jd), segment_node_id("rep", 1, 1))["status"] == "queued"

    def test_policy_runs_in_process_or_through_the_launcher(self, tmp_path, periodic_triple, fake_slurm, monkeypatch):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["split_policy"] = _split_policy
        setup_rounds(str(jd), _scheme(eq, policy="split_policy", initial_weights="uniform"))

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        policy = read_node(str(jd), "analyze_rep_r0001")
        assert policy["status"] == "completed" and policy["metadata"]["scheme"]["role"] == "policy"
        assert read_node(str(jd), segment_node_id("rep", 2, 3))["metadata"]["scheme"]["extra"] == {"recycled": True}

        # the launcher branch: a stand-in launcher that runs the CLI of this interpreter
        launcher = tmp_path / "mdclaw-launcher"
        # "$4" is the job dir, "$6" the node id of the policy call; keep the
        # owner record the driver wrote for it before running the tool
        launcher.write_text(
            '#!/bin/sh\ncp "$4/nodes/$6/owner.json" "$4/owner_seen_$6.json"\n'
            f'exec "{sys.executable}" -m mdclaw._cli "$@"\n')
        launcher.chmod(0o755)
        monkeypatch.setattr(driver_module, "_launcher_path", lambda: launcher)
        monkeypatch.setattr(driver_module, "_POLICY_MODE", "launcher")
        # the override tool is not visible to a subprocess: make round 2's policy the real analyze tool
        scheme_module._TOOL_OVERRIDES.clear()
        progress = json.loads((jd / "progress.json").read_text())
        progress["params"]["sampling_schemes"]["rep"]["policy"] = "we_resample"
        progress["params"]["sampling_schemes"]["rep"]["policy_args"] = {
            "pcoord": [{"type": "distance", "name": "d", "selection_group1": "name C1", "selection_group2": "name C2"}],
            "bins": {"edges": [[0.5, 1.0, 1.5]]}, "walkers_per_bin": 2,
            "target": {"pcoord_ranges": [[2.5, None]]}}
        (jd / "progress.json").write_text(json.dumps(progress))
        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        policy2 = read_node(str(jd), "analyze_rep_r0002")
        assert policy2["status"] == "completed" and policy2["metadata"]["analysis"] == "we_resample"
        assert read_node(str(jd), segment_node_id("rep", 3, 1))["status"] == "pending"
        # the driver owned the policy node while the launcher ran it (WE-22)
        seen = json.loads((jd / "owner_seen_analyze_rep_r0002.json").read_text())
        assert seen["pid"] == os.getpid() and seen["role"] == "policy" and seen["executor"] == "mps"
        assert not (jd / "nodes" / "analyze_rep_r0002" / "owner.json").exists()


class TestHeldJobs:
    def test_launch_failed_hold_is_released_then_cancelled(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 1}))
        fake_slurm.script = [{"state": "PENDING", "reason": "launch_failed_requeued_held"}] * 3

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        first = fake_slurm.submissions[0]["job_id"]
        assert fake_slurm.actions == [("release", first), ("release", first), ("cancel", first)]
        assert [f["node_id"] for f in result["failures"]] == [segment_node_id("rep", 1, 1)]
        assert read_node(str(jd), segment_node_id("rep", 1, 1))["status"] == "failed"
        assert read_node(str(jd), segment_node_id("rep", 1, 1, attempt=1))["status"] == "completed"
        assert any("released (1/2)" in w for w in result["warnings"])
        assert any("cancelled" in w for w in result["warnings"])

    def test_user_hold_is_reported_and_waited_for(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 1}))
        fake_slurm.script = [{"state": "PENDING", "reason": "JobHeldUser"}] * 2 + [{"state": "PENDING", "reason": "Priority"}]

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        assert fake_slurm.actions == [] and result["failures"] == []
        held = [w for w in result["warnings"] if "JobHeldUser" in w]
        assert len(held) == 1 and "scontrol release" in held[0]
        assert read_node(str(jd), segment_node_id("rep", 1, 1))["status"] == "completed"


# ---------------------------------------------------------------------------
# WE-17: several segments per MPS task (run_segment_batch)
# ---------------------------------------------------------------------------


class TestSegmentsPerTask:
    def test_batches_run_in_one_process_and_track_their_first_node(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        assert setup_rounds(str(jd), _scheme(eq))["success"]

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps", mps_segments_per_task=2)
        assert result["success"] is True, result
        assert result["mps"]["segments_per_task"] == 2 and result["failures"] == []
        # 3 replicas in tasks of 2: one job with two tasks
        assert [s["job_name"] for s in fake_slurm.submissions] == ["rep_r0001_1"]
        commands = fake_slurm.submissions[0]["commands"]
        assert len(commands) == 2 and result["slurm_jobs"][0]["tasks"] == 2
        first, second = (shlex.split(c) for c in commands)
        assert first[1] == "run_segment_batch" and first[first.index("--node-ids") + 1:first.index("--stage-tool")] == [
            segment_node_id("rep", 1, 1), segment_node_id("rep", 1, 2)]
        assert second[second.index("--node-ids") + 1:second.index("--stage-tool")] == [segment_node_id("rep", 1, 3)]
        assert first[first.index("--stage-tool") + 1] == "run_production"
        assert json.loads(first[first.index("--stage-args") + 1])["simulation_time_ns"] == 0.002
        assert first.count("--platform") == 1 and first[first.index("--platform") + 1] == "CUDA"
        for w in (1, 2, 3):
            node = read_node(str(jd), segment_node_id("rep", 1, w))
            assert node["status"] == "completed", node
            assert node["metadata"]["final_step"] == 1000
        # the first node of each task carries the Slurm job id, the second does not
        assert read_node(str(jd), segment_node_id("rep", 1, 1))["metadata"]["slurm_job_id"] == fake_slurm.submissions[0]["job_id"]
        assert "slurm_job_id" not in read_node(str(jd), segment_node_id("rep", 1, 2))["metadata"]
        assert read_node(str(jd), segment_node_id("rep", 2, 2))["metadata"]["continued_from"] == segment_node_id("rep", 1, 2)

    def test_segments_a_task_did_not_reach_go_out_again(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        # the first job ends (TIMEOUT) after the task's first segment
        fake_slurm.truncate_batches_once = True

        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps", mps_segments_per_task=2)
        assert result["success"] is True, result
        assert len(fake_slurm.submissions) == 2 and result["failures"] == []
        assert any("not reached" in w for w in result["warnings"])
        second = shlex.split(fake_slurm.submissions[1]["commands"][0])
        assert second[second.index("--node-ids") + 1:second.index("--stage-tool")] == [segment_node_id("rep", 1, 2)]
        for w in (1, 2):
            assert read_node(str(jd), segment_node_id("rep", 1, w))["status"] == "completed"
        assert not (jd / "nodes" / segment_node_id("rep", 1, 2, attempt=1)).exists()

    def test_resume_waits_for_a_batch_segment_under_a_live_owner(self, tmp_path, periodic_triple, fake_slurm, monkeypatch):  # noqa: F811
        from mdclaw._node import begin_node, complete_node
        from mdclaw.rounds.plan import first_round_plan
        from tests.test_rounds import _owner

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        scheme = read_scheme(str(jd), "rep")
        driver = driver_module._Driver(str(jd), scheme, executor="mps", platform=None, device_index=None,
                                       mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00",
                                       mps_poll_seconds=30, slurm_output_dir=None, mps_segments_per_task=2)
        created = driver.create_segments(1, first_round_plan(scheme), None)
        # replica 1 completed by a batch task; replica 2 running on a compute node, heartbeat fresh
        begin_node(str(jd), created[0])
        complete_node(str(jd), created[0], artifacts={}, metadata={"final_step": 1000})
        begin_node(str(jd), created[1])
        _owner(jd, created[1], host="c123", age_seconds=5)

        polls = {"n": 0}

        def finish_on_poll(seconds):
            polls["n"] += 1
            complete_node(str(jd), created[1], artifacts={}, metadata={"final_step": 1000})

        monkeypatch.setattr(driver_module, "_SLEEP", finish_on_poll)
        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps", mps_segments_per_task=2)
        assert result["success"] is True, result
        assert polls["n"] >= 1 and result["segments_run"] == 0 and result["recovered"] == []
        assert read_node(str(jd), created[1])["status"] == "completed"
        assert read_node(str(jd), segment_node_id("rep", 2, 1))["status"] == "pending"


    def test_automatic_segments_per_task_keeps_the_gpus_busy(self, tmp_path, periodic_triple, fake_slurm):  # noqa: F811
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq))
        # 3 segments, room for 8 x 8 tasks: one segment per task, plain run_production commands
        result = run_rounds(str(jd), "rep", max_rounds=1, executor="mps")
        assert result["success"] is True, result
        assert result["mps"]["segments_per_task"] == "auto"
        assert result["slurm_jobs"][0]["segments_per_task"] == 1 and result["slurm_jobs"][0]["tasks"] == 3
        assert all("run_production" in c for c in fake_slurm.submissions[0]["commands"])
        # one job of one task allowed: the 3 pending segments of round 2 pack into one task
        again = run_rounds(str(jd), "rep", max_rounds=1, executor="mps", mps_tasks_per_gpu=1, mps_max_jobs=1)
        assert again["success"] is True, again
        assert again["slurm_jobs"][0]["segments_per_task"] == 3 and again["slurm_jobs"][0]["tasks"] == 1
        assert "run_segment_batch" in fake_slurm.submissions[1]["commands"][0]
        assert all(read_node(str(jd), segment_node_id("rep", 2, w))["status"] == "completed" for w in (1, 2, 3))


class TestRunSegmentBatch:
    def test_batch_runs_pending_segments_and_reports_each(self, tmp_path, periodic_triple):  # noqa: F811
        from mdclaw.rounds.batch import run_segment_batch
        from mdclaw.rounds.owner import read_owner
        from mdclaw.rounds.plan import first_round_plan
        from mdclaw.simulation.production import run_production
        from mdclaw._tool_meta import node_tool

        seen = {}

        @node_tool(node_type="prod")
        def observing(job_dir, node_id, **kwargs):
            seen[node_id] = read_owner(job_dir, node_id)
            if node_id.endswith("w0002"):
                from mdclaw._node import begin_node, fail_node

                begin_node(job_dir, node_id)
                fail_node(job_dir, node_id, errors=["boom"], code="unhandled_exception")
                return {"success": False, "code": "unhandled_exception", "message": "boom"}
            return run_production(job_dir=job_dir, node_id=node_id, **kwargs)

        scheme_module._TOOL_OVERRIDES["observing"] = observing
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, stage_tool="observing"))
        scheme = read_scheme(str(jd), "rep")
        driver = driver_module._Driver(str(jd), scheme, executor="mps", platform=None, device_index=None,
                                       mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00",
                                       mps_poll_seconds=30, slurm_output_dir=None)
        created = driver.create_segments(1, first_round_plan(scheme), None)

        result = run_segment_batch(str(jd), created, stage_tool="observing", stage_args=json.dumps(STAGE_ARGS),
                                   platform="Reference")
        assert result["success"] is True, result
        assert (result["completed"], result["failed"], result["refused"], result["skipped"]) == (2, 1, 0, 0)
        assert [r["status"] for r in result["results"]] == ["completed", "failed", "completed"]
        assert result["results"][1]["code"] == "unhandled_exception"
        for node_id in created:
            assert seen[node_id]["executor"] == "mps" and seen[node_id]["role"] == "segment"
            assert seen[node_id]["scheme_id"] == "rep"
            assert not (jd / "nodes" / node_id / "owner.json").exists()
        # a second call skips what is no longer pending
        again = run_segment_batch(str(jd), created, stage_tool="observing", stage_args=STAGE_ARGS)
        assert again["skipped"] == 3 and again["completed"] == 0

    def test_batch_refuses_bad_input(self, tmp_path, periodic_triple):  # noqa: F811
        from mdclaw.rounds.batch import run_segment_batch

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        assert run_segment_batch(str(jd), [])["code"] == "rounds_batch_invalid"
        assert run_segment_batch(str(jd), ["prod_404"])["code"] == "rounds_batch_invalid"
        assert run_segment_batch(str(jd), [eq])["code"] == "rounds_batch_invalid"
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 1}))
        scheme = read_scheme(str(jd), "rep")
        from mdclaw.rounds.plan import first_round_plan

        driver = driver_module._Driver(str(jd), scheme, executor="mps", platform=None, device_index=None,
                                       mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00",
                                       mps_poll_seconds=30, slurm_output_dir=None)
        created = driver.create_segments(1, first_round_plan(scheme), None)
        assert run_segment_batch(str(jd), created, stage_args="{oops")["code"] == "rounds_batch_invalid"
        assert run_segment_batch(str(jd), created, stage_tool="no_such_tool")["code"] == "rounds_tool_invalid"
        assert run_segment_batch(str(tmp_path / "missing"), created)["code"] == "rounds_job_dir_unreachable"
