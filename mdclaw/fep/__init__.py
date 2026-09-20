"""fep server package — hybrid-topology free energy perturbation.

``build_hybrid_system`` (topo) emits the alchemical XML triple, ``run_fep``
(fep) samples lambda windows, ``analyze_fep`` (analyze) estimates the
window-to-window free energy with MBAR, and ``extract_tripeptide`` /
``estimate_ddg`` are stand-alone helpers for folding-stability ddG.

Absolute binding free energy of a ligand reuses ``run_fep`` / ``analyze_fep``
on a decoupling topology: ``extract_ligand`` (solvent-leg prep),
``build_decoupled_system`` (topo), ``add_boresch_restraint`` (topo child of the
complex leg's eq) and ``estimate_binding_dg`` (comparison analyze).
"""

from mdclaw.fep.abfe import add_boresch_restraint, build_decoupled_system, estimate_binding_dg, extract_ligand
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
        extract_ligand,
        build_decoupled_system,
        add_boresch_restraint,
        estimate_binding_dg,
    )
}

__all__ = [*TOOLS, "TOOLS"]
