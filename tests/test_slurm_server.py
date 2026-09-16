"""Unit tests for SLURM server tools.

All SLURM commands are mocked — no actual SLURM installation required.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mdclaw.slurm.cluster import (
    configure_container,
    inspect_cluster,
    set_policy,
    show_policy,
)
from mdclaw.slurm.config import (
    _build_singularity_command,
    _extract_bind_paths,
    _is_partition_allowed,
    _parse_memory_bytes,
    _parse_time_limit_seconds,
    _validate_against_policy,
    mps_active_thread_percentage,
)
from mdclaw.slurm.monitor import (
    cancel_job,
    check_job,
    check_job_log,
    list_jobs,
    list_tracked_jobs,
)
from mdclaw.slurm.mps import submit_mps_job
from mdclaw.slurm.submit import (
    submit_array_job,
    submit_job,
)
from mdclaw.slurm.node_sync import _stamp_slurm_on_node
from mdclaw.slurm.tracker import (
    _append_job_record,
    _find_job_metadata,
    _read_job_records,
    _update_job_record,
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=False)
    def test_sinfo_not_available(self, mock_check):
        result = inspect_cluster()
        assert result["success"] is False
        assert "sinfo" in str(result.get("errors") or result.get("message", ""))

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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
        assert "#SBATCH --gpus=1" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_container_commands_refused_before_sbatch(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)

        for command, offending in [
            ("singularity exec -nv image.sif mdclaw run", "singularity exec"),
            ("/usr/bin/apptainer run image.sif mdclaw run", "apptainer run"),
            ("apptainer shell image.sif", "apptainer shell"),
            ("docker run --rm image mdclaw run", "docker run"),
        ]:
            result = submit_job(script=command, output_dir=str(tmp_path))

            assert result["success"] is False
            assert result["code"] == "container_command_in_script"
            assert offending in result["message"]
            assert "configure_container" in " ".join(result["hints"])

        mock_check.assert_not_called()
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_container_command_escape_hatch_warns_about_single_hyphen_nv(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 10000\n")

        result = submit_job(
            script="singularity exec -nv image.sif mdclaw run",
            allow_container_command=True,
            output_dir=str(tmp_path),
        )

        assert result["success"] is True
        assert "singularity exec" in result["message"]
        assert "'-nv'" in result["message"]
        assert "'--nv'" in result["message"]
        assert result["message"] in result["warnings"]
        mock_run.assert_called_once()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=False)
    def test_sbatch_not_available(self, mock_check):
        result = submit_job(script="echo test")
        assert result["success"] is False
        assert "sbatch" in str(result.get("errors") or result.get("message", ""))

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_sbatch_directive_newline_rejected(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = submit_job(
            script="echo test",
            partition="cpu\n#SBATCH --account=hacked",
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["code"] == "sbatch_directive_injection"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_auto_partition_from_config(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Write cluster config
        config = {"partitions": [{"name": "batch", "gpus_per_node": 0}], "gpu_types": []}
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 11111\n")

        result = submit_job(script="echo test", output_dir=str(tmp_path))
        assert result["success"] is True
        assert "Auto-selected partition" in str(result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_platform_cuda_auto_sets_gpus(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44444\n")

        result = submit_job(
            script="mdclaw --job-dir /abs/jd --node-id prod_001 run_production "
            "--simulation-time-ns 100 --platform CUDA",
            job_name="cuda_job",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --gpus=1" in content
        assert any("Auto-set --gpus 1" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_platform_cuda_auto_selects_gpu_partition(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [
                {"name": "cpu", "gpus_per_node": 0},
                {"name": "gpu", "gpus_per_node": 4},
            ],
            "gpu_types": ["a100"],
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44455\n")

        # No --gpus passed: autodetection from --platform CUDA must both flip
        # gpus to 1 and steer partition auto-selection to the GPU partition.
        result = submit_job(
            script="mdclaw run_production --platform CUDA",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --partition=gpu" in content
        assert "#SBATCH --gpus=1" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_explicit_gpus_preserved_with_platform_cuda(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44466\n")

        result = submit_job(
            script="mdclaw run_production --platform CUDA",
            gpus=2,
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --gpus=2" in content
        assert not any("Auto-set --gpus 1" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_gres_suppresses_platform_autodetect(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44477\n")

        result = submit_job(
            script="mdclaw run_production --platform CUDA",
            gres="gpu:a100:2",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --gres=gpu:a100:2" in content
        assert "--gpus=" not in content
        assert not any("Auto-set --gpus 1" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_non_gpu_platform_no_auto_gpu(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44488\n")

        # --platform CPU and --platform auto must NOT trigger a GPU allocation.
        for cmd in (
            "mdclaw run_minimization --max-iterations 5000 --platform CPU",
            "mdclaw run_minimization --max-iterations 5000",
        ):
            result = submit_job(script=cmd, output_dir=str(tmp_path))
            assert result["success"] is True
            content = Path(result["script_file"]).read_text()
            assert "--gpus=" not in content
            assert "--gres" not in content
            assert not any("Auto-set --gpus 1" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_module_load_auto_insert(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MDCLAW_MODULE_LOADS", "cuda/12.0 amber/24")
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 44444\n")

        result = submit_job(script="echo test", partition="gpu", output_dir=str(tmp_path))
        assert result["success"] is True
        content = Path(result["script_file"]).read_text()
        assert "module load cuda/12.0" in content
        assert "module load amber/24" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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
            _mock_run_command(stdout=""),  # controller record expired
            _mock_run_command(stdout=json.dumps(sacct_json)),
        ]

        result = check_job("12345")
        assert result["success"] is True
        assert result["state"] == "COMPLETED"
        assert result["exit_code"] == "0"

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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
            _mock_run_command(stdout=""),  # controller record expired
            _mock_run_command(stdout=json.dumps(sacct_json)),
        ]

        result = check_job("67890")
        assert result["success"] is True
        assert result["state"] == "FAILED"
        assert "Segmentation fault" in result["stderr_tail"]

    @patch("mdclaw.slurm._base.check_external_tool", return_value=False)
    def test_squeue_not_available(self, mock_check):
        result = check_job("99999")
        assert result["success"] is False
        assert result["code"] == "slurm_status_unavailable"
        assert "no Slurm client" in result["message"]
        assert "MDCLAW_SLURM_PATH" in result["next_action"]


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    """Test list_jobs tool."""

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_list_jobs(self, mock_run, mock_check):
        squeue_json = {
            "jobs": [
                {
                    "job_id": 111,
                    "name": "md_production",
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
        assert result["jobs"][0]["name"] == "md_production"
        assert result["jobs"][1]["state"] == "PENDING"

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_cancel_success(self, mock_run, mock_check):
        mock_run.return_value = _mock_run_command(stdout="")

        result = cancel_job("12345")
        assert result["success"] is True
        assert "cancelled" in result["message"]

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_cancel_invalid_id(self, mock_run, mock_check):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "scancel", stderr="Invalid job id"
        )

        result = cancel_job("99999")
        assert result["success"] is False
        assert len(result["errors"]) > 0

    @patch("mdclaw.slurm._base.check_external_tool", return_value=False)
    def test_scancel_not_available(self, mock_check):
        result = cancel_job("12345")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# check_job_log
# ---------------------------------------------------------------------------


class TestFindJobMetadata:
    """Test the metadata search that check_job and check_job_log rely on."""

    def test_finds_metadata_in_subdirectory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        output_dir = tmp_path / "job_output"
        output_dir.mkdir()
        meta = {"slurm_job_id": "12345", "stderr_log": "job_12345.err"}
        (output_dir / "job_metadata.json").write_text(json.dumps(meta))

        assert _find_job_metadata("12345") == meta

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable directories")
    def test_unreadable_sibling_directory_does_not_abort_the_search(
        self, tmp_path, monkeypatch
    ):
        """A shared login node's /tmp holds other users' 700 directories."""
        monkeypatch.chdir(tmp_path)
        unreadable = tmp_path / "someone_elses_scratch"
        unreadable.mkdir(mode=0o700)
        os.chmod(unreadable, 0o000)
        output_dir = tmp_path / "job_output"
        output_dir.mkdir()
        meta = {"slurm_job_id": "12345"}
        (output_dir / "job_metadata.json").write_text(json.dumps(meta))
        try:
            assert _find_job_metadata("12345") == meta
            assert _find_job_metadata("99999") is None
        finally:
            os.chmod(unreadable, 0o700)


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
        assert violations[0]["field"] == "gpus"
        assert violations[0]["severity"] == "error"
        assert violations[0]["code"] == "policy_gpus_exceeded"

    def test_partition_violation(self):
        policy = {"allowed_partitions": ["cpu"]}
        violations = _validate_against_policy(
            partition="gpu", gpus=0, cpus_per_task=1, nodes=1,
            time_limit="01:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 1
        assert violations[0]["field"] == "partition"
        assert violations[0]["severity"] == "error"
        assert violations[0]["code"] == "policy_partition_not_allowed"

    def test_time_violation(self):
        policy = {"max_time_limit": "12:00:00"}
        violations = _validate_against_policy(
            partition=None, gpus=0, cpus_per_task=1, nodes=1,
            time_limit="24:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 1
        assert violations[0]["field"] == "time_limit"
        assert violations[0]["severity"] == "error"
        assert violations[0]["code"] == "policy_time_exceeded"

    def test_memory_violation(self):
        policy = {"max_memory": "64G"}
        violations = _validate_against_policy(
            partition=None, gpus=0, cpus_per_task=1, nodes=1,
            time_limit="01:00:00", memory="128G", policy=policy,
        )
        assert len(violations) == 1
        assert violations[0]["field"] == "memory"
        assert violations[0]["severity"] == "error"
        assert violations[0]["code"] == "policy_memory_exceeded"

    def test_multiple_violations(self):
        policy = {"max_gpus_per_job": 1, "max_nodes": 1, "max_cpus_per_task": 8}
        violations = _validate_against_policy(
            partition=None, gpus=4, cpus_per_task=32, nodes=2,
            time_limit="01:00:00", memory=None, policy=policy,
        )
        assert len(violations) == 3
        assert {violation["field"] for violation in violations} == {"gpus", "cpus_per_task", "nodes"}

    def test_empty_policy_no_violations(self):
        violations = _validate_against_policy(
            partition="gpu", gpus=8, cpus_per_task=64, nodes=4,
            time_limit="7-00:00:00", memory="512G", policy={},
        )
        assert violations == []

    def test_invalid_time_format_becomes_warning(self):
        policy = {"max_time_limit": "12:00:00"}
        violations = _validate_against_policy(
            partition=None, gpus=0, cpus_per_task=1, nodes=1,
            time_limit="not-a-time", memory=None, policy=policy,
        )
        assert len(violations) == 1
        assert violations[0]["field"] == "time_limit"
        assert violations[0]["severity"] == "warning"
        assert violations[0]["code"] == "policy_time_unparseable"


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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_policy_rejects_gpu_violation(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "policy": {"max_gpus_per_job": 2},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = submit_job(script="echo test", partition="gpu", gpus=4, output_dir=str(tmp_path))
        assert result["success"] is False
        assert result["error_type"] == "ValidationError"
        assert "max_gpus_per_job" in result.get("message", "")
        assert any("Lower --gpus to 2 or less." in hint for hint in result.get("hints", []))

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_policy_rejects_partition_violation(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "policy": {"allowed_partitions": ["cpu"]},
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = submit_job(script="echo test", partition="gpu", output_dir=str(tmp_path))
        assert result["success"] is False
        assert result["error_type"] == "ValidationError"
        assert any("allowed partitions" in hint.lower() for hint in result.get("hints", []))

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
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


# ---------------------------------------------------------------------------
# Container helpers
# ---------------------------------------------------------------------------


class TestExtractBindPaths:
    """Test _extract_bind_paths helper."""

    def test_file_args(self, tmp_path):
        cmd = f"mdclaw run_md --system-xml-file {tmp_path}/sys.system.xml --topology-pdb-file {tmp_path}/sys.topology.pdb"
        paths = _extract_bind_paths(cmd)
        assert str(tmp_path) in paths

    def test_dir_args(self, tmp_path):
        cmd = f"mdclaw run_md --output-dir {tmp_path}/output"
        paths = _extract_bind_paths(cmd)
        assert any("output" in p for p in paths)

    def test_no_file_args(self):
        paths = _extract_bind_paths("echo hello world")
        assert paths == []


class TestBuildSingularityCommand:
    """Test _build_singularity_command helper."""

    def test_plumed_input_and_source_overlay_are_preserved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        script = inputs / "plumed.dat"
        script.write_text("d: DISTANCE ATOMS=1,2\n")
        source = tmp_path / "source"
        source.mkdir()
        command = f"mdclaw run_production --plumed-file {script} --steering-time-ns 0.002"
        result = _build_singularity_command(command, {
            "image": "/opt/mdclaw-plumed.sif", "extra_flags": "--nv",
            "source_mode": "overlay", "source_root": str(source),
        }, str(tmp_path / "output"))
        assert command in result
        assert str(inputs) in result.split("--bind ")[1].split()[0].split(",")
        assert f"--env PYTHONPATH={source}" in result
        assert "--nv" in result and "/opt/mdclaw-plumed.sif" in result

    def test_basic_wrap(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        container = {"image": "/opt/mdclaw.sif"}
        result = _build_singularity_command("mdclaw --list", container, str(tmp_path))
        assert result.startswith("singularity exec")
        assert "/opt/mdclaw.sif" in result
        assert "mdclaw --list" in result

    def test_nv_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        container = {"image": "/opt/mdclaw.sif", "extra_flags": "--nv"}
        result = _build_singularity_command("mdclaw --list", container, str(tmp_path))
        assert "--nv" in result

    def test_bind_paths(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        container = {
            "image": "/opt/mdclaw.sif",
            "bind_paths": ["/scratch", "/data"],
        }
        result = _build_singularity_command("mdclaw --list", container, str(tmp_path))
        assert "/scratch" in result
        assert "/data" in result
        assert "--bind" in result

    def test_auto_extracts_file_args(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        container = {"image": "/opt/mdclaw.sif"}
        cmd = f"mdclaw run_md --system-xml-file {tmp_path}/sys.system.xml"
        result = _build_singularity_command(cmd, container, str(tmp_path))
        assert str(tmp_path) in result


# ---------------------------------------------------------------------------
# configure_container
# ---------------------------------------------------------------------------


class TestConfigureContainer:
    """Test configure_container tool."""

    @pytest.mark.parametrize("flags", ["-nv", "--cleanenv -nv", "'-nv'", "'--nv"])
    def test_invalid_flags_leave_saved_config_unchanged(self, tmp_path, monkeypatch, flags):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".mdclaw_cluster.json"
        original = json.dumps({"container": {"image": "/opt/mdclaw.sif", "extra_flags": "--nv"}})
        path.write_text(original)
        result = configure_container(extra_flags=flags)
        assert result["success"] is False
        assert result["code"] == "container_extra_flags_invalid"
        assert "--extra-flags=--nv" in " ".join(result["hints"])
        assert path.read_text() == original

    def test_legacy_flags_can_be_repaired(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".mdclaw_cluster.json"
        path.write_text(json.dumps({"container": {"image": "/opt/mdclaw.sif", "extra_flags": "-nv"}}))
        assert configure_container(bind_paths=["/scratch"])["code"] == "container_extra_flags_invalid"
        assert configure_container(extra_flags="--nv")["success"] is True
        assert json.loads(path.read_text())["container"]["extra_flags"] == "--nv"

    def test_set_image(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({}))

        result = configure_container(image="/opt/mdclaw.sif")
        assert result["success"] is True
        assert result["container"]["image"] == "/opt/mdclaw.sif"

        saved = json.loads((tmp_path / ".mdclaw_cluster.json").read_text())
        assert saved["container"]["image"] == "/opt/mdclaw.sif"

    def test_set_with_binds_and_flags(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({}))

        result = configure_container(
            image="/opt/mdclaw.sif",
            bind_paths=["/scratch", "/data"],
            extra_flags="--nv",
        )
        assert result["success"] is True
        assert result["container"]["bind_paths"] == ["/scratch", "/data"]
        assert result["container"]["extra_flags"] == "--nv"

    def test_disable(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {"container": {"image": "/opt/mdclaw.sif"}}
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = configure_container(disable=True)
        assert result["success"] is True

        saved = json.loads((tmp_path / ".mdclaw_cluster.json").read_text())
        assert "container" not in saved

    def test_no_image_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({}))

        result = configure_container(bind_paths=["/scratch"])
        assert result["success"] is False
        assert "image" in str(result["errors"]).lower()

    def test_merge_update(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "container": {
                "image": "/opt/mdclaw.sif",
                "extra_flags": "--nv",
            }
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))

        result = configure_container(bind_paths=["/scratch"])
        assert result["success"] is True
        assert result["container"]["image"] == "/opt/mdclaw.sif"
        assert result["container"]["extra_flags"] == "--nv"
        assert result["container"]["bind_paths"] == ["/scratch"]

    def test_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = configure_container(image="/opt/mdclaw.sif")
        assert result["success"] is True
        assert (tmp_path / ".mdclaw_cluster.json").exists()


# ---------------------------------------------------------------------------
# Container execution in submit_job
# ---------------------------------------------------------------------------


class TestContainerExecution:
    """Test that submit_job wraps commands with singularity when configured."""

    @pytest.mark.parametrize("array", [False, True])
    @pytest.mark.parametrize("environment", [None, "module load amber"])
    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_legacy_bad_flags_block_only_container_jobs(
        self, mock_run, mock_check, tmp_path, monkeypatch, array, environment,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "container": {"image": "/opt/mdclaw.sif", "extra_flags": "-nv"},
        }))
        jd = _make_job_with_nodes(tmp_path, "job_bad_flags", ["prod_001"])
        node_path = jd / "nodes" / "prod_001" / "node.json"
        original = node_path.read_bytes()
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 10001\n")
        kwargs = dict(partition="gpu", output_dir=str(tmp_path), environment=environment)
        if array:
            result = submit_array_job(tasks=[{
                "job_dir": str(jd), "node_id": "prod_001", "command": "echo test",
            }], **kwargs)
        else:
            result = submit_job(script="echo test", job_dir=str(jd), node_id="prod_001", **kwargs)
        if environment:
            assert result["success"] is True, result
            assert "singularity exec" not in Path(result["script_file"]).read_text()
        else:
            assert result["success"] is False
            assert result["code"] == "container_extra_flags_invalid"
            mock_run.assert_not_called()
            assert node_path.read_bytes() == original
            assert not list(tmp_path.glob("*.sbatch"))

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_container_wrap(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "container": {
                "image": "/opt/containers/mdclaw.sif",
                "extra_flags": "--nv",
                "bind_paths": ["/scratch"],
            },
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 10001\n")

        result = submit_job(
            script="mdclaw run_production --system-xml-file /data/sys.system.xml",
            partition="gpu",
            gpus=1,
            output_dir=str(tmp_path),
        )
        assert result["success"] is True

        content = Path(result["script_file"]).read_text()
        assert "singularity exec" in content
        assert "--nv" in content
        assert "/opt/containers/mdclaw.sif" in content
        assert "/scratch" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_environment_overrides_container(self, mock_run, mock_check, tmp_path, monkeypatch):
        """When environment is explicitly set, container wrapping is skipped."""
        monkeypatch.chdir(tmp_path)
        config = {
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "container": {
                "image": "/opt/containers/mdclaw.sif",
                "extra_flags": "--nv",
            },
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 10002\n")

        result = submit_job(
            script="echo test",
            partition="gpu",
            environment="module load cuda/12.0\nmodule load amber/24",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True

        content = Path(result["script_file"]).read_text()
        assert "singularity exec" not in content
        assert "module load cuda/12.0" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_no_container_no_wrap(self, mock_run, mock_check, tmp_path, monkeypatch):
        """Without container config, commands are not wrapped."""
        monkeypatch.chdir(tmp_path)
        config = {"partitions": [{"name": "gpu", "gpus_per_node": 4}]}
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(config))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 10003\n")

        result = submit_job(
            script="echo test",
            partition="gpu",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True

        content = Path(result["script_file"]).read_text()
        assert "singularity" not in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_inspect_preserves_container(self, mock_run, mock_check, tmp_path, monkeypatch):
        """inspect_cluster should preserve existing container config."""
        monkeypatch.chdir(tmp_path)
        existing = {
            "partitions": [],
            "container": {
                "image": "/opt/mdclaw.sif",
                "extra_flags": "--nv",
            },
        }
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps(existing))

        sinfo_json = {
            "sinfo": [{"partition": {"name": "gpu"}, "nodes": {"total": 4}, "gres": "gpu:a100:4"}]
        }
        mock_run.return_value = _mock_run_command(stdout=json.dumps(sinfo_json))

        result = inspect_cluster()
        assert result["success"] is True

        saved = json.loads((tmp_path / ".mdclaw_cluster.json").read_text())
        assert saved["container"]["image"] == "/opt/mdclaw.sif"
        assert saved["container"]["extra_flags"] == "--nv"


# ---------------------------------------------------------------------------
# Job Tracker (JSONL)
# ---------------------------------------------------------------------------


class TestJobTracker:
    """Test JSONL job tracking helpers."""

    def test_append_and_read(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _append_job_record({"job_id": "111", "status": "SUBMITTED"})
        _append_job_record({"job_id": "222", "status": "SUBMITTED"})

        records = _read_job_records()
        assert len(records) == 2
        assert records[0]["job_id"] == "111"
        assert records[1]["job_id"] == "222"

    def test_update_record(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _append_job_record({"job_id": "111", "status": "SUBMITTED"})
        _append_job_record({"job_id": "222", "status": "SUBMITTED"})

        _update_job_record("111", {"status": "RUNNING", "node": "gpu-01", "elapsed": "00:05:00"})

        records = _read_job_records()
        assert records[0]["status"] == "RUNNING"
        assert records[0]["node"] == "gpu-01"
        assert records[0]["elapsed"] == "00:05:00"
        assert "checked_at" in records[0]
        # Other record unchanged
        assert records[1]["status"] == "SUBMITTED"

    def test_update_preserves_fields(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _append_job_record({"job_id": "111", "status": "RUNNING", "node": "gpu-01"})
        _update_job_record("111", {"status": "COMPLETED", "exit_code": "0:0"})

        rec = _read_job_records()[0]
        assert rec["status"] == "COMPLETED"
        assert rec["node"] == "gpu-01"  # preserved
        assert rec["exit_code"] == "0:0"

    def test_read_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _read_job_records() == []

    def test_read_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _read_job_records() == []

    def test_list_tracked_jobs_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = list_tracked_jobs()
        assert result["success"] is True
        assert result["total"] == 0
        assert result["jobs"] == []

    def test_list_tracked_jobs_with_records(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _append_job_record({"job_id": "111", "status": "COMPLETED"})
        _append_job_record({"job_id": "222", "status": "RUNNING"})

        result = list_tracked_jobs()
        assert result["success"] is True
        assert result["total"] == 2
        # Newest first
        assert result["jobs"][0]["job_id"] == "222"
        assert result["jobs"][1]["job_id"] == "111"

    @patch("mdclaw.slurm._base.run_command")
    def test_submit_job_writes_tracker(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 99999")

        with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
             patch("mdclaw.slurm.submit._load_cluster_config", return_value=None):
            result = submit_job(script="echo hello", output_dir=str(tmp_path))

        assert result["success"] is True
        records = _read_job_records()
        assert len(records) == 1
        assert records[0]["job_id"] == "99999"
        assert records[0]["status"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# Node integration: submit_job + check_job + submit_array_job
# ---------------------------------------------------------------------------


def _make_job_with_nodes(tmp_path, job_name: str, node_ids: list[str]) -> Path:
    """Build a minimal schema-v3 job_dir with the requested nodes pre-created.

    Avoids importing the heavy node_server.create_node path — we only need
    the files ``_stamp_slurm_on_node`` and ``_sync_slurm_state_to_node``
    read/write (node.json + progress.json index).
    """
    jd = tmp_path / job_name
    jd.mkdir()
    (jd / "nodes").mkdir()

    progress = {
        "schema_version": 3,
        "job_id": "testjob",
        "nodes": {},
        "params": {"execution_mode": "autonomous"},
    }

    for nid in node_ids:
        node_dir = jd / "nodes" / nid
        node_dir.mkdir()
        (node_dir / "artifacts").mkdir()
        (node_dir / "node.json").write_text(json.dumps({
            "node_id": nid,
            "type": nid.split("_")[0],
            "status": "pending",
            "parents": [],
            "metadata": {},
            "artifacts": {},
            "warnings": [],
        }))
        progress["nodes"][nid] = {"type": nid.split("_")[0], "status": "pending", "parents": []}

    (jd / "progress.json").write_text(json.dumps(progress))
    return jd


class TestSubmitJobNodeIntegration:
    """submit_job: stamps node.json when --job-dir / --node-id are passed."""

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_stamps_node_metadata(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_x", ["prod_001"])
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77777\n")

        result = submit_job(
            script=f"mdclaw --job-dir {jd} --node-id prod_001 run_production --simulation-time-ns 0.1",
            job_dir=str(jd),
            node_id="prod_001",
            partition="gpu",
            gpus=1,
            output_dir=str(tmp_path),
        )
        assert result["success"] is True, result
        assert result["slurm_job_id"] == "77777"
        assert result["job_dir"] == str(jd)
        assert result["node_id"] == "prod_001"

        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["metadata"]["slurm_job_id"] == "77777"
        assert node["metadata"]["slurm_script_file"].endswith(".sbatch")
        assert "slurm_submitted_at" in node["metadata"]
        # For a non-array submit_job, parent == child, so chain deps can
        # still read a single stable id off the node.
        assert node["metadata"]["slurm_parent_job_id"] == "77777"
        assert node["status"] == "queued"

        # progress.json index should mirror status
        prog = json.loads((jd / "progress.json").read_text())
        assert prog["nodes"]["prod_001"]["status"] == "queued"

        # Tracker record carries job_dir / node_id
        records = _read_job_records()
        assert len(records) == 1
        assert records[0]["job_dir"] == str(jd.resolve())
        assert records[0]["node_id"] == "prod_001"

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm.node_sync.update_node_status")
    @patch("mdclaw.slurm._base.run_command")
    def test_stamp_status_failure_rolls_back_metadata(
        self, mock_run, mock_update_status, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_stamp_status_fail", ["prod_001"])
        commands = []

        def command_side_effect(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == "sbatch":
                return _mock_run_command(stdout="Submitted batch job 77777\n")
            if cmd[0] == "scancel":
                return _mock_run_command(stdout="")
            raise AssertionError(f"unexpected command: {cmd}")

        mock_run.side_effect = command_side_effect
        mock_update_status.return_value = {
            "success": False,
            "error": "progress lock failed",
        }

        result = submit_job(
            script="echo test",
            job_dir=str(jd),
            node_id="prod_001",
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["slurm_job_id"] == "77777"
        assert result["errors"] == [
            "could not mark node prod_001 queued: progress lock failed"
        ]
        assert ["scancel", "77777"] in commands
        assert _read_job_records() == []
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        for key in (
            "slurm_job_id",
            "slurm_parent_job_id",
            "slurm_array_task_id",
            "slurm_submission_intent_id",
        ):
            assert key not in node["metadata"]
        assert node["status"] == "pending"

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    def test_node_id_without_job_dir_is_rejected(self, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = submit_job(
            script="echo test",
            node_id="prod_001",  # missing job_dir
            output_dir=str(tmp_path),
        )
        assert result["success"] is False
        # Failure comes back as a structured validation error, not a raw string.
        assert "job_dir" in json.dumps(result, default=str)

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_missing_node_rejected_before_submission(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd = tmp_path / "empty_job"
        jd.mkdir()
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 11111\n")

        result = submit_job(
            script="echo test",
            job_dir=str(jd),
            node_id="prod_001",
            output_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert result["code"] == "slurm_node_unavailable"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_terminal_node_rejected_before_submission(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_terminal", ["prod_001"])
        node_file = jd / "nodes" / "prod_001" / "node.json"
        node = json.loads(node_file.read_text())
        node["status"] = "failed"
        node_file.write_text(json.dumps(node))

        result = submit_job(
            script="echo test",
            job_dir=str(jd),
            node_id="prod_001",
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["code"] == "node_terminal"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_existing_slurm_job_id_rejected_before_submission(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_duplicate", ["prod_001"])
        node_file = jd / "nodes" / "prod_001" / "node.json"
        node = json.loads(node_file.read_text())
        node["metadata"]["slurm_job_id"] = "77777"
        node_file.write_text(json.dumps(node))

        result = submit_job(
            script="echo test",
            job_dir=str(jd),
            node_id="prod_001",
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["code"] == "slurm_node_already_submitted"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_in_flight_submission_marker_blocks_second_submitter(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_race", ["prod_001"])
        second_result = {}

        def side_effect(cmd, **kwargs):
            assert cmd[0] == "sbatch"
            second_result["result"] = submit_job(
                script="echo second",
                job_name="second_submitter",
                job_dir=str(jd),
                node_id="prod_001",
                output_dir=str(tmp_path),
            )
            return _mock_run_command(stdout="Submitted batch job 88888\n")

        mock_run.side_effect = side_effect

        first = submit_job(
            script="echo first",
            job_name="first_submitter",
            job_dir=str(jd),
            node_id="prod_001",
            output_dir=str(tmp_path),
        )

        assert first["success"] is True, first
        assert second_result["result"]["success"] is False
        assert second_result["result"]["code"] == "slurm_node_submission_in_progress"
        assert mock_run.call_count == 1
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["metadata"]["slurm_job_id"] == "88888"
        assert "slurm_submission_intent_id" not in node["metadata"]

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_malformed_sbatch_job_id_rejected(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 12345; touch x\n")

        result = submit_job(
            script="echo test",
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["slurm_job_id"] is None
        assert "Could not parse sbatch output" in result["errors"][0]

    def test_stamp_rolls_back_metadata_when_queue_status_update_fails(
        self, tmp_path
    ):
        from mdclaw.slurm import node_sync as slurm_mod

        jd = _make_job_with_nodes(tmp_path, "job_stamp_status_fail", ["prod_001"])
        intent_id = "intent-1"
        error, _prior_status = slurm_mod._reserve_slurm_submission_on_node(
            str(jd),
            "prod_001",
            intent_id,
            kind="array",
            array_task_id=0,
        )
        assert error is None

        with patch(
            "mdclaw.slurm.node_sync.update_node_status",
            return_value={"success": False, "message": "status update failed"},
        ):
            stamp_err = slurm_mod._stamp_slurm_on_node(
                str(jd),
                "prod_001",
                "99999_0",
                script_file=str(tmp_path / "job.sbatch"),
                stdout_log=str(tmp_path / "job.out"),
                stderr_log=str(tmp_path / "job.err"),
                array_task_id=0,
                parent_job_id="99999",
                submission_intent_id=intent_id,
            )

        assert "could not mark node prod_001 queued" in stamp_err
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        for key in (
            "slurm_job_id",
            "slurm_parent_job_id",
            "slurm_array_task_id",
            "slurm_submission_intent_id",
        ):
            assert key not in node["metadata"]
        assert node["status"] == "pending"


class TestCheckJobNodeSync:
    """check_job: reflects SLURM state back onto the linked DAG node."""

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_failed_state_fails_node(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_failed", ["prod_001"])
        stdout_file = tmp_path / "job_55555.out"
        stderr_file = tmp_path / "job_55555.err"
        stdout_file.write_text("starting mdclaw\nlast stdout line\n")
        stderr_file.write_text("initial stderr\nfatal slurm stderr\n")

        # Pre-seed tracker so check_job can find the node linkage
        _append_job_record({
            "job_id": "55555",
            "job_name": "test",
            "status": "RUNNING",
            "job_dir": str(jd),
            "node_id": "prod_001",
            "stdout_log": str(stdout_file),
            "stderr_log": str(stderr_file),
        })

        # Mark the node queued first (submit_job would have done this)
        (jd / "nodes" / "prod_001" / "node.json").write_text(json.dumps({
            "node_id": "prod_001",
            "type": "prod",
            "status": "queued",
            "parents": [],
            "metadata": {"slurm_job_id": "55555"},
            "artifacts": {},
            "warnings": [],
        }))

        # Mock squeue (no record) then sacct returning FAILED
        def side_effect(cmd, **kwargs):
            if cmd[0] == "squeue":
                # squeue has no record for a completed-failed job; raise CPE like real slurm
                raise subprocess.CalledProcessError(1, cmd)
            # sacct --json returns FAILED
            return _mock_run_command(stdout=json.dumps({
                "jobs": [{
                    "state": {"current": ["FAILED"]},
                    "nodes": "compute01",
                    "exit_code": {"return_code": 1},
                    "time": {"elapsed": "00:01:23"},
                }]
            }))
        mock_run.side_effect = side_effect

        result = check_job("55555")
        assert result["state"] == "FAILED"

        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["status"] == "failed"
        assert node["metadata"].get("failure_code") == "slurm_failed"
        manifest = jd / "nodes" / "prod_001" / node["artifacts"]["failure"]
        manifest_data = json.loads(manifest.read_text())
        assert manifest_data["tool"] == "slurm"
        assert manifest_data["code"] == "slurm_failed"
        tool_result = json.loads((manifest.parent / "tool_result.json").read_text())
        assert tool_result["context"]["slurm_state"] == "FAILED"
        assert "last stdout line" in (manifest.parent / "stdout_tail.txt").read_text()
        assert "fatal slurm stderr" in (manifest.parent / "stderr_tail.txt").read_text()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_running_state_advances_queued_node(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_running", ["prod_001"])

        # Tracker records the node linkage
        _append_job_record({
            "job_id": "66666",
            "status": "SUBMITTED",
            "job_dir": str(jd),
            "node_id": "prod_001",
        })
        # Simulate submit_job having set the node to queued
        (jd / "nodes" / "prod_001" / "node.json").write_text(json.dumps({
            "node_id": "prod_001",
            "type": "prod",
            "status": "queued",
            "parents": [],
            "metadata": {"slurm_job_id": "66666"},
            "artifacts": {},
            "warnings": [],
        }))

        mock_run.return_value = _mock_run_command(stdout=json.dumps({
            "jobs": [{
                "job_id": 66666,
                "job_state": ["RUNNING"],
                "nodes": "compute02",
                "time": {"elapsed": "00:00:10"},
            }]
        }))

        result = check_job("66666")
        assert result["state"] == "RUNNING"

        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["status"] == "running"

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_completed_state_without_node_completion_marks_zombie_failed(
        self, mock_run, mock_check, tmp_path, monkeypatch,
    ):
        """SLURM COMPLETED with a still-running node is a zombie wrapper exit."""
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_ok", ["prod_001"])
        stdout_file = tmp_path / "job_88888.out"
        stderr_file = tmp_path / "job_88888.err"
        stdout_file.write_text("wrapper start\nwrapper exited without complete_node\n")
        stderr_file.write_text("no python traceback, but node stayed running\n")

        _append_job_record({
            "job_id": "88888",
            "status": "RUNNING",
            "job_dir": str(jd),
            "node_id": "prod_001",
            "stdout_log": str(stdout_file),
            "stderr_log": str(stderr_file),
        })

        # Node starts as "running" — but the tool hasn't called complete_node yet
        (jd / "nodes" / "prod_001" / "node.json").write_text(json.dumps({
            "node_id": "prod_001",
            "type": "prod",
            "status": "running",
            "parents": [],
            "metadata": {"slurm_job_id": "88888"},
            "artifacts": {},
            "warnings": [],
        }))

        def side_effect(cmd, **kwargs):
            if cmd[0] == "squeue":
                raise subprocess.CalledProcessError(1, cmd)
            return _mock_run_command(stdout=json.dumps({
                "jobs": [{
                    "state": {"current": ["COMPLETED"]},
                    "nodes": "compute03",
                    "exit_code": {"return_code": 0},
                    "time": {"elapsed": "00:05:00"},
                }]
            }))
        mock_run.side_effect = side_effect

        result = check_job("88888")
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["status"] == "failed"
        assert node["metadata"]["failure_code"] == "slurm_completed_without_node_completion"
        manifest = jd / "nodes" / "prod_001" / node["artifacts"]["failure"]
        manifest_data = json.loads(manifest.read_text())
        assert manifest_data["tool"] == "slurm"
        assert manifest_data["code"] == "slurm_completed_without_node_completion"
        tool_result = json.loads((manifest.parent / "tool_result.json").read_text())
        assert tool_result["context"]["slurm_state"] == "COMPLETED"
        assert "wrapper exited" in (manifest.parent / "stdout_tail.txt").read_text()
        assert "node stayed running" in (manifest.parent / "stderr_tail.txt").read_text()
        assert any("marked failed" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_completed_node_keeps_slurm_observation_in_events(
        self, mock_run, mock_check, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_completed_sealed", ["prod_001"])

        _append_job_record({
            "job_id": "99999",
            "status": "RUNNING",
            "job_dir": str(jd),
            "node_id": "prod_001",
        })

        (jd / "nodes" / "prod_001" / "node.json").write_text(json.dumps({
            "node_id": "prod_001",
            "type": "prod",
            "status": "completed",
            "parents": [],
            "metadata": {"final_step": 1000},
            "artifacts": {"trajectory": "artifacts/trajectory.dcd"},
            "warnings": [],
        }))

        def side_effect(cmd, **kwargs):
            if cmd[0] == "squeue":
                raise subprocess.CalledProcessError(1, cmd)
            return _mock_run_command(stdout=json.dumps({
                "jobs": [{
                    "state": {"current": ["COMPLETED"]},
                    "nodes": "compute03",
                    "exit_code": {"return_code": 0},
                    "time": {"elapsed": "00:05:00"},
                }]
            }))
        mock_run.side_effect = side_effect

        result = check_job("99999")

        assert result["state"] == "COMPLETED"
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["metadata"] == {"final_step": 1000}
        event_files = list((jd / "events").glob("*slurm_observed*"))
        assert len(event_files) == 1
        event = json.loads(event_files[0].read_text())
        assert event["details"]["slurm_state"] == "COMPLETED"
        assert event["details"]["slurm_elapsed"] == "00:05:00"

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_tracker_lookup_can_use_job_dir_not_cwd(
        self, mock_run, mock_check, tmp_path, monkeypatch,
    ):
        monitor_dir = tmp_path / "monitor"
        monitor_dir.mkdir()
        monkeypatch.chdir(monitor_dir)
        jd = _make_job_with_nodes(tmp_path, "job_tracker_elsewhere", ["prod_001"])

        # Simulate a tracker written next to the job_dir, not in cwd.
        (jd / ".mdclaw_jobs.jsonl").write_text(json.dumps({
            "job_id": "77788",
            "status": "SUBMITTED",
            "job_dir": str(jd),
            "node_id": "prod_001",
        }) + "\n")

        mock_run.return_value = _mock_run_command(stdout=json.dumps({
            "jobs": [{
                "job_id": 77788,
                "job_state": ["RUNNING"],
                "nodes": "compute04",
                "time": {"elapsed": "00:00:20"},
            }]
        }))

        result = check_job("77788", job_dir=str(jd))
        assert result["success"] is True
        assert result["state"] == "RUNNING"
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert node["status"] == "running"


class TestSubmitArrayJob:
    """submit_array_job: one sbatch with --array, one node per task."""

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_three_nodes_one_sbatch(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Three jobs, each with its own prod_001
        jds = [
            _make_job_with_nodes(tmp_path, f"job_{i}", ["prod_001"])
            for i in range(3)
        ]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 99999\n")

        result = submit_array_job(
            tasks=[
                {
                    "job_dir": str(jd),
                    "node_id": "prod_001",
                    "command": f"mdclaw --job-dir {jd} --node-id prod_001 run_production --simulation-time-ns 0.1",
                }
                for jd in jds
            ],
            job_name="prod_batch",
            partition="gpu",
            gpus=1,
            time_limit="00:30:00",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True, result
        assert result["parent_job_id"] == "99999"
        assert result["array_spec"] == "0-2"
        assert len(result["tasks"]) == 3

        # Child IDs follow SLURM's JOBID_TASKID form
        for idx, task in enumerate(result["tasks"]):
            assert task["slurm_job_id"] == f"99999_{idx}"
            assert task["array_task_id"] == idx
            assert task["job_dir"] == str(jds[idx].resolve())
            assert task["node_id"] == "prod_001"

        # Each node.json stamped
        for idx, jd in enumerate(jds):
            node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
            assert node["metadata"]["slurm_job_id"] == f"99999_{idx}"
            assert node["metadata"]["slurm_array_task_id"] == idx
            # parent_job_id lets downstream chain jobs read the array parent
            # off the node and build --dependency=aftercorr:<parent> without
            # having to consult the JSONL tracker.
            assert node["metadata"]["slurm_parent_job_id"] == "99999"
            assert node["status"] == "queued"

        # Generated sbatch contains --array, case statement, one arm per task
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --array=0-2" in content
        assert 'case "$SLURM_ARRAY_TASK_ID" in' in content
        for idx in range(3):
            assert f"  {idx})" in content
        # Log file pattern uses SLURM %A_%a substitutions
        assert "_%A_%a.out" in content or "%A_%a.out" in content

        # Tracker contains 3 rows (one per child)
        records = _read_job_records()
        ids = [r["job_id"] for r in records]
        assert ids == ["99999_0", "99999_1", "99999_2"]

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_array_container_command_refused_before_sbatch(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)

        result = submit_array_job(
            tasks=[{
                "job_dir": str(tmp_path / "unused_job"),
                "node_id": "prod_001",
                "command": "docker run image mdclaw run_production",
            }],
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["code"] == "container_command_in_script"
        assert "docker run" in result["message"]
        mock_check.assert_not_called()
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_array_platform_cuda_auto_sets_gpus(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [
            _make_job_with_nodes(tmp_path, f"job_{i}", ["prod_001"])
            for i in range(2)
        ]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 91919\n")

        # No --gpus passed; a --platform CUDA task command must flip the whole
        # array to --gpus 1.
        result = submit_array_job(
            tasks=[
                {
                    "job_dir": str(jd),
                    "node_id": "prod_001",
                    "command": f"mdclaw --job-dir {jd} --node-id prod_001 "
                    "run_production --simulation-time-ns 0.1 --platform CUDA",
                }
                for jd in jds
            ],
            job_name="cuda_batch",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True, result
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --gpus=1" in content
        assert any("Auto-set --gpus 1" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_array_banner_quotes_job_and_node_values(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 123\n")
        jd = _make_job_with_nodes(tmp_path, "job; touch pwned", ["prod_001; touch pwned"])
        injected_node = "prod_001; touch pwned"

        result = submit_array_job(
            tasks=[{
                "job_dir": str(jd),
                "node_id": injected_node,
                "command": "echo safe-command",
            }],
            output_dir=str(tmp_path),
        )

        assert result["success"] is True, result
        content = Path(result["script_file"]).read_text()
        assert "printf '%s %s %s\\n'" in content
        assert shlex.quote(f"node_id={injected_node}") in content
        assert "echo [array_task=" not in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_max_concurrent_applied(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"j{i}", ["prod_001"]) for i in range(5)]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 42\n")

        result = submit_array_job(
            tasks=[
                {"job_dir": str(jd), "node_id": "prod_001", "command": "echo x"}
                for jd in jds
            ],
            max_concurrent=2,
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert result["array_spec"] == "0-4%2"
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --array=0-4%2" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    def test_empty_tasks_rejected(self, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = submit_array_job(tasks=[], output_dir=str(tmp_path))
        assert result["success"] is False
        assert "tasks" in json.dumps(result, default=str)

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    def test_missing_field_rejected(self, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = submit_array_job(
            tasks=[{"job_dir": "/x", "node_id": "prod_001"}],  # missing 'command'
            output_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "command" in json.dumps(result, default=str)

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_sbatch_directive_newline_rejected_for_array(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_array_injection", ["prod_001"])
        result = submit_array_job(
            tasks=[{"job_dir": str(jd), "node_id": "prod_001", "command": "echo x"}],
            dependency="afterok:1\n#SBATCH --account=hacked",
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["code"] == "sbatch_directive_injection"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_array_missing_node_rejected_before_submission(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = submit_array_job(
            tasks=[{"job_dir": str(tmp_path / "missing_job"), "node_id": "prod_001", "command": "echo x"}],
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["code"] == "slurm_node_unavailable"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm.submit._stamp_slurm_on_node", return_value="stamp failed")
    @patch("mdclaw.slurm._base.run_command")
    def test_array_stamp_failure_scancels_parent_and_fails(
        self, mock_run, mock_stamp, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        jd = _make_job_with_nodes(tmp_path, "job_array_stamp_fail", ["prod_001"])
        commands = []

        def side_effect(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == "sbatch":
                return _mock_run_command(stdout="Submitted batch job 99999\n")
            if cmd[0] == "scancel":
                return _mock_run_command(stdout="")
            raise AssertionError(f"unexpected command: {cmd}")

        mock_run.side_effect = side_effect

        result = submit_array_job(
            tasks=[{"job_dir": str(jd), "node_id": "prod_001", "command": "echo x"}],
            output_dir=str(tmp_path),
        )

        assert result["success"] is False
        assert result["parent_job_id"] == "99999"
        assert result["errors"] == ["stamp failed"]
        assert ["scancel", "99999"] in commands
        assert _read_job_records() == []
        node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
        assert "slurm_submission_intent_id" not in node["metadata"]

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_array_later_stamp_failure_rolls_back_prior_stamped_node(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        from mdclaw.slurm import node_sync as slurm_mod

        monkeypatch.chdir(tmp_path)
        jd0 = _make_job_with_nodes(tmp_path, "job_array_stamp_ok", ["prod_001"])
        jd1 = _make_job_with_nodes(tmp_path, "job_array_stamp_bad", ["prod_001"])
        commands = []

        def command_side_effect(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == "sbatch":
                return _mock_run_command(stdout="Submitted batch job 99999\n")
            if cmd[0] == "scancel":
                return _mock_run_command(stdout="")
            raise AssertionError(f"unexpected command: {cmd}")

        mock_run.side_effect = command_side_effect
        real_stamp = slurm_mod._stamp_slurm_on_node
        stamp_calls = []

        def stamp_side_effect(*args, **kwargs):
            stamp_calls.append((args, kwargs))
            if len(stamp_calls) == 1:
                return real_stamp(*args, **kwargs)
            return "stamp failed"

        with patch(
            "mdclaw.slurm.submit._stamp_slurm_on_node",
            side_effect=stamp_side_effect,
        ):
            result = submit_array_job(
                tasks=[
                    {
                        "job_dir": str(jd0),
                        "node_id": "prod_001",
                        "command": "echo first",
                    },
                    {
                        "job_dir": str(jd1),
                        "node_id": "prod_001",
                        "command": "echo second",
                    },
                ],
                output_dir=str(tmp_path),
            )

        assert result["success"] is False
        assert result["parent_job_id"] == "99999"
        assert result["errors"] == ["stamp failed"]
        assert ["scancel", "99999"] in commands
        assert _read_job_records() == []

        first = json.loads((jd0 / "nodes" / "prod_001" / "node.json").read_text())
        first_meta = first["metadata"]
        for key in (
            "slurm_job_id",
            "slurm_parent_job_id",
            "slurm_array_task_id",
            "slurm_submission_intent_id",
        ):
            assert key not in first_meta
        assert first["status"] != "queued"

        second = json.loads((jd1 / "nodes" / "prod_001" / "node.json").read_text())
        assert "slurm_submission_intent_id" not in second["metadata"]
        assert "slurm_array_task_id" not in second["metadata"]


class TestListTrackedJobsFilters:
    """list_tracked_jobs: new job_dir/node_id filters."""

    def test_filter_by_job_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jd_a = tmp_path / "a"
        jd_a.mkdir()
        jd_b = tmp_path / "b"
        jd_b.mkdir()
        _append_job_record({"job_id": "1", "job_dir": str(jd_a.resolve()), "node_id": "prod_001"})
        _append_job_record({"job_id": "2", "job_dir": str(jd_b.resolve()), "node_id": "prod_001"})
        _append_job_record({"job_id": "3", "job_dir": str(jd_a.resolve()), "node_id": "prod_002"})

        result = list_tracked_jobs(job_dir=str(jd_a))
        ids = sorted(r["job_id"] for r in result["jobs"])
        assert ids == ["1", "3"]

        result = list_tracked_jobs(job_dir=str(jd_a), node_id="prod_002")
        ids = [r["job_id"] for r in result["jobs"]]
        assert ids == ["3"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_dependent_jobs_are_cancelled_by_the_scheduler_when_the_dependency_fails():
    """A job behind a failed afterok parent can never run; without
    --kill-on-invalid-dep it was held as DependencyNeverSatisfied for ever
    (MDDataBench campaign v2: 178 such jobs held 77 attempts unscored)."""
    from mdclaw.slurm.sbatch import _generate_array_sbatch_script, _generate_sbatch_script

    common = dict(command="echo hi", job_name="j", partition="gpu", nodes=1, ntasks=1,
                  cpus_per_task=1, gpus=1, gres=None, time_limit="00:10:00", memory=None,
                  output_dir="/tmp", account=None, qos=None, extra_sbatch=None,
                  environment=None, stdout_log="/tmp/o", stderr_log="/tmp/e")
    chained = _generate_sbatch_script(nodelist=None, dependency="afterok:123", **common)
    assert "#SBATCH --dependency=afterok:123" in chained
    assert "#SBATCH --kill-on-invalid-dep=yes" in chained
    alone = _generate_sbatch_script(nodelist=None, dependency=None, **common)
    assert "--kill-on-invalid-dep" not in alone
    array = _generate_array_sbatch_script(
        tasks=[{"command": "echo a", "job_dir": "/tmp/j", "node_id": "prod_001"},
               {"command": "echo b", "job_dir": "/tmp/j", "node_id": "prod_002"}], max_concurrent=None,
        dependency="afterok:123", **{k: v for k, v in common.items()
                                     if k not in ("command", "nodes", "ntasks")})
    assert "#SBATCH --kill-on-invalid-dep=yes" in array


# ---------------------------------------------------------------------------
# submit_mps_job
# ---------------------------------------------------------------------------


def _mps_tasks(jds, node_id="prod_001", platform="--platform CUDA"):
    return [
        {
            "job_dir": str(jd),
            "node_id": node_id,
            "command": (
                f"mdclaw --job-dir {jd} --node-id {node_id} run_production "
                f"--simulation-time-ns 0.1 {platform}"
            ).strip(),
        }
        for jd in jds
    ]


class TestSubmitMpsJob:
    """submit_mps_job: N nodes, one GPU job, one MPS daemon."""

    def test_active_thread_percentage_follows_nvidia_rule(self):
        # 200 / N, clamped to 1-100 (NVIDIA OpenMM/MPS blog, 2025).
        assert mps_active_thread_percentage(1) == 100
        assert mps_active_thread_percentage(2) == 100
        assert mps_active_thread_percentage(3) == 66
        assert mps_active_thread_percentage(4) == 50
        assert mps_active_thread_percentage(8) == 25
        assert mps_active_thread_percentage(400) == 1

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_three_nodes_one_job(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(3)]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77777\n")

        result = submit_mps_job(
            tasks=_mps_tasks(jds),
            job_name="prod_mps",
            partition="gpu",
            time_limit="01:00:00",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True, result
        assert result["slurm_job_id"] == "77777"
        assert result["mps"] == {
            "tasks": 3, "gpus": 1, "tasks_per_gpu": 3,
            "active_thread_percentage": 66, "cpus_per_task": 6,
        }
        assert [t["mps_slot"] for t in result["tasks"]] == [0, 1, 2]
        assert all(t["slurm_job_id"] == "77777" for t in result["tasks"])
        assert all(t["gpu_slot"] == 0 for t in result["tasks"])

        # Every node carries the same job id plus its slot, and is queued.
        for idx, jd in enumerate(jds):
            node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
            assert node["status"] == "queued"
            assert node["metadata"]["slurm_job_id"] == "77777"
            assert node["metadata"]["slurm_parent_job_id"] == "77777"
            assert node["metadata"]["slurm_mps_slot"] == idx
            assert "slurm_array_task_id" not in node["metadata"]
            assert node["metadata"]["slurm_stdout_log"].endswith(f"prod_mps_77777.task{idx}.out")
            assert "slurm_submission_intent_id" not in node["metadata"]

        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --gpus=1" in content
        assert "#SBATCH --cpus-per-task=6" in content
        assert "#SBATCH --array" not in content
        assert "export CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=66" in content
        assert 'export CUDA_MPS_PIPE_DIRECTORY="${MDCLAW_MPS_ROOT}/pipe"' in content
        assert "nvidia-cuda-mps-control -d" in content
        assert "echo quit | nvidia-cuda-mps-control" in content
        assert "trap mdclaw_mps_stop EXIT" in content
        assert content.count("MDCLAW_MPS_PIDS[") >= 3
        for idx in range(3):
            assert f"# slot {idx}: " in content
            assert f".task{idx}.out" in content
        assert "exit $MDCLAW_MPS_STATUS" in content
        # The generated wrapper must be valid bash.
        subprocess.run(["bash", "-n", result["script_file"]], check=True)

        # One tracker record per node, all under the same job id.
        records = _read_job_records()
        assert [r["job_id"] for r in records] == ["77777", "77777", "77777"]
        assert [r["mps_slot"] for r in records] == [0, 1, 2]
        assert all(r["job_stderr_log"].endswith("prod_mps_77777.err") for r in records)
        assert records[1]["stderr_log"].endswith("prod_mps_77777.task1.err")

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_tasks_spread_over_two_gpus(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(4)]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77778\n")

        result = submit_mps_job(
            tasks=_mps_tasks(jds), gpus=2, cpus_per_sim=3, output_dir=str(tmp_path),
        )
        assert result["success"] is True, result
        assert result["mps"]["tasks_per_gpu"] == 2
        assert result["mps"]["active_thread_percentage"] == 100
        assert result["mps"]["cpus_per_task"] == 12
        assert [t["gpu_slot"] for t in result["tasks"]] == [0, 1, 0, 1]
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --gpus=2" in content
        assert "MDCLAW_MPS_DEVICES[$((3 % ${#MDCLAW_MPS_DEVICES[@]}))]" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_explicit_cpus_and_percentage_win(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(2)]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77779\n")

        result = submit_mps_job(
            tasks=_mps_tasks(jds), cpus_per_task=9, active_thread_percentage=40,
            output_dir=str(tmp_path),
        )
        assert result["success"] is True, result
        content = Path(result["script_file"]).read_text()
        assert "#SBATCH --cpus-per-task=9" in content
        assert "export CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=40" in content

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_task_without_cuda_platform_refused(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(2)]
        for platform in ("", "--platform OpenCL", "--platform auto"):
            result = submit_mps_job(
                tasks=_mps_tasks(jds, platform=platform), output_dir=str(tmp_path),
            )
            assert result["success"] is False
            assert result["code"] == "mps_task_requires_cuda_platform", platform
        mock_run.assert_not_called()
        # Nothing was reserved on the nodes.
        node = json.loads((jds[0] / "nodes" / "prod_001" / "node.json").read_text())
        assert node["status"] == "pending"
        assert node["metadata"] == {}

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_too_many_tasks_per_gpu_refused(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(17)]
        result = submit_mps_job(tasks=_mps_tasks(jds), gpus=1, output_dir=str(tmp_path))
        assert result["success"] is False
        assert result["code"] == "mps_tasks_per_gpu_exceeded"
        mock_run.assert_not_called()

        # Two GPUs bring it to 9 per GPU: accepted with a warning.
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77780\n")
        result = submit_mps_job(tasks=_mps_tasks(jds), gpus=2, output_dir=str(tmp_path))
        assert result["success"] is True, result
        assert any("9 tasks per GPU" in w for w in result["warnings"])

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_container_wraps_each_task_and_binds_pipe_dir(
        self, mock_run, mock_check, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "mdclaw.sif").write_text("")
        configure_container(image=str(tmp_path / "mdclaw.sif"), extra_flags="--nv")
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(2)]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77781\n")

        result = submit_mps_job(tasks=_mps_tasks(jds), output_dir=str(tmp_path))
        assert result["success"] is True, result
        content = Path(result["script_file"]).read_text()
        wrapped = [line for line in content.splitlines() if "singularity exec" in line]
        assert len(wrapped) == 2
        for idx, line in enumerate(wrapped):
            assert "--nv" in line
            assert str(jds[idx].resolve()) in line
            # The client inside the container reaches the host daemon through
            # the pipe directory, whose name is only known once the job runs.
            assert ",$CUDA_MPS_PIPE_DIRECTORY " in line
        subprocess.run(["bash", "-n", result["script_file"]], check=True)

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_stamp_failure_scancels_and_rolls_back(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(2)]
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 77782\n")

        real_stamp = _stamp_slurm_on_node

        def flaky_stamp(job_dir, node_id, slurm_job_id, **kwargs):
            if kwargs.get("mps_slot") == 1:
                return "could not stamp node prod_001: simulated"
            return real_stamp(job_dir, node_id, slurm_job_id, **kwargs)

        with patch("mdclaw.slurm.mps._stamp_slurm_on_node", side_effect=flaky_stamp):
            result = submit_mps_job(tasks=_mps_tasks(jds), output_dir=str(tmp_path))
        assert result["success"] is False
        assert any("simulated" in e for e in result["errors"])
        cancelled = [c for c in mock_run.call_args_list if c.args[0][0] == "scancel"]
        assert cancelled and cancelled[0].args[0][1] == "77782"
        for jd in jds:
            node = json.loads((jd / "nodes" / "prod_001" / "node.json").read_text())
            assert node["status"] == "pending"
            assert "slurm_job_id" not in node["metadata"]
            assert "slurm_mps_slot" not in node["metadata"]
            assert "slurm_submission_intent_id" not in node["metadata"]
        assert _read_job_records() == []

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_check_job_failed_syncs_every_packed_node(self, mock_run, mock_check, tmp_path, monkeypatch):
        """One FAILED Slurm state must reach every node packed into the job,
        each with the tail of its own slot log, while a node that already
        completed itself keeps that status."""
        monkeypatch.chdir(tmp_path)
        jds = [_make_job_with_nodes(tmp_path, f"rep_{i}", ["prod_001"]) for i in range(3)]
        job_err = tmp_path / "prod_mps_88888.err"
        job_err.write_text("[mdclaw mps] slot 1 FAILED (exit 1)\n")
        for idx, jd in enumerate(jds):
            slot_err = tmp_path / f"prod_mps_88888.task{idx}.err"
            slot_err.write_text(f"slot {idx} stderr: boom\n" if idx == 1 else "")
            _append_job_record({
                "job_id": "88888", "job_name": "prod_mps", "status": "RUNNING",
                "job_dir": str(jd), "node_id": "prod_001", "mps_slot": idx,
                "stdout_log": str(tmp_path / f"prod_mps_88888.task{idx}.out"),
                "stderr_log": str(slot_err),
                "job_stdout_log": str(tmp_path / "prod_mps_88888.out"),
                "job_stderr_log": str(job_err),
            })
            status = "completed" if idx == 0 else "queued"
            (jd / "nodes" / "prod_001" / "node.json").write_text(json.dumps({
                "node_id": "prod_001", "type": "prod", "status": status,
                "parents": [], "metadata": {"slurm_job_id": "88888", "slurm_mps_slot": idx},
                "artifacts": {}, "warnings": [],
            }))

        def side_effect(cmd, **kwargs):
            if cmd[0] == "squeue":
                raise subprocess.CalledProcessError(1, cmd)
            return _mock_run_command(stdout=json.dumps({
                "jobs": [{
                    "state": {"current": ["FAILED"]},
                    "nodes": "c074",
                    "exit_code": {"return_code": 1},
                    "time": {"elapsed": "00:10:00"},
                }]
            }))
        mock_run.side_effect = side_effect

        result = check_job("88888")
        assert result["state"] == "FAILED"
        # The job-level tail comes from the wrapper's log, not slot 0's.
        assert "slot 1 FAILED" in result["stderr_tail"]

        n0 = json.loads((jds[0] / "nodes" / "prod_001" / "node.json").read_text())
        assert n0["status"] == "completed"
        for idx in (1, 2):
            node = json.loads((jds[idx] / "nodes" / "prod_001" / "node.json").read_text())
            assert node["status"] == "failed", idx
            assert node["metadata"].get("failure_code") == "slurm_failed"
        # Slot 1 recorded its own stderr as evidence.
        manifest = jds[1] / "nodes" / "prod_001" / "artifacts" / "failure" / "latest"
        evidence = " ".join(p.read_text() for p in manifest.rglob("*") if p.is_file())
        assert "slot 1 stderr: boom" in evidence
        # Every tracker record moved to FAILED.
        assert {r["status"] for r in _read_job_records()} == {"FAILED"}

    def test_list_tracked_jobs_sync_queries_each_job_id_once(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for idx in range(3):
            _append_job_record({
                "job_id": "88889", "status": "RUNNING", "mps_slot": idx,
                "job_dir": str(tmp_path / f"rep_{idx}"), "node_id": "prod_001",
            })
        _append_job_record({"job_id": "88890", "status": "RUNNING"})
        with patch("mdclaw.slurm.monitor.check_job") as mock_check_job:
            list_tracked_jobs(sync=True)
        assert sorted(c.args[0] for c in mock_check_job.call_args_list) == ["88889", "88890"]


class TestResolveContainerRuntime:
    """The runtime binary is resolved at submission so the sbatch script does
    not depend on the job's PATH (SIF-only sites lost jobs to
    'singularity: command not found')."""

    def _fake_runtime(self, tmp_path, name="singularity"):
        bindir = tmp_path / "hostbin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        return bindir, exe

    def test_resolved_on_mdclaw_slurm_path(self, tmp_path, monkeypatch):
        from mdclaw.slurm.config import resolve_container_runtime

        bindir, exe = self._fake_runtime(tmp_path)
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(bindir))
        monkeypatch.delenv("SINGULARITY_CONTAINER", raising=False)
        container = {"image": "/opt/mdclaw.sif"}
        warnings: list = []
        assert resolve_container_runtime(container, warnings=warnings) is None
        assert container["runtime_resolved"] == str(exe)
        assert warnings == []
        cmd = _build_singularity_command("mdclaw --list", container, str(tmp_path))
        assert cmd.startswith(f"{exe} exec")

    def test_apptainer_is_the_fallback_name(self, tmp_path, monkeypatch):
        from mdclaw.slurm.config import resolve_container_runtime

        bindir, exe = self._fake_runtime(tmp_path, "apptainer")
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(bindir))
        container = {"image": "/opt/mdclaw.sif"}
        assert resolve_container_runtime(container) is None
        assert container["runtime_resolved"] == str(exe)

    def test_configured_absolute_runtime_wins(self, tmp_path, monkeypatch):
        from mdclaw.slurm.config import resolve_container_runtime

        _, exe = self._fake_runtime(tmp_path, "singularity-ce")
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(tmp_path / "empty"))
        container = {"image": "/opt/mdclaw.sif", "runtime": str(exe)}
        assert resolve_container_runtime(container) is None
        assert container["runtime_resolved"] == str(exe)

    def test_inside_image_without_runtime_is_refused(self, tmp_path, monkeypatch):
        from mdclaw.slurm.config import resolve_container_runtime

        (tmp_path / "empty").mkdir()
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("SINGULARITY_CONTAINER", "/data/mdclaw.sif")
        container = {"image": "/opt/mdclaw.sif"}
        error = resolve_container_runtime(container)
        assert error["code"] == "container_runtime_not_found"
        assert "MDCLAW_SLURM_PATH" in " ".join(error["hints"])
        assert "runtime_resolved" not in container

    def test_outside_image_without_runtime_only_warns(self, tmp_path, monkeypatch):
        from mdclaw.slurm.config import resolve_container_runtime

        (tmp_path / "empty").mkdir()
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("SINGULARITY_CONTAINER", raising=False)
        monkeypatch.delenv("APPTAINER_CONTAINER", raising=False)
        container = {"image": "/opt/mdclaw.sif"}
        warnings: list = []
        assert resolve_container_runtime(container, warnings=warnings) is None
        assert len(warnings) == 1 and "PATH" in warnings[0]
        assert _build_singularity_command("mdclaw --list", container, str(tmp_path)).startswith("singularity exec")

    def test_configure_container_rejects_missing_absolute_runtime(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = configure_container(image="/opt/mdclaw.sif", runtime=str(tmp_path / "nope"))
        assert result["success"] is False
        assert result["code"] == "container_runtime_not_found"
        _, exe = self._fake_runtime(tmp_path)
        result = configure_container(image="/opt/mdclaw.sif", runtime=str(exe))
        assert result["success"] is True
        assert result["container"]["runtime"] == str(exe)

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_submit_from_inside_image_refuses_before_sbatch(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "empty").mkdir()
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("SINGULARITY_CONTAINER", "/data/mdclaw.sif")
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "container": {"image": "/opt/containers/mdclaw.sif", "extra_flags": "--nv"},
        }))
        result = submit_job(script="mdclaw run_production", partition="gpu", gpus=1, output_dir=str(tmp_path))
        assert result["success"] is False
        assert result["code"] == "container_runtime_not_found"
        mock_run.assert_not_called()

    @patch("mdclaw.slurm._base.check_external_tool", return_value=True)
    @patch("mdclaw.slurm._base.run_command")
    def test_submitted_script_carries_the_absolute_runtime(self, mock_run, mock_check, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bindir, exe = self._fake_runtime(tmp_path)
        monkeypatch.setenv("MDCLAW_SLURM_PATH", str(bindir))
        monkeypatch.setenv("SINGULARITY_CONTAINER", "/data/mdclaw.sif")
        (tmp_path / ".mdclaw_cluster.json").write_text(json.dumps({
            "partitions": [{"name": "gpu", "gpus_per_node": 4}],
            "container": {"image": "/opt/containers/mdclaw.sif", "extra_flags": "--nv"},
        }))
        mock_run.return_value = _mock_run_command(stdout="Submitted batch job 10002\n")
        result = submit_job(script="mdclaw run_production", partition="gpu", gpus=1, output_dir=str(tmp_path))
        assert result["success"] is True, result
        content = Path(result["script_file"]).read_text()
        assert f"{exe} exec --nv" in content
