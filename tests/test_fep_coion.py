"""Co-alchemical ion for charge-changing mutations (mdclaw.fep.coion).

A mutation that changes the net charge leaves a PME box with a different
neutralising background at its two end states, and that finite-size error is
not the same in the folded and unfolded boxes. One bulk water becomes a
counter-ion along the same lambda path so both end states carry the same box
charge.
"""
import json
from pathlib import Path

import numpy as np
import pytest

openmm = pytest.importorskip("openmm")
from openmm import app, unit  # noqa: E402

from mdclaw.fep.coion import (  # noqa: E402
    CoIonError,
    apply_coion_to_endstate,
    coion_restraint_force,
    plan_coalchemical_ions,
)

BOX_NM = 4.0
TIP3P = {"O": (-0.834, 0.315, 0.636), "H": (0.417, 1.0, 0.0)}
IONS = {"Na": (1.0, 0.244, 0.366), "Cl": (-1.0, 0.448, 0.149), "K": (1.0, 0.303, 0.810)}


def _box(ions=("Na", "Cl"), spacing=0.5):
    """A two-atom "solute" at the box centre, a grid of waters, some ions."""
    top, positions, params = app.Topology(), [], []
    chain = top.addChain("A")
    residue = top.addResidue("ALA", chain, "1")
    for name, shift in (("CA", 0.0), ("CB", 0.15)):
        top.addAtom(name, app.Element.getBySymbol("C"), residue)
        positions.append([BOX_NM / 2 + shift, BOX_NM / 2, BOX_NM / 2])
        params.append((0.0, 0.34, 0.4))
    solvent = top.addChain("W")
    grid = np.arange(spacing / 2, BOX_NM, spacing)
    for n, (x, y, z) in enumerate((x, y, z) for x in grid for y in grid for z in grid):
        if np.linalg.norm(np.array([x, y, z]) - BOX_NM / 2) < 0.4:
            continue
        water = top.addResidue("HOH", solvent, str(n))
        for name, element, dx in (("O", "O", 0.0), ("H1", "H", 0.0957), ("H2", "H", -0.0957)):
            top.addAtom(name, app.Element.getBySymbol(element), water)
            positions.append([x + dx, y, z])
            params.append(TIP3P[element])
    for n, symbol in enumerate(ions):
        ion = top.addResidue(symbol.upper(), solvent, f"i{n}")
        top.addAtom(symbol, app.Element.getBySymbol(symbol), ion)
        positions.append([0.05 + 0.3 * n, 0.05, 0.05])
        params.append(IONS[symbol])
    system = openmm.System()
    nb = openmm.NonbondedForce()
    for q, sigma, eps in params:
        system.addParticle(16.0)
        nb.addParticle(q, sigma, eps)
    system.addForce(nb)
    return top, system, np.array(positions), np.eye(3) * BOX_NM


def _plan(charge_change, **kwargs):
    top, system, pos, box = _box(**{k: kwargs.pop(k) for k in ("ions", "spacing") if k in kwargs})
    return plan_coalchemical_ions(top, system, pos, box, [0, 1], charge_change, **kwargs), system, pos


@pytest.mark.parametrize("charge_change,element,charge", [(-1.0, "Na", 1), (1.0, "Cl", -1)])
def test_the_ion_compensates_the_charge_change(charge_change, element, charge):
    plan, system, _ = _plan(charge_change)
    assert (plan.ion_element, plan.ion_charge) == (element, charge)
    assert plan.ion_parameters == pytest.approx(IONS[element])
    assert len(plan.waters) == 1
    water = plan.waters[0]
    assert water["site_distance_nm"] >= 1.5 and water["solute_distance_nm"] >= 1.0
    assert water["nearest_ion_distance_nm"] >= 0.6

    nb = system.getForces()[0]
    before = sum(nb.getParticleParameters(i)[0]._value for i in range(system.getNumParticles()))
    apply_coion_to_endstate(system, plan)
    after = sum(nb.getParticleParameters(i)[0]._value for i in range(system.getNumParticles()))
    assert after - before == pytest.approx(-charge_change)  # the box charge change is cancelled
    q, sigma, eps = nb.getParticleParameters(water["oxygen"])
    assert (q._value, sigma._value, eps._value) == pytest.approx(IONS[element])
    for index in water["others"]:
        q, _sigma, eps = nb.getParticleParameters(index)
        assert q._value == 0.0 and eps._value == 0.0


