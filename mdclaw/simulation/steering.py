"""Piecewise-linear native distance steering, with portable restart provenance."""

import hashlib
import json
import math
from pathlib import Path

from mdclaw.simulation.restraints import DistanceRestraintError

PROTOCOL_PARAMETER = "mdclaw_distance_steering_protocol"


def _protocol_id(protocol):
    return int(hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()[:13], 16)


def _restart_protocol(restart_from):
    if (not restart_from or Path(restart_from).suffix != ".xml"
            or not Path(restart_from).is_file()):
        return None, None
    from openmm import XmlSerializer

    state = XmlSerializer.deserialize(Path(restart_from).read_text())
    marker = dict(state.getParameters()).get(PROTOCOL_PARAMETER)
    if marker is None:
        return None, state
    sidecar = Path(restart_from).parent / "steering.json"
    if not sidecar.is_file():
        raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="Steered XML restart requires its companion steering.json.")
    protocol = json.loads(sidecar.read_text())
    if marker != _protocol_id(protocol):
        raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="Steering sidecar does not match the XML state.")
    return protocol, state


def validate_steering(time_ns, update_ps, restraints, timestep_fs):
    if time_ns is None:
        return
    for value, scale in ((time_ns, 1e6), (update_ps, 1000)):
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value <= 0
                or int(value * scale / timestep_fs) < 1):
            raise DistanceRestraintError(
                code="distance_steering_invalid", message="Steering times must be finite, positive and at least one timestep."
            )
    if not restraints:
        raise DistanceRestraintError(
            code="distance_steering_invalid", message="steering_time_ns requires distance_restraints."
        )
    names = {r["name"] for r in restraints}
    if names & {f"{name}_center_nm" for name in names}:
        raise DistanceRestraintError(code="distance_steering_invalid", message="Restraint names collide with steering center columns.")


class DistanceSteering:
    """Update centers on a fixed step grid; CSV evaluator logs the applied centers.

    Each interval uses its right-endpoint center (a staircase approximation to
    a linear ramp). The grid is independent of output and segment boundaries.
    The immutable sidecar plus XML step count reconstruct interrupted progress.
    """

    def __init__(self, simulation, loaded, *, time_ns, update_ps, timestep_fs,
                 restart_from, output_dir):
        from openmm.unit import picosecond

        self.simulation = simulation
        self.loaded = loaded
        self.force = loaded["forces"][0]
        self.elapsed = 0
        self.protocol = {
            "version": 1, "sampling_role": "steered",
            "signature": loaded["signature"], "timestep_fs": timestep_fs,
            "duration_steps": int(time_ns * 1e6 / timestep_fs),
            "update_steps": int(update_ps * 1000 / timestep_fs),
        }
        previous, saved = _restart_protocol(restart_from)
        if previous:
            if any(previous.get(k) != v for k, v in self.protocol.items()):
                raise DistanceRestraintError(
                    code="distance_steering_restart_mismatch",
                    message="Resume with the same restraints, steering duration, update interval and timestep.",
                )
            self.elapsed = saved.getStepCount() - previous["origin_step"]
            if self.elapsed < 0:
                raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="State precedes the steering origin.")
            self.protocol = previous
            simulation.context.setTime(saved.getTime())
            simulation.currentStep = previous["origin_step"] + self.elapsed
        else:
            self.protocol.update(
                initial_distances_nm=self.distances(),
                origin_time_ps=simulation.context.getState().getTime().value_in_unit(picosecond),
                origin_step=simulation.currentStep,
            )
        self.path = Path(output_dir) / "steering.json"
        self.path.write_text(json.dumps(self.protocol, indent=2) + "\n")
        simulation.context.setParameter(PROTOCOL_PARAMETER, _protocol_id(self.protocol))
        self.centers = {}
        interval = self.protocol["update_steps"]
        self.set_centers(((self.elapsed + interval - 1) // interval) * interval)
        evaluator = loaded["evaluator"]
        loaded["evaluator"] = lambda positions, box: {
            **evaluator(positions, box),
            **{f"{name}_center_nm": value for name, value in self.centers.items()},
        }
        loaded["cv_names"] = [*loaded["cv_names"], *(
            f"{r['name']}_center_nm" for r in loaded["restraints"]
        )]

    def distances(self):
        from openmm.unit import nanometer

        state = self.simulation.context.getState(getPositions=True)
        return self.loaded["evaluator"](
            state.getPositions(asNumpy=True).value_in_unit(nanometer),
            state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(nanometer),
        )

    def set_centers(self, elapsed):
        fraction = min(elapsed / self.protocol["duration_steps"], 1.0)
        for i, restraint in enumerate(self.loaded["restraints"]):
            name = restraint["name"]
            initial = self.protocol["initial_distances_nm"][name]
            center = initial + fraction * (restraint["target_distance_nm"] - initial)
            groups, parameters = self.force.getBondParameters(i)
            self.force.setBondParameters(i, groups, [parameters[0], center])
            self.centers[name] = center
        self.force.updateParametersInContext(self.simulation.context)

    def step(self, steps):
        end = self.elapsed + steps
        duration = self.protocol["duration_steps"]
        interval = self.protocol["update_steps"]
        while self.elapsed < end:
            boundary = min((self.elapsed // interval + 1) * interval, duration)
            if self.elapsed >= duration:
                boundary = end
            self.set_centers(boundary)
            count = min(boundary, end) - self.elapsed
            self.simulation.step(count)
            self.elapsed += count

    def summary(self):
        distances = self.distances()
        return {
            **self.protocol, "elapsed_steps": self.elapsed,
            "schedule_complete": self.elapsed >= self.protocol["duration_steps"],
            "final_distances_nm": {r["name"]: distances[r["name"]] for r in self.loaded["restraints"]},
            "final_centers_nm": dict(self.centers),
            "target_errors_nm": {
                r["name"]: distances[r["name"]] - r["target_distance_nm"]
                for r in self.loaded["restraints"]
            },
        }


def check_steering_handoff(restart_from, time_ns):
    """Do not silently treat an unfinished ramp as a fixed umbrella seed."""
    if not restart_from or time_ns is not None:
        return
    protocol, state = _restart_protocol(restart_from)
    if protocol is None:
        return
    elapsed = state.getStepCount() - protocol["origin_step"]
    if elapsed < protocol["duration_steps"]:
        raise DistanceRestraintError(
            code="distance_steering_incomplete",
            message="Finish steering first: repeat --steering-time-ns and the original update interval to resume the saved schedule.",
        )
