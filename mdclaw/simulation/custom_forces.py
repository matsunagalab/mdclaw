"""Custom force / CV bias plugin for production MD (PythonTorchForce-backed).

This module lets an agent attach an arbitrary biasing force to a production
run by writing a *single* Python function:

    def energy(positions, ctx):
        i = ctx.select("name CA and resid 10")
        j = ctx.select("name CA and resid 50")
        d = torch.linalg.norm(positions[i].mean(0) - positions[j].mean(0))
        return 0.5 * ctx.params["k"] * (d - ctx.params["d0"]) ** 2

The author writes only the potential energy (a scalar ``torch.Tensor`` in
kJ/mol). MDClaw computes the forces by autograd (``forces = -dE/dx``), wraps
the function in an ``openmmtorch.PythonTorchForce``, and absorbs the
device/dtype/unit pitfalls that ``openmm-torch`` warns about. Atom selection
goes through ``ctx.select`` (mdtraj VMD-style DSL), whose indices are
guaranteed to match the OpenMM ``System`` particle indices because the
``topology.pdb`` and ``system.xml`` come from the same build triple.

``PythonTorchForce`` is the only supported backend: upstream openmm-torch
deprecated ``TorchForce`` (it relies on TorchScript, which PyTorch no longer
maintains) and recommends ``PythonTorchForce`` for all cases. A pre-trained
model is therefore loaded inside the same ``energy`` function (e.g. via
``torch.load``) rather than through a separate TorchScript route.

The function may optionally return ``(energy, cv_dict)`` where ``cv_dict``
maps collective-variable names to scalar tensors; those values are logged per
frame so downstream analysis (and, later, pymbar reweighting) can consume
them.

``openmmtorch`` is imported lazily so that importing this module never
requires the plugin to be installed; missing dependencies surface as a stable
``custom_force_dependency_missing`` code.
"""

import os
import re
import runpy
import sys
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger, sha256_file  # noqa: E402

logger = setup_logger(__name__)

# Dedicated OpenMM force group for the custom bias. Group 0 holds the
# force-field forces (and any barostat), so isolating the bias in its own
# group lets the reporter read the bias-only potential energy via
# ``getState(getEnergy=True, groups={CUSTOM_FORCE_GROUP})`` for reweighting.
CUSTOM_FORCE_GROUP = 31

# Name of the entry-point function the user script must define.
_ENTRY_POINT = "energy"


