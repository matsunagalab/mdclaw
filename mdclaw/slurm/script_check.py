"""Read-only checks of the ``mdclaw`` commands inside a submitted script.

A job whose payload the CLI would refuse used to pass ``submit_job`` and die
on the compute node seconds after it started, when the agent had already left
and ``afterok`` cancelled the rest of the chain. MDDataBench v5 (2026-10):
14 attempts lost their first MD job to an argument the parser rejects
(``--nvt_time_ns``, ``--duration-ns``, ``--gpus 1`` inside the script), 3 to
a ``...`` pasted from an example command, and 28 more were refused with
"Cannot validate production CLI before submission: 2", argparse's message
having been swallowed. The parser the CLI builds runs here, before sbatch;
its message comes back with the corrected command when the fix is
unambiguous.

Only literal command lines are judged: a line the shell would evaluate
(``$VAR``, pipes, redirections, compound commands) is left to the job.
"""

from __future__ import annotations

import contextlib
import difflib
import inspect
import io
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import create_validation_error


_SHELL_EVAL = set("$`;&|<>()")
# Words that may stand in front of the mdclaw executable on a literal line.
_WRAPPER_WORDS = {"env", "exec", "time", "nohup", "command", "srun", "singularity", "apptainer"}
_INFORMATIONAL = {"--help", "-h", "--version", "--list", "--list-json", "--workflow"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")

# Text an example command carries in place of a value: "<job_dir>",
# "[--dependency afterok:<job>]", a bare "..." (the envelope's next.run_command
# ended in " ..." until 2026-10; three v5 attempts submitted it verbatim).
_ANGLE_PLACEHOLDER = re.compile(r"<[A-Za-z_][\w.:/-]*(?: [\w.:/-]+)*>")
_BRACKET_OPTION = re.compile(r"\[--[\w-]+[^\]]*\]")
_ELLIPSIS_TOKENS = {"...", "…"}


def find_placeholders(text: Optional[str]) -> list[str]:
    """Placeholder text left in a command or script (comment lines aside)."""
    found: list[str] = []
    for line in (text or "").replace("\\\n", " ").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern in (_BRACKET_OPTION, _ANGLE_PLACEHOLDER):
            for match in pattern.finditer(stripped):
                if match.group(0) not in found:
                    found.append(match.group(0))
        try:
            tokens = shlex.split(stripped, comments=True)
        except ValueError:
            tokens = stripped.split()
        for token in tokens:
            if token in _ELLIPSIS_TOKENS and token not in found:
                found.append(token)
    return found


def script_lines(command: str) -> list[str]:
    """The non-blank, non-comment lines of a script, continuations joined."""
    lines = []
    for raw in (command or "").replace("\\\n", " ").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def literal_mdclaw_tokens(line: str) -> Optional[tuple[list[str], list[str]]]:
    """``(prefix, argv)`` of a literal ``mdclaw ...`` or ``python -m mdclaw._cli
    ...`` line, or None for anything the shell would evaluate.

    ``prefix`` is what stands in front of the CLI's own arguments (the
    executable, and wrapper words or ``NAME=value`` assignments before it), so
    a corrected command keeps it.
    """
    if any(c in line for c in _SHELL_EVAL) or "\n" in line:
        return None
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if not tokens:
        return None
    index = 0
    while index < len(tokens) and _ASSIGNMENT.match(tokens[index]):
        index += 1
    if index < len(tokens) and Path(tokens[index]).name in _WRAPPER_WORDS:
        # A wrapper (srun, a deliberate container command): the CLI's
        # arguments start after the first token that names the executable.
        for later in range(index + 1, len(tokens)):
            if Path(tokens[later]).name == "mdclaw":
                return tokens[:later + 1], tokens[later + 1:]
        return None
    if index >= len(tokens):
        return None
    head = tokens[index]
    if Path(head).name == "mdclaw":
        return tokens[:index + 1], tokens[index + 1:]
    if Path(head).name in {"python", "python3"} and tokens[index + 1:index + 3] == ["-m", "mdclaw._cli"]:
        return tokens[:index + 3], tokens[index + 3:]
    return None


def literal_mdclaw_argv(line: str) -> Optional[list[str]]:
    parsed = literal_mdclaw_tokens(line)
    return parsed[1] if parsed else None


_TOOLS_CACHE: Optional[dict] = None


def tool_registry() -> dict:
    """The CLI's tool table: the running CLI's, else a one-off discovery."""
    global _TOOLS_CACHE
    from mdclaw import _cli

    if _cli._TOOLS:
        return _cli._TOOLS
    if _TOOLS_CACHE is None:
        _TOOLS_CACHE = _cli._discover_tools()
    return _TOOLS_CACHE


# ---------------------------------------------------------------------------
# Near-miss suggestions for arguments the parser does not know
# ---------------------------------------------------------------------------

_TIME_TO_NS = {"fs": 1e-6, "ps": 1e-3, "ns": 1.0, "us": 1e3}
_VALUE_WITH_UNIT = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(fs|ps|ns|us)?\s*$", re.I)
_UNIT_SUFFIX = re.compile(r"^(?P<stem>.+)-(?P<unit>fs|ps|ns|us)$")

# Stems agents used for a tool flag (flag names with hyphens and without a
# time unit). Applied only when the tool has the target flag.
_FLAG_STEMS: dict[str, set[str]] = {
    "--simulation-time-ns": {
        "duration", "time", "length", "sim-time", "simulation-time", "simulation-length",
        "production-time", "production-length", "production-duration", "prod-time",
        "total-time", "total-simulation-time", "run-time", "run-length", "md-time",
        "sim-length", "simulation-duration",
    },
    "--temperature-kelvin": {"temperature", "temp", "temp-k", "temperature-k"},
    "--pressure-bar": {"pressure"},
    "--random-seed": {"seed", "rng-seed"},
    "--device-index": {"device", "device-id", "gpu-id", "gpu-index", "cuda-device"},
    "--output-frequency-ps": {
        "output-frequency", "output-interval", "output-freq", "save-interval", "save-frequency",
        "report-interval", "report-frequency", "traj-interval", "trajectory-interval",
        "frame-interval", "write-interval", "dcd-interval",
    },
    "--timestep-fs": {"timestep", "time-step", "dt", "step-size"},
    "--max-iterations": {"iterations", "max-iter", "maxiter", "max-steps", "steps", "n-steps", "nsteps"},
    "--restraint-atoms": {"restraint-atom-selection", "restraint-selection", "restraint-atoms-selection",
                          "restraints", "restraint", "restrain"},
}
# Stems with more than one honest reading: named, never auto-corrected.
_AMBIGUOUS_STEMS: dict[str, tuple[str, ...]] = {
    "equilibration-time": ("--nvt-time-ns", "--npt-time-ns"),
    "eq-time": ("--nvt-time-ns", "--npt-time-ns"),
    "equil-time": ("--nvt-time-ns", "--npt-time-ns"),
    "simulation-time": ("--nvt-time-ns", "--npt-time-ns"),
}
# Targets whose value needs a stated unit before it can be carried over.
_TIME_FLAGS = {"--simulation-time-ns": "ns", "--output-frequency-ps": "ps", "--timestep-fs": "fs"}

# submit_job's own options, misplaced inside the script.
_SUBMIT_JOB_FLAGS = {
    "--gpus", "--gres", "--ntasks", "--nodes", "--cpus-per-task", "--time-limit", "--memory", "--mem",
    "--partition", "--account", "--qos", "--dependency", "--job-name", "--nodelist", "--extra-sbatch",
    "--environment", "--gpus-per-node", "--ntasks-per-node", "--output-dir-slurm",
}
# create_node's options: the node's parents and conditions are fixed there.
_CREATE_NODE_FLAGS = {"--continue-from", "--parent-node-ids", "--conditions", "--label", "--node-type",
                      "--dependency-node-ids"}
# Dynamics settings a minimization has no use for.
_IRRELEVANT_FLAGS = {
    "run_minimization": {"--timestep-fs", "--temperature-kelvin", "--pressure-bar", "--simulation-time-ns",
                         "--nvt-time-ns", "--npt-time-ns", "--nvt-steps", "--npt-steps", "--random-seed",
                         "--output-frequency-ps", "--ensemble"},
}
# Hints for flags that map onto another setting rather than onto one flag.
_FLAG_HINTS = {
    "ensemble": "the ensemble follows --pressure-bar (> 0: NPT, 0: NVT); there is no --ensemble flag",
}


def _normalize_flag(token: str) -> str:
    name = token.split("=", 1)[0].lstrip("-").lower().replace("_", "-")
    return f"--{name}"


def convert_time_value(value: str, from_unit: Optional[str], to_unit: str) -> Optional[str]:
    """``value`` (a number, optionally with a time unit) in ``to_unit``."""
    match = _VALUE_WITH_UNIT.match(str(value))
    if not match:
        return None
    unit = (match.group(2) or from_unit or "").lower()
    if unit not in _TIME_TO_NS:
        return None
    converted = float(match.group(1)) * _TIME_TO_NS[unit] / _TIME_TO_NS[to_unit]
    return f"{converted:.12g}"


@dataclass
class Suggestion:
    argument: list[str]
    replacement: Optional[list[str]]  # None: drop the argument
    confident: bool
    reason: str
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"argument": " ".join(self.argument), "reason": self.reason,
                               "confident": self.confident}
        if self.replacement is not None:
            out["replacement"] = " ".join(self.replacement)
        else:
            out["replacement"] = None
        if self.candidates:
            out["candidates"] = self.candidates
        return out