def test_the_water_farthest_from_the_site_is_chosen():
    plan, _, pos = _plan(-1.0)
    top, _system, _pos, box = _box()
    oxygens = [a.index for a in top.atoms() if a.name == "O"]
    delta = pos[oxygens] - pos[[0, 1]].mean(axis=0)
    delta -= np.round(delta / BOX_NM) * BOX_NM
    # Nothing acceptable lies farther away than the chosen one (minimum image).
    assert plan.waters[0]["site_distance_nm"] >= np.linalg.norm(delta, axis=1).max() - 0.5


def test_a_double_charge_change_takes_two_separated_waters():
    plan, _, pos = _plan(-2.0)
    a, b = (pos[w["oxygen"]] for w in plan.waters)
    delta = a - b
    delta -= np.round(delta / BOX_NM) * BOX_NM
    assert len(plan.waters) == 2 and np.linalg.norm(delta) >= 1.0


def test_a_neutral_mutation_needs_no_ion():
    assert _plan(0.0)[0] is None


def test_the_salt_in_the_box_supplies_the_parameters():
    plan, _, _ = _plan(-1.0, ions=("K", "Cl"))
    assert plan.ion_element == "K" and plan.ion_parameters == pytest.approx(IONS["K"])


@pytest.mark.parametrize("kwargs,charge_change,code", [
    ({"ions": ("Cl",)}, -1.0, "fep_coion_parameters_unavailable"),   # no cation to copy
    ({"min_site_distance_nm": 9.0}, -1.0, "fep_coion_box_too_small"),
    ({}, -3.0, "fep_coion_unsupported"),
    ({}, -0.5, "fep_coion_unsupported"),
])
def test_refusals_name_the_reason(kwargs, charge_change, code):
    with pytest.raises(CoIonError) as err:
        _plan(charge_change, **kwargs)
    assert err.value.code == code
    if code != "fep_coion_unsupported":
        assert "--charge-correction none" in str(err.value)


def test_the_tether_is_zero_at_the_build_position_and_in_its_own_group():
    _top, system, pos, box = _box()
    system.setDefaultPeriodicBoxVectors(*box)
    for force in system.getForces():
        force.setForceGroup(0)
    system.getForces()[0].setNonbondedMethod(openmm.NonbondedForce.CutoffPeriodic)
    system.addForce(coion_restraint_force([2], pos))
    context = openmm.Context(system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
    context.setPositions(pos * unit.nanometer)
    energy = lambda: context.getState(getEnergy=True, groups={4}).getPotentialEnergy()._value  # noqa: E731
    assert energy() == pytest.approx(0.0, abs=1e-9)
    moved = pos.copy()
    moved[2, 0] += 0.1
    context.setPositions(moved * unit.nanometer)
    assert energy() == pytest.approx(0.5 * 1000.0 * 0.1 ** 2)


def test_legs_must_treat_the_charge_change_the_same_way(tmp_path):
    from mdclaw.fep.analysis import check_legs_compatible

    def leg(name, manifest):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"forcefield": "ff19SB", **manifest}))
        return {"mutation": {"label": "A:A14D"}, "hybrid_manifest_file": str(path)}

    corrected = leg("folded", {"charge_correction": "coalchemical_ion"})
    legacy = leg("unfolded", {})  # written before the option existed: uncorrected
    mismatches, _ = check_legs_compatible(corrected, legacy)
    assert any(m.startswith("charge_correction") for m in mismatches)
    mismatches, _ = check_legs_compatible(corrected, leg("unfolded2", {"charge_correction": "coalchemical_ion"}))
    assert not [m for m in mismatches if m.startswith("charge_correction")]


