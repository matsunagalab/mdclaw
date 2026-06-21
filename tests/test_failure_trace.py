"""Failure tracing and recovery-option tests for schema-v3 DAG nodes."""

from mdclaw._node import (
    create_node,
    record_node_failure,
    trace_failure,
)
from tests.pipeline_helpers import complete_node_with_placeholders


def _complete(job_dir, node_type, artifacts, **kwargs):
    result = create_node(str(job_dir), node_type, **kwargs)
    assert result["success"] is True, result
    complete_node_with_placeholders(str(job_dir), result["node_id"], artifacts=artifacts)
    return result["node_id"]


def test_trace_failure_recommends_recreating_blocked_parent(tmp_path):
    job_dir = tmp_path / "job_trace_blocked"
    job_dir.mkdir()
    source = _complete(job_dir, "source", {"source_bundle": "artifacts/source_bundle.json"})
    prep = _complete(
        job_dir,
        "prep",
        {"merged_pdb": "artifacts/merged.pdb"},
        parent_node_ids=[source],
    )
    solv = _complete(
        job_dir,
        "solv",
        {"solvated_pdb": "artifacts/solvated.pdb"},
        parent_node_ids=[prep],
    )
    topo = create_node(str(job_dir), "topo", parent_node_ids=[solv])
    assert topo["success"] is True
    record_node_failure(
        str(job_dir),
        topo["node_id"],
        {
            "success": False,
            "code": "openmm_system_build_failed",
            "errors": ["Topology build failed"],
            "warnings": [],
        },
        tool="build_amber_system",
        exit_code=1,
    )
    min_node = create_node(str(job_dir), "min", parent_node_ids=[topo["node_id"]])
    assert min_node["success"] is True
    record_node_failure(
        str(job_dir),
        min_node["node_id"],
        {
            "success": False,
            "code": "input_resolution_blocked",
            "errors": ["parent topo_001 is failed"],
            "warnings": [],
        },
        tool="run_minimization",
        exit_code=1,
    )

    trace = trace_failure(str(job_dir), min_node["node_id"])
    options = [
        option for option in trace["recovery_options"]
        if option.get("source") == "input_resolution_recovery"
    ]
    assert options
    assert options[0]["node_type"] == "topo"
    assert options[0]["parent_node_ids"] == [solv]
    assert "--node-type topo" in options[0]["next_command"]


def test_trace_failure_surfaces_tool_workflow_recommendation(tmp_path):
    job_dir = tmp_path / "job_trace_workflow_recommendation"
    job_dir.mkdir()
    source = _complete(job_dir, "source", {"source_bundle": "artifacts/source_bundle.json"})
    prep = create_node(str(job_dir), "prep", parent_node_ids=[source])
    assert prep["success"] is True
    recommendation = {
        "options": [
            {
                "action": "regenerate_source_structure",
                "next_skill": "mdclaw:modeller-predict",
            }
        ]
    }
    record_node_failure(
        str(job_dir),
        prep["node_id"],
        {
            "success": False,
            "code": "pdbfixer_missing_residues_out_of_scope",
            "errors": ["Internal missing residues exceed repair scope"],
            "warnings": [],
            "workflow_recommendation": recommendation,
            "recommended_next_action": "regenerate_source_structure",
            "recommended_next_skills": ["mdclaw:modeller-predict"],
        },
        tool="prepare_complex",
        exit_code=1,
    )

    trace = trace_failure(str(job_dir), prep["node_id"])
    options = [
        option for option in trace["recovery_options"]
        if option["action"] == "follow_workflow_recommendation"
    ]
    assert options
    assert options[0]["workflow_recommendation"] == recommendation
    assert options[0]["recommended_next_action"] == "regenerate_source_structure"
