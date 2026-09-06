"""Shared steering clock and portable restart; potentials own their updates."""

import hashlib
import json
import math
from pathlib import Path

from mdclaw.simulation.restraints import DistanceRestraintError

PROTOCOL_PARAMETER = "mdclaw_distance_steering_protocol"
PROGRESS_PARAMETER = "mdclaw_steering_progress"


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


def validate_steering(time_ns, update_ps, restraints, timestep_fs, custom_force_script=None):
    if time_ns is None:
        return
    for value, scale in ((time_ns, 1e6), (update_ps, 1000)):
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value <= 0
                or int(value * scale / timestep_fs) < 1):
            raise DistanceRestraintError(
                code="distance_steering_invalid", message="Steering times must be finite, positive and at least one timestep."
            )
    if not restraints and not custom_force_script:
        raise DistanceRestraintError(
            code="distance_steering_invalid", message="steering_time_ns requires distance_restraints or custom_force_script."
        )
    names = {r["name"] for r in restraints or []}
    if names & {f"{name}_center_nm" for name in names}:
        raise DistanceRestraintError(code="distance_steering_invalid", message="Restraint names collide with steering center columns.")


class SteeringSchedule:
    """Update a potential on a fixed step grid, independently of reporting.

    Each interval uses its right-endpoint center (a staircase approximation to
    a linear ramp). The grid is independent of output and segment boundaries.
    The immutable sidecar plus XML step count reconstruct interrupted progress.
    """

    def __init__(self, simulation, signature, *, time_ns, update_ps, timestep_fs,
                 restart_from, output_dir, initial):
        from openmm.unit import picosecond

        self.simulation = simulation
        self.elapsed = 0
        self.fixed = time_ns is None
        previous, saved = _restart_protocol(restart_from)
        if self.fixed and previous is None:
            raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="Fixed steering requires a completed steering protocol.")
        self.protocol = {
            "version": 1, "sampling_role": "steered",
            "signature": signature, "timestep_fs": timestep_fs,
            "duration_steps": previous["duration_steps"] if self.fixed else int(time_ns * 1e6 / timestep_fs),
            "update_steps": previous["update_steps"] if self.fixed else int(update_ps * 1000 / timestep_fs),
        }
        if previous:
            if any(previous.get(k) != v for k, v in self.protocol.items()):
                raise DistanceRestraintError(
                    code="distance_steering_restart_mismatch",
                    message="Resume with the same restraints/script/parameters, steering duration, update interval and timestep.",
                )
            self.elapsed = saved.getStepCount() - previous["origin_step"]
            if self.elapsed < 0:
                raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="State precedes the steering origin.")
            check_steering_handoff(restart_from, time_ns)
            self.protocol = previous
            simulation.context.setTime(saved.getTime())
            simulation.currentStep = previous["origin_step"] + self.elapsed
        else:
            self.protocol.update(
                **initial,
                origin_time_ps=simulation.context.getState().getTime().value_in_unit(picosecond),
                origin_step=simulation.currentStep,
            )
        self.path = Path(output_dir) / "steering.json"
        self.path.write_text(json.dumps(self.protocol, indent=2) + "\n")
        simulation.context.setParameter(PROTOCOL_PARAMETER, _protocol_id(self.protocol))

    def start(self):
        interval = self.protocol["update_steps"]
        self._apply(((self.elapsed + interval - 1) // interval) * interval)

    def progress_at(self, elapsed):
        return 1.0 if self.fixed else min(elapsed / self.protocol["duration_steps"], 1.0)

    def step(self, steps):
        end = self.elapsed + steps
        duration = self.protocol["duration_steps"]
        interval = self.protocol["update_steps"]
        while self.elapsed < end:
            boundary = min((self.elapsed // interval + 1) * interval, duration)
            if self.elapsed >= duration or self.fixed:
                boundary = end
            self._apply(boundary)
            count = min(boundary, end) - self.elapsed
            self.simulation.step(count)
            self.elapsed += count

    def summary(self):
        return {
            **self.protocol, "elapsed_steps": self.elapsed,
            "mode": "fixed" if self.fixed else "steered",
            "schedule_complete": self.elapsed >= self.protocol["duration_steps"],
        }


class DistanceSteering(SteeringSchedule):
    def __init__(self, simulation, loaded, **kwargs):
        self.simulation = simulation
        self.loaded = loaded
        self.force = loaded["forces"][0]
        self.centers = {}
        super().__init__(simulation, loaded["signature"],
                         initial={"initial_distances_nm": self.distances()}, **kwargs)
        self.start()
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

    def _apply(self, elapsed):
        fraction = self.progress_at(elapsed)
        for i, restraint in enumerate(self.loaded["restraints"]):
            name = restraint["name"]
            initial = self.protocol["initial_distances_nm"][name]
            center = initial + fraction * (restraint["target_distance_nm"] - initial)
            groups, parameters = self.force.getBondParameters(i)
            self.force.setBondParameters(i, groups, [parameters[0], center])
            self.centers[name] = center
        self.force.updateParametersInContext(self.simulation.context)

    def summary(self):
        distances = self.distances()
        return {
            **super().summary(),
            "final_distances_nm": {r["name"]: distances[r["name"]] for r in self.loaded["restraints"]},
            "final_centers_nm": dict(self.centers),
            "target_errors_nm": {
                r["name"]: distances[r["name"]] - r["target_distance_nm"]
                for r in self.loaded["restraints"]
            },
        }


def prepare_torch_steering(*, restart_from, time_ns, signature, positions, box,
                           is_periodic, output_dir):
    """Freeze the actual input geometry before validating/building the force.

    A fixed umbrella copies the same reference, not its current coordinates.
    No user callbacks or arbitrary Python checkpoint objects are needed.
    """
    import numpy as np
    from openmm.unit import nanometer

    previous, saved = _restart_protocol(restart_from)
    if time_ns is None and previous is None:
        return None
    path = Path(output_dir) / "steering_initial.npz"
    if previous:
        if previous["signature"] != signature or "initial_sha256" not in previous:
            raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="Steering script/parameters or bias route changed.")
        source = Path(restart_from).parent / "steering_initial.npz"
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != previous["initial_sha256"]:
            raise DistanceRestraintError(code="distance_steering_restart_mismatch", message="Missing or changed steering_initial.npz; keep it with the XML and steering.json.")
        if source.resolve() != path.resolve():
            path.write_bytes(source.read_bytes())
    else:
        if saved is not None:
            positions = saved.getPositions(asNumpy=True)
            box = saved.getPeriodicBoxVectors(asNumpy=True)
        pos = np.asarray(positions.value_in_unit(nanometer), dtype=float)
        initial_box = np.asarray(box.value_in_unit(nanometer), dtype=float) if is_periodic else np.empty((0, 3))
        np.savez(path, positions=pos, box=initial_box)
    with np.load(path, allow_pickle=False) as data:
        pos, initial_box = data["positions"], data["box"]
    return {"initial_positions": pos, "initial_box": initial_box if initial_box.size else None,
            "initial_file": path, "initial_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "fixed": time_ns is None}


class TorchSteering(SteeringSchedule):
    def __init__(self, simulation, loaded, prepared, **kwargs):
        super().__init__(simulation, loaded["signature"], initial={
            "initial_file": "steering_initial.npz",
            "initial_sha256": prepared["initial_sha256"],
        }, **kwargs)
        self.start()

    def _apply(self, elapsed):
        self.simulation.context.setParameter(PROGRESS_PARAMETER, self.progress_at(elapsed))

    def summary(self):
        return {**super().summary(),
                "progress": self.simulation.context.getParameter(PROGRESS_PARAMETER)}


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
