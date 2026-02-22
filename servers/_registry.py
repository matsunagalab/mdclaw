"""MDClaw tool registry — maps server names to module paths."""

SERVER_REGISTRY = {
    "research": "servers.research_server",
    "structure": "servers.structure_server",
    "solvation": "servers.solvation_server",
    "amber": "servers.amber_server",
    "md_simulation": "servers.md_simulation_server",
    "genesis": "servers.genesis_server",
    "literature": "servers.literature_server",
    "metal": "servers.metal_server",
    "slurm": "servers.slurm_server",
}
