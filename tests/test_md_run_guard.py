"""MD stage tools refuse, before begin_node, the runs that cannot succeed here.

MDDataBench v5 (qwen3.6-35b, 3 conditions, 2026-10):

- 97 attempts ran min/eq on the Slurm login node (``run_equilibration
  --platform CPU``): ~52,400 s, 19 of 35 timeouts, one login-node OOM.
- 21 attempts passed ``--platform CUDA`` where OpenMM had no CUDA; the node
  was sealed by an unhandled error after begin_node.
- A job allocated GPUs inside a container without ``--nv`` ran ``auto`` on
  the CPU (031/065/092).
- 4 attempts re-ran a node a Slurm job was running; run_equilibration
  swallowed NodeAlreadyRunningError and failed the node under its owner.
- 36 eq/prod nodes declared conditions the command did not pass and were
  sealed failed although nothing ran.

Real node-mode calls on the two-carbon periodic triple of
tests/test_production_temperature_inheritance.py; platform availability and
the Slurm environment are mocked, no MD runs past the refusals.
"""
import json
import os
import shlex
import subprocess

import pytest

from mdclaw._node import create_node, read_node
from mdclaw.node.io import _atomic_write_json
from mdclaw.node.owner import owner_path, write_owner
from mdclaw.simulation import run_guard
from tests.test_production_temperature_inheritance import _job_with_topo, periodic_triple  # noqa: F401

_SLURM_KEYS = ("SLURM_JOB_ID", "SLURM_JOBID", "SLURM_JOB_GPUS", "SLURM_STEP_GPUS", "SLURM_GPUS_ON_NODE",
               "CUDA_VISIBLE_DEVICES", "SLURM_CONF", "SLURM_CONF_SERVER", "MDCLAW_SLURM_PATH")


@pytest.fixture
def triple(request):
    return request.getfixturevalue("periodic_triple")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No Slurm, no escape, an empty PATH for sbatch, no slurm.conf, a cwd
    without .mdclaw_cluster.json; the probe cache starts empty."""
    for key in (*_SLURM_KEYS, run_guard.ALLOW_LOCAL_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(run_guard, "_which", lambda tool, path: None)
    monkeypatch.setattr(run_guard, "SLURM_CONF_PATHS", ())
    monkeypatch.setattr(run_guard, "CGROUP_PATH", str(tmp_path / "no_cgroup"))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    run_guard._PROBES.clear()
    yield monkeypatch
    run_guard._PROBES.clear()


@pytest.fixture
def login_node(clean_env):
    """A configless Slurm login node, as RIKYU's (inherited inside the SIF)."""
    clean_env.setenv("SLURM_CONF_SERVER", "sctl1:6817,sctl2:6817")
    return clean_env


def _fake_platforms(monkeypatch, usable=("Reference", "CPU")):
    reasons = {}

    def probe(name, properties=None):
        if name in usable:
            return None
        reasons[name] = f'There is no registered Platform called "{name}"'
        return reasons[name]

    monkeypatch.setattr(run_guard, "probe_platform", probe)
    monkeypatch.setattr(run_guard, "usable_platforms", lambda: list(usable))


def _eq_node(jd, topo, conditions=None):
    return create_node(jd, "eq", parent_node_ids=[topo], conditions=conditions)["node_id"]


EQ_ARGS = dict(pressure_bar=0, nvt_steps=200, npt_steps=0, restraint_force_constant=0.0, hmr=False)


def _cli(argv, capsys):
    from mdclaw import _cli

    with pytest.raises(SystemExit) as exc:
        _cli.main([*argv, "--output", "full"])
    return exc.value.code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# cluster and allocation detection
# ---------------------------------------------------------------------------

def test_a_slurm_cluster_is_seen_from_inside_a_container(clean_env, tmp_path):
    assert run_guard.slurm_cluster_evidence({}) is None
    assert "SLURM_CONF_SERVER" in run_guard.slurm_cluster_evidence({"SLURM_CONF_SERVER": "sctl1:6817"})
    assert "SLURM_CONF" in run_guard.slurm_cluster_evidence({"SLURM_CONF": "/etc/slurm/slurm.conf"})
    conf = tmp_path / "slurm.conf"
    conf.write_text("ClusterName=x\n")
    clean_env.setattr(run_guard, "SLURM_CONF_PATHS", (str(conf),))
    assert str(conf) in run_guard.slurm_cluster_evidence({})
    clean_env.setattr(run_guard, "SLURM_CONF_PATHS", ())
    (tmp_path / ".mdclaw_cluster.json").write_text("{}")
    assert ".mdclaw_cluster.json" in run_guard.slurm_cluster_evidence({}, [tmp_path])


