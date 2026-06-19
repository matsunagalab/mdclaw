"""Unit tests for input_resolution_recovery (stuck-parent recovery hint).

When a workflow tool is blocked because a parent node is not completed, the CLI
must surface a structured ``create_node`` suggestion for the blocking parent's
stage so a weak agent re-creates the stuck ancestor instead of re-running the
same blocked node. Mirrors the P05 benchmark failure where the agent re-ran a
``min`` node ~17x against a ``topo`` parent stuck in ``running``.

Run with: conda run -n mdclaw pytest tests/test_input_resolution_recovery.py -v
"""

import json
from pathlib import Path

from mdclaw._node import input_resolution_recovery


def _write_progress(tmp_path: Path, nodes: dict) -> str:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "progress.json").write_text(json.dumps({
        "schema_version": 3,
        "job_id": "main",
        "nodes": nodes,
    }))
    return str(job_dir)


def _chain(min_status="pending", topo_status="completed"):
    return {
        "source_001": {"type": "source", "status": "completed", "parents": [], "dependencies": []},
        "prep_001": {"type": "prep", "status": "completed", "parents": ["source_001"], "dependencies": []},
        "solv_001": {"type": "solv", "status": "completed", "parents": ["prep_001"], "dependencies": []},
        "topo_001": {"type": "topo", "status": topo_status, "parents": ["solv_001"], "dependencies": []},
        "min_001": {"type": "min", "status": min_status, "parents": ["topo_001"], "dependencies": []},
    }


def test_running_parent_yields_create_topo_hint(tmp_path):
    job_dir = _write_progress(tmp_path, _chain(min_status="failed", topo_status="running"))
    hint = input_resolution_recovery(job_dir, "min_001")
    assert hint is not None
    assert hint["action"] == "create_node"
    assert hint["node_type"] == "topo"
    assert hint["blocking_node_id"] == "topo_001"
    assert hint["blocking_status"] == "running"
    assert hint["suggested_parent_node_ids"] == ["solv_001"]
    assert "create_node --job-dir" in hint["next_command"]
    assert "--node-type topo" in hint["next_command"]
    assert "--parent-node-ids solv_001" in hint["next_command"]


def test_failed_parent_also_recovers(tmp_path):
    job_dir = _write_progress(tmp_path, _chain(min_status="failed", topo_status="failed"))
    hint = input_resolution_recovery(job_dir, "min_001")
    assert hint is not None
    assert hint["blocking_status"] == "failed"
    assert hint["node_type"] == "topo"


def test_pending_parent_also_recovers(tmp_path):
    job_dir = _write_progress(tmp_path, _chain(min_status="pending", topo_status="pending"))
    hint = input_resolution_recovery(job_dir, "min_001")
    assert hint is not None
    assert hint["blocking_status"] == "pending"


def test_all_completed_returns_none(tmp_path):
    job_dir = _write_progress(tmp_path, _chain(min_status="completed", topo_status="completed"))
    assert input_resolution_recovery(job_dir, "min_001") is None


def test_missing_node_returns_none(tmp_path):
    job_dir = _write_progress(tmp_path, _chain())
    assert input_resolution_recovery(job_dir, "does_not_exist") is None


def test_missing_progress_returns_none(tmp_path):
    assert input_resolution_recovery(str(tmp_path / "nope"), "min_001") is None


def test_dependency_block_recovers(tmp_path):
    """A non-completed *dependency* (not just a parent) also triggers recovery."""
    nodes = _chain(min_status="completed", topo_status="completed")
    nodes["eq_001"] = {
        "type": "eq", "status": "failed",
        "parents": ["min_001"],
        "dependencies": ["topo_001"],
    }
    nodes["topo_001"]["status"] = "running"
    job_dir = _write_progress(tmp_path, nodes)
    hint = input_resolution_recovery(job_dir, "eq_001")
    assert hint is not None
    # parent min_001 is completed; the blocking ref is the dependency topo_001
    assert hint["blocking_node_id"] == "topo_001"
    assert hint["node_type"] == "topo"
