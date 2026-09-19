"""fep server package — hybrid-topology free energy perturbation.

``build_hybrid_system`` (topo) emits the alchemical XML triple, ``run_fep``
(fep) samples lambda windows, ``analyze_fep`` (analyze) estimates the
window-to-window free energy with MBAR, and ``extract_tripeptide`` /
``estimate_ddg`` are stand-alone helpers for folding-stability ddG.
"""

from mdclaw.fep.analysis import analyze_fep, estimate_ddg
from mdclaw.fep.build import build_hybrid_system
from mdclaw.fep.run import run_fep
from mdclaw.fep.tripeptide import extract_tripeptide

TOOLS = {
    fn.__name__: fn
    for fn in (
        build_hybrid_system,
        run_fep,
        analyze_fep,
        extract_tripeptide,
        estimate_ddg,
    )
}

__all__ = [*TOOLS, "TOOLS"]