def test_sbatch_on_the_search_path_is_a_cluster(monkeypatch, tmp_path):
    monkeypatch.setattr(run_guard, "SLURM_CONF_PATHS", ())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sbatch = bin_dir / "sbatch"
    sbatch.write_text("#!/bin/sh\n")
    sbatch.chmod(0o755)
    assert "sbatch" in run_guard.slurm_cluster_evidence({"PATH": str(bin_dir)})
    # MDCLAW_SLURM_PATH (clients bound into the SIF) is searched instead of PATH
    assert "sbatch" in run_guard.slurm_cluster_evidence({"PATH": "/nonexistent",
                                                         "MDCLAW_SLURM_PATH": str(bin_dir)})


def test_inside_a_job_or_with_the_escape_md_may_run(clean_env):
    on_login = {"SLURM_CONF_SERVER": "sctl1:6817"}
    assert run_guard.outside_allocation(environ=on_login)
    assert run_guard.outside_allocation(environ={**on_login, "SLURM_JOB_ID": "222174"}) is None
    assert run_guard.outside_allocation(environ={**on_login, run_guard.ALLOW_LOCAL_ENV: "1"}) is None
    assert run_guard.outside_allocation(environ={}) is None          # a workstation


def test_a_job_whose_container_dropped_the_slurm_variables_is_still_a_job(clean_env, tmp_path):
    """``singularity exec --cleanenv`` inside a job: no SLURM_JOB_ID, but the
    cgroup names the job; a .mdclaw_cluster.json in the job's cwd must not
    turn the job into a login node."""
    (tmp_path / "cwd" / ".mdclaw_cluster.json").write_text("{}")
    assert run_guard.outside_allocation()                                   # looks like a login node
    cgroup = tmp_path / "cgroup"
    clean_env.setattr(run_guard, "CGROUP_PATH", str(cgroup))
    cgroup.write_text("0::/system.slice/slurmstepd.scope/job_222174/step_batch/user/task_0\n")
    assert run_guard.slurm_allocation() == "222174"
    assert run_guard.outside_allocation() is None
    from mdclaw.simulation.minimize import run_minimization
    no_node = str(tmp_path / "no_job")
    assert run_guard.md_preflight(run_minimization, {}, no_node, "min_001") is None
    clean_env.setattr(run_guard, "CGROUP_PATH", str(tmp_path / "no_cgroup"))
    assert run_guard.md_preflight(run_minimization, {}, no_node, "min_001")["code"] == "md_outside_allocation"
    clean_env.setattr(run_guard, "CGROUP_PATH", str(cgroup))
    cgroup.write_text("12:memory:/slurm/uid_100160/job_99/step_0\n")
    assert run_guard.slurm_allocation() == "99"
    cgroup.write_text("0::/user.slice/user-100160.slice/session-26918.scope\n")     # RIKYU login node
    assert run_guard.slurm_allocation() is None
    assert run_guard.slurm_allocation({}) is None                           # an explicit environ only


def test_gpu_allocation_comes_from_slurm(clean_env):
    assert run_guard.allocated_gpus({"SLURM_JOB_GPUS": "0"}) is None          # not in a job
    assert run_guard.allocated_gpus({"SLURM_JOB_ID": "1", "SLURM_JOB_GPUS": "0"}) == "SLURM_JOB_GPUS=0"
    assert run_guard.allocated_gpus({"SLURM_JOB_ID": "1", "SLURM_GPUS_ON_NODE": "0"}) is None
    assert run_guard.allocated_gpus({"SLURM_JOB_ID": "1", "SLURM_GPUS_ON_NODE": "1"})
    assert run_guard.allocated_gpus({"SLURM_JOB_ID": "1", "CUDA_VISIBLE_DEVICES": "NoDevFiles"}) is None
    assert run_guard.allocated_gpus({"SLURM_JOB_ID": "1", "CUDA_VISIBLE_DEVICES": "0"})


