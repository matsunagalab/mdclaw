"""MDClaw - skills and CLIs for vibe-MD simulations.

The package provides tools for MD preparation, simulation, analysis, and
evidence capture in autonomous scientific investigation.
"""

from __future__ import annotations

import os


def _preload_torch_for_openmm_torch() -> None:
    """Load libtorch before OpenMM scans plugins, when torch is available.

    OpenMM discovers plugin shared libraries when ``openmm`` is first imported.
    The ``openmm-torch`` plugin depends on libtorch symbols being present at
    that moment; otherwise the plugin can fail to load and PythonTorchForce
    kernels are never registered for any platform.  Keep this best-effort so
    non-torch MDClaw installs still import normally.
    """
    if os.environ.get("MDCLAW_PRELOAD_TORCH_FOR_OPENMM", "1").lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    try:
        import torch  # noqa: F401
    except ImportError:
        return


_preload_torch_for_openmm_torch()

__version__ = "0.6.3"

__all__ = [
    "structure_server",
    "genesis_server",
    "surrogate_server",
    "solvation_server",
    "amber_server",
    "openmm_system_server",
    "md_simulation_server",
    "slurm_server",
    "analyze_server",
    "visualization_server",
    "study_server",
    "evidence_server",
    "benchmark",
    "throughput_server",
]