def _suggest(tool: str, group: list[str], tool_flags: set[str]) -> Suggestion:
    """What an unknown argument group (a flag and its values) probably meant."""
    head = group[0]
    if not head.startswith("-"):
        return Suggestion(group, None, False, f"unexpected value {head!r} with no flag in front of it")
    values = group[1:]
    if "=" in head:
        values = [head.split("=", 1)[1], *values]
    flag = _normalize_flag(head)
    name = flag[2:]
    if flag in tool_flags:
        return Suggestion(group, [flag, *values], True, "flags are spelled with hyphens")
    unit_match = _UNIT_SUFFIX.match(name)
    stem, unit = (unit_match.group("stem"), unit_match.group("unit")) if unit_match else (name, None)
    if unit:
        for other in ("ns", "ps", "fs"):
            target = f"--{stem}-{other}"
            if other != unit and target in tool_flags:
                converted = convert_time_value(values[0], unit, other) if len(values) == 1 else None
                if converted is not None:
                    return Suggestion(group, [target, converted], True,
                                      f"{tool} takes {target} ({values[0]} {unit} = {converted} {other})")
                return Suggestion(group, None, False, f"{tool} takes {target} (in {other})", [target])
    if flag == "--time" and values and re.match(r"^\d+(?:-\d+)?:\d", values[0]):
        return Suggestion(group, None, True,
                          "--time HH:MM:SS is the job's wall time: pass --time-limit to submit_job, "
                          "not inside --script")
    for target, stems in _FLAG_STEMS.items():
        if stem not in stems or target not in tool_flags:
            continue
        target_unit = _TIME_FLAGS.get(target)
        if target_unit is None:
            return Suggestion(group, [target, *values], True, f"{tool} takes {target}")
        value_unit = None
        if len(values) == 1:
            match = _VALUE_WITH_UNIT.match(values[0])
            value_unit = match.group(2) if match else None
        if len(values) == 1 and (unit or value_unit):
            converted = convert_time_value(values[0], unit, target_unit)
            if converted is not None:
                return Suggestion(group, [target, converted], True,
                                  f"{tool} takes {target} ({values[0]}{' ' + unit if unit else ''} = "
                                  f"{converted} {target_unit})")
        return Suggestion(group, None, False, f"{tool} takes {target}, in {target_unit}", [target])
    if stem in _AMBIGUOUS_STEMS:
        options = [t for t in _AMBIGUOUS_STEMS[stem] if t in tool_flags]
        if options:
            return Suggestion(group, None, False,
                              f"{tool} sets each stage on its own: {' and/or '.join(options)}", options)
    if flag in _SUBMIT_JOB_FLAGS:
        return Suggestion(group, None, True,
                          f"{flag} is a submit_job option: pass it to submit_job, not inside --script")
    if flag in _CREATE_NODE_FLAGS:
        return Suggestion(group, None, True,
                          f"{flag} belongs to create_node (it fixes the node's parents and conditions); "
                          f"{tool} reads them from the node")
    if flag in _IRRELEVANT_FLAGS.get(tool, set()):
        return Suggestion(group, None, True, f"{tool} has no {flag} (no dynamics run); drop it")
    if name in _FLAG_HINTS:
        return Suggestion(group, None, False, _FLAG_HINTS[name])
    names = sorted(f[2:] for f in tool_flags)
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    if close:
        best = close[0]
        ratio = difflib.SequenceMatcher(None, name, best).ratio()
        if len(close) == 1 and ratio >= 0.85:
            return Suggestion(group, [f"--{best}", *values], True, f"did you mean --{best}?")
        return Suggestion(group, None, False, "did you mean " + " or ".join(f"--{c}" for c in close) + "?",
                          [f"--{c}" for c in close])
    return Suggestion(group, None, False, f"{tool} has no {flag}; see mdclaw --list-json {tool}")


