"""Tests for append-only event logging."""

import json

import pytest

from mdclaw._event import read_events, write_event


@pytest.fixture
def job_dir(tmp_path):
    jd = tmp_path / "job_evt_test"
    jd.mkdir()
    return jd


class TestWriteEvent:

    def test_creates_event_file(self, job_dir):
        path = write_event(str(job_dir), "eq_001", "tool_started")
        assert path.exists()
        assert path.suffix == ".json"
        assert "eq_001" in path.name
        assert "tool_started" in path.name

    def test_event_content(self, job_dir):
        path = write_event(str(job_dir), "eq_001", "tool_completed",
                           tool="run_equilibration",
                           success=True,
                           cli="mdclaw --job-dir <jd> --node-id eq_001 run_equilibration",
                           details={"platform": "CUDA"})
        ev = json.loads(path.read_text())
        assert ev["node_id"] == "eq_001"
        assert ev["event_type"] == "tool_completed"
        assert ev["tool"] == "run_equilibration"
        assert ev["success"] is True
        assert ev["cli"].startswith("mdclaw")
        assert ev["details"]["platform"] == "CUDA"
        assert "timestamp" in ev

    def test_creates_events_dir(self, job_dir):
        """events/ directory created automatically."""
        write_event(str(job_dir), "prep_001", "node_created")
        assert (job_dir / "events").is_dir()

    def test_multiple_events_unique_files(self, job_dir):
        write_event(str(job_dir), "eq_001", "tool_started")
        write_event(str(job_dir), "eq_001", "tool_completed")
        write_event(str(job_dir), "prod_001", "tool_started")
        files = list((job_dir / "events").glob("*.json"))
        assert len(files) == 3

    def test_optional_fields_omitted(self, job_dir):
        path = write_event(str(job_dir), "prep_001", "node_created")
        ev = json.loads(path.read_text())
        assert "tool" not in ev
        assert "success" not in ev
        assert "cli" not in ev
        assert "details" not in ev

    def test_tmp_file_cleaned_when_replace_fails(self, job_dir, monkeypatch):
        from mdclaw import _event

        def fail_replace(src, dst):
            raise OSError("replace failed")

        monkeypatch.setattr(_event.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replace failed"):
            write_event(str(job_dir), "prep_001", "node_created")

        assert list((job_dir / "events").glob(".*.tmp.*")) == []


class TestReadEvents:

    def test_read_all(self, job_dir):
        write_event(str(job_dir), "eq_001", "tool_started")
        write_event(str(job_dir), "eq_001", "tool_completed")
        write_event(str(job_dir), "prod_001", "tool_started")
        events = read_events(str(job_dir))
        assert len(events) == 3

    def test_filter_by_node(self, job_dir):
        write_event(str(job_dir), "eq_001", "tool_started")
        write_event(str(job_dir), "prod_001", "tool_started")
        events = read_events(str(job_dir), node_id="eq_001")
        assert len(events) == 1
        assert events[0]["node_id"] == "eq_001"

    def test_filter_by_type(self, job_dir):
        write_event(str(job_dir), "eq_001", "tool_started")
        write_event(str(job_dir), "eq_001", "tool_completed")
        events = read_events(str(job_dir), event_type="tool_completed")
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_completed"

    def test_empty_dir(self, job_dir):
        assert read_events(str(job_dir)) == []

    def test_nonexistent_dir(self, tmp_path):
        assert read_events(str(tmp_path / "nope")) == []

    def test_sorted_by_timestamp(self, job_dir):
        write_event(str(job_dir), "a_001", "first")
        write_event(str(job_dir), "b_001", "second")
        write_event(str(job_dir), "c_001", "third")
        events = read_events(str(job_dir))
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps)
