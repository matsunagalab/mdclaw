"""Unit tests for the production custom force / CV bias plugin.

These tests exercise the autograd wrapping, atom-selection context, error
codes, signature stability, and the CV/bias reporter without requiring a real
``openmmtorch`` install: a lightweight fake ``PythonTorchForce`` captures the
compute callable so the autograd math can be checked directly.
"""

import json
import sys
import types

import numpy as np
import pytest

from mdclaw.simulation import custom_forces as cf


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

_N_CA = 5


def _write_pdb(path) -> int:
    """Write a tiny PDB of ``_N_CA`` CA atoms (one per ALA residue)."""
    lines = []
    for i in range(_N_CA):
        x = float(i)  # Angstrom; spaced 1 A along x
        lines.append(
            f"ATOM  {i + 1:>5}  CA  ALA A{i + 1:>4}    "
            f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
        )
    lines.append("END")
    path.write_text("\n".join(lines) + "\n")
    return _N_CA


class _FakeSystem:
    def __init__(self, n_particles):
        self._n = n_particles

    def getNumParticles(self):
        return self._n


def _reference_quantity(pdb_path):
    """Reference positions as an OpenMM Quantity (nm), read from the PDB."""
    import mdtraj as md
    from openmm.unit import nanometer

    top = md.load(str(pdb_path))
    return top.xyz[0].astype(np.float64) * nanometer


@pytest.fixture
def fake_openmmtorch(monkeypatch):
    """Inject a fake ``openmmtorch`` whose PythonTorchForce stores the fn."""

    class _FakePythonTorchForce:
        def __init__(self, fn, *args, **kwargs):
            self.fn = fn
            self.group = None
            self.pbc = False

        def setForceGroup(self, group):
            self.group = group

        def setUsesPeriodicBoundaryConditions(self, value):
            self.pbc = value

    class _FakeTorchForce:
        def __init__(self, arg, *a, **k):
            self.arg = arg

        def setUsesPeriodicBoundaryConditions(self, value):
            self.pbc = value

        def addGlobalParameter(self, name, value):
            pass

    mod = types.ModuleType("openmmtorch")
    mod.PythonTorchForce = _FakePythonTorchForce
    mod.TorchForce = _FakeTorchForce
    monkeypatch.setitem(sys.modules, "openmmtorch", mod)
    return mod


@pytest.fixture
def pdb_setup(tmp_path):
    pdb = tmp_path / "topology.pdb"
    n = _write_pdb(pdb)
    return {
        "pdb": pdb,
        "n": n,
        "system": _FakeSystem(n),
        "reference": _reference_quantity(pdb),
        "tmp": tmp_path,
    }


def _load(pdb_setup, script_text, params=None):
    script = pdb_setup["tmp"] / "energy.py"
    script.write_text(script_text)
    return cf.load_custom_forces(
        system=pdb_setup["system"],
        topology_pdb_file=str(pdb_setup["pdb"]),
        reference_positions=pdb_setup["reference"],
        custom_force_script=str(script),
        custom_force_parameters=params or {},
    )


# --------------------------------------------------------------------------
# autograd correctness
# --------------------------------------------------------------------------

POSITIONAL_RESTRAINT = """
import torch

def energy(positions, ctx):
    sel = ctx.select("name CA")
    k = ctx.params["k"]
    disp = positions[sel] - ctx.reference[sel]
    return 0.5 * k * (disp ** 2).sum()
"""

DISTANCE_BIAS = """
import torch

def energy(positions, ctx):
    i = ctx.select("index 0")
    j = ctx.select("index 4")
    d = torch.linalg.norm(positions[i][0] - positions[j][0])
    k = ctx.params["k"]; d0 = ctx.params["d0"]
    return 0.5 * k * (d - d0) ** 2, {"d": d}
"""


