"""Regression tests for the raw-only MDPrepBench v0.3 exporters."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def _make_artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    system = tmp_path / "system.xml"
    topology = tmp_path / "topology.pdb"
    state = tmp_path / "state.xml"
    system.write_text("<System/>\n")
    topology.write_text("ATOM      1  CA  ALA A   1       0.000   0.000   0.000\nEND\n")
    state.write_text("<State/>\n")
    return system, topology, state


def test_package_openmm_submission_writes_only_raw_artifacts(tmp_path: Path):
    from mdclaw.benchmark.cli import package_openmm_submission

    system, topology, state = _make_artifacts(tmp_path)
    parent = tmp_path / "wt_prepared_structure.pdb"
    parent.write_text(topology.read_text())
    submission = tmp_path / "submission"

    result = package_openmm_submission(
        submission_dir=str(submission),
        task_id="P08_demo",
        system_xml_file=str(system),
        topology_pdb_file=str(topology),
        state_xml_file=str(state),
        prepared_structure_file=str(topology),
        extra_output_files=[f"wt_prepared_structure.pdb={parent}"],
    )

    assert result["success"], result
    assert sorted(
        path.relative_to(submission).as_posix()
        for path in submission.rglob("*")
        if path.is_file()
    ) == [
        "prepared_structure.pdb",
        "topology/state.xml",
        "topology/system.xml",
        "topology/topology.pdb",
        "wt_prepared_structure.pdb",
    ]
    assert not (submission / "manifest.json").exists()
    assert not (submission / "provenance.json").exists()


def test_package_openmm_submission_rejects_evaluator_owned_extra(tmp_path: Path):
    from mdclaw.benchmark.cli import package_openmm_submission

    system, topology, state = _make_artifacts(tmp_path)
    generated = tmp_path / "manifest.json"
    generated.write_text("{}\n")
    result = package_openmm_submission(
        submission_dir=str(tmp_path / "submission"),
        task_id="P01_demo",
        system_xml_file=str(system),
        topology_pdb_file=str(topology),
        state_xml_file=str(state),
        prepared_structure_file=str(topology),
        extra_output_files=[f"manifest.json={generated}"],
    )

    assert not result["success"]
    assert "reserved extra output path" in result["errors"][0]


def _make_completed_min_job(tmp_path: Path) -> tuple[Path, str, str]:
    from mdclaw._node import complete_node, create_node

    system, topology, state = _make_artifacts(tmp_path)
    job_dir = tmp_path / "study" / "jobs" / "main"
    prep = create_node(str(job_dir), "prep")
    prep_path = job_dir / "nodes" / prep["node_id"] / "artifacts" / "prepared.pdb"
    prep_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(topology, prep_path)
    complete_node(
        str(job_dir), prep["node_id"], artifacts={"prepared_pdb": "artifacts/prepared.pdb"}
    )

    topo = create_node(str(job_dir), "topo", parent_node_ids=[prep["node_id"]])
    topo_dir = job_dir / "nodes" / topo["node_id"] / "artifacts"
    topo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(system, topo_dir / "system.xml")
    shutil.copy2(topology, topo_dir / "topology.pdb")
    complete_node(
        str(job_dir),
        topo["node_id"],
        artifacts={
            "system_xml": "artifacts/system.xml",
            "topology_pdb": "artifacts/topology.pdb",
        },
    )

    minimum = create_node(str(job_dir), "min", parent_node_ids=[topo["node_id"]])
    min_dir = job_dir / "nodes" / minimum["node_id"] / "artifacts"
    min_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state, min_dir / "state.xml")
    shutil.copy2(topology, min_dir / "minimized_structure.pdb")
    (min_dir / "minimization_report.json").write_text(json.dumps({
        "minimization": {
            "completed": True,
            "energy_is_finite": True,
            "positions_are_finite": True,
            "atom_count_preserved": True,
        }
    }))
    complete_node(
        str(job_dir),
        minimum["node_id"],
        artifacts={
            "state": "artifacts/state.xml",
            "minimized_structure": "artifacts/minimized_structure.pdb",
            "minimization_report": "artifacts/minimization_report.json",
        },
    )
    return job_dir, topo["node_id"], minimum["node_id"]


def test_package_mdprep_submission_requires_completed_min_node(tmp_path: Path):
    from mdclaw.benchmark.cli import package_mdprep_submission

    job_dir, topo_id, min_id = _make_completed_min_job(tmp_path)
    rejected = package_mdprep_submission(
        submission_dir=str(tmp_path / "bad"),
        task_id="P01_demo",
        job_dir=str(job_dir),
        node_id=topo_id,
    )
    assert not rejected["success"]
    assert "requires a completed min node" in rejected["errors"][0]

    submission = tmp_path / "submission"
    result = package_mdprep_submission(
        submission_dir=str(submission),
        task_id="P01_demo",
        job_dir=str(job_dir),
        node_id=min_id,
    )
    assert result["success"], result
    assert (submission / "topology" / "state.xml").read_text() == "<State/>\n"
    assert result["mdclaw_dag"]["min_node_id"] == min_id
    assert not (submission / "manifest.json").exists()
