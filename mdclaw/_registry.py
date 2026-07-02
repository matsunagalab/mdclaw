"""MDClaw tool registry — maps server names to module paths."""

SERVER_REGISTRY = {
    "research": "mdclaw.research",
    "structure": "mdclaw.structure",
    "solvation": "mdclaw.solvation",
    "amber": "mdclaw.amber",
    "openmm_system": "mdclaw.openmm_system",
    "md_simulation": "mdclaw.simulation",
    "genesis": "mdclaw.genesis",
    "surrogate": "mdclaw.surrogate",
    "literature": "mdclaw.literature",
    "metal": "mdclaw.metal",
    "slurm": "mdclaw.slurm",
    "node": "mdclaw.node",
    "analyze": "mdclaw.analyze",
    "visualization": "mdclaw.visualization",
    "study": "mdclaw.study",
    "evidence": "mdclaw.evidence",
    "benchmark": "mdclaw.benchmark",
    "throughput": "mdclaw.throughput",
}