def test_platform_problems(clean_env):
    _fake_platforms(clean_env)
    gpu_job = {"SLURM_JOB_ID": "1", "SLURM_JOB_GPUS": "0"}
    assert run_guard.platform_problem("auto", None, {}) is None             # auto off a GPU job: OpenMM picks
    assert run_guard.platform_problem("CPU", None, gpu_job) is None          # explicit CPU is a choice
    assert run_guard.platform_problem("CUDA", None, {})["code"] == "platform_unavailable"
    assert run_guard.platform_problem("gpu", None, {})["code"] == "platform_unavailable"
    assert run_guard.platform_problem("auto", None, gpu_job)["code"] == "gpu_allocated_platform_unavailable"
    assert run_guard.platform_problem("CUDA", None, gpu_job)["code"] == "gpu_allocated_platform_unavailable"
    _fake_platforms(clean_env, usable=("Reference", "CPU", "CUDA"))
    assert run_guard.platform_problem("auto", None, gpu_job) is None


def test_the_real_probe_accepts_cpu_and_names_a_missing_platform():
    run_guard._PROBES.clear()
    assert run_guard.probe_platform("Reference") is None
    assert "NoSuchPlatform" in run_guard.probe_platform("NoSuchPlatform")
    assert "Reference" in run_guard.usable_platforms()


def test_flags_render_the_cli_forms():
    from mdclaw.simulation.equilibrate import run_equilibration
    from mdclaw.simulation.tempering import run_sst2

    flags = run_guard.cli_flags(run_equilibration, {
        "platform": "CPU", "nvt_time_ns": 0.5, "is_membrane": True, "restraint_atoms": "solute_heavy",
        "job_dir": "/x", "node_id": "eq_001", "temperature_kelvin": None})
    assert flags == ["--nvt-time-ns", "0.5", "--is-membrane", "true", "--platform", "CPU"]
    assert run_guard.cli_flags(run_sst2, {"temperatures_kelvin": ["300", "330"]}) == [
        "--temperatures-kelvin", "300", "330"]


# ---------------------------------------------------------------------------
# md_outside_allocation
# ---------------------------------------------------------------------------

def test_equilibration_on_a_login_node_is_refused_with_the_submit_command(tmp_path, triple, login_node, capsys):
    from mdclaw.simulation.equilibrate import run_equilibration

    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo)
    r = run_equilibration(job_dir=jd, node_id=eq, platform="CPU", nvt_time_ns=0.001, npt_time_ns=0.0,
                          pressure_bar=0, hmr=False)
    assert r["success"] is False and r["code"] == "md_outside_allocation"
    assert "SLURM_CONF_SERVER" in r["message"]
    node = read_node(jd, eq)
    assert node["status"] == "pending"
    assert node["metadata"]["last_refusal"]["code"] == "md_outside_allocation"
    command = r["next_action"]
    assert "..." not in command and "<" not in command
    argv = shlex.split(command)
    assert argv[:2] == ["mdclaw", "submit_job"]
    assert argv[argv.index("--job-dir") + 1] == str(os.path.realpath(jd))
    assert argv[argv.index("--node-id") + 1] == eq and argv[argv.index("--gpus") + 1] == "1"
    script = shlex.split(argv[argv.index("--script") + 1])
    assert script[:5] == ["mdclaw", "--job-dir", os.path.realpath(jd), "--node-id", eq]
    assert script[5] == "run_equilibration"
    assert script[script.index("--platform") + 1] == "CUDA"          # CPU in a GPU job idles the GPU
    assert script[script.index("--nvt-time-ns") + 1] == "0.001"
    assert "MDCLAW_ALLOW_LOCAL_MD" not in json.dumps(r)                # the escape is not advertised

    # the CLI leaves the node pending too
    code, out = _cli(["--job-dir", jd, "--node-id", eq, "run_equilibration", "--platform", "CPU"], capsys)
    assert code == 1 and out["code"] == "md_outside_allocation"
    assert read_node(jd, eq)["status"] == "pending"


