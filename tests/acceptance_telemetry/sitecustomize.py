"""Opt-in, test-only coordinate capture for otherwise unchanged MDClaw CLI.

Activate only via PYTHONPATH and MDCLAW_ACCEPTANCE_TELEMETRY. Reporters and
post-step snapshots do not alter forces, integrators, steps or random seeds.
"""

import os

if os.environ.get("MDCLAW_ACCEPTANCE_TELEMETRY"):
    import json
    from pathlib import Path
    import uuid
    from openmm import XmlSerializer
    from openmm.app import Simulation, DCDReporter

    _init = Simulation.__init__
    _step = Simulation.step
    _root = Path(os.environ["MDCLAW_ACCEPTANCE_TELEMETRY"])
    _root.mkdir(parents=True, exist_ok=True)

    def _recording_init(self, *args, **kwargs):
        _init(self, *args, **kwargs)
        self._acceptance_id = uuid.uuid4().hex
        self.reporters.append(DCDReporter(str(_root / (self._acceptance_id + ".dcd")), 10000))

    def _recording_step(self, steps):
        result = _step(self, steps)
        state = self.context.getState(getPositions=True, getVelocities=True)
        prefix = _root / self._acceptance_id
        prefix.with_suffix(".xml").write_text(XmlSerializer.serialize(state))
        forces = list(self.system.getForces())
        data = {"steps": self.currentStep, "forces": [type(f).__name__ for f in forces]}
        for force in forces:
            if type(force).__name__ == "MonteCarloMembraneBarostat":
                data["membrane_barostat"] = {
                    "xy_mode": force.getXYMode(),
                    "z_mode": force.getZMode(),
                }
        prefix.with_suffix(".json").write_text(json.dumps(data, indent=2))
        return result

    Simulation.__init__ = _recording_init
    Simulation.step = _recording_step
