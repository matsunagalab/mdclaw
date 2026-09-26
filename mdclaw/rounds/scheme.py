"""Sampling schemes: what a round-driven run is, and how its nodes are named.

A scheme lives under ``progress.json.params.sampling_schemes[<scheme_id>]``.
It names the stage tool that runs one segment, the completed node(s) the
first round starts from, how many replicas run, and the policy that plans
the next round: the built-in ``replicas`` rule (every replica continues) or
an analyze tool that writes a ``next_round`` artifact (``we_resample``).

Node ids follow the structured rule of ``create_node(_node_id=...)``:

- segment (``prod``): ``prod_<scheme>_r<round>_w<replica>[_t<attempt>]``
- policy node (``analyze``): ``analyze_<scheme>_r<round>[_t<attempt>]``

so a directory listing sorts by scheme, round and replica, and the state of
a scheme is read back from the ids and statuses in ``progress.json`` alone.
"""

from __future__ import annotations

import inspect
import json
import math
import random
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from mdclaw._common import setup_logger
from mdclaw._lock import file_lock
from mdclaw._tool_meta import tool_node_type
from mdclaw.node.constants import ID_LIST_CAP
from mdclaw.node.io import _atomic_write_json, _read_artifact_from_node, _read_node_json
from mdclaw.node.progress import _load_progress_v3
from mdclaw.rounds.owner import owner_liveness

logger = setup_logger(__name__)

PARAMS_KEY = "sampling_schemes"
SCHEME_SCHEMA_VERSION = 1
REPLICAS_POLICY = "replicas"
DEFAULT_STAGE_TOOL = "run_production"
MAX_ATTEMPTS = 3

SCHEME_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
SEGMENT_ID_RE = re.compile(
    r"^prod_(?P<scheme>[a-z][a-z0-9]{0,15})_r(?P<round>\d{4})_w(?P<replica>\d{4})"
    r"(?:_t(?P<attempt>\d{4}))?$"
)
POLICY_ID_RE = re.compile(
    r"^analyze_(?P<scheme>[a-z][a-z0-9]{0,15})_r(?P<round>\d{4})(?:_t(?P<attempt>\d{4}))?$"
)

# Arguments the driver owns per segment; a scheme cannot fix them.
_RESERVED_STAGE_ARGS = frozenset({
    "job_dir", "node_id", "random_seed", "restart_from", "state_xml_file",
    "system_xml_file", "topology_pdb_file", "output_dir", "name", "continue_from",
})
_START_NODE_TYPES = frozenset({"eq", "prod"})
_SEED_MODULUS = 2_147_483_647
# The temperature of a scheme whose start nodes record none, and of every
# scheme recorded before setup_rounds pinned one: run_production's former
# default, so the segments of a running scheme keep the temperature they ran at.
DEFAULT_SEGMENT_TEMPERATURE_K = 300.0
_TEMPERATURE_TOLERANCE_K = 1e-6

# Test hook: callables consulted before the tool registry.
_TOOL_OVERRIDES: dict[str, Callable] = {}
_TOOLS_CACHE: Optional[dict] = None


