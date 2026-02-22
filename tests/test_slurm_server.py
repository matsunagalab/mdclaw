"""Unit tests for SLURM server tools.

All SLURM commands are mocked — no actual SLURM installation required.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from servers.slurm_server import (
    _is_partition_allowed,
    _parse_memory_bytes,
    _parse_time_limit_seconds,
    _validate_against_policy,
    cancel_job,
    check_job,
    check_job_log,
    inspect_cluster,
    list_jobs,
    set_policy,
    show_policy,
    submit_job,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_run_command(stdout="", stderr="", returncode=0):
    """Create a mock CompletedProcess."""
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# inspect_cluster
# ---------------------------------------------------------------------------


class TestInspectCluster:
    """Test inspect_cluster tool."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_json_parse_success(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sinfo_json = {
            "sinfo": [
                {
                    "partition": {"name": "gpu"},
                    "nodes": {"total": 4},
                    "gres": "gpu:a100:4",
                    "time": {"maximum": "7-00:00:00"},
                    "memory": {"maximum": 256000},
                },
                {
                    "partition": {"name": "cpu"},
                    "nodes": {"total": 10},
                    "gres": "",
                    "time": {"maximum": "3-00:00:00"},
                    "memory": {"maximum": 128000},
                },
            ]
        }
        mock_run.return_value = _mock_run_command(stdout=json.dumps(sinfo_json))

        result = inspect_cluster()
        assert result["success"] is True
        assert len(result["partitions"]) == 2
        assert "a100" in result["gpu_types"]
        assert result["config_file"] is not None
        assert Path(result["config_file"]).exists()

    @patch("servers.slurm_server.check_external_tool", return_value=False)
    def test_sinfo_not_available(self, mock_check):
        result = inspect_cluster()
        assert result["success"] is False
        assert "sinfo" in str(result.get("errors") or result.get("message", ""))

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_text_fallback(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # First call (--json) fails, second call (text) succeeds
        text_output = (
            "PARTITION NODELIST STATE GRES TIMELIMIT MEMORY CPUS\n"
            "gpu* node01 idle gpu:v100:2 7-00:00:00 128000 32\n"
            "gpu* node02 idle gpu:v100:2 7-00:00:00 128000 32\n"
            "cpu node03 idle (null) 3-00:00:00 64000 64\n"
        )
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "sinfo --json"),
            _mock_run_command(stdout=text_output),
        ]

        result = inspect_cluster()
        assert result["success"] is True
        assert len(result["partitions"]) >= 1
        assert "text fallback" in str(result["warnings"])

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_config_file_written(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sinfo_json = {"sinfo": [{"partition": {"name": "default"}, "nodes": {"total": 1}, "gres": ""}]}
        mock_run.return_value = _mock_run_command(stdout=json.dumps(sinfo_json))

        out_file = str(tmp_path / "my_cluster.json")
        result = inspect_cluster(output_file=out_file)
        assert result["success"] is True
        assert Path(out_file).exists()
        data = json.loads(Path(out_file).read_text())
        assert "partitions" in data


# ---------------------------------------------------------------------------
# submit_job
# ---------------------------------------------------------------------------


class TestSubmitJob:
    """Test submit_job tool."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_command_string_submission(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 12345\n")

        result = submit_job(
            script="echo hello world",
            job_name="test_job",
            partition="cpu",
            time_limit="01:00:00",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert result["slurm_job_id"] == "12345"
        assert result["job_name"] == "test_job"
        assert Path(result["script_file"]).exists()

        # Verify script content
        content = Path(result["script_file"]).read_text()
        assert "#!/bin/bash" in content
        assert "#SBATCH --job-name=test_job" in content
        assert "#SBATCH --partition=cpu" in content
        assert "echo hello world" in content

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_existing_script_file(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        # Create a script file
        script_file = tmp_path / "my_script.sh"
        script_file.write_text("#!/bin/bash\n#SBATCH --nodes=2\necho run simulation\n")

        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 99999\n")

        result = submit_job(
            script=str(script_file),
            job_name="wrap_test",
            partition="gpu",
            gpus=1,
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert result["slurm_job_id"] == "99999"

        # The generated script should have our SBATCH headers
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --job-name=wrap_test" in content
        assert "#SBATCH --gpus-per-node=1" in content

    @patch("servers.slurm_server.check_external_tool", return_value=False)
    def test_sbatch_not_available(self, mock_check):
        result = submit_job(script="echo test")
        assert result["success"] is False
        assert "sbatch" in str(result.get("errors") or result.get("message", ""))

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_auto_partition_from_config(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Write cluster config
        config = {"partitions": [{"name": "batch", "gpus_per_node": 0}], "gpu_types": []}
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 11111\n")

        result = submit_job(script="echo test", output_dir=str(tmp_path))
        assert result["success"] is True
        assert "Auto-selected partition" in str(result["warnings"])

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_gpu_partition_auto_select(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [
                {"name": "cpu", "gpus_per_node": 0},
                {"name": "gpu", "gpus_per_node": 4},
            ],
            "gpu_types": ["a100"],
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 22222\n")

        result = submit_job(script="echo gpu_test", gpus=1, output_dir=str(tmp_path))
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --partition=gpu" in content

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_account_qos_extra_sbatch(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 33333\n")

        result = submit_job(
            script="echo test",
            partition="gpu",
            account="myproject",
            qos="high",
            extra_sbatch="--constraint=a100\n--exclusive",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --account=myproject" in content
        assert "#SBATCH --qos=high" in content
        assert "#SBATCH --constraint=a100" in content
        assert "#SBATCH --exclusive" in content

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_module_load_auto_insert(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MDCLAW_MODULE_LOADS", "cuda/12.0 amber/24")
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44444\n")

        result = submit_job(script="echo test", partition="gpu", output_dir=str(tmp_path))
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "module load cuda/12.0" in content
        assert "module load amber/24" in content

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_metadata_written(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 55555\n")

        result = submit_job(
            script="echo test", job_name="meta_test",
            partition="cpu", output_dir=str(tmp_path),
        )
        assert result["success"] is True
        meta_path = tmp_path / "job_metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["slurm_job_id"] == "55555"
        assert meta["job_name"] == "meta_test"


# ---------------------------------------------------------------------------
# check_job
# ---------------------------------------------------------------------------


class TestCheckJob:
    """Test check_job tool."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_running_state(self, mock_run, mock_check):
        squeue_json = {
            "jobs": [{
                "job_id": 12345,
                "job_state": ["RUNNING"],
                "nodes": "node01",
                "time": {"elapsed": 3600},
            }]
        }
        mock_run.return_value = _mock_run_command(stdout=json.dumps(squeue_json))

        result = check_job("12345")
        assert result["success"] is True
        assert result["state"] == "RUNNING"
        assert result["node"] == "node01"

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_completed_via_sacct(self, mock_run, mock_check):
        # squeue returns empty (job finished), sacct returns completed
        squeue_json = {"jobs": []}
        sacct_json = {
            "jobs": [{
                "job_id": 12345,
                "state": {"current": ["COMPLETED"]},
                "nodes": "node02",
                "exit_code": {"return_code": 0},
                "time": {"elapsed": 7200},
            }]
        }
        mock_run.side_effect = [
            _mock_run_command(stdout=json.dumps(squeue_json)),
            _mock_run_command(stdout=json.dumps(sacct_json)),
        ]

        result = check_job("12345")
        assert result["success"] is True
        assert result["state"] == "COMPLETED"
        assert result["exit_code"] == "0"

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_failed_with_stderr(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        # Write stderr log and metadata
        stderr_file = tmp_path / "test_job_67890.err"
        stderr_file.write_text("Error line 1\nError line 2\nSegmentation fault\n")
        meta = {"slurm_job_id": "67890", "stderr_log": str(stderr_file)}
        (tmp_path / "job_metadata.json").write_text(json.dumps(meta))

        squeue_json = {"jobs": []}
        sacct_json = {
            "jobs": [{
                "state": {"current": ["FAILED"]},
                "nodes": "node01",
                "exit_code": {"return_code": 139},
                "time": {"elapsed": 100},
            }]
        }
        mock_run.side_effect = [
            _mock_run_command(stdout=json.dumps(squeue_json)),
            _mock_run_command(stdout=json.dumps(sacct_json)),
        ]

        result = check_job("67890")
        assert result["success"] is True
        assert result["state"] == "FAILED"
        assert "Segmentation fault" in result["stderr_tail"]

    @patch("servers.slurm_server.check_external_tool", return_value=False)
    def test_squeue_not_available(self, mock_check):
        result = check_job("99999")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    """Test list_jobs tool."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_list_jobs(self, mock_run, mock_check):
        squeue_json = {
            "jobs": [
                {
                    "job_id": 111,
                    "name": "md_run",
                    "job_state": ["RUNNING"],
                    "partition": "gpu",
                    "time": {"elapsed": 3600},
                    "nodes": "node01",
                },
                {
                    "job_id": 222,
                    "name": "analysis",
                    "job_state": ["PENDING"],
                    "partition": "cpu",
                    "time": {"elapsed": 0},
                    "nodes": "",
                },
            ]
        }
        mock_run.return_value = _mock_run_command(stdout=json.dumps(squeue_json))

        result = list_jobs()
        assert result["success"] is True
        assert result["total"] == 2
        assert result["jobs"][0]["name"] == "md_run"
        assert result["jobs"][1]["state"] == "PENDING"

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_empty_queue(self, mock_run, mock_check):
        mock_run.return_value = _mock_run_command(stdout=json.dumps({"jobs": []}))

        result = list_jobs()
        assert result["success"] is True
        assert result["total"] == 0
        assert result["jobs"] == []


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


class TestCancelJob:
    """Test cancel_job tool."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_cancel_success(self, mock_run, mock_check):
        mock_run.return_value = _mock_run_command(stdout="")

        result = cancel_job("12345")
        assert result["success"] is True
        assert "cancelled" in result["message"]

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_cancel_invalid_id(self, mock_run, mock_check):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "scancel", stderr="Invalid job id"
        )

        result = cancel_job("99999")
        assert result["success"] is False
        assert len(result["errors"]) > 0

    @patch("servers.slurm_server.check_external_tool", return_value=False)
    def test_scancel_not_available(self, mock_check):
        result = cancel_job("12345")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# check_job_log
# ---------------------------------------------------------------------------


class TestCheckJobLog:
    """Test check_job_log tool."""

    def test_read_stderr_via_metadata(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        # Create stderr file and metadata
        stderr_file = tmp_path / "job_12345.err"
        stderr_file.write_text("line 1\nline 2\nline 3\nERROR: something\n")
        meta = {"slurm_job_id": "12345", "stderr_log": str(stderr_file)}
        (tmp_path / "job_metadata.json").write_text(json.dumps(meta))

        result = check_job_log("12345", log_type="stderr")
        assert result["success"] is True
        assert result["total_lines"] == 4
        assert "ERROR: something" in result["content"]

    def test_read_stdout_via_metadata(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        stdout_file = tmp_path / "job_12345.out"
        stdout_file.write_text("Starting simulation...\nStep 1000\nDone\n")
        meta = {"slurm_job_id": "12345", "stdout_log": str(stdout_file)}
        (tmp_path / "job_metadata.json").write_text(json.dumps(meta))

        result = check_job_log("12345", log_type="stdout")
        assert result["success"] is True
        assert "Done" in result["content"]

    def test_fallback_slurm_pattern(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        # No metadata, but slurm default pattern exists
        stderr_file = tmp_path / "slurm-99999.err"
        stderr_file.write_text("some error output\n")

        result = check_job_log("99999", log_type="stderr")
        assert result["success"] is True
        assert "some error output" in result["content"]

    def test_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = check_job_log("88888", log_type="stderr")
        assert result["success"] is False
        assert len(result["errors"]) > 0

    def test_tail_lines_limit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        # Create a file with many lines
        lines = [f"line {i}" for i in range(200)]
        stderr_file = tmp_path / "slurm-77777.err"
        stderr_file.write_text("\n".join(lines) + "\n")

        result = check_job_log("77777", log_type="stderr", tail_lines=10)
        assert result["success"] is True
        assert result["total_lines"] == 200
        # Should only have last 10 lines
        content_lines = result["content"].strip().splitlines()
        assert len(content_lines) == 10
        assert "line 190" in result["content"]


# ---------------------------------------------------------------------------
# Policy Helpers
# ---------------------------------------------------------------------------


class TestParseTimeLimitSeconds:
    """Test _parse_time_limit_seconds helper."""

    def test_hhmmss(self):
        assert _parse_time_limit_seconds("24:00:00") == 86400

    def test_d_hhmmss(self):
        assert _parse_time_limit_seconds("1-12:00:00") == 129600

    def test_hhmm(self):
        assert _parse_time_limit_seconds("01:30") == 5400

    def test_minutes_only(self):
        assert _parse_time_limit_seconds("30") == 1800


class TestParseMemoryBytes:
    """Test _parse_memory_bytes helper."""

    def test_gigabytes(self):
        assert _parse_memory_bytes("128G") == 128 * 1024**3

    def test_megabytes(self):
        assert _parse_memory_bytes("64000M") == 64000 * 1024**2

    def test_no_suffix(self):
        # Assumes megabytes
        assert _parse_memory_bytes("1024") == 1024 * 1024**2


class TestIsPartitionAllowed:
    """Test _is_partition_allowed helper."""

    def test_no_policy(self):
        assert _is_partition_allowed("gpu", {}) is True

    def test_allowed_list_match(self):
        policy = {"allowed_partitions": ["gpu", "cpu"]}
        assert _is_partition_allowed("gpu", policy) is True

    def test_allowed_list_no_match(self):
        policy = {"allowed_partitions": ["gpu", "cpu"]}
        assert _is_partition_allowed("debug", policy) is False

    def test_denied_list_match(self):
        policy = {"denied_partitions": ["premium"]}
        assert _is_partition_allowed("premium", policy) is False

    def test_denied_list_no_match(self):
        policy = {"denied_partitions": ["premium"]}
        assert _is_partition_allowed("gpu", policy) is True

    def test_allowed_takes_precedence(self):
        policy = {"allowed_partitions": ["gpu"], "denied_partitions": ["gpu"]}
        # In allowed list, so allowed check passes, but also in denied list
        assert _is_partition_allowed("gpu", policy) is False


class TestValidateAgainstPolicy:
    """Test _validate_against_policy helper."""

    def test_no_violations(self):
        policy = {"max_gpus_per_job": 4, "max_nodes": 2}
        violations = _validate_against_policy(
            partition=None, gpus=2, cpus_per_task=8, nodes=1,
            time_limit="12:00:00", memory=None, policy=policy,
        )
        assert violations == []

    def test_gpu_violation(self):
        policy = {"max_gpus_per_job": 2}
        violations = _validate_against_policy(
            partition=None, gpus=4, cpus_per_task=1, nodes=1,
            time_limit="01:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 1
        assert "max_gpus_per_job" in violations[0]

    def test_partition_violation(self):
        policy = {"allowed_partitions": ["cpu"]}
        violations = _validate_against_policy(
            partition="gpu", gpus=0, cpus_per_task=1, nodes=1,
            time_limit="01:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 1
        assert "allowed_partitions" in violations[0]

    def test_time_violation(self):
        policy = {"max_time_limit": "12:00:00"}
        violations = _validate_against_policy(
            partition=None, gpus=0, cpus_per_task=1, nodes=1,
            time_limit="24:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 1
        assert "max_time_limit" in violations[0]

    def test_memory_violation(self):
        policy = {"max_memory": "64G"}
        violations = _validate_against_policy(
            partition=None, gpus=0, cpus_per_task=1, nodes=1,
            time_limit="01:00:00", memory="128G", policy=policy,
        )
        assert len(violations) == 1
        assert "max_memory" in violations[0]

    def test_multiple_violations(self):
        policy = {"max_gpus_per_job": 1, "max_nodes": 1, "max_cpus_per_task": 8}
        violations = _validate_against_policy(
            partition=None, gpus=4, cpus_per_task=32, nodes=2,
            time_limit="01:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 3

    def test_empty_policy_no_violations(self):
        violations = _validate_against_policy(
            partition="gpu", gpus=8, cpus_per_task=64, nodes=4,
            time_limit="7-00:00:00", memory="512G", policy={},
        )
        assert violations == []


# ---------------------------------------------------------------------------
# set_policy
# ---------------------------------------------------------------------------


class TestSetPolicy:
    """Test set_policy tool."""

    def test_create_new_policy(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a minimal cluster config first
        (tmp_path / ".mdclaw_cluster.json").write_text(
            json.dumps({"partitions": [], "gpu_types": []})
        )

        result = set_policy(
            allowed_partitions=["gpu", "cpu"],
            max_gpus_per_job=2,
            max_nodes=1,
            default_account="myproject",
        )
        assert result["success"] is True
        assert result["policy"]["allowed_partitions"] == ["gpu", "cpu"]
        assert result["policy"]["max_gpus_per_job"] == 2
        assert result["policy"]["defaults"]["account"] == "myproject"

        # Verify it's saved to file
        saved = json.loads((tmp_path / ".mdclaw_cluster.json").read_text())
        assert saved["policy"]["max_gpus_per_job"] == 2

    def test_merge_update(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [],
            "policy": {
                "max_gpus_per_job": 2,
                "max_nodes": 1,
                "defaults": {"account": "old_project"},
            },
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        # Only update max_gpus_per_job, other fields preserved
        result = set_policy(max_gpus_per_job=4)
        assert result["success"] is True
        assert result["policy"]["max_gpus_per_job"] == 4
        assert result["policy"]["max_nodes"] == 1  # preserved
        assert result["policy"]["defaults"]["account"] == "old_project"  # preserved

    def test_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No .mdclaw_cluster.json exists — should create one
        result = set_policy(max_gpus_per_job=1)
        assert result["success"] is True
        assert (tmp_path / ".mdclaw_cluster.json").exists()

    def test_set_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({}))

        result = set_policy(
            default_partition="gpu",
            default_account="proj123",
            default_qos="normal",
        )
        assert result["success"] is True
        assert result["policy"]["defaults"]["partition"] == "gpu"
        assert result["policy"]["defaults"]["account"] == "proj123"
        assert result["policy"]["defaults"]["qos"] == "normal"


# ---------------------------------------------------------------------------
# show_policy
# ---------------------------------------------------------------------------


class TestShowPolicy:
    """Test show_policy tool."""

    def test_with_policy(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [],
            "policy": {"max_gpus_per_job": 2, "allowed_partitions": ["gpu"]},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = show_policy()
        assert result["success"] is True
        assert result["has_policy"] is True
        assert result["policy"]["max_gpus_per_job"] == 2

    def test_without_policy(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {"partitions": []}
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = show_policy()
        assert result["success"] is True
        assert result["has_policy"] is False
        assert result["policy"] == {}

    def test_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = show_policy()
        assert result["success"] is True
        assert result["has_policy"] is False


# ---------------------------------------------------------------------------
# inspect_cluster with policy
# ---------------------------------------------------------------------------


class TestInspectClusterPolicy:
    """Test inspect_cluster preserves and applies policy."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_preserves_existing_policy(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Write existing config with policy
        existing = {
            "partitions": [],
            "policy": {"max_gpus_per_job": 2, "allowed_partitions": ["gpu"]},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(existing))

        sinfo_json = {
            "sinfo": [
                {"partition": {"name": "gpu"}, "nodes": {"total": 4}, "gres": "gpu:a100:4"},
                {"partition": {"name": "cpu"}, "nodes": {"total": 10}, "gres": ""},
            ]
        }
        mock_run.return_value = _mock_run_command(stdout=json.dumps(sinfo_json))

        result = inspect_cluster()
        assert result["success"] is True

        # Policy preserved in file
        saved = json.loads((tmp_path / ".mdclaw_cluster.json").read_text())
        assert saved["policy"]["max_gpus_per_job"] == 2
        assert saved["policy"]["allowed_partitions"] == ["gpu"]

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_filters_partitions_by_policy(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        existing = {
            "partitions": [],
            "policy": {"allowed_partitions": ["gpu"]},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(existing))

        sinfo_json = {
            "sinfo": [
                {"partition": {"name": "gpu"}, "nodes": {"total": 4}, "gres": "gpu:a100:4"},
                {"partition": {"name": "cpu"}, "nodes": {"total": 10}, "gres": ""},
            ]
        }
        mock_run.return_value = _mock_run_command(stdout=json.dumps(sinfo_json))

        result = inspect_cluster()
        assert result["success"] is True
        # Returned partitions filtered by policy
        partition_names = [p["name"] for p in result["partitions"]]
        assert "gpu" in partition_names
        assert "cpu" not in partition_names


# ---------------------------------------------------------------------------
# submit_job with policy
# ---------------------------------------------------------------------------


class TestSubmitJobPolicy:
    """Test submit_job policy validation and defaults."""

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_policy_rejects_gpu_violation(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "policy": {"max_gpus_per_job": 2},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = submit_job(script="echo test", partition="gpu", gpus=4, output_dir=str(tmp_path))
        assert result["success"] is False
        assert "policy" in result.get("message", "").lower() or "policy" in str(result.get("errors", []))

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_policy_rejects_partition_violation(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "policy": {"allowed_partitions": ["cpu"]},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = submit_job(script="echo test", partition="gpu", output_dir=str(tmp_path))
        assert result["success"] is False

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_policy_applies_defaults(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "policy": {
                "defaults": {"partition": "gpu", "account": "proj1", "qos": "normal"},
            },
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77777\n")

        result = submit_job(script="echo test", output_dir=str(tmp_path))
        assert result["success"] is True

        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --partition=gpu" in content
        assert "#SBATCH --account=proj1" in content
        assert "#SBATCH --qos=normal" in content

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_user_explicit_overrides_defaults(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}, {"name": "cpu", "gpus_per_node": 0}],
            "policy": {
                "allowed_partitions": ["gpu", "cpu"],
                "defaults": {"partition": "gpu"},
            },
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 88888\n")

        result = submit_job(script="echo test", partition="cpu", output_dir=str(tmp_path))
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --partition=cpu" in content

    @patch("servers.slurm_server.check_external_tool", return_value=True)
    @patch("servers.slurm_server.run_command")
    def test_no_policy_no_restriction(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {"partitions": [{"name": "gpu", "gpus_per_node": 4}]}
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 99999\n")

        result = submit_job(
            script="echo test", partition="gpu", gpus=8,
            nodes=4, cpus_per_task=64, output_dir=str(tmp_path),
        )
        assert result["success"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
