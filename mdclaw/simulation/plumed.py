"""Bounded, history-free PLUMED inputs and portable production continuation.

PLUMED owns force/schedule evaluation. MDClaw owns files, provenance and the
OpenMM clock. This is not a parser or checkpoint engine for arbitrary PLUMED.
"""
import contextlib
import csv
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

from mdclaw.simulation.custom_forces import CUSTOM_FORCE_GROUP, CustomForceError

PARAMETER = "mdclaw_plumed_protocol"


def _error(message, code=None):
    if code is None:
        code = "plumed_input_invalid"
    raise CustomForceError(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _number(text):
    try:
        value = float(text)
    except (ValueError, TypeError):
        _error(f"Expected a literal number, got {text!r}.")
    if not math.isfinite(value):
        _error("PLUMED numbers must be finite.")
    return value


def parse_input(text):
    """Accept a small explicit action/keyword set; reject unhandled I/O/history.

    Native multiline ... blocks and comments work; INCLUDE, external files,
    macros, regex selectors and executable actions do not.
    """
    lines, block = [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if block is not None:
            if line.startswith("..."):
                if line != "...":
                    _error("Use a bare ... to close multiline actions.")
                lines.append(block)
                block = None
            else:
                block += " " + line
        elif line.endswith("..."):
            block = line[:-3].strip()
        else:
            lines.append(line)
    if block is not None:
        _error("Unclosed PLUMED multiline action.")
    allowed = {
        "UNITS": {"LENGTH", "TIME", "ENERGY"}, "GROUP": {"ATOMS"},
        "COM": {"ATOMS", "NOPBC"}, "CENTER": {"ATOMS", "NOPBC"},
        "DISTANCE": {"ATOMS", "NOPBC"}, "ANGLE": {"ATOMS", "NOPBC"},
        "TORSION": {"ATOMS", "NOPBC"}, "WHOLEMOLECULES": set(),
        "RESTRAINT": {"ARG", "AT", "KAPPA"}, "MOVINGRESTRAINT": {"ARG"},
        "PRINT": {"ARG", "STRIDE", "FILE"},
    }
    actions, labels, cvs, biases, prints = [], set(), set(), [], []
    duration = 0
    for line in lines:
        tokens = line.split()
        label = tokens.pop(0)[:-1] if tokens[0].endswith(":") else None
        action = tokens.pop(0) if tokens else ""
        if action not in allowed:
            _error(f"Action {action!r} is outside the history-free PLUMED contract.")
        if label is not None:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label) or label in labels or label in {"step", "time", "time_ps", "bias_energy_kj_mol"}:
                _error("PLUMED labels must be unique simple identifiers.")
            labels.add(label)
        elif action not in {"UNITS", "PRINT", "WHOLEMOLECULES"}:
            _error(f"Give {action} a label.")
        opts = {}
        for token in tokens:
            key, sep, value = token.partition("=")
            special = ((action == "MOVINGRESTRAINT" and re.fullmatch(r"(?:STEP|AT|KAPPA)\d+", key))
                       or (action == "WHOLEMOLECULES" and re.fullmatch(r"ENTITY\d+", key)))
            if key in opts or (key not in allowed[action] and not special):
                _error(f"Unsupported or duplicate keyword {key} in {action}.")
            if not sep and key != "NOPBC":
                _error(f"{key} requires a value.")
            if sep and key == "NOPBC":
                _error("NOPBC is a flag, not a key=value option.")
            if sep and not re.fullmatch(r"[A-Za-z0-9_.,+/-]+", value):
                _error(f"Only literal, whitespace-free values are supported: {token!r}.")
            opts[key] = value if sep else None
        if action in {"GROUP", "COM", "CENTER", "DISTANCE", "ANGLE", "TORSION"} and "ATOMS" not in opts:
            _error(f"{action} requires ATOMS.")
        if action == "WHOLEMOLECULES" and not opts:
            _error("WHOLEMOLECULES requires explicit ENTITY indices.")
        if action == "UNITS":
            if any(opts.get(k, v).lower() != v for k, v in {"LENGTH": "nm", "TIME": "ps", "ENERGY": "kj/mol"}.items()):
                _error("Managed PLUMED uses nm, ps and kJ/mol; do not change UNITS.")
        if action in {"DISTANCE", "ANGLE", "TORSION"}:
            cvs.add(label)
        if action in {"RESTRAINT", "MOVINGRESTRAINT"}:
            args = opts.get("ARG", "").split(",")
            if not args or any(arg not in cvs for arg in args):
                _error("Bias ARG must name previously defined scalar CVs.")
            if action == "RESTRAINT":
                required = ["AT", "KAPPA"]
            else:
                steps = sorted(int(k[4:]) for k in opts if k.startswith("STEP"))
                if len(steps) < 2 or steps != list(range(len(steps))):
                    _error("MOVINGRESTRAINT requires consecutive STEP0..STEPn.")
                grid = [_number(opts[f"STEP{i}"]) for i in steps]
                if grid[0] != 0 or any(v != int(v) for v in grid) or any(b <= a for a, b in zip(grid, grid[1:])):
                    _error("STEP0 must be 0; subsequent steps must be strictly increasing integers.")
                duration = max(duration, int(grid[-1]))
                required = ["AT0", "KAPPA0"]
            if any(k not in opts for k in required):
                _error(f"{action} requires {required}.")
            for key, value in opts.items():
                if key.startswith(("AT", "KAPPA")):
                    values = [_number(v) for v in value.split(",")]
                    if len(values) != len(args) or (key.startswith("KAPPA") and min(values) < 0):
                        _error("AT/KAPPA must match ARG count; KAPPA must be nonnegative.")
                    if action == "MOVINGRESTRAINT" and int(re.search(r"\d+$", key)[0]) not in steps:
                        _error("AT/KAPPA index has no matching STEP.")
            biases.append(label)
        if action == "PRINT":
            prints.append(opts)
        actions.append((label, action, opts))
    if len(prints) != 1 or prints[0].get("FILE") != "COLVAR":
        _error("Use exactly one PRINT with FILE=COLVAR; other file I/O is unsupported.")
    stride = _number(prints[0].get("STRIDE", "0"))
    if stride < 1 or stride != int(stride):
        _error("PRINT STRIDE must be a positive integer.")
    columns = prints[0].get("ARG", "").split(",")
    supported_columns = cvs | {f"{b}.bias" for b in biases}
    for label, action, opts in actions:
        if action == "MOVINGRESTRAINT":
            supported_columns |= {f"{label}.{arg}_{suffix}" for arg in opts["ARG"].split(",") for suffix in ("cntr", "kappa")}
    if len(set(columns)) != len(columns) or any(c not in supported_columns for c in columns):
        _error("PRINT ARG must list supported CV/bias/center/kappa columns explicitly (no work or wildcards).")
    # Always record every bias energy so the converted CSV has an exact total.
    prints[0]["ARG"] = ",".join(dict.fromkeys(columns + [f"{b}.bias" for b in biases]))
    return {"actions": actions, "duration_steps": duration, "stride": int(stride), "biases": biases}


def read_protocol(restart_from):
    if not restart_from or Path(restart_from).suffix.lower() != ".xml" or not Path(restart_from).is_file():
        return None, None
    from openmm import XmlSerializer
    state = XmlSerializer.deserialize(Path(restart_from).read_text())
    marker = dict(state.getParameters()).get(PARAMETER)
    if marker is None:
        return None, state
    try:
        protocol = json.loads((Path(restart_from).parent / "plumed.json").read_text())
    except (OSError, ValueError) as exc:
        _error(f"PLUMED restart requires matching plumed.json: {exc}", code="plumed_restart_mismatch")
    if marker != int(_digest(protocol)[:13], 16):
        _error("PLUMED protocol does not match XML state.", code="plumed_restart_mismatch")
    return protocol, state


def validate_run(path, restart_from, time_ns, update_ps, timestep_fs):
    previous, _ = read_protocol(restart_from)
    if not path:
        if previous:
            _error("Cannot drop PLUMED from its continuation; inherit the recorded input.", code="plumed_restart_mismatch")
        return None
    if restart_from and Path(restart_from).suffix.lower() != ".xml":
        _error("PLUMED requires a portable XML restart.", code="production_bias_checkpoint_unsupported")
    if update_ps != 1.0:
        _error("PLUMED updates each MD step; steering_update_interval_ps is not applicable.")
    try:
        text = Path(path).read_text()
    except OSError as exc:
        _error(f"Cannot read plumed_file: {exc}")
    parsed = parse_input(text)
    duration = parsed["duration_steps"]
    if time_ns is not None:
        if isinstance(time_ns, bool) or _number(time_ns) <= 0 or not math.isclose(float(time_ns) * 1e6 / timestep_fs, duration, abs_tol=1e-6, rel_tol=0):
            _error("steering_time_ns must equal the final MOVINGRESTRAINT step times timestep.")
    elif duration and not previous:
        _error("Declare steering_time_ns for a new MOVINGRESTRAINT protocol.")
    return text, parsed


@contextlib.contextmanager
def native_log(path):
    """PLUMED 2.1's Python wrapper does not expose setLogStream(FILE*).

    The CLI executes one tool per process. Capture native stdout too, without
    changing process cwd or relying on Python's sys.stdout redirection.
    """
    libc = ctypes.CDLL(None)
    libc.fflush(None)
    original = os.dup(1)
    try:
        with open(path, "ab", buffering=0) as output:
            os.dup2(output.fileno(), 1)
            yield
            libc.fflush(None)
    finally:
        libc.fflush(None)
        os.dup2(original, 1)
        os.close(original)


class PlumedRun:
    def __init__(self, validated, *, system, topology, restart_from, timestep_fs,
                 time_ns, output_dir, report_interval, temperature_kelvin):
        from openmm import CustomExternalForce
        from openmm.unit import dalton, picosecond
        try:
            from openmmplumed import PlumedForce
        except ImportError as exc:
            _error(f"Build PLUMED against this OpenMM prefix with container/scripts/build-plumed.sh: {exc}", code="plumed_dependency_missing")
        self.out = Path(output_dir)
        self.text, self.parsed = validated
        if self.parsed["stride"] != report_interval:
            _error("PRINT STRIDE must match output_frequency_ps / timestep_fs.")
        previous, state = read_protocol(restart_from)
        origin = previous["origin_step"] if previous else (state.getStepCount() if state else 0)
        self.start_step = state.getStepCount() if state else 0
        if not 0 <= self.start_step < 2**31 - 1 or origin + self.parsed["duration_steps"] >= 2**31 - 1:
            _error("PLUMED plugin's 32-bit step counter would overflow.")
        self.start_time = state.getTime().value_in_unit(picosecond) if state else 0.0
        self.fixed = time_ns is None
        signature = {"sha256": hashlib.sha256(self.text.encode()).hexdigest(), "mass_weighting": "physical_element"}
        self.protocol = dict(version=1, signature=signature, timestep_fs=timestep_fs,
                             origin_step=origin, duration_steps=self.parsed["duration_steps"])
        if previous and self.protocol != previous:
            _error("PLUMED input/timestep changed across continuation.", code="plumed_restart_mismatch")
        elapsed = self.start_step - origin
        if elapsed < 0:
            _error("XML precedes PLUMED origin.", code="plumed_restart_mismatch")
        if self.fixed and elapsed < self.protocol["duration_steps"]:
            _error("Finish the ramp first; resume with the original steering_time_ns.", code="plumed_steering_incomplete")
        atoms = list(topology.atoms())
        if len(atoms) != system.getNumParticles():
            _error("PLUMED requires matching topology atoms and System particles.")
        for filename in ("plumed.COLVAR", "plumed.log", "plumed.runtime.dat", "plumed.json"):
            if (self.out / filename).exists() or (self.out / filename).is_symlink():
                _error("Use a fresh node/output directory; PLUMED never overwrites existing run artifacts.")
        atom_groups = set()
        runtime = []
        for label, action, original_opts in self.parsed["actions"]:
            opts = dict(original_opts)
            for key, value in opts.items():
                if key == "ATOMS" or key.startswith("ENTITY"):
                    for item in value.split(","):
                        if item in atom_groups:
                            continue
                        if not re.fullmatch(r"\d+(?:-\d+)?", item):
                            _error(f"Unknown atom/group {item!r}; use 1-based indices or preceding groups.")
                        indices = [int(n) for n in item.split("-")]
                        if min(indices) < 1 or max(indices) > len(atoms) or indices != sorted(indices):
                            _error(f"Atom range {item} outside topology.")
                if action == "MOVINGRESTRAINT" and key.startswith("STEP"):
                    opts[key] = str(int(_number(value)) + origin)
            if action in {"GROUP", "COM", "CENTER"}:
                atom_groups.add(label)
            if action == "PRINT":
                opts["FILE"] = str((self.out / "plumed.COLVAR").resolve())
                opts["FMT"] = "%0.12g"
            runtime.append((f"{label}: " if label else "") + action + " " + " ".join(k if v is None else f"{k}={v}" for k, v in opts.items()))
        runtime.append(f"FLUSH STRIDE={report_interval}")
        script = "\n".join(runtime) + "\n"
        if any(c.isspace() or c in "#{}" for c in str(self.out.resolve())):
            _error("PLUMED output directory must not contain whitespace or PLUMED comment/group delimiters.")
        (self.out / "plumed.dat").write_text(self.text)
        (self.out / "plumed.runtime.dat").write_text(script)
        (self.out / "plumed.json").write_text(json.dumps(self.protocol, indent=2) + "\n")
        force = PlumedForce(script)
        if not hasattr(force, "setMasses"):
            _error("PLUMED plugin >=2.1 with setMasses is required.", code="plumed_dependency_missing")
        force.setTemperature(temperature_kelvin)
        force.setMasses([a.element.mass.value_in_unit(dalton) if a.element else 0 for a in atoms])
        force.setRestart(False)  # every child owns new output files; no history input
        force.setForceGroup(CUSTOM_FORCE_GROUP)
        system.addForce(force)
        marker = CustomExternalForce("0")
        marker.addGlobalParameter(PARAMETER, int(_digest(self.protocol)[:13], 16))
        system.addForce(marker)

    def restore_clock(self, simulation):
        simulation.currentStep = self.start_step
        simulation.context.setTime(self.start_time)

    def finish(self, simulation):
        """Normalize explicit COLVAR fields, retaining the raw file as evidence."""
        end = simulation.currentStep
        fields, rows = None, []
        for line in (self.out / "plumed.COLVAR").read_text().splitlines():
            if line.startswith("#! FIELDS "):
                fields = line.split()[2:]
            elif line and not line.startswith("#"):
                if fields is None:
                    _error("COLVAR lacks FIELDS header.", code="plumed_output_invalid")
                values = [_number(v) for v in line.split()]
                if len(values) != len(fields):
                    _error("COLVAR column count mismatch.", code="plumed_output_invalid")
                data = dict(zip(fields, values))
                step = round(data["time"] * 1000 / self.protocol["timestep_fs"])
                if self.start_step < step <= end:
                    energy = sum(data[f"{b}.bias"] for b in self.parsed["biases"])
                    time_ps = self.start_time + (step - self.start_step) * self.protocol["timestep_fs"] / 1000
                    rows.append((step, time_ps, energy, *values[1:]))
        expected = list(range((self.start_step // self.parsed["stride"] + 1) * self.parsed["stride"], end + 1, self.parsed["stride"]))
        if [r[0] for r in rows] != expected or not rows:
            _error("COLVAR frames do not match the simulation report grid.", code="plumed_output_invalid")
        csv_file = self.out / "collective_variables.csv"
        with csv_file.open("w") as handle:
            writer = csv.writer(handle)
            writer.writerow(["step", "time_ps", "bias_energy_kj_mol", *fields[1:]])
            writer.writerows(rows)
        role = "steered" if not self.fixed else ("fixed_bias" if self.parsed["biases"] else "unbiased")
        metadata = dict(protocol=self.protocol, sampling_role=role, cv_names=fields[1:],
                        units={"length": "nm", "time": "ps", "energy": "kJ/mol", "angles": "rad"})
        build_file = Path(sys.prefix) / "share/mdclaw/plumed-build.json"
        metadata["build"] = json.loads(build_file.read_text()) if build_file.is_file() else {"source": "external installation"}
        meta_file = self.out / "collective_variables.meta.json"
        meta_file.write_text(json.dumps(metadata, indent=2) + "\n")
        return dict(plumed={**metadata, "elapsed_steps": end - self.protocol["origin_step"],
                            "schedule_complete": end - self.protocol["origin_step"] >= self.protocol["duration_steps"]},
                    sampling_role=role, collective_variables_file=str(csv_file),
                    collective_variables_meta_file=str(meta_file))