@pytest.mark.slow
class TestSolvatedChargeChangingBuild:
    """build_hybrid_system end to end on solvated ACE-ASP-NME -> ACE-ALA-NME."""

    @staticmethod
    def _solvated(tmp_path, ionic_strength):
        from tests.test_fep import TestHybridVacuum

        top, pos = TestHybridVacuum._peptide("ASP", tmp_path)
        ff = app.ForceField("amber14-all.xml", "amber14/tip3p.xml")
        modeller = app.Modeller(top, pos)
        modeller.addSolvent(ff, model="tip3p", padding=1.7 * unit.nanometer,
                            ionicStrength=ionic_strength * unit.molar, neutralize=True)
        box = modeller.topology.getPeriodicBoxVectors()
        dims = {k: box[i][i].value_in_unit(unit.angstrom) - 2.0 for i, k in enumerate(("box_a", "box_b", "box_c"))}
        pdb = tmp_path / "solvated.pdb"
        with pdb.open("w") as fh:
            app.PDBFile.writeFile(modeller.topology, modeller.positions, fh, keepIds=True)
        return dict(mutation="A:D2A", pdb_file=str(pdb), box_dimensions=dims, endstate_builder="openmm",
                    forcefield_xml=["amber14-all.xml", "amber14/tip3p.xml"], platform="CPU", n_windows=5)

    @staticmethod
    def _net_charge(system_xml, state):
        system = openmm.XmlSerializer.deserialize(Path(system_xml).read_text())
        nb = next(f for f in system.getForces() if isinstance(f, openmm.NonbondedForce))
        total = sum(nb.getParticleParameters(i)[0]._value for i in range(nb.getNumParticles()))
        for k in range(nb.getNumParticleParameterOffsets()):
            name, _index, dq, _ds, _de = nb.getParticleParameterOffset(k)
            total += dq * state[name]
        return total

    def test_both_end_states_carry_the_same_box_charge(self, tmp_path):
        pytest.importorskip("openmmforcefields")
        from mdclaw.fep.build import build_hybrid_system
        from mdclaw.fep.hybrid import STATE_A, STATE_B

        kwargs = self._solvated(tmp_path, 0.5)
        res = build_hybrid_system(**kwargs, output_dir=str(tmp_path / "coion"))
        assert res["success"], res
        assert res["endpoint_validation"]["passed"], res["endpoint_validation"]  # B is checked with the ion in place
        correction = res["charge_correction"]
        assert correction["method"] == "coalchemical_ion" and correction["charge_change_e"] == pytest.approx(1.0)
        assert correction["ion"]["element"] == "Cl" and len(correction["waters"]) == 1
        assert self._net_charge(res["system_xml"], STATE_A) == pytest.approx(0.0, abs=1e-6)
        assert self._net_charge(res["system_xml"], STATE_B) == pytest.approx(0.0, abs=1e-6)
        assert json.loads(Path(res["hybrid_manifest"]).read_text())["charge_correction"] == "coalchemical_ion"
        assert not [w for w in res["warnings"] if "Charge-changing" in w]

        plain = build_hybrid_system(**kwargs, charge_correction="none", output_dir=str(tmp_path / "none"))
        assert plain["success"] and plain["charge_correction"]["method"] == "none"
        assert self._net_charge(plain["system_xml"], STATE_B) == pytest.approx(1.0, abs=1e-6)
        assert any("--charge-correction none" in w for w in plain["warnings"])

    def test_no_salt_is_refused_rather_than_run_uncorrected(self, tmp_path):
        pytest.importorskip("openmmforcefields")
        from mdclaw.fep.build import build_hybrid_system

        # D -> A needs a Cl-; a box neutralised with Na+ only has none to copy.
        res = build_hybrid_system(**self._solvated(tmp_path, 0.0), output_dir=str(tmp_path / "out"))
        assert res["success"] is False and res["code"] == "fep_coion_parameters_unavailable"