class CustomForceError(RuntimeError):
    """Raised when a custom-force script / module cannot be loaded or violates
    the energy contract. Carries a stable ``code`` attribute so callers can
    branch deterministically (mirrors ``_ModernSystemContractError``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _EvalContext:
    """The ``ctx`` handed to a user ``energy(positions, ctx)`` function.

    Constructed fresh for every evaluation so ``select`` / ``reference``
    adapt to the device and dtype of the current ``positions`` tensor (which
    ``openmm-torch`` may hand over as float32/float64 on CPU or GPU).
    """

    __slots__ = ("_positions", "_mdtraj_top", "_reference_np", "params", "box",
                 "_atomic_numbers", "_collapsed_resnames")

    def __init__(self, *, positions, mdtraj_top, reference_np, params, box=None,
                 collapsed_resnames=None):
        self._positions = positions
        self._mdtraj_top = mdtraj_top
        self._reference_np = reference_np
        self.params = dict(params or {})
        self.box = box
        self._atomic_numbers = None
        # Residue names topology.pdb spells that mdtraj collapsed on load, with
        # what it collapsed each onto. See ``_collapsed_resnames``.
        self._collapsed_resnames = dict(collapsed_resnames or {})

    def select(self, selection: str):
        """Resolve a mdtraj VMD-style selection string to a ``long`` tensor of
        atom indices on the same device as ``positions``.

        The indices are valid OpenMM particle indices (and rows of the
        ``positions`` tensor) because ``topology.pdb`` shares atom ordering
        with ``system.xml``.
        """
        import numpy as np
        import torch

        self._refuse_collapsed_resname(selection)
        idx = self._mdtraj_top.select(selection)
        if idx is None or len(idx) == 0:
            raise CustomForceError(
                "custom_force_selection_empty",
                f"ctx.select({selection!r}) matched 0 atoms.",
            )
        return torch.as_tensor(
            np.asarray(idx, dtype=np.int64), device=self._positions.device
        )

    def _refuse_collapsed_resname(self, selection: str) -> None:
        """Refuse a selection whose residue name mdtraj cannot tell apart.

        mdtraj renames on load -- HID, HIE and HIP all arrive as HIS, CYX as
        CYS, ASH as ASP, WAT as HOH -- so a selection naming either side of one
        of those is answered on a topology that no longer distinguishes them.
        Both answers are wrong and neither is empty, which is why the
        matched-nothing check below does not catch it: on a real PLpro topology
        ``resname CYS and name SG`` returns 10 SG atoms including the two
        disulfide cysteines the file calls CYX, and restraining those is a
        force applied to the wrong atoms.

        Refused rather than corrected: which of the two the caller meant is not
        recoverable from the string, and a force is not a thing to guess at.
        """
        if not self._collapsed_resnames:
            return
        words = set(re.findall(r"[A-Za-z][A-Za-z0-9+-]*", selection or ""))
        for spelled, collapsed_onto in sorted(self._collapsed_resnames.items()):
            if spelled in words or collapsed_onto in words:
                raise CustomForceError(
                    "custom_force_selection_ambiguous",
                    f"ctx.select({selection!r}) names a residue mdtraj cannot "
                    f"tell apart: topology.pdb spells {spelled}, and mdtraj "
                    f"loads it as {collapsed_onto}, so the selection would "
                    f"answer for both. Select those atoms another way -- by "
                    f"index, or by a name or a bond that survives the load.",
                )

    @property
    def reference(self):
        """Reference coordinates (N,3) in nm, cast to the positions
        device/dtype. Sourced from the build/topo geometry and held fixed
        across restarts so positional restraints do not drift."""
        import torch

        return torch.as_tensor(
            self._reference_np,
            dtype=self._positions.dtype,
            device=self._positions.device,
        )

    @property
    def atomic_numbers(self):
        """List of atomic numbers indexed by particle (for ML potentials)."""
        if self._atomic_numbers is None:
            self._atomic_numbers = [
                (a.element.atomic_number if a.element is not None else 0)
                for a in self._mdtraj_top.atoms
            ]
        return self._atomic_numbers


def _split_energy_output(out: Any):
    """Split a user ``energy`` return value into ``(E, cv_dict)``.

    Accepts a bare scalar tensor or a ``(scalar, dict)`` tuple. Raises
    ``custom_force_contract_error`` for anything else.
    """
    if isinstance(out, tuple):
        if len(out) != 2 or not isinstance(out[1], dict):
            raise CustomForceError(
                "custom_force_contract_error",
                "energy(positions, ctx) must return a scalar tensor or a "
                "(scalar_tensor, {cv_name: scalar}) tuple.",
            )
        return out[0], out[1]
    return out, {}


def _as_scalar_energy(value: Any):
    """Validate that ``value`` is a finite scalar tensor and return it."""
    import torch

    if not isinstance(value, torch.Tensor):
        raise CustomForceError(
            "custom_force_contract_error",
            f"energy must be a torch.Tensor scalar, got {type(value).__name__}.",
        )
    if value.numel() != 1:
        raise CustomForceError(
            "custom_force_contract_error",
            f"energy must be a scalar (numel==1), got shape {tuple(value.shape)}.",
        )
    if not torch.isfinite(value).all():
        raise CustomForceError(
            "custom_force_contract_error",
            "energy evaluated to a non-finite value (NaN/Inf).",
        )
    return value.reshape(())


def _load_energy_function(script_path: Path) -> Callable:
    """Execute the user script in isolation and return its ``energy`` callable."""
    try:
        namespace = runpy.run_path(str(script_path))
    except Exception as exc:  # noqa: BLE001 - surface any author error
        raise CustomForceError(
            "custom_force_script_error",
            f"Failed to import custom-force script {script_path}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    fn = namespace.get(_ENTRY_POINT)
    if not callable(fn):
        raise CustomForceError(
            "custom_force_script_error",
            f"Custom-force script {script_path} must define a callable "
            f"'{_ENTRY_POINT}(positions, ctx)'.",
        )
    return fn


def _reference_positions_to_numpy(reference_positions):
    """Convert OpenMM positions (Quantity of Vec3) to an (N,3) nm numpy array."""
    import numpy as np
    from openmm.unit import nanometer

    plain = reference_positions.value_in_unit(nanometer)
    # ``plain`` may be a list of Vec3 (PDBFile/State positions) or an (N,3)
    # numpy array (asNumpy state). Both support integer row indexing.
    return np.array([[row[0], row[1], row[2]] for row in plain], dtype=np.float64)


def _collapsed_resnames(topology_pdb_file: str, mdtraj_top) -> dict:
    """Residue names the file spells that mdtraj renamed on load.

    Read from the two sides rather than from a hard-coded alias table: what
    mdtraj collapses is mdtraj's business and changes with its version, and the
    only thing that matters here is whether this file lost a distinction. Atom
    order is shared -- ``topology.pdb`` is the same contract ``system.xml`` is
    indexed by -- so the comparison is record against atom.
    """
    spelled: dict[str, str] = {}
    residues = list(mdtraj_top.residues)
    by_index = {}
    for residue in residues:
        for atom in residue.atoms:
            by_index[atom.index] = residue.name
    n = 0
    try:
        with open(topology_pdb_file) as handle:
            for line in handle:
                if not line.startswith(("ATOM  ", "HETATM")):
                    continue
                loaded = by_index.get(n)
                n += 1
                if loaded is None:
                    continue
                on_disk = line[17:21].strip().upper()
                if on_disk and on_disk != str(loaded).strip().upper():
                    spelled[on_disk] = str(loaded).strip().upper()
    except OSError:
        return {}
    return spelled


def _load_mdtraj_topology(topology_pdb_file: str, system):
    """Load the mdtraj topology and assert it matches the System particle
    count (the index-consistency guarantee)."""
    import mdtraj as md

    mdtraj_top = md.load_topology(str(topology_pdb_file))
    n_particles = system.getNumParticles()
    if mdtraj_top.n_atoms != n_particles:
        raise CustomForceError(
            "custom_force_topology_mismatch",
            f"topology.pdb has {mdtraj_top.n_atoms} atoms but system.xml has "
            f"{n_particles} particles; ctx.select() indices would not match "
            f"the System. Rebuild the topo node so the triple is consistent.",
        )
    return mdtraj_top


def _preload_libtorch_cuda() -> None:
    """Make libtorch's CUDA symbols resident, then re-run OpenMM's plugin scan.

    ``libOpenMMTorchCUDA.so`` resolves its libtorch_cuda symbols at plugin-scan
    time. A bare ``import torch`` is not enough: torch dlopens
    ``libtorch_cuda.so`` lazily on first CUDA use, so at scan time the plugin
    fails to load and a PythonTorchForce Context later dies with "Platform does
    not support the requested kernel". We dlopen the CUDA libraries here with
    ``RTLD_GLOBAL`` so the plugin can see their symbols, WITHOUT initializing a
    CUDA context (no stray GPU memory, no device pinning), and then rescan the
    plugin directory because ``import openmm`` has already scanned it once.

    ``find_spec`` locates torch on disk without executing it — importing torch
    just to read its path cost ~2 s on every call. Best-effort: torch-less or
    CPU-only installs leave the CUDA kernel unavailable and CPU/Reference work.
    """
    import ctypes
    import importlib.util
    import os

    spec = importlib.util.find_spec("torch")
    if spec is None or not spec.submodule_search_locations:
        return
    lib_dir = os.path.join(list(spec.submodule_search_locations)[0], "lib")
    # Order matters: libtorch_cuda.so depends on libc10_cuda.so. Both resolve
    # their own further deps via their $ORIGIN RPATH inside torch/lib.
    loaded = False
    for name in ("libc10_cuda.so", "libtorch_cuda.so"):
        path = os.path.join(lib_dir, name)
        if os.path.exists(path):
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                return
            loaded = True
    if not loaded:
        return
    from openmm import Platform

    Platform.loadPluginsFromDirectory(Platform.getDefaultPluginsDirectory())


def _import_openmmtorch():
    """Lazy import of the openmm-torch plugin; raises a stable code if absent."""
    try:
        _preload_libtorch_cuda()
        import openmmtorch  # noqa: F401

        return openmmtorch
    except ImportError as exc:
        raise CustomForceError(
            "custom_force_dependency_missing",
            "openmm-torch (openmmtorch) is not installed; custom force / CV "
            "bias requires it. Install via conda-forge 'openmm-torch' or "
            "pip 'openmmtorch'.",
        ) from exc


def _build_python_torch_force(
    *,
    energy_fn: Callable,
    mdtraj_top,
    reference_np,
    params: dict,
    collapsed_resnames: dict | None = None,
):
    """Wrap a user ``energy(positions, ctx)`` in a PythonTorchForce whose
    compute function returns ``(energy, forces=-dE/dx)`` via autograd.

    Returns ``(force, evaluator, cv_names)`` where ``evaluator(positions_np,
    box_np) -> dict`` re-evaluates the CV values for logging.
    """
    import torch

    openmmtorch = _import_openmmtorch()
    uses_pbc = bool(params.get("pbc", False))

    def _evaluate(positions_tensor, box_tensor):
        """Run the user function and return (E_scalar_tensor, cv_dict)."""
        ctx = _EvalContext(
            positions=positions_tensor,
            mdtraj_top=mdtraj_top,
            reference_np=reference_np,
            params=params,
            box=box_tensor,
            collapsed_resnames=collapsed_resnames,
        )
        out = energy_fn(positions_tensor, ctx)
        energy_value, cv_dict = _split_energy_output(out)
        return _as_scalar_energy(energy_value), cv_dict

    # ---- build-time validation: one autograd pass on the reference geometry.
    ref_pos = torch.as_tensor(reference_np, dtype=torch.double).requires_grad_(True)
    try:
        E0, cv0 = _evaluate(ref_pos, None)
        grad0 = torch.autograd.grad(E0, ref_pos, allow_unused=True)[0]
    except CustomForceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CustomForceError(
            "custom_force_script_error",
            f"energy(positions, ctx) raised during validation: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if grad0 is not None and not torch.isfinite(grad0).all():
        raise CustomForceError(
            "custom_force_contract_error",
            "Gradient of energy w.r.t. positions is non-finite (NaN/Inf).",
        )
    cv_names = sorted(str(k) for k in cv0.keys())

    def _box_from_state(state, pos):
        if not uses_pbc:
            return None
        try:
            from openmm.unit import nanometer

            vectors = state.getPeriodicBoxVectors().value_in_unit(nanometer)
            return torch.tensor(
                [[row[0], row[1], row[2]] for row in vectors],
                dtype=pos.dtype, device=pos.device,
            )
        except Exception:  # noqa: BLE001 - non-periodic / missing box
            return None

    def _compute(state, positions):
        """PythonTorchForce compute hook: ``(state, positions) -> (E, forces)``."""
        with torch.enable_grad():
            pos = positions.detach().requires_grad_(True)
            box_tensor = _box_from_state(state, pos)
            energy_value, _cv = _evaluate(pos, box_tensor)
            grad = torch.autograd.grad(energy_value, pos, allow_unused=True)[0]
            if grad is None:
                forces = torch.zeros_like(pos)
            else:
                forces = -grad
        return energy_value, forces

    if not hasattr(openmmtorch, "PythonTorchForce"):
        raise CustomForceError(
            "custom_force_dependency_missing",
            "The installed openmm-torch has no PythonTorchForce. As of "
            "openmm-torch 1.5.1 the released conda-forge/pip package still "
            "ships only the deprecated TorchForce; PythonTorchForce was added "
            "to master on 2026-06-24 and is not in any tagged release yet. "
            "Install an openmm-torch built from master (the MDClaw container "
            "source-builds it) to use custom forces.",
        )
    force = openmmtorch.PythonTorchForce(_compute)
    try:
        force.setUsesPeriodicBoundaryConditions(uses_pbc)
    except Exception:  # noqa: BLE001 - older API may differ
        pass

    def _evaluator(positions_np, box_np):
        """CPU re-evaluation used by the reporter to log CV values."""
        pos = torch.as_tensor(positions_np, dtype=torch.double)
        box_tensor = (
            torch.as_tensor(box_np, dtype=torch.double) if box_np is not None else None
        )
        with torch.no_grad():
            _E, cv_dict = _evaluate(pos, box_tensor)
        return {str(k): float(v) for k, v in cv_dict.items()}

    return force, _evaluator, cv_names


class CustomForceReporter:
    """OpenMM reporter that logs the bias potential energy (always) and any
    collective-variable values.

    The bias energy is read from the dedicated ``CUSTOM_FORCE_GROUP`` so it is
    isolated from the force-field energy — exactly the per-frame quantity a
    later pymbar/MBAR reweighting needs. Script CV values are obtained by
    re-evaluating the user's ``energy`` function on the reported frame; native
    distance CV values are computed from the reported positions.

    Output: ``collective_variables.csv`` with columns
    ``step,time_ps,bias_energy_kj_mol[,<cv...>]``.
    """

    def __init__(
        self,
        file_path: str,
        report_interval: int,
        *,
        force_group: int,
        evaluator: Optional[Callable],
        cv_names: list,
        append: bool = False,
    ):
        self._interval = int(report_interval)
        self._force_group = int(force_group)
        self._evaluator = evaluator
        self._cv_names = list(cv_names or [])
        self._needs_positions = bool(self._evaluator and self._cv_names)
        mode = "a" if append else "w"
        self._out = open(file_path, mode)
        if not append:
            header = ["step", "time_ps", "bias_energy_kj_mol", *self._cv_names]
            self._out.write(",".join(header) + "\n")
            self._out.flush()

    def describeNextReport(self, simulation):
        steps = self._interval - simulation.currentStep % self._interval
        # (steps, positions, velocities, forces, energy)
        return (steps, self._needs_positions, False, False, False)

    def report(self, simulation, state):
        from openmm.unit import kilojoule_per_mole, nanometer, picosecond

        bias_state = simulation.context.getState(
            getEnergy=True, groups={self._force_group}
        )
        bias_energy = bias_state.getPotentialEnergy().value_in_unit(
            kilojoule_per_mole
        )
        time_ps = state.getTime().value_in_unit(picosecond)
        step = simulation.currentStep

        cv_values = {}
        if self._needs_positions:
            positions_np = state.getPositions(asNumpy=True).value_in_unit(nanometer)
            box_np = None
            try:
                box = state.getPeriodicBoxVectors(asNumpy=True)
                if box is not None:
                    box_np = box.value_in_unit(nanometer)
            except Exception:  # noqa: BLE001 - non-periodic systems
                box_np = None
            try:
                cv_values = self._evaluator(positions_np, box_np)
            except Exception as exc:  # noqa: BLE001 - never abort MD for logging
                logger.warning("CV evaluation failed at step %s: %s", step, exc)
                cv_values = {}

        row = [str(step), f"{time_ps:.6f}", f"{bias_energy:.6f}"]
        for name in self._cv_names:
            value = cv_values.get(name)
            row.append("" if value is None else f"{value:.6f}")
        self._out.write(",".join(row) + "\n")
        self._out.flush()

    def close(self):
        try:
            self._out.flush()
            self._out.close()
        except Exception:  # noqa: BLE001
            pass


def write_cv_metadata(
    meta_path: str,
    *,
    signature: Optional[dict],
    cv_names: list,
    temperature_kelvin: float,
    parameters: Optional[dict],
) -> None:
    """Write the sidecar ``collective_variables.meta.json`` used to
    reconstruct pymbar/MBAR inputs across nodes."""
    import json

    payload = {
        "signature": signature,
        "cv_names": list(cv_names or []),
        "temperature_kelvin": float(temperature_kelvin),
        "parameters": parameters or {},
        "bias_energy_unit": "kJ/mol",
    }
    Path(meta_path).write_text(json.dumps(payload, indent=2, default=str))


def custom_force_signature(
    *,
    custom_force_script: Optional[str],
    custom_force_parameters: Optional[dict],
) -> Optional[dict]:
    """Reproducibility signature for the custom force.

    ``None`` when no custom force is configured. Otherwise a dict with the
    kind, the SHA-256 of the script content, and the parameters, so a biased
    node is distinct from an unbiased one and reproducible across runs.
    """
    if custom_force_script:
        return {
            "kind": "torch_script_energy",
            "sha256": sha256_file(Path(custom_force_script)),
            "parameters": custom_force_parameters or {},
        }
    return None


def load_custom_forces(
    *,
    system,
    topology_pdb_file: str,
    reference_positions,
    custom_force_script: Optional[str] = None,
    custom_force_parameters: Optional[dict] = None,
) -> dict:
    """Resolve a custom force from a user ``energy(positions, ctx)`` script.

    Returns a dict::

        {
            "forces": [openmm.Force, ...],   # to addForce before Simulation
            "evaluator": callable | None,    # (positions_np, box_np) -> cv_dict
            "cv_names": [str, ...],
            "has_cv": bool,
            "kind": str,
            "signature": dict,
        }

    Raises ``CustomForceError`` (with a stable ``code``) on any failure.
    """
    if not custom_force_script:
        raise CustomForceError(
            "custom_force_contract_error",
            "load_custom_forces called without a custom_force_script.",
        )

    params = dict(custom_force_parameters or {})
    mdtraj_top = _load_mdtraj_topology(topology_pdb_file, system)
    collapsed = _collapsed_resnames(topology_pdb_file, mdtraj_top)
    reference_np = _reference_positions_to_numpy(reference_positions)

    script_path = Path(custom_force_script)
    if not script_path.is_file():
        raise CustomForceError(
            "custom_force_script_error",
            f"Custom-force script not found: {custom_force_script}",
        )
    energy_fn = _load_energy_function(script_path)
    force, evaluator, cv_names = _build_python_torch_force(
        energy_fn=energy_fn,
        mdtraj_top=mdtraj_top,
        reference_np=reference_np,
        params=params,
        collapsed_resnames=collapsed,
    )

    signature = custom_force_signature(
        custom_force_script=custom_force_script,
        custom_force_parameters=custom_force_parameters,
    )
    return {
        "forces": [force],
        "evaluator": evaluator,
        "cv_names": cv_names,
        "has_cv": bool(cv_names),
        "kind": "torch_script_energy",
        "signature": signature,
    }