def test_autograd_positional_restraint(pdb_setup, fake_openmmtorch):
    import torch

    k = 500.0
    loaded = _load(pdb_setup, POSITIONAL_RESTRAINT, {"k": k})
    force = loaded["forces"][0]

    ref = np.array(
        [[float(i), 0.0, 0.0] for i in range(_N_CA)], dtype=np.float64
    ) * 0.1  # PDB Angstrom -> nm
    # Displace every atom by +0.05 nm in y.
    pos_np = ref.copy()
    pos_np[:, 1] += 0.05
    positions = torch.tensor(pos_np, dtype=torch.double)

    energy, forces = force.fn(None, positions)

    expected_energy = 0.5 * k * np.sum((pos_np - ref) ** 2)
    assert float(energy.detach()) == pytest.approx(expected_energy, rel=1e-3)
    # Force = -k * (pos - ref): only the y component is non-zero. Tolerance
    # absorbs the float32 precision of the mdtraj-sourced ctx.reference.
    expected_forces = -k * (pos_np - ref)
    np.testing.assert_allclose(
        forces.detach().numpy(), expected_forces, rtol=1e-3, atol=1e-2
    )
    assert loaded["has_cv"] is False
    assert loaded["cv_names"] == []


def test_autograd_distance_bias_with_cv(pdb_setup, fake_openmmtorch):
    import torch

    k, d0 = 1000.0, 0.2
    loaded = _load(pdb_setup, DISTANCE_BIAS, {"k": k, "d0": d0})
    force = loaded["forces"][0]
    assert loaded["has_cv"] is True
    assert loaded["cv_names"] == ["d"]

    pos_np = np.array(
        [[float(i), 0.0, 0.0] for i in range(_N_CA)], dtype=np.float64
    ) * 0.1
    positions = torch.tensor(pos_np, dtype=torch.double)
    energy, forces = force.fn(None, positions)

    d = np.linalg.norm(pos_np[0] - pos_np[4])
    assert float(energy.detach()) == pytest.approx(0.5 * k * (d - d0) ** 2, rel=1e-6)
    # Atoms 1-3 are not in the CV; their forces must be zero.
    np.testing.assert_allclose(forces.detach().numpy()[1:4], 0.0, atol=1e-9)

    # The evaluator (reporter path) recovers the CV value.
    cv = loaded["evaluator"](pos_np, None)
    assert cv["d"] == pytest.approx(d, rel=1e-6)


def test_device_dtype_absorption(pdb_setup, fake_openmmtorch):
    import torch

    loaded = _load(pdb_setup, POSITIONAL_RESTRAINT, {"k": 100.0})
    force = loaded["forces"][0]
    pos_np = np.array(
        [[float(i), 0.1, 0.0] for i in range(_N_CA)], dtype=np.float64
    ) * 0.1
    for dtype in (torch.float32, torch.float64):
        positions = torch.tensor(pos_np, dtype=dtype)
        energy, forces = force.fn(None, positions)
        assert torch.isfinite(energy).all()
        assert forces.shape == positions.shape
        assert forces.dtype == positions.dtype


# --------------------------------------------------------------------------
# error codes
# --------------------------------------------------------------------------

def test_contract_error_non_scalar(pdb_setup, fake_openmmtorch):
    script = """
import torch
def energy(positions, ctx):
    return positions[ctx.select("name CA")]  # vector, not scalar
"""
    with pytest.raises(cf.CustomForceError) as exc:
        _load(pdb_setup, script, {})
    assert exc.value.code == "custom_force_contract_error"


def test_selection_empty(pdb_setup, fake_openmmtorch):
    script = """
import torch
def energy(positions, ctx):
    sel = ctx.select("name ZZ")  # matches nothing
    return positions[sel].sum()
"""
    with pytest.raises(cf.CustomForceError) as exc:
        _load(pdb_setup, script, {})
    assert exc.value.code == "custom_force_selection_empty"


def test_topology_mismatch(pdb_setup, fake_openmmtorch):
    script = pdb_setup["tmp"] / "energy.py"
    script.write_text(POSITIONAL_RESTRAINT)
    with pytest.raises(cf.CustomForceError) as exc:
        cf.load_custom_forces(
            system=_FakeSystem(pdb_setup["n"] + 3),  # wrong particle count
            topology_pdb_file=str(pdb_setup["pdb"]),
            reference_positions=pdb_setup["reference"],
            custom_force_script=str(script),
            custom_force_parameters={"k": 1.0},
        )
    assert exc.value.code == "custom_force_topology_mismatch"


def test_script_missing_entry_point(pdb_setup, fake_openmmtorch):
    with pytest.raises(cf.CustomForceError) as exc:
        _load(pdb_setup, "x = 1\n", {})
    assert exc.value.code == "custom_force_script_error"


