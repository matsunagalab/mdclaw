"""MDClaw tool registry — maps server names to module paths."""

SERVER_REGISTRY = {
    "research": "mdclaw.research_server",
    "structure": "mdclaw.structure_server",
    "solvation": "mdclaw.solvation_server",
    "amber": "mdclaw.amber_server",
    "openmm_system": "mdclaw.openmm_system_server",
    "md_simulation": "mdclaw.md_simulation_server",
    "genesis": "mdclaw.genesis_server",
    "literature": "mdclaw.literature_server",
    "metal": "mdclaw.metal_server",
    "slurm": "mdclaw.slurm_server",
    "node": "mdclaw.node_server",
    "analyze": "mdclaw.analyze_server",
    "visualization": "mdclaw.visualization_server",
    "study": "mdclaw.study_server",
    "evidence": "mdclaw.evidence_server",
    "benchmark": "mdclaw.benchmark",
    "throughput": "mdclaw.throughput_server",
}
