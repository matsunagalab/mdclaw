"""Records replaced by tmp + rename on another host can be absent for a
moment on a shared file system; readers retry before believing a miss."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mdclaw.node import io as io_module
from mdclaw.node import progress as progress_module
from mdclaw.node.io import _load_json_settled, _read_node_json_path
from mdclaw.node.lifecycle import read_node
from mdclaw.node.progress import _load_progress_v3

TEXT = json.dumps({"schema_version": 3, "nodes": {"a": {"status": "completed"}}, "params": {}})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(io_module.time, "sleep", lambda seconds: None)


def test_transient_absence_is_retried_then_read(tmp_path):
    path = tmp_path / "progress.json"
    with patch.object(Path, "read_text", side_effect=[FileNotFoundError(path), TEXT]) as read:
        assert _load_json_settled(path)["nodes"] == {"a": {"status": "completed"}}
    assert read.call_count == 2


def test_persistent_absence_is_none_and_corruption_raises(tmp_path):
    path = tmp_path / "progress.json"
    with patch.object(Path, "read_text", side_effect=FileNotFoundError(path)) as read:
        assert _load_json_settled(path, attempts=3) is None
    assert read.call_count == 3
    path.write_text("{not json")
    with pytest.raises(ValueError, match="Corrupt JSON"):
        _load_json_settled(path, attempts=2)
    assert _read_node_json_path(path) is None
    with pytest.raises(ValueError, match="Corrupt node.json"):
        _read_node_json_path(path, strict=True)


def test_progress_loader_does_not_reinitialize_on_a_transient_miss(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    path.write_text(TEXT)

    def boom(job_dir):
        raise AssertionError("init_progress_v3 must not run for a file that is only hidden for a moment")

    monkeypatch.setattr(progress_module, "init_progress_v3", boom)
    with patch.object(Path, "read_text", side_effect=[FileNotFoundError(path), TEXT]):
        data = _load_progress_v3(path, create_if_missing=True)
    assert data["nodes"] == {"a": {"status": "completed"}}
    with patch.object(Path, "read_text", side_effect=FileNotFoundError(path)):
        assert _load_progress_v3(path) is None
    with patch.object(Path, "read_text", side_effect=[FileNotFoundError(path)] + ["{oops"] * 6):
        with pytest.raises(ValueError, match="Unreadable progress.json"):
            _load_progress_v3(path)


def test_read_node_retries_then_reports_a_missing_node(tmp_path):
    node = tmp_path / "nodes" / "prod_001"
    node.mkdir(parents=True)
    record = json.dumps({"node_id": "prod_001", "status": "pending"})
    with patch.object(Path, "read_text", side_effect=[FileNotFoundError(), record]):
        assert read_node(str(tmp_path), "prod_001")["status"] == "pending"
    with pytest.raises(FileNotFoundError):
        read_node(str(tmp_path), "prod_404")