def test_dependency_missing(pdb_setup, monkeypatch):
    # Setting the module to None makes ``import openmmtorch`` raise ImportError.
    monkeypatch.setitem(sys.modules, "openmmtorch", None)
    script = pdb_setup["tmp"] / "energy.py"
    script.write_text(POSITIONAL_RESTRAINT)
    with pytest.raises(cf.CustomForceError) as exc:
        cf.load_custom_forces(
            system=pdb_setup["system"],
            topology_pdb_file=str(pdb_setup["pdb"]),
            reference_positions=pdb_setup["reference"],
            custom_force_script=str(script),
            custom_force_parameters={"k": 1.0},
        )
    assert exc.value.code == "custom_force_dependency_missing"


def test_both_routes_rejected(pdb_setup):
    with pytest.raises(cf.CustomForceError) as exc:
        cf.load_custom_forces(
            system=pdb_setup["system"],
            topology_pdb_file=str(pdb_setup["pdb"]),
            reference_positions=pdb_setup["reference"],
            custom_force_script="a.py",
            custom_force_module="b.pt",
        )
    assert exc.value.code == "custom_force_contract_error"


# --------------------------------------------------------------------------
# ctx selection / atomic numbers
# --------------------------------------------------------------------------

def test_ctx_select_matches_mdtraj(pdb_setup):
    import mdtraj as md
    import torch

    top = md.load_topology(str(pdb_setup["pdb"]))
    positions = torch.zeros((pdb_setup["n"], 3), dtype=torch.double)
    ctx = cf._EvalContext(
        positions=positions, mdtraj_top=top, reference_np=np.zeros((pdb_setup["n"], 3)),
        params={},
    )
    idx = ctx.select("name CA")
    np.testing.assert_array_equal(idx.numpy(), top.select("name CA"))
    # All atoms are carbons (atomic number 6).
    assert ctx.atomic_numbers == [6] * pdb_setup["n"]


# --------------------------------------------------------------------------
# signature stability
# --------------------------------------------------------------------------

def test_signature_stability(pdb_setup):
    script = pdb_setup["tmp"] / "energy.py"
    script.write_text(POSITIONAL_RESTRAINT)
    sig1 = cf.custom_force_signature(
        custom_force_script=str(script), custom_force_module=None,
        custom_force_parameters={"k": 1.0},
    )
    sig2 = cf.custom_force_signature(
        custom_force_script=str(script), custom_force_module=None,
        custom_force_parameters={"k": 1.0},
    )
    assert sig1 == sig2
    assert sig1["kind"] == "torch_script_energy"
    assert sig1["sha256"] and len(sig1["sha256"]) == 64
    # Editing the script changes the hash.
    script.write_text(POSITIONAL_RESTRAINT + "\n# edit\n")
    sig3 = cf.custom_force_signature(
        custom_force_script=str(script), custom_force_module=None,
        custom_force_parameters={"k": 1.0},
    )
    assert sig3["sha256"] != sig1["sha256"]


def test_signature_none_without_force():
    assert cf.custom_force_signature(
        custom_force_script=None, custom_force_module=None,
        custom_force_parameters=None,
    ) is None


# --------------------------------------------------------------------------
# CV / bias reporter
# --------------------------------------------------------------------------

class _FakeQuantity:
    def __init__(self, value):
        self._value = value

    def value_in_unit(self, unit):
        return self._value


class _FakeEnergyState:
    def __init__(self, energy_kj):
        self._e = energy_kj

    def getPotentialEnergy(self):
        from openmm.unit import kilojoule_per_mole

        return self._e * kilojoule_per_mole


class _FakeContext:
    def __init__(self, energy_kj):
        self._e = energy_kj
        self.requested_groups = None

    def getState(self, getEnergy=False, groups=None):
        self.requested_groups = groups
        return _FakeEnergyState(self._e)


class _FakeSim:
    def __init__(self, step, energy_kj):
        self.currentStep = step
        self.context = _FakeContext(energy_kj)


class _FakeReportState:
    def __init__(self, n):
        self._n = n

    def getTime(self):
        from openmm.unit import picosecond

        return 12.0 * picosecond

    def getPositions(self, asNumpy=False):
        from openmm.unit import nanometer

        return np.zeros((self._n, 3)) * nanometer

    def getPeriodicBoxVectors(self, asNumpy=False):
        return None