def test_minimization_and_production_are_refused_on_a_login_node(tmp_path, triple, login_node):
    from mdclaw.simulation.minimize import run_minimization
    from mdclaw.simulation.production import run_production
    from tests.test_production_temperature_inheritance import _eq

    (tmp_path / "a").mkdir()
    jd, topo = _job_with_topo(tmp_path / "a", triple)
    mn = create_node(jd, "min", parent_node_ids=[topo])["node_id"]
    r = run_minimization(job_dir=jd, node_id=mn, platform="CPU", hmr=False)
    assert r["code"] == "md_outside_allocation" and read_node(jd, mn)["status"] == "pending"
    assert "run_minimization" in r["next_action"]

    (tmp_path / "b").mkdir()
    jd, topo = _job_with_topo(tmp_path / "b", triple)
    login_node.setenv("SLURM_JOB_ID", "1")          # the eq ran in its job
    eq = _eq(jd, topo, 300.0)
    login_node.delenv("SLURM_JOB_ID")
    prod = create_node(jd, "prod", parent_node_ids=[eq])["node_id"]
    r = run_production(job_dir=jd, node_id=prod, simulation_time_ns=0.001, hmr=False)
    assert r["code"] == "md_outside_allocation" and read_node(jd, prod)["status"] == "pending"
    assert "--simulation-time-ns 0.001" in r["next_action"]


def test_a_queued_node_is_not_sealed_by_a_login_node_rerun(tmp_path, triple, login_node, capsys):
    """The agent submitted the node and then ran it again by hand."""
    from mdclaw.node.lifecycle import _apply_status

    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo)
    _apply_status(jd, eq, "queued", payload={"metadata": {"slurm_job_id": "222174"}})
    code, out = _cli(["--job-dir", jd, "--node-id", eq, "run_equilibration", "--platform", "CPU",
                      "--pressure-bar", "0"], capsys)
    assert code == 1 and out["code"] == "md_outside_allocation"
    assert out["next_action"].startswith(f"'{eq}' is already queued") and "wait_node" in out["next_action"]
    node = read_node(jd, eq)
    assert node["status"] == "queued" and "last_refusal" not in node["metadata"]


def test_the_same_run_inside_a_slurm_job_is_not_refused(tmp_path, triple, login_node):
    from mdclaw.simulation.equilibrate import run_equilibration

    login_node.setenv("SLURM_JOB_ID", "222174")
    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo)
    r = run_equilibration(job_dir=jd, node_id=eq, platform="Reference", random_seed=1, **EQ_ARGS)
    assert r["success"], r["errors"]
    assert read_node(jd, eq)["status"] == "completed"


def test_run_rounds_local_on_a_login_node_is_refused(login_node, monkeypatch, tmp_path):
    from mdclaw.rounds import driver

    monkeypatch.setattr(driver, "read_scheme", lambda job_dir, scheme_id: {"scheme_id": scheme_id,
                                                                            "policy": "replicas"})
    r = driver.run_rounds(job_dir=str(tmp_path), scheme_id="rep", max_rounds=2)
    assert r["success"] is False and r["code"] == "md_outside_allocation"
    argv = shlex.split(r["next_action"])
    assert argv[:2] == ["mdclaw", "submit_job"] and argv[argv.index("--gpus") + 1] == "1"
    script = shlex.split(argv[argv.index("--script") + 1])
    assert script[:2] == ["mdclaw", "run_rounds"]
    assert script[script.index("--max-rounds") + 1] == "2" and "--max-wall-hours" in script
    # the mps executor only submits and waits: it is not refused
    assert run_guard.rounds_outside_allocation(str(tmp_path), "rep") is not None
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    assert run_guard.rounds_outside_allocation(str(tmp_path), "rep") is None


# ---------------------------------------------------------------------------
# platform_unavailable / gpu_allocated_platform_unavailable
# ---------------------------------------------------------------------------

def test_an_unavailable_platform_leaves_the_node_pending(tmp_path, triple, clean_env, capsys):
    from mdclaw.simulation.minimize import run_minimization

    _fake_platforms(clean_env)
    jd, topo = _job_with_topo(tmp_path, triple)
    mn = create_node(jd, "min", parent_node_ids=[topo])["node_id"]
    r = run_minimization(job_dir=jd, node_id=mn, platform="CUDA", hmr=False)
    assert r["code"] == "platform_unavailable"
    assert "There is no registered Platform called" in r["message"]
    assert r["context"]["usable_platforms"] == ["Reference", "CPU"]
    assert read_node(jd, mn)["status"] == "pending"
    script = shlex.split(r["next_action"].split(": ", 1)[1])
    assert script[5] == "run_minimization" and script[script.index("--platform") + 1] == "CPU"

    code, out = _cli(["--job-dir", jd, "--node-id", mn, "run_minimization", "--platform", "CUDA"], capsys)
    assert code == 1 and out["code"] == "platform_unavailable"
    assert read_node(jd, mn)["status"] == "pending"


