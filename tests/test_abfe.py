"""Absolute binding free energy: ligand decoupling, Boresch restraint, cycle closure."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from mdclaw.fep.abfe import (
    DEFAULT_ELEC_LAMBDAS,
    DEFAULT_RESTRAINT_LAMBDAS,
    DEFAULT_STERICS_LAMBDAS,
    AbfeError,
    abfe_windows,
    select_ligand_record,
)
from mdclaw.fep.boresch import (
    KB_KJ_MOL_K,
    STANDARD_VOLUME_NM3,
    BoreschError,
    BoreschRestraint,
    boresch_coordinates,
    standard_state_restraint_free_energy,
)
from mdclaw.fep.protocol import ProtocolError, load_protocol, protocols_equivalent


# --------------------------------------------------------------------------- #
# protocol                                                                      #
# --------------------------------------------------------------------------- #

def test_solvent_leg_switches_charges_off_before_sterics():
    windows, phases = abfe_windows(list(DEFAULT_ELEC_LAMBDAS), list(DEFAULT_STERICS_LAMBDAS))
    assert len(windows) == len(DEFAULT_ELEC_LAMBDAS) + len(DEFAULT_STERICS_LAMBDAS) - 1
    assert windows[0]["lambda"] == 0.0 and windows[-1]["lambda"] == 1.0
    assert "fep_restraint" not in windows[0]["parameters"]
    for w in windows:
        p = w["parameters"]
        # a charge never sits inside a soft core
        assert p["fep_sterics_old"] == 1.0 or p["fep_elec_old"] == 0.0
        assert p["fep_core"] == p["fep_sterics_new"] == p["fep_elec_new"] == 0.0
    assert [ph["name"] for ph in phases] == ["decharge", "decouple_sterics"]
    assert phases[0]["lambda_hi"] == phases[1]["lambda_lo"]


def test_complex_leg_restrains_first(tmp_path):
    windows, phases = abfe_windows([1, 0.5, 0], [1, 0.5, 0], list(DEFAULT_RESTRAINT_LAMBDAS))
    assert [ph["name"] for ph in phases] == ["restrain", "decharge", "decouple_sterics"]
    first_decharge = phases[1]["first_window"]
    assert windows[0]["parameters"]["fep_restraint"] == 0.0
    assert all(w["parameters"]["fep_restraint"] == 1.0 for w in windows[first_decharge:])
    assert all(w["parameters"]["fep_elec_old"] == 1.0 for w in windows[:first_decharge + 1])

    # the six-parameter protocol round-trips; an unknown parameter does not
    path = tmp_path / "p.json"
    protocol = {"global_parameters": list(windows[0]["parameters"]), "windows": windows, "mutation": {"label": "decouple:LIG"}}
    path.write_text(json.dumps(protocol))
    loaded = load_protocol(path)
    assert loaded["windows"][-1]["parameters"]["fep_restraint"] == 1.0
    assert protocols_equivalent(loaded, json.loads(path.read_text()))
    solvent, _ = abfe_windows([1, 0.5, 0], [1, 0.5, 0])
    assert not protocols_equivalent(loaded, {"windows": solvent, "mutation": {"label": "decouple:LIG"}})
    path.write_text(json.dumps({**protocol, "global_parameters": ["fep_elec_old", "fep_made_up"]}))
    with pytest.raises(ProtocolError):
        load_protocol(path)


# --------------------------------------------------------------------------- #
# ligand identity                                                               #
# --------------------------------------------------------------------------- #

_LIGANDS = [{"residue_name": "BEN", "chain_id": "A", "resnum": 246}, {"residue_name": "GOL", "chain_id": "A", "resnum": 301},
            {"residue_name": "GOL", "chain_id": "A", "resnum": 302}]


def test_ligand_selection_is_explicit_when_there_is_a_choice():
    assert select_ligand_record(_LIGANDS[:1], None)["residue_name"] == "BEN"
    assert select_ligand_record(_LIGANDS, "BEN")["resnum"] == 246
    assert select_ligand_record(_LIGANDS, "A:GOL:302")["resnum"] == 302
    for ligand, code in ((None, "abfe_ligand_ambiguous"), ("GOL", "abfe_ligand_ambiguous"), ("XYZ", "abfe_ligand_not_found")):
        with pytest.raises(AbfeError) as err:
            select_ligand_record(_LIGANDS, ligand)
        assert err.value.code == code and err.value.extra["ligand_candidates"]
    with pytest.raises(AbfeError) as err:
        select_ligand_record([], None)
    assert err.value.code == "abfe_ligand_not_found"


# --------------------------------------------------------------------------- #
# Boresch restraint                                                             #
# --------------------------------------------------------------------------- #

_trapezoid = getattr(np, "trapezoid", None) or np.trapz


def _restraint(**kw):
    base = dict(receptor_atoms=(0, 1, 2), ligand_atoms=(3, 4, 5), r0_nm=0.5, theta_a0=math.radians(80),
                theta_b0=math.radians(110), phi_a0=0.3, phi_b0=-2.0, phi_c0=3.0)
    return BoreschRestraint(**{**base, **kw})


def test_restraint_free_energy_matches_the_configurational_integral():
    """The closed form is the Gaussian limit of
    ``-kT ln[ Z_restrained / (8 pi^2 V0) ]``; integrate Z numerically."""
    r = _restraint()
    T = 300.0
    kT = KB_KJ_MOL_K * T
    x = np.linspace(0.2, 0.8, 4001)
    z_r = _trapezoid(x ** 2 * np.exp(-0.5 * r.k_distance * (x - r.r0_nm) ** 2 / kT), x)
    th = np.linspace(0.0, math.pi, 4001)
    z_a = _trapezoid(np.sin(th) * np.exp(-0.5 * r.k_angle * (th - r.theta_a0) ** 2 / kT), th)
    z_b = _trapezoid(np.sin(th) * np.exp(-0.5 * r.k_angle * (th - r.theta_b0) ** 2 / kT), th)
    ph = np.linspace(-math.pi, math.pi, 4001)
    z_phi = _trapezoid(np.exp(-0.5 * r.k_angle * ph ** 2 / kT), ph)
    numeric = -kT * math.log(z_r * z_a * z_b * z_phi ** 3 / (8 * math.pi ** 2 * STANDARD_VOLUME_NM3))
    analytic = standard_state_restraint_free_energy(r, T)
    assert analytic > 0  # confining the ligand costs free energy
    assert analytic == pytest.approx(numeric, abs=0.15)
    assert 25.0 < analytic < 45.0  # ~ +8 kcal/mol for these force constants


def test_coordinates_follow_openmm_conventions():
    """The reference values measured here must be the zero of the OpenMM force
    (same angle and dihedral sign conventions), in a periodic box too."""
    openmm = pytest.importorskip("openmm")
    from openmm import unit

    from mdclaw.fep.boresch import boresch_force

    rng = np.random.default_rng(7)
    pos = rng.uniform(0.8, 2.2, size=(6, 3))
    box = np.eye(3) * 3.0
    shifted = pos.copy()
    shifted[3:] += box[0]  # the ligand sits in the neighbouring image
    for coords in (pos, shifted):
        values = boresch_coordinates(coords[None], box[None], (0, 1, 2), (3, 4, 5))[0]
        restraint = _restraint(r0_nm=values[0], theta_a0=values[1], theta_b0=values[2], phi_a0=values[3],
                               phi_b0=values[4], phi_c0=values[5])
        system = openmm.System()
        for _ in range(6):
            system.addParticle(12.0)
        system.setDefaultPeriodicBoxVectors(*box)
        system.addForce(boresch_force(restraint, periodic=True))
        context = openmm.Context(system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
        context.setPositions(coords * unit.nanometer)
        energy = lambda: context.getState(getEnergy=True).getPotentialEnergy()._value  # noqa: E731
        assert energy() == pytest.approx(0.0, abs=1e-6)
        context.setParameter("fep_restraint", 0.0)
        moved = coords.copy()
        moved[5] += 0.3
        context.setPositions(moved * unit.nanometer)
        assert energy() == 0.0                      # switched off
        context.setParameter("fep_restraint", 1.0)
        assert energy() > 0.1                       # only phiC moved: a dihedral term
        context.setParameter("fep_restraint", 0.5)
        half = energy()
        context.setParameter("fep_restraint", 1.0)
        assert half == pytest.approx(0.5 * energy())


def _complex_topology():
    app = pytest.importorskip("openmm.app")
    top = app.Topology()
    chain = top.addChain("A")
    for n in range(4):
        residue = top.addResidue("ALA", chain, str(n + 1))
        for name, el in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
            top.addAtom(name, app.Element.getBySymbol(el), residue)
    lig = top.addResidue("LIG", top.addChain("L"), "1")
    atoms = [top.addAtom(f"C{i}", app.Element.getBySymbol("C"), lig) for i in range(4)]
    atoms.append(top.addAtom("H1", app.Element.getBySymbol("H"), lig))
    for i, j in ((0, 1), (1, 2), (2, 3), (3, 4)):
        top.addBond(atoms[i], atoms[j])
    return top, [a.index for a in atoms]


def _frames(noise, n=60, seed=1):
    rng = np.random.default_rng(seed)
    base = np.zeros((21, 3))
    for n_res in range(4):
        origin = np.array([0.38 * n_res, 0.0, 0.0])
        base[4 * n_res:4 * n_res + 4] = origin + np.array([[0, 0, 0], [0.12, 0.08, 0], [0.24, 0, 0.03], [0.28, -0.1, 0.1]])
    base[16:] = np.array([0.5, 0.75, 0.2]) + np.array([[0, 0, 0], [0.14, 0.05, 0.02], [0.2, 0.18, 0.1], [0.34, 0.2, 0.2], [0.4, 0.3, 0.2]])
    frames = np.repeat(base[None], n, axis=0) + rng.normal(0, 0.003, size=(n, 21, 3))
    frames[:, 16:] += rng.normal(0, noise, size=(n, 1, 3))  # rigid-body jitter of the ligand
    return frames


def test_selection_anchors_bonded_heavy_atoms_to_a_backbone():
    from mdclaw.fep.boresch import select_boresch_restraint

    top, ligand = _complex_topology()
    restraint = select_boresch_restraint(top, _frames(0.01), None, ligand)
    names = [a.name for a in top.atoms()]
    assert [names[i] for i in restraint.receptor_atoms] == ["N", "C", "CA"]
    assert all(i in ligand and names[i] != "H1" for i in restraint.ligand_atoms)
    assert len(set(restraint.ligand_atoms)) == 3
    assert 0.4 <= restraint.r0_nm <= 1.5
    assert math.radians(40) <= restraint.theta_a0 <= math.radians(140)
    assert math.radians(40) <= restraint.theta_b0 <= math.radians(140)
    assert restraint.statistics["n_frames"] == 60
    assert BoreschRestraint.from_json(restraint.to_json()).ligand_atoms == restraint.ligand_atoms


def _spinning_frames(n=100, seed=2):
    """The ligand stays put but turns about its own centroid: every frame a
    fresh random rotation about a fixed axis (benzene spinning in the T4L
    cavity: r std 0.03 nm, one dihedral std 86 deg, 103 well hops in 200 ps)."""
    rng = np.random.default_rng(seed)
    frames = _frames(0.005, n=n, seed=seed)
    axis = np.array([0.3, 0.2, 0.93]) / np.linalg.norm([0.3, 0.2, 0.93])
    for f in range(n):
        lig = frames[f, 16:]
        centre = lig.mean(axis=0)
        ang = rng.uniform(0, 2 * math.pi)
        k = axis
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        R = np.eye(3) + math.sin(ang) * K + (1 - math.cos(ang)) * K @ K
        frames[f, 16:] = (lig - centre) @ R.T + centre
    return frames


def test_a_ligand_that_turns_in_place_is_restrained_at_its_mode_not_refused():
    from mdclaw.fep.boresch import boresch_coordinates, select_boresch_restraint

    top, ligand = _complex_topology()
    frames = _spinning_frames()
    restraint = select_boresch_restraint(top, frames, None, ligand)
    stats = restraint.statistics
    assert stats["reorients"] is True
    assert stats["reorienting_coordinates"], stats
    assert all(math.degrees(stats["std"][c]) > 25 for c in stats["reorienting_coordinates"])
    assert stats["std"]["r_nm"] < 0.15
    # the angular references are values the ligand adopted, not a mean between wells
    series = boresch_coordinates(frames, None, restraint.receptor_atoms, restraint.ligand_atoms)
    refs = np.array([restraint.theta_a0, restraint.theta_b0, restraint.phi_a0, restraint.phi_b0, restraint.phi_c0])
    assert any(np.allclose(row[1:], refs, atol=1e-9) for row in series)
    assert math.radians(40) <= restraint.theta_a0 <= math.radians(140)


def test_selection_refuses_instead_of_restraining_a_loose_pose():
    from mdclaw.fep.boresch import select_boresch_restraint

    top, ligand = _complex_topology()
    for frames, atoms, code in ((_frames(0.4), ligand, "abfe_restraint_unstable"),      # ligand wanders
                                (_frames(0.01)[:5], ligand, "abfe_restraint_unstable"),  # too few frames
                                (_frames(0.01), ligand[:2], "abfe_ligand_too_small")):
        with pytest.raises(BoreschError) as err:
            select_boresch_restraint(top, frames, None, atoms)
        assert err.value.code == code
    far = _frames(0.01)
    far[:, 16:] += 5.0                                                                   # not in the site
    with pytest.raises(BoreschError) as err:
        select_boresch_restraint(top, far, None, ligand)
    assert err.value.code == "abfe_restraint_unstable"


# --------------------------------------------------------------------------- #
# DAG nodes                                                                     #
# --------------------------------------------------------------------------- #

def _complete(job, node_id, artifacts=None, metadata=None, files=()):
    from mdclaw._node import complete_node

    base = Path(job) / "nodes" / node_id
    for rel, text in files:
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text)
    complete_node(str(job), node_id, artifacts=artifacts or {}, metadata=metadata or {})


_COMPLEX_PDB = "\n".join([
    "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N",
    "ATOM      2  CA  ALA A   1       1.400   0.000   0.000  1.00  0.00           C",
    "HETATM    3  C1  GOL B 483       5.000   5.000   5.000  1.00  0.00           C",
    "HETATM    4  O1  GOL B 483       6.200   5.000   5.000  1.00  0.00           O",
    "HETATM    5  C1  BEN C 481       9.000   9.000   9.000  1.00  0.00           C",
    "END", ""])
_RECORDS = [
    {"residue_name": "BEN", "chain_id": "Ax2", "author_chain": "A", "resnum": 481, "net_charge": 1,
     "sdf_file": "artifacts/split/ben.sdf", "smiles": "NC(=N)c1ccccc1"},
    {"residue_name": "GOL", "chain_id": "Ax4", "author_chain": "A", "resnum": 483, "net_charge": 0,
     "sdf_file": "artifacts/split/gol.sdf", "smiles": "OCC(O)CO"},
]


def _job_with_complex_prep(tmp_path):
    from mdclaw._node import create_node

    job = tmp_path / "job"
    job.mkdir()
    source = create_node(str(job), "source")["node_id"]
    _complete(job, source, {"source_bundle": "artifacts/sb.json"}, files=[("artifacts/sb.json", "{}")])
    prep = create_node(str(job), "prep", parent_node_ids=[source])["node_id"]
    _complete(job, prep, {"merged_pdb": "artifacts/merge/merged.pdb", "ligand_chemistry": _RECORDS},
              files=[("artifacts/merge/merged.pdb", _COMPLEX_PDB), ("artifacts/split/gol.sdf", "gol"),
                     ("artifacts/split/ben.sdf", "ben")])
    return job, source, prep


def test_extract_ligand_derives_the_solvent_leg_prep(tmp_path):
    from mdclaw._node import create_node, read_node
    from mdclaw.fep.abfe import extract_ligand

    job, source, prep = _job_with_complex_prep(tmp_path)
    node = create_node(str(job), "prep", parent_node_ids=[prep])["node_id"]
    ambiguous = extract_ligand(job_dir=str(job), node_id=node)
    assert ambiguous["code"] == "abfe_ligand_ambiguous" and read_node(str(job), node)["status"] == "pending"
    charged = extract_ligand(job_dir=str(job), node_id=node, ligand="BEN")
    assert charged["code"] == "abfe_charged_ligand_unsupported" and read_node(str(job), node)["status"] == "pending"

    done = extract_ligand(job_dir=str(job), node_id=node, ligand="A:GOL:483")   # the chain a user sees
    assert done["success"], done
    written = Path(done["merged_pdb"]).read_text()
    atoms = [ln for ln in written.splitlines() if ln.startswith(("ATOM", "HETATM"))]
    assert len(atoms) == 2 and all(ln[17:20] == "GOL" for ln in atoms)
    data = read_node(str(job), node)
    assert data["metadata"]["leg_role"] == "solvent" and data["metadata"]["derived_from_prep_node_id"] == prep
    assert [r["residue_name"] for r in data["artifacts"]["ligand_chemistry"]] == ["GOL"]
    # both legs read one prepared ligand: the record points back at the parent's file
    from mdclaw._node import find_ancestor_artifact

    solv = create_node(str(job), "solv", parent_node_ids=[node])["node_id"]
    resolved = find_ancestor_artifact(str(job), solv, "prep", "ligand_chemistry")
    assert Path(resolved[0]["sdf_file"]).read_text() == "gol"
    assert not Path(data["artifacts"]["ligand_chemistry"][0]["sdf_file"]).is_absolute()

    orphan = create_node(str(job), "prep", parent_node_ids=[source])["node_id"]
    refused = extract_ligand(job_dir=str(job), node_id=orphan, ligand="GOL")
    assert refused["code"] == "abfe_ligand_prep_required" and "--abandon" in refused["hints"][-1]


def test_restraint_topology_hangs_under_eq_and_unrestrained_complex_refuses_fep(tmp_path):
    from mdclaw._node import create_node, resolve_node_inputs

    job, _source, prep = _job_with_complex_prep(tmp_path)
    solv = create_node(str(job), "solv", parent_node_ids=[prep])["node_id"]
    _complete(job, solv)
    topo = create_node(str(job), "topo", parent_node_ids=[solv])["node_id"]
    triple = {"system_xml": "artifacts/s.xml", "topology_pdb": "artifacts/t.pdb", "state_xml": "artifacts/st.xml",
              "hybrid_manifest": "artifacts/hybrid_manifest.json", "amber_metadata": "artifacts/amber_metadata.json"}
    files = [(rel, "{}" if rel.endswith("json") else "x") for rel in triple.values()]
    files[-1] = ("artifacts/amber_metadata.json", json.dumps({"parameters": {}, "forcefield_provenance": {}}))
    _complete(job, topo, triple, {"fep": {"kind": "abfe_decouple", "leg": "complex", "restraint_required": True}}, files)
    mn = create_node(str(job), "min", parent_node_ids=[topo])["node_id"]
    _complete(job, mn, {"state": "artifacts/min.xml"}, files=[("artifacts/min.xml", "x")])
    eq = create_node(str(job), "eq", parent_node_ids=[mn])["node_id"]
    _complete(job, eq, {"state": "artifacts/eq.xml"}, files=[("artifacts/eq.xml", "x")])

    fep = create_node(str(job), "fep", parent_node_ids=[eq])["node_id"]
    inputs = resolve_node_inputs(str(job), fep, "fep")
    assert inputs["input_resolution_code"] == "abfe_restraint_required"
    assert "add_boresch_restraint" in inputs["input_resolution_error"]

    restrained = create_node(str(job), "topo", parent_node_ids=[eq])
    assert restrained["success"], restrained                      # topo <- eq is a legal edge
    # ... and never what auto-resolution picks for an ordinary topo
    assert create_node(str(job), "topo")["node_id"] != restrained["node_id"]

    # The leg's fep nodes hang under the restrained topo and start from the eq state above it.
    restrained_files = {**triple, "fep_protocol": "artifacts/fep_protocol.json"}
    _complete(job, restrained["node_id"], restrained_files,
              {"fep": {"kind": "abfe_decouple", "leg": "complex", "restraint_required": False, "restraint": "boresch"}},
              files + [("artifacts/fep_protocol.json", "{}")])
    under_topo = create_node(str(job), "fep", parent_node_ids=[restrained["node_id"]])
    assert under_topo["success"], under_topo                      # fep <- topo is a legal edge
    inputs = resolve_node_inputs(str(job), under_topo["node_id"], "fep")
    assert "input_resolution_code" not in inputs, inputs
    assert inputs["topology_resolved_from_node_id"] == restrained["node_id"]
    assert inputs["restart_from"].endswith(f"{eq}/artifacts/eq.xml")
    # Omitting the parent still lands on the eq, where the refusal names the fix.
    auto = create_node(str(job), "fep")["node_id"]
    assert resolve_node_inputs(str(job), auto, "fep")["input_resolution_code"] == "abfe_restraint_required"
    # run_fep reports that as a structured refusal and leaves the node pending
    # (it used to die with a TypeError while building the error).
    from mdclaw._node import read_node
    from mdclaw.fep.run import run_fep

    refused = run_fep(job_dir=str(job), node_id=auto, sampling_time_ns=0.001)
    assert refused["success"] is False and refused["code"] == "abfe_restraint_required", refused
    assert read_node(str(job), auto)["status"] == "pending"

    # ... but that edge cannot be used to skip min / eq.
    solv2 = create_node(str(job), "solv", parent_node_ids=[prep])["node_id"]
    _complete(job, solv2)
    raw = create_node(str(job), "topo", parent_node_ids=[solv2])["node_id"]
    _complete(job, raw, restrained_files, {"fep": {"mutation": "A:L99A"}}, files + [("artifacts/fep_protocol.json", "{}")])
    skipping = create_node(str(job), "fep", parent_node_ids=[raw])["node_id"]
    assert resolve_node_inputs(str(job), skipping, "fep")["input_resolution_code"] == "fep_equilibration_required"


def _leg_file(tmp_path, name, leg, dg, **manifest):
    base = {"kind": "abfe_decouple", "leg": leg, "ligand": {"residue_name": "BNZ", "smiles": "c1ccccc1"},
            "forcefield": "ff19SB", "water_model": "opc", "hmr": True, "softcore_alpha": 0.5,
            "schedules": {"elec_lambdas": [1, 0], "sterics_lambdas": [1, 0]}}
    man = tmp_path / f"{name}_manifest.json"
    man.write_text(json.dumps({**base, **manifest}))
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"dG_kj_mol": dg, "dG_error_kj_mol": 0.5, "temperature_kelvin": 300.0,
                                "hybrid_manifest_file": str(man), "mutation": {"label": "decouple:BNZ"}}))
    return str(path)


def test_binding_free_energy_closes_the_cycle(tmp_path):
    from mdclaw.fep.abfe import estimate_binding_dg

    restraint = _restraint()
    complex_leg = _leg_file(tmp_path, "complex", "complex", 60.0, boresch=restraint.to_json())
    solvent_leg = _leg_file(tmp_path, "solvent", "solvent", 5.0)
    out = tmp_path / "dg.json"
    res = estimate_binding_dg(complex=complex_leg, solvent=solvent_leg, output_file=str(out))
    assert res["success"], res
    dg_r = standard_state_restraint_free_energy(restraint, 300.0)
    assert res["dG_bind_kj_mol"] == pytest.approx(5.0 - 60.0 + dg_r)
    assert res["dG_bind_error_kj_mol"] == pytest.approx(math.sqrt(0.5))
    assert res["terms_kj_mol"]["restraint_standard_state"] == pytest.approx(dg_r)
    twelve = estimate_binding_dg(complex=complex_leg, solvent=solvent_leg, ligand_symmetry_number=12, output_file=str(out))
    assert twelve["dG_bind_kj_mol"] == pytest.approx(res["dG_bind_kj_mol"] - KB_KJ_MOL_K * 300.0 * math.log(12))

    # a ligand that turned in its site has its sampled orientations paid for in
    # the restrain phase: a symmetry number on top would count them twice
    turning = _restraint(statistics={"reorients": True, "reorienting_coordinates": ["phi_b_rad"]})
    turning_leg = _leg_file(tmp_path, "turning", "complex", 60.0, boresch=turning.to_json())
    twice = estimate_binding_dg(complex=turning_leg, solvent=solvent_leg, ligand_symmetry_number=12, output_file=str(out))
    assert twice["success"] is False and twice["code"] == "abfe_symmetry_already_sampled", twice
    once = estimate_binding_dg(complex=turning_leg, solvent=solvent_leg, ligand_symmetry_number=1, output_file=str(out))
    assert once["success"] and once["ligand_reorients_in_site"] is True
    assert any("no symmetry correction" in w for w in once["warnings"])

    for kwargs, code in (
        (dict(complex=solvent_leg, solvent=complex_leg), "abfe_legs_invalid"),                      # swapped
        (dict(complex=_leg_file(tmp_path, "bare", "complex", 60.0), solvent=solvent_leg), "abfe_legs_invalid"),
        (dict(complex=complex_leg, solvent=_leg_file(tmp_path, "tip3p", "solvent", 5.0, water_model="tip3p")),
         "abfe_legs_incompatible"),
        (dict(complex=complex_leg, solvent=_leg_file(tmp_path, "mut", "solvent", 5.0, kind="hybrid")), "abfe_legs_invalid"),
    ):
        bad = estimate_binding_dg(**kwargs, output_file=str(out))
        assert bad["success"] is False and bad["code"] == code, bad


# --------------------------------------------------------------------------- #
# decoupling on a real solvated System (slow)                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
def test_decoupled_end_states(tmp_path):
    openmm = pytest.importorskip("openmm")
    from openmm import app, unit

    from mdclaw.fep.decouple import decouple_ligand, ligand_net_charge, validate_decoupling
    from mdclaw.fep.hybrid import HybridBuildError
    from tests.test_fep import TestHybridVacuum

    top, pos = TestHybridVacuum._peptide("ALA", tmp_path)
    ff = app.ForceField("amber14-all.xml", "amber14/tip3p.xml")
    modeller = app.Modeller(top, pos)
    modeller.addSolvent(ff, model="tip3p", padding=1.2 * unit.nanometer)
    system = ff.createSystem(modeller.topology, nonbondedMethod=app.PME, nonbondedCutoff=0.9 * unit.nanometer,
                             constraints=app.HBonds)
    ligand = [a.index for a in modeller.topology.atoms() if a.residue.name in ("ACE", "ALA", "NME")]
    positions = np.array(modeller.positions.value_in_unit(unit.nanometer))

    alchemical, report = decouple_ligand(system, ligand)
    assert report["n_ligand_atoms"] == len(ligand) and abs(ligand_net_charge(system, ligand)) < 1e-6
    checks = validate_decoupling(alchemical, system, positions, ligand, platform_name="CPU")
    assert checks["passed"], checks
    assert abs(checks["coupled"]["difference_kj_mol"]) < 0.05
    assert abs(checks["decoupled"]["difference_kj_mol"]) < 0.05   # the ligand was moved onto another atom
    xml = openmm.XmlSerializer.serialize(alchemical)
    assert all(f'name="{p}"' in xml for p in ("fep_elec_old", "fep_sterics_old", "fep_core"))

    for atoms, code in ((ligand[:6], "abfe_ligand_covalent"), ([], "abfe_ligand_invalid"),
                        (list(range(system.getNumParticles())), "abfe_ligand_invalid")):
        with pytest.raises(HybridBuildError) as err:
            decouple_ligand(system, atoms)
        assert err.value.code == code


def test_next_leads_through_the_solvent_leg_to_the_binding_node(tmp_path):
    """After the complex leg: start the solvent leg with extract_ligand; with
    both legs analysed: create the comparison node and run estimate_binding_dg."""
    from mdclaw._cli import _discover_tools
    from mdclaw._envelope import next_step
    from mdclaw._node import create_node
    from tests.pipeline_helpers import complete_node_with_placeholders as complete

    job, _source, prep = _job_with_complex_prep(tmp_path)
    tools = _discover_tools()

    def leg(parent_prep):
        previous = parent_prep
        for kind in ("solv", "topo", "min", "eq", "fep"):
            previous = create_node(str(job), kind, parent_node_ids=[previous])["node_id"]
            complete(str(job), previous, {})
        node = create_node(str(job), "analyze", parent_node_ids=[previous],
                           conditions={"analysis_data_scope": "alchemical"})["node_id"]
        complete(str(job), node, {"fep_result": "artifacts/fep_result.json"},
                 metadata={"analysis": "fep_mbar", "mutation": "decouple:GOL"})
        return node

    complex_leg = leg(prep)
    step = next_step(str(job), complex_leg, tools)
    assert step["action"] == "create" and step["node_type"] == "prep", step
    assert f"--parent-node-ids {prep}" in step["create_command"]
    assert step["stage_tools"][0] == "extract_ligand" and "extract_ligand --ligand GOL" in step["run_command"]

    ligand_prep = create_node(str(job), "prep", parent_node_ids=[prep])["node_id"]
    complete(str(job), ligand_prep, {"merged_pdb": "artifacts/merge/merged.pdb"}, metadata={"leg_role": "solvent"})
    solvent_leg = leg(ligand_prep)
    step = next_step(str(job), solvent_leg, tools)
    assert step["action"] == "create" and step["stage_tools"] == ["estimate_binding_dg"], step
    assert f"--parent-node-ids {complex_leg} {solvent_leg}" in step["create_command"]

    closing = create_node(str(job), "analyze", parent_node_ids=[solvent_leg, complex_leg],
                          conditions={"analysis_data_scope": "comparison"})["node_id"]
    pending = next_step(str(job), complex_leg, tools)
    assert pending["action"] == "run" and pending["node_id"] == closing
    assert pending["stage_tools"][0] == "estimate_binding_dg"