class RoundsError(Exception):
    """Structured failure with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def segment_node_id(scheme_id: str, round_index: int, replica: int, attempt: int = 0) -> str:
    suffix = f"_t{attempt:04d}" if attempt else ""
    return f"prod_{scheme_id}_r{round_index:04d}_w{replica:04d}{suffix}"


def policy_node_id(scheme_id: str, round_index: int, attempt: int = 0) -> str:
    suffix = f"_t{attempt:04d}" if attempt else ""
    return f"analyze_{scheme_id}_r{round_index:04d}{suffix}"


def segment_label(scheme_id: str, round_index: int, replica: int) -> str:
    return f"{scheme_id}:r{round_index}:w{replica}"


def segment_seed(base_seed: int, round_index: int, replica: int, attempt: int = 0) -> int:
    """A distinct, deterministic non-zero seed per (round, replica, attempt).

    Siblings restarted from one ancestor with one seed integrate the same
    noise (``run_production`` derives the effective seed from the seed and
    the ancestor's step count), so every segment gets its own.
    """
    value = (int(base_seed) * 1_000_003 + int(round_index) * 10_007
             + int(replica) * 101 + int(attempt))
    return value % _SEED_MODULUS + 1


# ---------------------------------------------------------------------------
# tool lookup
# ---------------------------------------------------------------------------


def resolve_tool(name: str) -> tuple[Callable, Optional[str]]:
    """``(function, node_type)`` of a registered tool (or a test override)."""
    global _TOOLS_CACHE
    fn = _TOOL_OVERRIDES.get(name)
    if fn is not None:
        return fn, tool_node_type(fn)
    if _TOOLS_CACHE is None:
        from mdclaw._cli import _discover_tools

        _TOOLS_CACHE = _discover_tools()
    info = _TOOLS_CACHE.get(name)
    if info is None:
        raise RoundsError(
            code="rounds_tool_invalid",
            message=f"tool {name!r} is not registered (check the name with mdclaw --list-json {name})",
        )
    return info["fn"], info.get("node_type") or tool_node_type(info["fn"])


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _progress_path(job_dir: str) -> Path:
    return Path(job_dir).resolve() / "progress.json"


def _read_progress(job_dir: str) -> dict:
    jd = Path(job_dir).resolve()
    if not jd.is_dir():
        # Inside a container only the bound directories exist: a job_dir that
        # is not visible from here is not a job to bootstrap. Say so before
        # an agent "fixes" an existing job by creating a second one.
        raise RoundsError(
            code="rounds_job_dir_unreachable",
            message=(f"{jd} does not exist from this process. Inside a container that usually means the "
                     "directory is not bound: run from the job's parent directory or bind it "
                     "(the launcher binds the working directory and absolute path arguments)."),
        )
    try:
        progress = _load_progress_v3(jd / "progress.json")
    except ValueError as exc:
        raise RoundsError(code="rounds_job_invalid", message=str(exc)) from exc
    if progress is None:
        raise RoundsError(
            code="rounds_job_invalid",
            message=f"{jd} exists but has no progress.json; bootstrap the job first",
        )
    return progress


def read_scheme(job_dir: str, scheme_id: str) -> dict:
    schemes = (_read_progress(job_dir).get("params") or {}).get(PARAMS_KEY) or {}
    scheme = schemes.get(scheme_id) if isinstance(schemes, dict) else None
    if not isinstance(scheme, dict):
        known = sorted(schemes) if isinstance(schemes, dict) else []
        raise RoundsError(
            code="rounds_scheme_missing",
            message=f"no sampling scheme {scheme_id!r} in {job_dir}"
                    + (f"; recorded schemes: {known}" if known else "; none recorded yet"),
        )
    return scheme


def write_scheme(job_dir: str, scheme: dict, *, overwrite: bool = False) -> None:
    jd = Path(job_dir).resolve()
    with file_lock(jd / "progress.lock"):
        progress = _read_progress(job_dir)
        params = progress.setdefault("params", {})
        schemes = params.setdefault(PARAMS_KEY, {})
        existing = schemes.get(scheme["scheme_id"])
        if existing is not None and not overwrite:
            raise RoundsError(
                code="rounds_scheme_exists",
                message=f"scheme {scheme['scheme_id']!r} is already recorded; pass overwrite=True "
                        "to replace it before its first round runs",
            )
        if existing is not None and _scheme_has_nodes(progress.get("nodes") or {}, scheme["scheme_id"]):
            raise RoundsError(
                code="rounds_scheme_exists",
                message=f"scheme {scheme['scheme_id']!r} already has rounds in this job; a scheme "
                        "cannot change under its nodes. Use a new scheme_id.",
            )
        schemes[scheme["scheme_id"]] = scheme
        _atomic_write_json(jd / "progress.json", progress)


def _scheme_has_nodes(nodes_index: dict, scheme_id: str) -> bool:
    for nid in nodes_index:
        match = SEGMENT_ID_RE.match(nid) or POLICY_ID_RE.match(nid)
        if match and match.group("scheme") == scheme_id:
            return True
    return False


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _bad(message: str) -> RoundsError:
    return RoundsError(code="rounds_scheme_invalid", message=message)


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _bad(f"{field} must be an integer >= {minimum} (got {value!r})")
    return value


def normalize_scheme(spec: Any, *, job_dir: str) -> dict:
    """Validate a scheme spec against the job and return its stored form."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            raise _bad(f"scheme must be a JSON object: {exc}") from exc
    if not isinstance(spec, dict):
        raise _bad("scheme must be a JSON object")
    known = {
        "scheme_id", "policy", "policy_args", "stage_tool", "stage_args", "start",
        "initial_weights", "segment_conditions", "seed", "max_rounds", "description",
    }
    unknown = sorted(set(spec) - known)
    if unknown:
        raise _bad(f"unknown scheme fields {unknown}; known: {sorted(known)}")

    scheme_id = spec.get("scheme_id")
    if not isinstance(scheme_id, str) or not SCHEME_ID_RE.match(scheme_id):
        raise _bad("scheme_id must match [a-z][a-z0-9]{0,15} (lowercase, no underscore), "
                   f"got {scheme_id!r}")

    policy = spec.get("policy", REPLICAS_POLICY)
    if not isinstance(policy, str) or not policy:
        raise _bad("policy must be 'replicas' or the name of an analyze tool")
    if policy != REPLICAS_POLICY:
        _fn, node_type = resolve_tool(policy)
        if node_type != "analyze":
            raise RoundsError(
                code="rounds_tool_invalid",
                message=f"policy {policy!r} is not an analyze-stage tool (it runs on the round's "
                        "analyze node and writes next_round.json)",
            )
    policy_args = spec.get("policy_args") or {}
    if not isinstance(policy_args, dict):
        raise _bad("policy_args must be a JSON object")

    stage_tool = spec.get("stage_tool", DEFAULT_STAGE_TOOL)
    stage_fn, stage_type = resolve_tool(stage_tool)
    if stage_type != "prod":
        raise RoundsError(
            code="rounds_tool_invalid",
            message=f"stage_tool {stage_tool!r} is not a prod-stage tool "
                    "(run_production, run_sst2)",
        )
    stage_args = spec.get("stage_args") or {}
    if not isinstance(stage_args, dict):
        raise _bad("stage_args must be a JSON object of stage tool arguments")
    reserved = sorted(set(stage_args) & _RESERVED_STAGE_ARGS)
    if reserved:
        raise _bad(f"stage_args must not set {reserved}; the driver sets them per segment")

    start = spec.get("start")
    if not isinstance(start, dict):
        raise _bad('start must be {"node_ids": [...], "n_replicas": N}')
    node_ids = start.get("node_ids")
    if not isinstance(node_ids, list) or not node_ids or not all(isinstance(n, str) for n in node_ids):
        raise _bad("start.node_ids must be a non-empty list of node ids")
    n_replicas = _positive_int(start.get("n_replicas"), "start.n_replicas")
    _validate_start_nodes(job_dir, node_ids)

    initial_weights = spec.get("initial_weights")
    if initial_weights is None and policy != REPLICAS_POLICY:
        initial_weights = "uniform"
    if initial_weights is not None and initial_weights != "uniform":
        if (not isinstance(initial_weights, list) or len(initial_weights) != n_replicas
                or any(isinstance(w, bool) or not isinstance(w, (int, float))
                       or not math.isfinite(w) or w <= 0 for w in initial_weights)):
            raise _bad("initial_weights must be 'uniform' or one positive number per replica")
        total = float(sum(initial_weights))
        if abs(total - 1.0) > 1e-9:
            raise _bad(f"initial_weights must sum to 1 (got {total!r})")
        initial_weights = [float(w) for w in initial_weights]

    segment_conditions = spec.get("segment_conditions") or {}
    if not isinstance(segment_conditions, dict):
        raise _bad("segment_conditions must be a JSON object")
    if "random_seed" in segment_conditions:
        raise _bad("segment_conditions must not declare random_seed; the driver declares it")

    # One temperature per scheme, pinned before any segment runs. Round 1 and
    # recycled walkers restart from start / basis nodes, continued, split and
    # merged walkers from their parent segment, and run_production takes an
    # omitted temperature from the node it restarts from: start nodes at
    # different temperatures would put walkers of one ensemble at different
    # temperatures. Before that inheritance a scheme started from a 310 K eq
    # ran every segment at the 300 K default — the rounds form of
    # 010_membrane_6kux (MDDataBench 3cond, 2026-09-26: eq at 310 K,
    # production without the flag at 300 K).
    basis = policy_args.get("basis_node_ids")
    basis = [b for b in basis if isinstance(b, str)] if isinstance(basis, list) else []
    temperature = resolve_segment_temperature(
        job_dir, stage_tool=stage_tool, stage_fn=stage_fn, stage_args=stage_args,
        node_ids=[*node_ids, *basis], segment_conditions=segment_conditions,
    )

    seed = spec.get("seed")
    if seed is None:
        seed = random.SystemRandom().randrange(1, _SEED_MODULUS)
    seed = _positive_int(seed, "seed")

    max_rounds = spec.get("max_rounds")
    if max_rounds is not None:
        max_rounds = _positive_int(max_rounds, "max_rounds")

    description = spec.get("description")
    if description is not None and not isinstance(description, str):
        raise _bad("description must be a string")

    # A weighted-ensemble policy is checked here, before any segment runs:
    # its arguments, its CVs on this topology, and that the start structures
    # lie outside the target. Found the hard way (2026-09-21): a selection
    # typo surfaced only after the first round's MD, when the scheme could
    # no longer be overwritten.
    start_pcoords = None
    if policy == "we_resample":
        from mdclaw.analyze.cv import CVError
        from mdclaw.we.policy import WEError, validate_scheme_policy
        from mdclaw.we.resample import ResampleError

        try:
            check = validate_scheme_policy(
                job_dir, {"policy_args": policy_args, "start": {"node_ids": list(node_ids)}},
            )
        except (WEError, CVError, ResampleError) as exc:
            raise RoundsError(code=exc.code, message=f"policy_args: {exc}") from exc
        start_pcoords = check["start_pcoords"]

    return {
        "schema_version": SCHEME_SCHEMA_VERSION,
        "scheme_id": scheme_id,
        "description": description,
        "policy": policy,
        "policy_args": policy_args,
        "stage_tool": stage_tool,
        "stage_args": stage_args,
        "start": {"node_ids": list(node_ids), "n_replicas": n_replicas},
        "initial_weights": initial_weights,
        "segment_conditions": segment_conditions,
        "seed": seed,
        "max_rounds": max_rounds,
        "start_pcoords": start_pcoords,
        "segment_temperature_kelvin": temperature["segment_temperature_kelvin"],
        "segment_temperature_source": temperature["segment_temperature_source"],
        "start_temperatures_kelvin": temperature["node_temperatures"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _validate_start_nodes(job_dir: str, node_ids: list[str]) -> None:
    index = _read_progress(job_dir).get("nodes") or {}
    for nid in node_ids:
        info = index.get(nid)
        if info is None:
            raise RoundsError(code="rounds_start_node_invalid",
                              message=f"start node {nid!r} does not exist in this job")
        if info.get("type") not in _START_NODE_TYPES:
            raise RoundsError(code="rounds_start_node_invalid",
                              message=f"start node {nid!r} is a {info.get('type')!r} node; "
                                      "segments start from a completed eq or prod node")
        if info.get("status") != "completed":
            raise RoundsError(code="rounds_start_node_invalid",
                              message=f"start node {nid!r} is {info.get('status')!r}; "
                                      "it must be completed")
        if _read_artifact_from_node(job_dir, nid, "state") is None:
            raise RoundsError(code="rounds_start_node_invalid",
                              message=f"start node {nid!r} has no state artifact to restart from")


# ---------------------------------------------------------------------------
# segment temperature
# ---------------------------------------------------------------------------


def _real_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def stage_takes_temperature(stage_tool: str, stage_fn: Optional[Callable] = None) -> bool:
    """Whether a stage tool accepts ``temperature_kelvin`` (run_production
    does, run_sst2 takes a ladder instead). A wrapper with ``**kwargs``
    forwards it."""
    if stage_fn is None:
        if stage_tool == DEFAULT_STAGE_TOOL:
            # No registry lookup: the mps driver runs on the host, where the
            # science stack (and so the tool registry) may not import.
            return True
        try:
            stage_fn, _ = resolve_tool(stage_tool)
        except Exception:  # noqa: BLE001 - an unresolvable tool gets no flag
            return False
    try:
        params = inspect.signature(stage_fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "temperature_kelvin" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def node_temperature(job_dir: str, node_id: str) -> Optional[float]:
    """The temperature a node ran at, as a restart from it would inherit it
    (``integrator_signature``, else ``metadata.temperature_kelvin``); None
    when it records none."""
    from mdclaw.node.inputs import _resolve_restart_temperature

    return _resolve_restart_temperature(job_dir, node_id).get("restart_temperature_kelvin")


def _prod_integrator_conflicts(job_dir: str, node_ids: list[str], stage_args: dict) -> dict[str, str]:
    """Prod start/basis nodes a segment could not continue from with these
    stage_args.

    A segment restarting from a prod node is a prod -> prod continuation,
    which run_production refuses when an explicit temperature or timestep
    differs from the parent's (``production_restart_integrator_mismatch``).
    With the value set in stage_args every segment from such a node would be
    refused, and the scheme cannot be replaced once its first round exists.
    Eq start nodes are fine: eq -> prod may change the temperature.
    """
    from mdclaw.node.io import _read_metadata_field
    from mdclaw.node.lifecycle import read_node

    wanted = {key: stage_args.get(key) for key in ("temperature_kelvin", "timestep_fs")
              if _real_number(stage_args.get(key))}
    if not wanted:
        return {}
    conflicts: dict[str, str] = {}
    for nid in dict.fromkeys(node_ids):
        try:
            node_type = read_node(job_dir, nid).get("node_type")
        except (FileNotFoundError, ValueError, OSError):
            continue
        signature = _read_metadata_field(job_dir, nid, "integrator_signature")
        if node_type != "prod" or not isinstance(signature, dict):
            continue
        drift = [f"{key} {signature[key]:g} vs {float(value):g}" for key, value in wanted.items()
                 if _real_number(signature.get(key))
                 and abs(float(signature[key]) - float(value)) > _TEMPERATURE_TOLERANCE_K]
        if drift:
            conflicts[nid] = ", ".join(drift)
    return conflicts


def _refuse_prod_conflicts(conflicts: dict[str, str], role: str) -> None:
    if conflicts:
        raise RoundsError(
            code="rounds_start_temperature_mismatch",
            message=(f"{role} {', '.join(f'{nid} ({why})' for nid, why in conflicts.items())} ran at other "
                     "integrator settings than stage_args asks for. A segment from a prod node continues "
                     "it and must keep its temperature and timestep, so every such segment would be "
                     "refused (production_restart_integrator_mismatch). Start from eq nodes, or from prod "
                     "nodes at those settings, or drop the value from stage_args."),
        )


def _temperature_list(temperatures: dict[str, Optional[float]]) -> str:
    return ", ".join(f"{nid}: {'none recorded' if t is None else f'{t:g} K'}" for nid, t in temperatures.items())


def resolve_segment_temperature(job_dir: str, *, stage_tool: str, stage_fn: Optional[Callable],
                                stage_args: dict, node_ids: list[str], segment_conditions: dict) -> dict:
    """The one temperature every segment of a new scheme runs at.

    ``stage_args.temperature_kelvin`` when given (the scheme runs there on
    purpose); otherwise the temperature recorded by every start node and
    every explicit WE basis node, which must agree
    (``rounds_start_temperature_mismatch``). Nodes that record none take the
    others' value; when none records one, segments run at 300 K and
    ``segment_temperature_kelvin`` is null. A stage tool without a
    ``temperature_kelvin`` argument (run_sst2) gets ``not_applicable``.
    """
    if not stage_takes_temperature(stage_tool, stage_fn):
        return {"segment_temperature_kelvin": None, "segment_temperature_source": "not_applicable",
                "node_temperatures": {}}
    explicit = stage_args.get("temperature_kelvin")
    node_temperatures: dict[str, Optional[float]] = {}
    if explicit is not None:
        if not _real_number(explicit) or explicit <= 0:
            raise _bad(f"stage_args.temperature_kelvin must be a positive number (got {explicit!r})")
        _refuse_prod_conflicts(_prod_integrator_conflicts(job_dir, node_ids, stage_args),
                               "start/basis node(s)")
        value: Optional[float] = float(explicit)
        source = "stage_args"
        described = "stage_args.temperature_kelvin"
    else:
        node_temperatures = {nid: node_temperature(job_dir, nid) for nid in dict.fromkeys(node_ids)}
        recorded = {nid: t for nid, t in node_temperatures.items() if t is not None}
        if recorded:
            low, high = min(recorded.values()), max(recorded.values())
            if high - low > _TEMPERATURE_TOLERANCE_K:
                raise RoundsError(
                    code="rounds_start_temperature_mismatch",
                    message=(f"the start and basis nodes ran at different temperatures "
                             f"({_temperature_list(node_temperatures)}). A segment restarts from one of them "
                             "or from its parent segment, so one scheme would mix ensembles. Start every "
                             "replica (and every WE basis) from nodes at one temperature, or put "
                             "temperature_kelvin in stage_args to run every segment at one temperature on "
                             "purpose."),
                )
            value = next(iter(recorded.values()))
            source = "start_nodes"
            described = f"the temperature of {', '.join(recorded)}"
        else:
            value = None
            source = "default"
            described = "the default: no start or basis node records a temperature"
    declared = segment_conditions.get("temperature_kelvin")
    run_at = value if value is not None else DEFAULT_SEGMENT_TEMPERATURE_K
    if _real_number(declared) and abs(float(declared) - run_at) > _TEMPERATURE_TOLERANCE_K:
        # Every segment would be refused by the declared-condition check and
        # the scheme could not be replaced once its first round exists.
        raise RoundsError(
            code="rounds_start_temperature_mismatch",
            message=(f"segment_conditions declares temperature_kelvin={declared} but the segments would run "
                     f"at {run_at:g} K ({described}); start from nodes at {declared} K, or put "
                     f"temperature_kelvin: {declared} in stage_args to run the scheme there on purpose."),
        )
    return {"segment_temperature_kelvin": value, "segment_temperature_source": source,
            "node_temperatures": node_temperatures}


def scheme_segment_temperature(scheme: dict, stage_fn: Optional[Callable] = None) -> Optional[float]:
    """The ``temperature_kelvin`` the driver passes to every segment of a
    scheme, or None when the stage tool takes none.

    ``stage_args.temperature_kelvin``, else the temperature setup_rounds
    pinned (``segment_temperature_kelvin``), else 300 K: start nodes that
    record none, and schemes recorded before setup_rounds pinned one — their
    segments ran at run_production's former 300 K default, and an explicit
    value keeps a running scheme there (continued walkers would otherwise
    inherit 300 K from their parents and recycled ones the basis's
    temperature). Every segment command then carries an explicit flag the
    Slurm preflight can check.
    """
    stage_args = scheme.get("stage_args") or {}
    given = stage_args.get("temperature_kelvin")
    if given is not None:
        return float(given) if _real_number(given) else None     # the stage tool reports a bad value
    if "segment_temperature_kelvin" in scheme:
        if scheme.get("segment_temperature_source") == "not_applicable":
            return None
        pinned = scheme.get("segment_temperature_kelvin")
        return float(pinned) if _real_number(pinned) else DEFAULT_SEGMENT_TEMPERATURE_K
    if stage_takes_temperature(scheme.get("stage_tool") or DEFAULT_STAGE_TOOL, stage_fn):
        return DEFAULT_SEGMENT_TEMPERATURE_K
    return None


def check_basis_temperatures(job_dir: str, scheme: dict, node_ids: list[str]) -> None:
    """Refuse basis nodes whose temperature differs from the scheme's
    (``rounds_start_temperature_mismatch``): a recycled walker restarts from
    its basis node, and one ensemble must stay at one temperature.

    Only schemes whose temperature setup_rounds took from its nodes are
    checked. With ``stage_args.temperature_kelvin`` the temperature is set on
    purpose (setup_rounds does not compare nodes either), and a scheme
    recorded before the temperature was pinned runs every segment at an
    explicit 300 K, so a basis elsewhere cannot split its ensemble — refusing
    would stop a running campaign.
    """
    if "segment_temperature_kelvin" not in scheme:
        return
    if scheme.get("segment_temperature_source") == "stage_args":
        # Set on purpose: any node temperature is fine for an eq basis, but a
        # prod basis must match the explicit settings its recycled walkers
        # continue with.
        _refuse_prod_conflicts(_prod_integrator_conflicts(job_dir, node_ids,
                                                          scheme.get("stage_args") or {}),
                               "basis node(s)")
        return
    if scheme.get("segment_temperature_source") not in ("start_nodes", "default"):
        return
    expected = scheme_segment_temperature(scheme)
    if expected is None:
        return
    temperatures = {nid: node_temperature(job_dir, nid) for nid in dict.fromkeys(node_ids)}
    off = {nid: t for nid, t in temperatures.items()
           if t is not None and abs(t - expected) > _TEMPERATURE_TOLERANCE_K}
    if off:
        raise RoundsError(
            code="rounds_start_temperature_mismatch",
            message=(f"basis node(s) {_temperature_list(off)} differ from the {expected:g} K scheme "
                     f"{scheme.get('scheme_id')!r} runs at: a recycled walker would restart from a state at "
                     "another temperature. Recycle to nodes at the scheme's temperature."),
        )


# ---------------------------------------------------------------------------
# state read back from the DAG
# ---------------------------------------------------------------------------


def scheme_state(job_dir: str, scheme_id: str) -> dict:
    """Rounds of a scheme as the DAG records them.

    Per round: the latest attempt of every replica (``latest``), status
    counts over those, and the latest policy node. Only ``progress.json`` is
    read (plus the owner record of each running node), so this is cheap even
    for thousands of segments. A running record carries ``owner_alive`` /
    ``owner_reason`` and ``stale`` (its owner is gone: see ``rounds.owner``);
    a failed record carries ``retired`` (abandoned on purpose, never retried)
    and the round counts them in ``n_retired``.
    """
    index = _read_progress(job_dir).get("nodes") or {}
    rounds: dict[int, dict] = {}

    def _round(r: int) -> dict:
        return rounds.setdefault(r, {"round": r, "attempts": {}, "policy_attempts": []})

    for nid, info in index.items():
        match = SEGMENT_ID_RE.match(nid)
        if match and match.group("scheme") == scheme_id:
            entry = _round(int(match.group("round")))
            replica = int(match.group("replica"))
            entry["attempts"].setdefault(replica, []).append({
                "node_id": nid,
                "attempt": int(match.group("attempt") or 0),
                "status": info.get("status"),
            })
            continue
        match = POLICY_ID_RE.match(nid)
        if match and match.group("scheme") == scheme_id:
            _round(int(match.group("round")))["policy_attempts"].append({
                "node_id": nid,
                "attempt": int(match.group("attempt") or 0),
                "status": info.get("status"),
            })

    summaries: list[dict] = []
    for r in sorted(rounds):
        entry = rounds[r]
        latest: dict[int, dict] = {}
        for replica, attempts in sorted(entry["attempts"].items()):
            latest[replica] = max(attempts, key=lambda a: a["attempt"])
        counts: dict[str, int] = {}
        for record in latest.values():
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        policy = (max(entry["policy_attempts"], key=lambda a: a["attempt"])
                  if entry["policy_attempts"] else None)
        n_retired = 0
        for record in list(latest.values()) + ([policy] if policy else []):
            if record["status"] == "running":
                liveness = owner_liveness(job_dir, record["node_id"])
                record["owner"] = liveness["owner"]
                record["owner_alive"] = liveness["alive"]
                record["owner_reason"] = liveness["reason"]
                record["stale"] = liveness["alive"] is False
            elif record["status"] == "failed":
                # A replica retired on purpose (update_workflow_state --abandon)
                # is not retried; the round completes without it.
                meta = (_read_node_json(job_dir, record["node_id"]) or {}).get("metadata") or {}
                record["retired"] = meta.get("failure_code") == "node_abandoned"
                if record["retired"] and record in latest.values():
                    n_retired += 1
        summaries.append({
            "round": r,
            "n_replicas": len(latest),
            "n_retired": n_retired,
            "latest": latest,
            "status_counts": counts,
            "policy": policy,
        })
    return {
        "scheme_id": scheme_id,
        "rounds": summaries,
        "current_round": summaries[-1]["round"] if summaries else None,
    }


def completed_segments(round_summary: dict) -> dict[int, str]:
    """``{replica: node_id}`` of the completed latest attempts of a round."""
    return {
        replica: record["node_id"]
        for replica, record in round_summary["latest"].items()
        if record["status"] == "completed"
    }


def segment_scheme_metadata(job_dir: str, node_id: str) -> dict:
    """The ``metadata.scheme`` block a driver wrote on a segment (or {})."""
    node = _read_node_json(job_dir, node_id) or {}
    scheme = (node.get("metadata") or {}).get("scheme")
    return scheme if isinstance(scheme, dict) else {}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def _error(exc: RoundsError, **extra) -> dict:
    return {
        "success": False,
        "code": exc.code,
        "message": str(exc),
        "errors": [str(exc)],
        "warnings": [],
        **extra,
    }


def _analysis_hint(policy: Optional[str]) -> str:
    """What to analyze once a scheme is over: its policy nodes, or — for the
    built-in replicas policy, which has none — its segments."""
    if policy in (None, REPLICAS_POLICY):
        return "analyze its segments (an analyze node over the last round's segments, or concat_trajectory per replica)"
    return "create an analyze node over its last policy node for the final analysis"


def scheme_next(job_dir: str, scheme_id: str, *, executor: str = "local",
                busy: Optional[list[str]] = None, done: Optional[str] = None,
                reason: Optional[str] = None, policy: Optional[str] = None) -> dict:
    """The envelope ``next`` of a scheme-level result: a scheme is advanced
    only by ``run_rounds`` (``run``), waited for while another driver owns a
    round (``wait``), or analyzed once the policy stopped it (``done``)."""
    jd = str(Path(job_dir).resolve())
    inspect = f"mdclaw inspect_rounds --job-dir {shlex.quote(jd)} --scheme-id {shlex.quote(scheme_id)}"
    if done:
        return {"action": "done", "scheme_id": scheme_id, "inspect_command": inspect,
                "note": f"scheme '{scheme_id}' finished ({done}); {_analysis_hint(policy)}"}
    if busy:
        return {"action": "wait", "scheme_id": scheme_id, "node_id": busy[0],
                "wait_command": f"mdclaw wait_node --job-dir {shlex.quote(jd)} --node-id {shlex.quote(busy[0])}",
                "inspect_command": inspect,
                "note": (f"{len(busy)} node(s) of scheme '{scheme_id}' are running or queued under "
                         "another run_rounds (or its Slurm job)"
                         + (f" ({reason})" if reason else "")
                         + "; rerun run_rounds after they stop")}
    run = f"mdclaw run_rounds --job-dir {shlex.quote(jd)} --scheme-id {shlex.quote(scheme_id)}"
    if executor != "local":
        run += f" --executor {executor}"
    return {"action": "run", "scheme_id": scheme_id, "stage_tools": ["run_rounds"],
            "run_command": run, "inputs": "auto_resolved", "inspect_command": inspect}


def setup_rounds(job_dir: str, scheme: dict, overwrite: bool = False) -> dict:
    """Record a sampling scheme on the job (``params.sampling_schemes``).

    ``scheme`` is a JSON object::

        {"scheme_id": "rep",                 # [a-z][a-z0-9]{0,15}
         "policy": "replicas",               # or an analyze tool, e.g. "we_resample"
         "policy_args": {},                  # passed to that tool
         "stage_tool": "run_production",     # the prod-stage tool that runs one segment
         "stage_args": {"simulation_time_ns": 0.1, "platform": "CUDA"},
         "start": {"node_ids": ["eq_001"], "n_replicas": 8},
         "initial_weights": "uniform",       # weighted policies; or one number per replica
         "segment_conditions": {},           # declared on every segment (keys the stage tool reports)
         "seed": 20260921,                   # base of every segment's random_seed
         "max_rounds": null}

    Round 1 starts ``n_replicas`` segments from ``start.node_ids`` (cycled);
    each segment is a ``prod`` node run by ``stage_tool`` with ``stage_args``
    plus its own ``random_seed``. The temperature is not a stage argument:
    it is read from the start nodes (and explicit WE ``basis_node_ids``),
    which must agree (``rounds_start_temperature_mismatch``), recorded as
    ``segment_temperature_kelvin`` and passed to every segment (the result's
    ``segments_run_at_kelvin``; 300 K when no node records one); put
    ``temperature_kelvin`` in ``stage_args`` only to run the scheme at
    another temperature on purpose, and only from eq start nodes (a segment
    from a prod node continues it and must keep its temperature and
    timestep). After a round the policy plans the next one:
    ``replicas`` continues every replica; an analyze tool runs on a policy
    node whose parents are the round's segments and writes ``next_round``
    (see ``mdclaw.rounds.plan``). ``run_rounds`` advances the scheme.
    """
    try:
        normalized = normalize_scheme(scheme, job_dir=job_dir)
        write_scheme(job_dir, normalized, overwrite=overwrite)
    except RoundsError as exc:
        return _error(exc, job_dir=job_dir)
    jd = Path(job_dir).resolve()
    per_segment = (normalized.get("stage_args") or {}).get("simulation_time_ns")
    run_at = scheme_segment_temperature(normalized)
    source = normalized["segment_temperature_source"]
    warnings: list[str] = []
    if run_at is None:
        temperature_note = ""
    else:
        temperature_note = f", segments at {run_at:g} K (" + {
            "stage_args": "stage_args",
            "start_nodes": "the start nodes' temperature",
            "default": "default: the start nodes record no temperature",
        }.get(source, source) + ")"
        if source == "default":
            warnings.append(
                f"no start or basis node records a temperature, so every segment runs at {run_at:g} K; "
                "put temperature_kelvin in stage_args (and rerun setup_rounds --overwrite true) if the "
                "study runs at another temperature")
    return {
        "success": True,
        "code": "ok",
        "message": (
            f"scheme '{normalized['scheme_id']}' recorded: policy {normalized['policy']}, "
            f"{normalized['start']['n_replicas']} replicas from {normalized['start']['node_ids']}, "
            f"{normalized['stage_tool']}"
            + (f" {per_segment} ns per segment" if per_segment is not None else "")
            + temperature_note
            + "; nothing runs until run_rounds"
        ),
        "job_dir": str(jd),
        "scheme_id": normalized["scheme_id"],
        "scheme": normalized,
        # What the scheme recorded (null: the start nodes record none) and
        # what every segment is run with.
        "segment_temperature_kelvin": normalized["segment_temperature_kelvin"],
        "segment_temperature_source": source,
        "segments_run_at_kelvin": run_at,
        "warnings": warnings,
        "next_action": (
            f"mdclaw run_rounds --job-dir {jd} --scheme-id {normalized['scheme_id']} "
            "--max-rounds <N>"
        ),
        "next": scheme_next(job_dir, normalized["scheme_id"]),
    }


def inspect_rounds(job_dir: str, scheme_id: str) -> dict:
    """Read-only state of a scheme: rounds, segment statuses, policy nodes,
    aggregate sampled time and what to do next."""
    try:
        scheme = read_scheme(job_dir, scheme_id)
        state = scheme_state(job_dir, scheme_id)
    except RoundsError as exc:
        return _error(exc, job_dir=job_dir, scheme_id=scheme_id)
    jd = Path(job_dir).resolve()
    aggregate_ns = 0.0
    rounds_out = []
    total_events = 0
    total_flux_weight = 0.0
    policy_keys = ("n_in", "n_out", "flux_events", "flux_weight", "target_weight", "weight_min", "weight_max")
    # Aggregate sampled time: completed segments x the scheme's segment
    # length when stage_args says it (O(1); WE-25: reading every completed
    # segment's node.json took 298 s on a 13,300-segment scheme), else the
    # per-node metadata as before.
    per_segment = (scheme.get("stage_args") or {}).get("simulation_time_ns")
    if isinstance(per_segment, bool) or not isinstance(per_segment, (int, float)):
        per_segment = None
    for summary in state["rounds"]:
        failed = [rec["node_id"] for rec in summary["latest"].values() if rec["status"] == "failed"]
        pending = [rec["node_id"] for rec in summary["latest"].values()
                   if rec["status"] in ("pending", "queued", "running")]
        running = [rec["node_id"] for rec in summary["latest"].values() if rec["status"] in ("queued", "running")]
        stale = [rec["node_id"] for rec in summary["latest"].values() if rec.get("stale")]
        retired = [rec["node_id"] for rec in summary["latest"].values() if rec.get("retired")]
        owners = {rec["node_id"]: rec["owner_reason"] for rec in summary["latest"].values()
                  if rec["status"] == "running"}
        for rec in summary["latest"].values():
            if rec["status"] != "completed":
                continue
            if per_segment is not None:
                aggregate_ns += float(per_segment)
                continue
            meta = (_read_node_json(job_dir, rec["node_id"]) or {}).get("metadata") or {}
            value = meta.get("simulation_time_ns")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                aggregate_ns += float(value)
        policy = summary["policy"]
        policy_summary: dict = {}
        if policy and policy["status"] in ("queued", "running"):
            running.append(policy["node_id"])
            if policy.get("stale"):
                stale.append(policy["node_id"])
            if policy["status"] == "running":
                owners[policy["node_id"]] = policy.get("owner_reason")
        if policy and policy["status"] == "completed":
            pmeta = (_read_node_json(job_dir, policy["node_id"]) or {}).get("metadata") or {}
            policy_summary = {k: pmeta.get(k) for k in policy_keys if k in pmeta}
            total_events += int(pmeta.get("flux_events") or 0)
            total_flux_weight += float(pmeta.get("flux_weight") or 0.0)
        rounds_out.append({
            "round": summary["round"],
            "n_replicas": summary["n_replicas"],
            "status_counts": summary["status_counts"],
            "policy": policy,
            "policy_summary": policy_summary,
            "failed": failed[:ID_LIST_CAP],
            "open": pending[:ID_LIST_CAP],
            "running": running[:ID_LIST_CAP],
            "stale": stale[:ID_LIST_CAP],
            "retired": retired[:ID_LIST_CAP],
            "owners": dict(list(owners.items())[:ID_LIST_CAP]),
        })
    current = state["rounds"][-1] if state["rounds"] else None
    stale_all = [nid for entry in rounds_out for nid in entry["stale"]]
    busy = [nid for entry in rounds_out for nid in entry["running"] if nid not in stale_all]
    owner_note = None
    closed = scheme.get("closed") if isinstance(scheme.get("closed"), dict) else None
    closed_note = None
    if closed:
        closed_note = f"closed at {closed.get('at')}" + (f": {closed.get('reason')}" if closed.get("reason") else "")
        next_action = (f"scheme {scheme_id!r} is {closed_note}; nothing of it runs again — "
                       f"{_analysis_hint(scheme['policy'])}, or setup_rounds with a new scheme_id")
    elif busy:
        reasons = {nid: r for entry in rounds_out for nid, r in entry["owners"].items() if nid in busy and r}
        owner_note = "; ".join(f"{nid}: {reason}" for nid, reason in list(reasons.items())[:3]) or None
        next_action = (f"wait: round {current['round']} has running or queued nodes ({busy[:3]}...), owned by "
                       f"a run_rounds process or its Slurm job"
                       + (f" ({owner_note})" if owner_note else "")
                       + f"; rerun run_rounds only after it stops "
                       f"(mdclaw wait_node --job-dir {jd} --node-id {busy[0]})")
    elif stale_all:
        next_action = (f"{len(stale_all)} node(s) left running by a run_rounds that died ({stale_all[:3]}); "
                       f"rerun mdclaw run_rounds --job-dir {jd} --scheme-id {scheme_id}: it seals them "
                       "failed (rounds_owner_lost) and retries them")
    elif current is None:
        next_action = f"mdclaw run_rounds --job-dir {jd} --scheme-id {scheme_id} --max-rounds 1"
    elif current["status_counts"].get("completed", 0) == current["n_replicas"] and (
            scheme["policy"] == REPLICAS_POLICY
            or (current["policy"] or {}).get("status") == "completed"):
        next_action = (f"round {current['round']} is complete; continue with mdclaw run_rounds "
                       f"--job-dir {jd} --scheme-id {scheme_id} --max-rounds <N>, or analyze")
    else:
        next_action = f"mdclaw run_rounds --job-dir {jd} --scheme-id {scheme_id}"
    counts = ", ".join(f"{n} {status}" for status, n in sorted((current or {}).get("status_counts", {}).items()))
    return {
        "success": True,
        "code": "ok",
        "message": (
            f"scheme '{scheme_id}': {len(state['rounds'])} round(s)"
            + (f", round {current['round']} {counts or 'empty'}" if current else " (none created yet)")
            + f", {round(aggregate_ns, 6)} ns aggregate, {total_events} recycling event(s)"
            + (f"; {closed_note}" if closed_note else "")
        ),
        "closed": closed,
        "job_dir": str(jd),
        "scheme_id": scheme_id,
        "scheme": scheme,
        "current_round": state["current_round"],
        "n_rounds": len(state["rounds"]),
        "busy": bool(busy),
        "stale": stale_all[:ID_LIST_CAP],
        "rounds": rounds_out,
        "aggregate_ns": round(aggregate_ns, 6),
        "aggregate_ns_source": "stage_args" if per_segment is not None else "node_metadata",
        "flux_events_total": total_events,
        "flux_weight_total": total_flux_weight,
        "warnings": [],
        "next_action": next_action,
        "next": (scheme_next(job_dir, scheme_id, done=closed_note, policy=scheme["policy"]) if closed_note
                 else scheme_next(job_dir, scheme_id, busy=busy, reason=owner_note)),
    }


def _update_scheme(job_dir: str, scheme_id: str, **fields) -> dict:
    """Set bookkeeping fields (``closed``) on a recorded scheme, no validation."""
    jd = Path(job_dir).resolve()
    with file_lock(jd / "progress.lock"):
        progress = _read_progress(job_dir)
        schemes = progress.setdefault("params", {}).setdefault(PARAMS_KEY, {})
        scheme = schemes.get(scheme_id)
        if not isinstance(scheme, dict):
            raise RoundsError(code="rounds_scheme_missing", message=f"no sampling scheme {scheme_id!r} in {job_dir}")
        scheme.update(fields)
        _atomic_write_json(jd / "progress.json", progress)
    return scheme


def close_rounds(job_dir: str, scheme_id: str, reason: Optional[str] = None) -> dict:
    """Close a sampling scheme: nothing of it runs again.

    ``run_rounds`` answers ``rounds_scheme_closed``, ``inspect_rounds`` and
    the envelope's ``next`` of every node of the scheme say ``done``. Use it
    when a trial is over, the budget is spent or the design is being redone;
    the final analysis (an analyze node over the last policy node) still
    works. Segments that are already running finish on their own; pending
    ones stay pending as a record — retire them with
    ``update_workflow_state --abandon`` if they should not show up as open
    work. Continue the study with a new scheme (``setup_rounds`` under a new
    ``scheme_id``): a scheme that has rounds is never replaced.

    Args:
        job_dir: The job holding the scheme.
        scheme_id: The scheme to close.
        reason: Why (kept with the scheme, shown by inspect_rounds).

    Returns:
        dict with ``closed`` ({at, reason}), ``already_closed`` and the
        scheme's open segment ids (``open_node_ids``).
    """
    try:
        scheme = read_scheme(job_dir, scheme_id)
        already = scheme.get("closed") if isinstance(scheme.get("closed"), dict) else None
        if not already:
            scheme = _update_scheme(job_dir, scheme_id,
                                    closed={"at": datetime.now(timezone.utc).isoformat(), "reason": reason})
        state = scheme_state(job_dir, scheme_id)
    except RoundsError as exc:
        return _error(exc, job_dir=job_dir, scheme_id=scheme_id)
    jd = Path(job_dir).resolve()
    open_nodes = [rec["node_id"] for summary in state["rounds"] for rec in summary["latest"].values()
                  if rec["status"] in ("pending", "queued", "running")]
    closed = scheme["closed"]
    note = f"closed at {closed.get('at')}" + (f": {closed.get('reason')}" if closed.get("reason") else "")
    warnings = []
    if open_nodes:
        warnings.append(
            f"{len(open_nodes)} segment(s) of the scheme are still open ({open_nodes[:3]}...): running ones "
            "finish on their own, pending ones stay pending — retire them with "
            f"mdclaw update_workflow_state --job-dir {jd} --node-id <id> --abandon --reason <why>")
    return {
        "success": True,
        "code": "ok",
        "message": (f"scheme {scheme_id!r} " + ("was already " if already else "") + note
                    + "; nothing of it runs again"),
        "job_dir": str(jd),
        "scheme_id": scheme_id,
        "closed": closed,
        "already_closed": bool(already),
        "open_node_ids": open_nodes[:ID_LIST_CAP],
        "warnings": warnings,
        "next_action": (f"{_analysis_hint(scheme.get('policy'))} (mdclaw inspect_rounds --job-dir {jd} "
                        f"--scheme-id {scheme_id}), or setup_rounds with a new scheme_id"),
        "next": scheme_next(job_dir, scheme_id, done=note, policy=scheme.get("policy")),
    }
