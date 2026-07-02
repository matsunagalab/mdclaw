"""OpenMM-XML / TorchForce research-mode topology builder.

Companion to ``mdclaw.amber.build_amber_system``. Where
``build_amber_system`` is the curated Amber-catalog path (ff19SB / ff14SB /
phosaa / lipid21 / GLYCAM, all routed through the openmmforcefields
catalog), ``build_openmm_system`` is the research-mode escape hatch: the
user supplies a list of OpenMM ``ForceField`` XML files (e.g.
``GB99dms.xml`` from the Greener group) together with optional ligand
molecules, and the tool emits the same ``system.xml + topology.pdb +
state.xml`` artifact triple so min/eq/prod can consume the result through
the same DAG resolver, plus a minimization report for benchmark evidence.

The tool is intentionally permissive — there is no FF×water guardrail
matrix here, because by definition the user is bringing their own XML
that mdclaw's Amber25 catalog does not know about. We only block on
critical correctness conditions (e.g. GB99dms requires OpenMM 8.0+).

TorchForce / .pt overlays (garnet-style ML potentials) are explicitly out
of scope for this PR.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,  # noqa: F401  (kept for parity / future extension)
    ensure_directory,
    setup_logger,
)

logger = setup_logger(__name__)

WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------














# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------




# =============================================================================
# Tool registry
# =============================================================================