def test_gpu_job_without_gpu_access_refuses_auto_instead_of_running_on_cpu(tmp_path, triple, clean_env):
    from mdclaw.simulation.equilibrate import run_equilibration

    _fake_platforms(clean_env)
    clean_env.setenv("SLURM_JOB_ID", "221339")
    clean_env.setenv("SLURM_JOB_GPUS", "0")
    clean_env.setenv("SINGULARITY_CONTAINER", "/data1/x/mdclaw.sif")
    clean_env.setenv("APPTAINER_CONTAINER", "/data1/x/mdclaw.sif")
    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo)
    r = run_equilibration(job_dir=jd, node_id=eq, random_seed=1, **EQ_ARGS)            # platform auto
    assert r["code"] == "gpu_allocated_platform_unavailable"
    assert "SLURM_JOB_GPUS=0" in r["message"] and "--nv" in r["message"]
    assert "configure_container --image /data1/x/mdclaw.sif --extra-flags=--nv" in r["next_action"]
    assert read_node(jd, eq)["status"] == "pending"
    # an explicit CPU run in the same job is the agent's choice and runs
    r = run_equilibration(job_dir=jd, node_id=eq, platform="Reference", random_seed=1, **EQ_ARGS)
    assert r["success"], r["errors"]


def test_inside_its_own_job_a_platform_refusal_seals_with_its_code(tmp_path, triple, clean_env, capsys):
    """The Slurm job's end would fail the queued node anyway; it is failed
    with the precise code and the branch command."""
    from mdclaw.node.lifecycle import _apply_status

    _fake_platforms(clean_env)
    clean_env.setenv("SLURM_JOB_ID", "221339")
    clean_env.setenv("SLURM_JOB_GPUS", "0")
    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo)
    _apply_status(jd, eq, "queued", payload={"metadata": {"slurm_job_id": "221339"}})
    code, out = _cli(["--job-dir", jd, "--node-id", eq, "run_equilibration", "--pressure-bar", "0"], capsys)
    assert code == 1 and out["code"] == "gpu_allocated_platform_unavailable"
    assert "mdclaw create_node" in out["next_action"]
    node = read_node(jd, eq)
    assert node["status"] == "failed" and node["metadata"]["failure_code"] == "gpu_allocated_platform_unavailable"


# ---------------------------------------------------------------------------
# NodeAlreadyRunningError is not swallowed
# ---------------------------------------------------------------------------

@pytest.fixture
def live_other_owner():
    other = subprocess.Popen(["sleep", "60"])
    yield other
    other.kill()
    other.wait()


def _owned_by(jd, node_id, other):
    from mdclaw.node.lifecycle import _apply_status

    record = write_owner(jd, node_id, executor="stage_tool", scheme_id=None)
    record["pid"] = other.pid
    _atomic_write_json(owner_path(jd, node_id), record)
    _apply_status(jd, node_id, "running")


@pytest.mark.parametrize("stage", ["min", "eq"])
def test_a_node_another_live_run_owns_is_not_failed(tmp_path, triple, live_other_owner, stage):
    from mdclaw.node.lifecycle import NodeAlreadyRunningError
    from mdclaw.simulation.equilibrate import run_equilibration
    from mdclaw.simulation.minimize import run_minimization

    jd, topo = _job_with_topo(tmp_path, triple)
    node = create_node(jd, stage, parent_node_ids=[topo])["node_id"]
    _owned_by(jd, node, live_other_owner)
    with pytest.raises(NodeAlreadyRunningError):
        if stage == "min":
            run_minimization(job_dir=jd, node_id=node, platform="Reference", hmr=False)
        else:
            run_equilibration(job_dir=jd, node_id=node, platform="Reference", **EQ_ARGS)
    assert read_node(jd, node)["status"] == "running"