def _unknown_groups(extras: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    for token in extras:
        if token.startswith("-") and not _NUMBER.match(token):
            groups.append([token])
        elif groups and groups[-1][0].startswith("-"):
            groups[-1].append(token)
        else:
            groups.append([token])
    return groups


def _replace_groups(argv: list[str], fixes: list[tuple[list[str], Optional[list[str]]]]) -> Optional[list[str]]:
    """argv with each unknown group replaced (None drops it), or None when a
    group cannot be located."""
    out = list(argv)
    position = 0
    for group, replacement in fixes:
        index = None
        for i in range(position, len(out) - len(group) + 1):
            if out[i:i + len(group)] == group:
                index = i
                break
        if index is None:
            return None
        new = replacement or []
        out[index:index + len(group)] = new
        position = index + len(new)
    return out


# ---------------------------------------------------------------------------
# Parsing one literal command the way the CLI would
# ---------------------------------------------------------------------------


@dataclass
class CommandCheck:
    line: str
    prefix: list[str]
    argv: list[str]
    tool: Optional[str] = None
    info: Optional[dict] = None
    values: dict = field(default_factory=dict)
    explicit: set = field(default_factory=set)
    json_payload: Optional[dict] = None
    target_job_dir: Optional[str] = None
    target_node_id: Optional[str] = None
    informational: bool = False
    error: Optional[str] = None           # the parser's own message
    problems: list[str] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    corrected_argv: Optional[list[str]] = None

    @property
    def command(self) -> str:
        return shlex.join([*self.prefix, *self.argv])

    @property
    def corrected_command(self) -> Optional[str]:
        if self.corrected_argv is None:
            return None
        return shlex.join([*self.prefix, *self.corrected_argv])

    @property
    def ok(self) -> bool:
        return not self.error and not self.problems


def _argparse_message(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if "error:" in line:
            return line
    return lines[-1] if lines else "the command line could not be parsed"


def _parse_argv(argv: list[str], tool: str, info: dict):
    """``(namespace, extras, error_message)`` from the CLI's own parser."""
    from mdclaw import _cli

    specs = _cli._tool_param_specs(info["fn"], requires_node=bool(info.get("requires_node")))
    hoisted = _cli._hoist_global_options(argv, {spec.cli_flag for spec in specs})
    parser = _cli._build_parser({tool: info})
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(io.StringIO()):
            namespace, extras = parser.parse_known_args(hoisted)
    except SystemExit:
        return None, [], _argparse_message(buffer.getvalue()), hoisted
    return namespace, extras, None, hoisted


_INVALID_VALUE = re.compile(r"argument (--[\w-]+): invalid (?:float|int) value: '([^']*)'")


def _repair_value(argv: list[str], message: str) -> Optional[tuple[list[str], Suggestion]]:
    """``--simulation-time-ns 100ps`` -> ``--simulation-time-ns 0.1``."""
    match = _INVALID_VALUE.search(message or "")
    if not match:
        return None
    flag, value = match.group(1), match.group(2)
    if re.match(r"^\d+(?:-\d+)?:\d", value):
        # "--time 00:20:00": argparse read --time as an abbreviation of a
        # tool flag (--timestep-fs); it is the job's wall time.
        for i, token in enumerate(argv[:-1]):
            if argv[i + 1] == value and token.startswith("--") and flag.startswith(token):
                out = argv[:i] + argv[i + 2:]
                return out, Suggestion([token, value], None, True,
                                       f"{token} {value} is the job's wall time: pass --time-limit to "
                                       "submit_job, not inside --script")
        return None
    unit_match = _UNIT_SUFFIX.match(flag[2:])
    if not unit_match:
        return None
    converted = convert_time_value(value, None, unit_match.group("unit"))
    if converted is None:
        return None
    out = list(argv)
    for i, token in enumerate(out):
        if token == flag and i + 1 < len(out) and out[i + 1] == value:
            out[i + 1] = converted
            return out, Suggestion([flag, value], [flag, converted], True,
                                   f"{flag} takes a number in {unit_match.group('unit')}")
        if token == f"{flag}={value}":
            out[i] = f"{flag}={converted}"
            return out, Suggestion([token], [f"{flag}={converted}"], True,
                                   f"{flag} takes a number in {unit_match.group('unit')}")
    return None


def _command_values(namespace, info, specs, argv, json_payload):
    from mdclaw import _cli

    values: dict[str, Any] = {}
    raw = vars(namespace)
    for spec in specs:
        default = None if spec.default is inspect.Parameter.empty else spec.default
        if json_payload is not None:
            value = json_payload.get(spec.name, default)
            if value is not None and spec.name in json_payload:
                try:
                    value = _cli._coerce_value(value, spec.hint)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
        else:
            value = raw.get(spec.name, default)
        values[spec.name] = value
    explicit = _cli._explicit_parameter_names(argv, specs, list(json_payload or {}))
    return values, explicit


def check_command(line: str, prefix: list[str], argv: list[str], *,
                  job_dir: Optional[str] = None, node_id: Optional[str] = None,
                  _depth: int = 0) -> CommandCheck:
    """Parse one literal mdclaw command exactly as the CLI would and say what
    it would refuse; ``corrected_argv`` when every fix is unambiguous."""
    from mdclaw import _cli

    check = CommandCheck(line=line, prefix=list(prefix), argv=list(argv))

    def recheck(candidate: list[str]) -> Optional[CommandCheck]:
        # A correction is re-checked as its own command, at most a few
        # levels deep and never when it did not change the command.
        if _depth >= 4 or list(candidate) == list(argv):
            return None
        return check_command(line, prefix, candidate, job_dir=job_dir, node_id=node_id, _depth=_depth + 1)
    if any(a.split("=", 1)[0] in _INFORMATIONAL for a in argv):
        check.informational = True
        return check
    tool = _cli._detect_subcommand(argv)
    if tool is None:
        check.informational = True
        return check
    tools = tool_registry()
    if tool not in tools:
        check.tool = tool
        replacement = _cli._RENAMED_TOOLS.get(tool)
        candidates = difflib.get_close_matches(tool, list(tools), n=3, cutoff=0.75)
        remainder = argv[argv.index(tool) + 1:]
        # "mdclaw slurm submit_job" / "mdclaw slurm.submit_job": a namespace in front of a tool.
        stripped = tool.split(".")[-1]
        if stripped in tools and stripped != tool:
            check.corrected_argv = [*argv[:argv.index(tool)], stripped, *remainder]
        elif remainder and _cli._detect_subcommand(remainder) in tools and not remainder[0].startswith("-"):
            check.corrected_argv = [*argv[:argv.index(tool)], *remainder]
        elif len(candidates) == 1 and difflib.SequenceMatcher(None, tool, candidates[0]).ratio() >= 0.85:
            check.corrected_argv = [*argv[:argv.index(tool)], candidates[0], *remainder]
        check.error = f"Unknown MDClaw tool {tool!r}"
        if replacement:
            check.problems.append(f"{tool} was consolidated; use: mdclaw {replacement}")
        elif candidates:
            check.problems.append("did you mean: " + ", ".join(candidates) + "?")
        else:
            check.problems.append("mdclaw --list names every tool")
        if check.corrected_argv:
            again = recheck(check.corrected_argv)
            check.corrected_argv = None if again is None else (again.argv if again.ok else again.corrected_argv)
        return check
    info = tools[tool]
    check.tool, check.info = tool, info
    if info.get("contract_error"):
        check.error = f"Tool {tool!r} cannot be run: its CLI contract is invalid ({info['contract_error']})"
        return check
    specs = _cli._tool_param_specs(info["fn"], requires_node=bool(info.get("requires_node")))
    spec_by_name = {spec.name: spec for spec in specs}
    tool_flags = {spec.cli_flag for spec in specs} | {f"--no-{s.name.replace('_', '-')}" for s in specs
                                                     if s.inner is bool} | {"--json-input"}

    namespace, extras, message, hoisted = _parse_argv(argv, tool, info)
    working = hoisted
    repaired_value = False
    if message:
        repaired = _repair_value(working, message)
        if repaired is None:
            check.error = message
            return check
        # "--simulation-time-ns 100ps": the original is still refused (with
        # the parser's message); the repaired line is the correction.
        working, suggestion = repaired
        check.suggestions.append(suggestion)
        check.error = message
        repaired_value = True
        namespace, extras, message, working = _parse_argv(working, tool, info)
        if message:
            return check
    if extras:
        groups = _unknown_groups(extras)
        suggestions = [_suggest(tool, group, tool_flags) for group in groups]
        check.suggestions.extend(suggestions)
        check.error = f"mdclaw {tool}: error: unrecognized arguments: {' '.join(extras)}"
        if all(s.confident for s in suggestions):
            fixed = _replace_groups(working, [(s.argument, s.replacement) for s in suggestions])
            again = recheck(fixed) if fixed is not None else None
            if again is not None:
                check.corrected_argv = again.argv if again.ok else again.corrected_argv
        return check

    json_payload = None
    if namespace.json_input:
        try:
            json_payload = json.loads(namespace.json_input)
        except json.JSONDecodeError as exc:
            check.error = f"--json-input is not valid JSON: {exc.msg}"
            return check
        if not isinstance(json_payload, dict):
            check.error = "--json-input must be a JSON object of parameter names to values"
            return check
        unknown = sorted(str(k) for k in json_payload if k not in spec_by_name)
        if unknown:
            check.error = f"{tool} does not accept: {', '.join(unknown)}"
            for key in unknown:
                close = difflib.get_close_matches(key, list(spec_by_name), n=2, cutoff=0.6)
                if close:
                    check.problems.append(f"{key!r}: did you mean {' or '.join(repr(c) for c in close)}?")
            return check
    check.json_payload = json_payload
    values, explicit = _command_values(namespace, info, specs, working, json_payload)
    check.values, check.explicit = values, explicit
    check.target_job_dir = namespace._global_job_dir or values.get("job_dir")
    check.target_node_id = namespace._global_node_id or values.get("node_id")
    if check.target_job_dir:
        values["job_dir"] = check.target_job_dir
    if check.target_node_id:
        values["node_id"] = check.target_node_id

    missing = [spec.cli_flag for spec in specs
               if values.get(spec.name) is None and spec.default is inspect.Parameter.empty
               and not spec.optional]
    if missing:
        check.problems.append(f"{tool} is missing required arguments: {', '.join(missing)}")

    fixed = list(working)
    requires_node = bool(info.get("requires_node"))
    takes_context = "job_dir" in spec_by_name or "node_id" in spec_by_name
    if requires_node:
        absent = [flag for flag, have in (("--job-dir", check.target_job_dir), ("--node-id", check.target_node_id))
                  if not have]
        if absent:
            check.problems.append(
                f"{tool} is a workflow tool and runs only on a node: the command has no "
                f"{' / '.join(absent)}")
            if job_dir and node_id:
                insert = []
                if not check.target_job_dir:
                    insert += ["--job-dir", str(job_dir)]
                if not check.target_node_id:
                    insert += ["--node-id", str(node_id)]
                fixed = insert + fixed
        elif job_dir and node_id:
            same_job = Path(check.target_job_dir).resolve() == Path(job_dir).resolve()
            if not same_job or check.target_node_id != node_id:
                check.problems.append(
                    f"the command runs {tool} on --job-dir {check.target_job_dir} --node-id "
                    f"{check.target_node_id}, but this submission is for --job-dir {job_dir} "
                    f"--node-id {node_id}")
                script_node = Path(check.target_job_dir) / "nodes" / str(check.target_node_id) / "node.json"
                if not script_node.is_file() and (Path(job_dir) / "nodes" / str(node_id) / "node.json").is_file():
                    fixed = _set_context(fixed, str(job_dir) if not same_job else None,
                                         node_id if check.target_node_id != node_id else None)
                else:
                    fixed = None
    elif (namespace._global_job_dir or namespace._global_node_id) and not takes_context:
        check.problems.append(
            f"{tool} is a standalone helper: --job-dir/--node-id do not apply and the CLI refuses them")
        fixed = _set_context(fixed, None, None, drop=True)
    if check.problems and fixed is not None and not missing:
        again = recheck(fixed)
        if again is not None and again.ok:
            check.corrected_argv = again.argv
    elif repaired_value and not check.problems:
        check.corrected_argv = working
    return check


def _set_context(argv: list[str], job_dir: Optional[str], node_id: Optional[str], *,
                 drop: bool = False) -> list[str]:
    out: list[str] = []
    skip = False
    for i, token in enumerate(argv):
        if skip:
            skip = False
            continue
        name = token.split("=", 1)[0]
        if name in {"--job-dir", "--node-id"}:
            value = job_dir if name == "--job-dir" else node_id
            if drop:
                skip = "=" not in token
                continue
            if value is not None:
                if "=" in token:
                    out.append(f"{name}={value}")
                else:
                    out.extend([name, value])
                    skip = True
                continue
        out.append(token)
    return out


# ---------------------------------------------------------------------------
# The whole script
# ---------------------------------------------------------------------------


@dataclass
class ScriptCheck:
    commands: list[CommandCheck]
    opaque_lines: int
    error: Optional[dict] = None


def _quote_script(command: str) -> str:
    return shlex.quote(command)


def placeholder_error(field_name: str, found: list[str], *, command: Optional[str] = None,
                      corrected: Optional[str] = None) -> dict:
    listed = ", ".join(repr(p) for p in found)
    message = (f"{field_name} still holds placeholder text from an example ({listed}); the job would "
               "pass it to the CLI literally and fail on the compute node")
    hints = ["Replace each placeholder with the real value: the node's --job-dir and --node-id, the "
             "tool's own arguments (mdclaw --list-json <tool>) in place of '...', the job id an "
             "earlier submit_job returned in place of <job>."]
    if "..." in found or "…" in found:
        hints.append("'...' stands for the stage tool's arguments; drop it to run with the defaults, "
                     "or put the arguments the node declares there.")
    error = create_validation_error(
        field_name, message,
        expected="literal values only", actual=listed,
        hints=hints, code="script_placeholder_unfilled",
        context_extra={"placeholders": found},
    )
    if corrected:
        error["corrected_command"] = corrected
        error["next_action"] = f"Resubmit with --script {_quote_script(corrected)}"
    elif field_name == "dependency":
        error["next_action"] = ("Resubmit with the placeholder in --dependency replaced by the slurm_job_id "
                                "the parent's submit_job returned, or without --dependency: submit_job adds "
                                "the afterok on a queued parent's job itself")
    else:
        error["next_action"] = "Replace the placeholders in --script with real values and resubmit"
    return error


def _placeholder_fix(command: str, found: list[str], job_dir: Optional[str], node_id: Optional[str]) -> Optional[str]:
    """The command with placeholders filled where the value is known."""
    lines = script_lines(command)
    if len(lines) != 1:
        return None
    text = lines[0]
    known = {"<job_dir>": job_dir, "<jd>": job_dir, "<job-dir>": job_dir,
             "<node_id>": node_id, "<node-id>": node_id, "<nid>": node_id}
    for placeholder in found:
        lowered = placeholder.lower()
        if lowered in known and known[lowered]:
            text = text.replace(placeholder, shlex.quote(str(known[lowered])))
        elif placeholder in _ELLIPSIS_TOKENS:
            text = " ".join(tok for tok in text.split(" ") if tok not in _ELLIPSIS_TOKENS)
        elif _BRACKET_OPTION.fullmatch(placeholder):
            text = text.replace(placeholder, " ").strip()
        else:
            return None
    text = " ".join(text.split())
    if find_placeholders(text):
        return None
    parsed = literal_mdclaw_tokens(text)
    if parsed is None:
        return None
    again = check_command(text, parsed[0], parsed[1], job_dir=job_dir, node_id=node_id)
    if again.informational or not again.tool:
        return None
    return again.command if again.ok else again.corrected_command


def check_script(command: str, *, job_dir: Optional[str] = None, node_id: Optional[str] = None,
                 field_name: str = "script", script_file: Optional[str] = None) -> ScriptCheck:
    """Every literal mdclaw line of ``command``, parsed as the CLI would;
    ``error`` is the refusal for the first line the CLI would reject."""
    found = find_placeholders(command)
    if found:
        corrected = _placeholder_fix(command, found, job_dir, node_id) if not script_file else None
        return ScriptCheck([], 0, placeholder_error(field_name, found, command=command, corrected=corrected))
    checks: list[CommandCheck] = []
    opaque = 0
    for line in script_lines(command):
        parsed = literal_mdclaw_tokens(line)
        if parsed is None:
            if re.search(r"(?:^|[\s/;&|(])mdclaw(?:\s|$)|mdclaw\._cli", line):
                opaque += 1
            continue
        prefix, argv = parsed
        check = check_command(line, prefix, argv, job_dir=job_dir, node_id=node_id)
        checks.append(check)
        if not check.ok and not check.informational:
            return ScriptCheck(checks, opaque, _arguments_error(check, field_name, script_file, len(script_lines(command))))
    return ScriptCheck(checks, opaque)


def _arguments_error(check: CommandCheck, field_name: str, script_file: Optional[str], n_lines: int) -> dict:
    reasons = [check.error] if check.error else []
    reasons += check.problems
    message = (f"The CLI would refuse this command when the job starts: {'; '.join(reasons)}")
    hints = [s.reason if not s.candidates else f"{' '.join(s.argument)}: {s.reason}"
             for s in check.suggestions]
    if check.tool and check.info:
        hints.append(f"Every parameter of {check.tool}: mdclaw --list-json {check.tool}")
    error = create_validation_error(
        field_name, message,
        expected="a command line the mdclaw CLI accepts",
        actual=check.command,
        hints=hints,
        code="script_arguments_invalid",
        context_extra={"tool": check.tool, "argparse_error": check.error,
                       "problems": check.problems},
    )
    error["suggestions"] = [s.as_dict() for s in check.suggestions]
    corrected = check.corrected_command
    if corrected:
        error["corrected_command"] = corrected
        if script_file or n_lines > 1:
            where = f"the script file {script_file}" if script_file else "the script"
            error["next_action"] = f"In {where}, replace the line `{check.command}` with `{corrected}`, then resubmit"
        else:
            error["next_action"] = f"Resubmit with --script {_quote_script(corrected)}"
    else:
        error["next_action"] = (f"Fix the command ({'; '.join(reasons)}); check it with "
                                f"mdclaw --list-json {check.tool or '<tool>'}, then resubmit")
    return error