def test_reporter_writes_bias_and_cv(tmp_path):
    csv = tmp_path / "cv.csv"
    reporter = cf.CustomForceReporter(
        str(csv), 10, force_group=cf.CUSTOM_FORCE_GROUP,
        evaluator=lambda pos, box: {"d": 1.234},
        cv_names=["d"],
    )
    sim = _FakeSim(step=10, energy_kj=7.5)
    reporter.report(sim, _FakeReportState(_N_CA))
    reporter.close()

    rows = csv.read_text().strip().splitlines()
    assert rows[0] == "step,time_ps,bias_energy_kj_mol,d"
    fields = rows[1].split(",")
    assert fields[0] == "10"
    assert float(fields[1]) == pytest.approx(12.0)
    assert float(fields[2]) == pytest.approx(7.5)
    assert float(fields[3]) == pytest.approx(1.234)
    # Bias energy was requested from the dedicated force group only.
    assert sim.context.requested_groups == {cf.CUSTOM_FORCE_GROUP}


def test_reporter_bias_only_no_cv(tmp_path):
    csv = tmp_path / "cv.csv"
    reporter = cf.CustomForceReporter(
        str(csv), 10, force_group=cf.CUSTOM_FORCE_GROUP,
        evaluator=None, cv_names=[],
    )
    reporter.report(_FakeSim(step=20, energy_kj=3.0), _FakeReportState(_N_CA))
    reporter.close()
    rows = csv.read_text().strip().splitlines()
    assert rows[0] == "step,time_ps,bias_energy_kj_mol"
    assert rows[1].split(",")[2] == f"{3.0:.6f}"


def test_continue_from_inherits_custom_force(tmp_path):
    """A child prod node continued from a biased parent inherits its
    custom-force script and parameters via the inputs resolver."""
    from mdclaw.node.inputs import _resolve_prod_custom_force

    job = tmp_path / "job"
    parent = job / "nodes" / "prod_parent" / "artifacts"
    parent.mkdir(parents=True)
    (parent / "custom_force_script.py").write_text("def energy(p, c):\n    return p.sum()\n")
    (job / "nodes" / "prod_parent" / "node.json").write_text(json.dumps({
        "node_id": "prod_parent",
        "node_type": "prod",
        "artifacts": {"custom_force_script": "artifacts/custom_force_script.py"},
        "metadata": {"custom_force_parameters": {"k": 750.0}},
    }))
    child = job / "nodes" / "prod_child"
    child.mkdir(parents=True)
    (child / "node.json").write_text(json.dumps({
        "node_id": "prod_child",
        "node_type": "prod",
        "metadata": {"continued_from": "prod_parent"},
    }))

    inherited = _resolve_prod_custom_force(str(job), "prod_child")
    assert inherited["custom_force_script"].endswith("custom_force_script.py")
    assert inherited["custom_force_parameters"] == {"k": 750.0}


def test_continue_from_no_custom_force(tmp_path):
    """No inheritance when the parent had no custom force."""
    from mdclaw.node.inputs import _resolve_prod_custom_force

    job = tmp_path / "job"
    (job / "nodes" / "prod_parent").mkdir(parents=True)
    (job / "nodes" / "prod_parent" / "node.json").write_text(json.dumps({
        "node_id": "prod_parent", "node_type": "prod", "artifacts": {}, "metadata": {},
    }))
    child = job / "nodes" / "prod_child"
    child.mkdir(parents=True)
    (child / "node.json").write_text(json.dumps({
        "node_id": "prod_child", "node_type": "prod",
        "metadata": {"continued_from": "prod_parent"},
    }))
    assert _resolve_prod_custom_force(str(job), "prod_child") == {}


def test_write_cv_metadata(tmp_path):
    meta = tmp_path / "cv.meta.json"
    cf.write_cv_metadata(
        str(meta), signature={"kind": "torch_script_energy", "sha256": "abc"},
        cv_names=["d"], temperature_kelvin=300.0, parameters={"k": 1.0},
    )
    data = json.loads(meta.read_text())
    assert data["cv_names"] == ["d"]
    assert data["temperature_kelvin"] == 300.0
    assert data["bias_energy_unit"] == "kJ/mol"
    assert data["parameters"] == {"k": 1.0}