def test_the_cli_does_not_seal_a_node_another_live_run_owns(tmp_path, triple, live_other_owner, capsys):
    """021_antibody_3eoa cli_sif r1: eq_001 sealed while Slurm job 222174 ran it."""
    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo)
    _owned_by(jd, eq, live_other_owner)
    code, out = _cli(["--job-dir", jd, "--node-id", eq, "run_equilibration", "--platform", "Reference",
                      "--pressure-bar", "0", "--nvt-steps", "200", "--npt-steps", "0"], capsys)
    assert code == 1 and out["code"] == "node_already_running"
    assert "wait_node" in out["next_action"]
    node = read_node(jd, eq)
    assert node["status"] == "running" and "failure_code" not in node["metadata"]


# ---------------------------------------------------------------------------
# declared conditions the command did not pass
# ---------------------------------------------------------------------------

def test_declared_conditions_the_call_missed_keep_the_node_pending(tmp_path, triple, clean_env, capsys):
    """010_membrane_6kux cli_skill_sif r2: declared 310 K, 0.5 + 0.5 ns; the
    bare command ran the defaults and the node was sealed."""
    jd, topo = _job_with_topo(tmp_path, triple)
    # 200 steps at 2 fs are 0.0004 ns; the node declares half that, 310 K and no NPT
    eq = _eq_node(jd, topo, conditions={"temperature_kelvin": 310, "nvt_time_ns": 0.0002,
                                        "npt_time_ns": 0.0})
    code, out = _cli(["--job-dir", jd, "--node-id", eq, "run_equilibration", "--platform", "Reference",
                      "--pressure-bar", "0", "--nvt-steps", "200", "--hmr", "false",
                      "--restraint-force-constant", "0", "--random-seed", "1"], capsys)
    assert code == 1 and out["code"] == "node_execution_context_invalid"
    node = read_node(jd, eq)
    assert node["status"] == "pending"
    assert node["metadata"]["last_refusal"]["code"] == "node_execution_context_invalid"
    assert "still pending" in out["next_action"]
    rerun = shlex.split(out["next_action"].split("declare: ", 1)[1])
    assert rerun[:6] == ["mdclaw", "--job-dir", os.path.realpath(jd), "--node-id", eq, "run_equilibration"]
    assert rerun[rerun.index("--temperature-kelvin") + 1] == "310"
    assert rerun[rerun.index("--nvt-time-ns") + 1] == "0.0002"
    assert rerun[rerun.index("--npt-time-ns") + 1] == "0.0"
    assert "--nvt-steps" not in rerun                  # the time flag replaces the step flag
    assert rerun[rerun.index("--platform") + 1] == "Reference"

    # the named command runs on the same node
    code, out = _cli(rerun[1:], capsys)
    assert code == 0, out
    assert read_node(jd, eq)["status"] == "completed"


def test_on_a_login_node_the_declared_values_go_into_the_submit_command(tmp_path, triple, login_node):
    from mdclaw.simulation.equilibrate import run_equilibration

    jd, topo = _job_with_topo(tmp_path, triple)
    # the 250000-step defaults are 0.5 ns each at the 2 fs of this non-HMR topology
    eq = _eq_node(jd, topo, conditions={"nvt_time_ns": 0.25, "npt_time_ns": 0.25})
    r = run_equilibration(job_dir=jd, node_id=eq, platform="CPU")
    assert r["code"] == "node_execution_context_invalid" and read_node(jd, eq)["status"] == "pending"
    argv = shlex.split(r["next_action"].split("declare: ", 1)[1])
    assert argv[:2] == ["mdclaw", "submit_job"]
    script = shlex.split(argv[argv.index("--script") + 1])
    assert script[script.index("--nvt-time-ns") + 1] == "0.25"
    assert script[script.index("--npt-time-ns") + 1] == "0.25"
    assert script[script.index("--platform") + 1] == "CUDA"


def test_a_mismatch_no_flag_can_fix_keeps_the_old_contract(tmp_path, triple, clean_env):
    """A declared key that is not a flag of the tool (``ensemble``) cannot be
    passed on a rerun: the result is unchanged and the node is not recorded."""
    from mdclaw.simulation.equilibrate import run_equilibration

    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq_node(jd, topo, conditions={"ensemble": "NPT"})
    r = run_equilibration(job_dir=jd, node_id=eq, platform="Reference", **EQ_ARGS)
    assert r["code"] == "node_execution_context_invalid"
    assert "rerun_command" not in r and r.get("node_status") is None
    node = read_node(jd, eq)
    assert node["status"] == "pending" and "last_refusal" not in node["metadata"]
