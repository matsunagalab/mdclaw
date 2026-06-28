"""simulation server package.

Behavior-preserving split of the former ``mdclaw.simulation_server`` module.
Public tool functions are re-exported here and assembled into ``TOOLS``."""

from mdclaw.simulation.platform import (
    export_state_pdb,
    inspect_openmm_platforms,
)
from mdclaw.simulation.minimize import (
    run_minimization,
)
from mdclaw.simulation.equilibrate import (
    run_equilibration,
)
from mdclaw.simulation.production import (
    run_production,
)
from mdclaw.simulation.custom_forces import (  # noqa: F401
    CustomForceError,
    CustomForceReporter,
    custom_force_signature,
    load_custom_forces,
    write_cv_metadata,
)

TOOLS = {
    "export_state_pdb": export_state_pdb,
    "inspect_openmm_platforms": inspect_openmm_platforms,
    "run_minimization": run_minimization,
    "run_equilibration": run_equilibration,
    "run_production": run_production,
}

__all__ = [
    "export_state_pdb",
    "inspect_openmm_platforms",
    "run_minimization",
    "run_equilibration",
    "run_production",
    "TOOLS",
]
