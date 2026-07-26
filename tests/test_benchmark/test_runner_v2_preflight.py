"""Runner-side binding tests for MDStudyBench v2 confirmatory inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from mdclaw.benchmark.run import (
    _preflight_v2_pending_inputs,
    _v2_preflight_input_binding_errors,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preflight_hashes_are_bound_to_post_run_inspection(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "system.xml"
    topology = tmp_path / "topology.pdb"
    state = tmp_path / "state.xml"
    base.write_text("base-system")
    topology.write_text("topology")
    state.write_text("start-state")

    def fake_resolve_node_inputs(
        job_dir: str,
        node_id: str,
        node_type: str,
    ) -> dict[str, str]:
        assert Path(job_dir).is_absolute()
        assert node_id in {"prod_001", "prod_002"}
        assert node_type == "prod"
        return {
            "system_xml_file": str(base),
            "topology_pdb_file": str(topology),
            "restart_from": str(state),
        }

    monkeypatch.setattr(
        "mdclaw.node.inputs.resolve_node_inputs",
        fake_resolve_node_inputs,
    )
    runs = [
        {
            "run_id": "reference-1",
            "job_dir": tmp_path / "reference",
            "node_id": "prod_001",
        },
        {
            "run_id": "variant-1",
            "job_dir": tmp_path / "variant",
            "node_id": "prod_002",
        },
    ]
    errors: list[dict[str, str]] = []

    _preflight_v2_pending_inputs(runs, errors)

    assert errors == []
    expected = {
        "base_system": _sha256(base),
        "topology": _sha256(topology),
        "start_state": _sha256(state),
    }
    assert all(run["preflight_input_sha256"] == expected for run in runs)
    event = {
        "input_artifacts": {
            name: {"sha256": digest}
            for name, digest in expected.items()
        }
    }
    assert _v2_preflight_input_binding_errors(
        spec=runs[0],
        event=event,
    ) == []

    event["input_artifacts"]["base_system"]["sha256"] = "0" * 64
    assert _v2_preflight_input_binding_errors(
        spec=runs[0],
        event=event,
    ) == ["confirmatory_base_system_changed_after_preflight"]


def test_preflight_requires_a_resolved_start_state(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "system.xml"
    topology = tmp_path / "topology.pdb"
    base.write_text("base-system")
    topology.write_text("topology")

    monkeypatch.setattr(
        "mdclaw.node.inputs.resolve_node_inputs",
        lambda *_args: {
            "system_xml_file": str(base),
            "topology_pdb_file": str(topology),
        },
    )
    runs = [
        {
            "run_id": "reference-1",
            "job_dir": tmp_path,
            "node_id": "prod_001",
        }
    ]
    errors: list[dict[str, str]] = []

    _preflight_v2_pending_inputs(runs, errors)

    assert {error["code"] for error in errors} == {
        "confirmatory_start_state_missing"
    }
