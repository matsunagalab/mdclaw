"""Tests for the artifact-as-truth recompute checks, graded capability scoring,
attestation/verified + tooling-condition flow, and the MDClaw / no-MDClaw
packagers.

These exercise the fairness redesign: the scorer recomputes physical properties
from the submitted OpenMM artifact, grades per-capability partial credit on top
of a small physical-validity gate, and treats MDClaw-free submissions as
first-class entrants judged by the same neutral scorer.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw.benchmark import cli, run as benchmark_run, scoring
from mdclaw.benchmark.models import DeterministicCheck

REPO_ROOT = Path(__file__).resolve().parents[2]
STANDALONE_PACKAGER = REPO_ROOT / "benchmarks" / "tools" / "package_submission.py"


# ---------------------------------------------------------------------------
# OpenMM fixture builders


def _write_bundle(
    sub: Path,
    *,
    charges: list[float],
    residues: list[tuple[str, int]] | None = None,
    box_nm: float | None = None,
    with_nonbonded: bool = True,
    opc_water: bool = False,
) -> dict:
    """Build a tiny OpenMM triple under ``sub/topology`` and return a manifest.

    ``residues`` is a list of (residue_name, n_atoms); when omitted a single
    one-atom ALA residue is built. ``charges`` must match the total atom count.
    """
    from openmm import (
        Context,
        NonbondedForce,
        System,
        ThreeParticleAverageSite,
        Vec3,
        VerletIntegrator,
        XmlSerializer,
        unit,
    )
    from openmm.app import Element, PDBFile, Topology

    residues = residues or [("ALA", 1)]
    topology = Topology()
    chain = topology.addChain("A")
    positions = []
    idx = 0
    grid = 0.0
    for res_name, n_atoms in residues:
        residue = topology.addResidue(res_name, chain, str(idx + 1))
        for a in range(n_atoms):
            if opc_water and res_name.upper() in {"HOH", "WAT"} and n_atoms == 4:
                atom_name = ["O", "H1", "H2", "EPW"][a]
                element = "O" if a == 0 else ("H" if a in {1, 2} else "C")
            else:
                atom_name = f"X{a}"
                element = "C"
            topology.addAtom(atom_name, Element.getBySymbol(element), residue)
            positions.append(Vec3(grid, 0.0, 0.0))
            grid += 0.3
            idx += 1
    positions = positions * unit.nanometer

    system = System()
    for _ in range(idx):
        system.addParticle(12.0)
    if with_nonbonded:
        nb = NonbondedForce()
        for i, q in enumerate(charges):
            if opc_water and len(charges) == 4:
                sigma = 0.3166 if i == 0 else 0.1
                epsilon = 0.890 if i == 0 else 0.0
            else:
                sigma = 0.1
                epsilon = 0.0
            nb.addParticle(q, sigma, epsilon)
        system.addForce(nb)
    if opc_water and residues == [("HOH", 4)]:
        system.setParticleMass(3, 0.0)
        system.setVirtualSite(
            3, ThreeParticleAverageSite(0, 1, 2, 0.1477, 0.42615, 0.42615)
        )
    if box_nm is not None:
        system.setDefaultPeriodicBoxVectors(
            Vec3(box_nm, 0, 0) * unit.nanometer,
            Vec3(0, box_nm, 0) * unit.nanometer,
            Vec3(0, 0, box_nm) * unit.nanometer,
        )

    integrator = VerletIntegrator(1.0 * unit.femtoseconds)
    context = Context(system, integrator)
    context.setPositions(positions)
    if box_nm is not None:
        context.setPeriodicBoxVectors(
            Vec3(box_nm, 0, 0) * unit.nanometer,
            Vec3(0, box_nm, 0) * unit.nanometer,
            Vec3(0, 0, box_nm) * unit.nanometer,
        )
    state = context.getState(getPositions=True, getVelocities=True)

    topo_dir = sub / "topology"
    topo_dir.mkdir(parents=True, exist_ok=True)
    (topo_dir / "system.xml").write_text(XmlSerializer.serialize(system))
    (topo_dir / "state.xml").write_text(XmlSerializer.serialize(state))
    with (topo_dir / "topology.pdb").open("w") as fh:
        PDBFile.writeFile(topology, state.getPositions(), fh, keepIds=True)

    manifest = {
        "schema_version": "1.0",
        "task_id": "t",
        "status": "completed",
        "outputs": {
            "topology": [
                "topology/system.xml",
                "topology/topology.pdb",
                "topology/state.xml",
            ]
        },
    }
    (sub / "manifest.json").write_text(json.dumps(manifest))
    return manifest


# ---------------------------------------------------------------------------
# Recompute checks


def test_forcefield_applied_rescan_passes_for_full_nonbonded(tmp_path: Path):
    manifest = _write_bundle(tmp_path, charges=[0.0])
    check = DeterministicCheck(
        check_id="ff", check_type="forcefield_applied_rescan",
    )
    passed, score, msg = scoring._check_forcefield_applied_rescan(
        check, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg


def test_forcefield_applied_rescan_fails_without_nonbonded(tmp_path: Path):
    manifest = _write_bundle(tmp_path, charges=[0.0], with_nonbonded=False)
    check = DeterministicCheck(
        check_id="ff", check_type="forcefield_applied_rescan",
    )
    passed, score, msg = scoring._check_forcefield_applied_rescan(
        check, tmp_path, manifest,
    )
    assert not passed and score == 0.0, msg


def test_net_charge_check_accepts_neutral(tmp_path: Path):
    manifest = _write_bundle(tmp_path, charges=[1.0, -1.0],
                             residues=[("NA", 1), ("CL", 1)])
    check = DeterministicCheck(
        check_id="q", check_type="net_charge_check", require_neutral=True,
    )
    passed, score, msg = scoring._check_net_charge(check, tmp_path, manifest, {})
    assert passed and score == 1.0, msg


def test_net_charge_check_rejects_nonneutral(tmp_path: Path):
    manifest = _write_bundle(tmp_path, charges=[1.0, 0.0],
                             residues=[("NA", 1), ("ALA", 1)])
    check = DeterministicCheck(
        check_id="q", check_type="net_charge_check", require_neutral=True,
    )
    passed, score, msg = scoring._check_net_charge(check, tmp_path, manifest, {})
    assert not passed and score == 0.0, msg


def test_water_model_fingerprint_matches_three_site(tmp_path: Path):
    manifest = _write_bundle(
        tmp_path,
        charges=[0.0, 0.0, 0.0],
        residues=[("HOH", 3)],
    )
    check = DeterministicCheck(
        check_id="w", check_type="water_model_fingerprint",
        required_water_model="TIP3P", sites_per_water=3,
    )
    passed, score, msg = scoring._check_water_model_fingerprint(
        check, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg


def test_water_model_fingerprint_detects_site_count_mismatch(tmp_path: Path):
    # 3-site water submitted where a 4-site model was requested.
    manifest = _write_bundle(
        tmp_path,
        charges=[0.0, 0.0, 0.0],
        residues=[("HOH", 3)],
    )
    check = DeterministicCheck(
        check_id="w", check_type="water_model_fingerprint",
        required_water_model="OPC", sites_per_water=4,
    )
    passed, score, msg = scoring._check_water_model_fingerprint(
        check, tmp_path, manifest,
    )
    assert not passed and score == 0.0, msg


def test_water_model_fingerprint_rejects_generic_four_site_for_opc(tmp_path: Path):
    manifest = _write_bundle(
        tmp_path,
        charges=[0.0, 0.0, 0.0, 0.0],
        residues=[("HOH", 4)],
    )
    check = DeterministicCheck(
        check_id="w", check_type="water_model_fingerprint",
        required_water_model="OPC", sites_per_water=4,
    )
    passed, score, msg = scoring._check_water_model_fingerprint(
        check, tmp_path, manifest,
    )
    assert not passed and score == 0.0, msg
    assert "OPC parameter fingerprint mismatch" in msg


def test_water_model_fingerprint_matches_opc_parameters(tmp_path: Path):
    manifest = _write_bundle(
        tmp_path,
        charges=[0.0, 0.679142, 0.679142, -1.358284],
        residues=[("HOH", 4)],
        opc_water=True,
    )
    check = DeterministicCheck(
        check_id="w", check_type="water_model_fingerprint",
        required_water_model="OPC", sites_per_water=4,
    )
    passed, score, msg = scoring._check_water_model_fingerprint(
        check, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg
    assert "OPC-like fingerprint" in msg


def test_ion_concentration_recompute_from_box_volume(tmp_path: Path):
    # 2 K+ / 2 Cl- ion pairs in a (4 nm)^3 box -> ~0.052 M; use a wide tol.
    residues = [("K", 1), ("K", 1), ("CL", 1), ("CL", 1)]
    manifest = _write_bundle(
        tmp_path, charges=[1.0, 1.0, -1.0, -1.0], residues=residues, box_nm=4.0,
    )
    check = DeterministicCheck(
        check_id="ion", check_type="ion_concentration_recompute",
        cation_residue_names=["K"], anion_residue_names=["CL"],
        target_molar=0.052, molar_tolerance=0.02, min_ion_count=2,
    )
    passed, score, msg = scoring._check_ion_concentration_recompute(
        check, tmp_path, manifest, {},
    )
    assert passed and score == 1.0, msg


def _pdb_atom_line(
    serial: int,
    atom: str,
    resname: str,
    chain: str,
    resseq: int,
    x: float,
    y: float = 0.0,
    z: float = 0.0,
) -> str:
    element = "".join(ch for ch in atom if ch.isalpha())[:1] or "C"
    return (
        f"ATOM  {serial:5d} {atom:<4} {resname:>4} {chain:1}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           {element:>2}\n"
    )


def _write_structure_submission(sub: Path, pdb_text: str) -> dict:
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "prepared_structure.pdb").write_text(pdb_text)
    manifest = {
        "schema_version": "1.0",
        "task_id": "t",
        "status": "completed",
        "outputs": {"prepared_structure": "prepared_structure.pdb"},
    }
    (sub / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_disulfide_bond_rescan_counts_sg_pairs(tmp_path: Path):
    pdb_text = "".join([
        _pdb_atom_line(1, "SG", "CYS", "A", 1, 0.0),
        _pdb_atom_line(2, "SG", "CYS", "A", 2, 2.0),
        _pdb_atom_line(3, "SG", "CYS", "A", 3, 8.0),
        _pdb_atom_line(4, "SG", "CYS", "A", 4, 10.0),
        "END\n",
    ])
    manifest = _write_structure_submission(tmp_path, pdb_text)
    check = DeterministicCheck(
        check_id="ss", check_type="disulfide_bond_rescan",
        min_disulfide_count=2,
    )
    passed, score, msg = scoring._check_disulfide_bond_rescan(
        check, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg


def test_nucleic_content_rescan_checks_type_and_chain_count(tmp_path: Path):
    pdb_text = "".join([
        _pdb_atom_line(1, "P", "DA", "A", 1, 0.0),
        _pdb_atom_line(2, "P", "DC", "A", 2, 1.0),
        _pdb_atom_line(3, "P", "DG", "B", 1, 2.0),
        _pdb_atom_line(4, "P", "DT", "B", 2, 3.0),
        "END\n",
    ])
    manifest = _write_structure_submission(tmp_path, pdb_text)
    check = DeterministicCheck(
        check_id="dna", check_type="nucleic_content_rescan",
        required_nucleic_acid_type="DNA",
        exact_nucleic_chain_count=2,
        min_nucleic_residue_count=4,
    )
    passed, score, msg = scoring._check_nucleic_content_rescan(
        check, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg


def test_residue_ratio_rescan_uses_aliases_and_atom_floor(tmp_path: Path):
    lines = []
    serial = 1
    residues = [("OPC", 1), ("OPC", 2), ("OPE", 3), ("HL1", 4)]
    for resname, resseq in residues:
        for atom_i in range(20):
            lines.append(
                _pdb_atom_line(serial, f"C{atom_i}", resname, "A", resseq, float(serial))
            )
            serial += 1
    manifest = _write_structure_submission(tmp_path, "".join(lines) + "END\n")
    check = DeterministicCheck(
        check_id="ratio", check_type="residue_ratio_rescan",
        required_residue_ratio={"POPC": 2, "POPE": 1, "CHL1": 1},
        residue_aliases={
            "POPC": ["OPC"],
            "POPE": ["OPE"],
            "CHL1": ["HL1"],
        },
        min_residue_atom_count=20,
    )
    passed, score, msg = scoring._check_residue_ratio_rescan(
        check, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg


def test_solvent_regime_rescan_detects_explicit_and_membrane(tmp_path: Path):
    manifest = _write_bundle(
        tmp_path,
        charges=[0.0, 0.0, 0.0, 0.0],
        residues=[("HOH", 3), ("CHL", 1)],
    )
    explicit = DeterministicCheck(
        check_id="explicit", check_type="solvent_regime_rescan",
        required_solvent_regime="explicit",
    )
    membrane = DeterministicCheck(
        check_id="membrane", check_type="solvent_regime_rescan",
        required_solvent_regime="membrane",
        lipid_residue_names=["CHL"],
    )
    passed, score, msg = scoring._check_solvent_regime_rescan(
        explicit, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg
    passed, score, msg = scoring._check_solvent_regime_rescan(
        membrane, tmp_path, manifest,
    )
    assert passed and score == 1.0, msg


# ---------------------------------------------------------------------------
# Artifact-as-truth backend detection


def test_mislabeled_backend_still_loads_and_warns(tmp_path: Path):
    manifest = _write_bundle(tmp_path, charges=[0.0])
    # Declare a non-OpenMM backend even though the bundle is OpenMM.
    metrics = {"topology": {"backend": "gromacs"}}
    warnings = scoring._backend_label_mismatch_warnings(
        tmp_path, manifest, metrics,
    )
    assert any("deserializes as OpenMM" in w for w in warnings)
    # The check itself still runs on the artifact regardless of the label.
    check = DeterministicCheck(
        check_id="load", check_type="openmm_system_load",
    )
    passed, score, _ = scoring._check_openmm_system_load(
        check, tmp_path, manifest, metrics,
    )
    assert passed and score == 1.0


# ---------------------------------------------------------------------------
# Graded capability scoring


def _full_prep_submission(sub: Path, *, charges: list[float],
                          residues=None, box_nm=None) -> None:
    """A complete prep submission: bundle + minimized/prepared + reports."""
    manifest = _write_bundle(sub, charges=charges, residues=residues,
                             box_nm=box_nm)
    manifest["outputs"].update({
        "metrics": "metrics.json",
        "provenance": "provenance.json",
        "prepared_structure": "prepared_structure.pdb",
        "minimized_structure": "minimized_structure.pdb",
        "minimization_report": "minimization_report.json",
    })
    (sub / "manifest.json").write_text(json.dumps(manifest))
    # Reuse the topology pdb as both prepared and minimized structure.
    pdb_text = (sub / "topology" / "topology.pdb").read_text()
    (sub / "prepared_structure.pdb").write_text(pdb_text)
    (sub / "minimized_structure.pdb").write_text(pdb_text)
    (sub / "minimization_report.json").write_text(json.dumps({
        "minimization": {
            "attempted": True, "completed": True,
            "energy_is_finite": True, "positions_are_finite": True,
            "atom_count_preserved": True,
            "energy_initial_kj_mol": 0.0, "energy_final_kj_mol": 0.0,
        }
    }))
    (sub / "metrics.json").write_text(json.dumps(
        {"topology": {"backend": "openmm"}}
    ))
    (sub / "provenance.json").write_text(json.dumps({
        "command_log": [
            {"stage": "source", "command": "src", "exit_code": 0},
            {"stage": "prep", "command": "prep", "exit_code": 0},
            {"stage": "topo", "command": "topo", "exit_code": 0},
            {"stage": "min", "command": "min", "exit_code": 0},
        ]
    }))


def _graded_task(det_checks):
    from mdclaw.benchmark.models import Task, TaskScoring

    return Task(
        schema_version="1.0", task_id="t", category="system_preparation",
        primary_score="preparation", execution_mode="lite",
        time_limit_minutes=30,
        required_outputs=[
            "manifest.json", "metrics.json", "provenance.json",
            "prepared_structure.pdb", "minimized_structure.pdb",
            "minimization_report.json",
        ],
        scoring=TaskScoring(
            deterministic_checks=det_checks, integrity_checks=[],
            integrity_policy="warn",
        ),
        task_intent="x",
    )


def test_identity_failure_yields_partial_not_zero(tmp_path: Path):
    sub = tmp_path / "submission"
    _full_prep_submission(sub, charges=[0.0])
    task = _graded_task([
        DeterministicCheck(check_id="load", check_type="openmm_system_load",
                           weight=1.0),
        DeterministicCheck(check_id="ff", check_type="forcefield_applied_rescan",
                           weight=1.0),
        # An identity check that cannot pass (missing residue).
        DeterministicCheck(
            check_id="ident", check_type="structure_component_rescan",
            weight=1.0, structure_path="prepared_structure.pdb",
            structure_manifest_path="outputs.prepared_structure",
            min_residue_counts={"TRP": 5},
        ),
    ])
    score = scoring.score_submission(task, sub, run_id="r")
    assert 0.0 < score.weighted_total < 1.0
    assert score.capability_scores.get("physical_validity") == 1.0
    assert (score.capability_scores.get("identity") or 0.0) < 1.0


def test_gate_failure_zeros_the_task(tmp_path: Path):
    sub = tmp_path / "submission"
    # Build a bundle with no force field so the physical-validity gate fails.
    _write_bundle(sub, charges=[0.0], with_nonbonded=False)
    pdb_text = (sub / "topology" / "topology.pdb").read_text()
    (sub / "prepared_structure.pdb").write_text(pdb_text)
    (sub / "minimized_structure.pdb").write_text(pdb_text)
    (sub / "minimization_report.json").write_text(json.dumps({
        "minimization": {
            "attempted": True, "completed": True,
            "energy_is_finite": True, "positions_are_finite": True,
            "atom_count_preserved": True,
            "energy_initial_kj_mol": 0.0, "energy_final_kj_mol": 0.0,
        }
    }))
    (sub / "metrics.json").write_text(json.dumps({"topology": {"backend": "openmm"}}))
    (sub / "provenance.json").write_text(json.dumps({
        "command_log": [
            {"stage": "source", "command": "src", "exit_code": 0},
            {"stage": "prep", "command": "prep", "exit_code": 0},
            {"stage": "topo", "command": "topo", "exit_code": 0},
            {"stage": "min", "command": "min", "exit_code": 0},
        ]
    }))
    manifest = json.loads((sub / "manifest.json").read_text())
    manifest["outputs"].update({
        "metrics": "metrics.json", "provenance": "provenance.json",
        "prepared_structure": "prepared_structure.pdb",
        "minimized_structure": "minimized_structure.pdb",
        "minimization_report": "minimization_report.json",
    })
    (sub / "manifest.json").write_text(json.dumps(manifest))
    task = _graded_task([
        DeterministicCheck(check_id="load", check_type="openmm_system_load",
                           weight=1.0),
        DeterministicCheck(check_id="ff", check_type="forcefield_applied_rescan",
                           weight=1.0),
    ])
    score = scoring.score_submission(task, sub, run_id="r")
    assert score.status == "failed"
    assert score.weighted_total == 0.0


# ---------------------------------------------------------------------------
# Attestation + tooling condition flow


def test_attestation_verified_and_condition_flow(tmp_path: Path):
    out = tmp_path / "runs"
    prep = benchmark_run.prepare_benchmark_run(
        output_dir=str(out), run_id="verif",
        task_ids=["P01_prep_simple_monomer_t4l"],
    )
    assert prep["success"]
    att = json.loads((Path(prep["run_dir"]) / "attestation.json").read_text())
    assert att["tooling_condition"] == "unknown"
    assert len(att["public_package_sha256"]) == 64

    summary = benchmark_run.summarize_benchmark_run(run_dir=prep["run_dir"])
    s = summary["summary"]
    assert s["verified"] is True
    assert s["tooling_condition"] == "unknown"
    assert set(s["capability_scores"]) == {
        "identity", "physical_validity", "fidelity", "provenance",
    }


def test_mdclaw_free_condition_recorded(tmp_path: Path):
    out = tmp_path / "runs"
    init = benchmark_run.init_benchmark_run(
        output_dir=str(out), run_id="free",
        tooling_condition="mdclaw-free", harness_name="mdcrow",
        backend_name="mdcrow-openmm",
        task_ids=["P01_prep_simple_monomer_t4l"],
    )
    assert init["success"]
    att = json.loads((Path(init["run_dir"]) / "attestation.json").read_text())
    assert att["tooling_condition"] == "mdclaw-free"


def test_run_without_attestation_is_unverified(tmp_path: Path):
    out = tmp_path / "runs"
    init = benchmark_run.init_benchmark_run(
        output_dir=str(out), run_id="noatt",
        task_ids=["P01_prep_simple_monomer_t4l"],
    )
    # Remove the attestation to simulate an externally assembled run.
    (Path(init["run_dir"]) / "attestation.json").unlink()
    summary = benchmark_run.summarize_benchmark_run(run_dir=init["run_dir"])
    assert summary["summary"]["verified"] is False


# ---------------------------------------------------------------------------
# Packagers


def _make_external_triple(tmp: Path) -> tuple[Path, Path, Path]:
    from openmm import (
        Context,
        NonbondedForce,
        System,
        Vec3,
        VerletIntegrator,
        XmlSerializer,
        unit,
    )
    from openmm.app import Element, PDBFile, Topology

    top = Topology()
    chain = top.addChain("A")
    res = top.addResidue("ALA", chain, "1")
    top.addAtom("CA", Element.getBySymbol("C"), res)
    top.addAtom("CB", Element.getBySymbol("C"), res)
    pos = [Vec3(0, 0, 0), Vec3(0.15, 0, 0)] * unit.nanometer
    sysm = System()
    sysm.addParticle(12.0)
    sysm.addParticle(12.0)
    nb = NonbondedForce()
    nb.addParticle(0.0, 0.1, 0.0)
    nb.addParticle(0.0, 0.1, 0.0)
    sysm.addForce(nb)
    integ = VerletIntegrator(0.001 * unit.picoseconds)
    ctx = Context(sysm, integ)
    ctx.setPositions(pos)
    state = ctx.getState(getPositions=True, getVelocities=True)
    sx = tmp / "system.xml"
    tp = tmp / "topology.pdb"
    st = tmp / "state.xml"
    sx.write_text(XmlSerializer.serialize(sysm))
    st.write_text(XmlSerializer.serialize(state))
    with tp.open("w") as fh:
        PDBFile.writeFile(top, state.getPositions(), fh)
    return sx, tp, st


def test_package_openmm_submission_builds_raw_bundle(tmp_path: Path):
    sx, tp, st = _make_external_triple(tmp_path)
    parent = tmp_path / "wt_prepared_structure.pdb"
    parent.write_text(tp.read_text())
    sub = tmp_path / "submission"

    result = cli.package_openmm_submission(
        submission_dir=str(sub),
        task_id="P08_demo",
        system_xml_file=str(sx),
        topology_pdb_file=str(tp),
        state_xml_file=str(st),
        prepared_structure_file=str(tp),
        extra_output_files=[f"wt_prepared_structure.pdb={parent}"],
    )

    assert result["success"], result
    assert (sub / "topology" / "system.xml").read_text() == sx.read_text()
    assert (sub / "topology" / "state.xml").read_text() == st.read_text()
    assert (sub / "wt_prepared_structure.pdb").read_text() == parent.read_text()
    assert not (sub / "manifest.json").exists()
    assert not (sub / "metrics.json").exists()
    assert not (sub / "provenance.json").exists()


def test_standalone_packager_writes_same_raw_contract(tmp_path: Path):
    sx, tp, st = _make_external_triple(tmp_path)
    parent = tmp_path / "wt_prepared_structure.pdb"
    parent.write_text(tp.read_text())
    sub = tmp_path / "submission"
    result = subprocess.run(
        [
            sys.executable,
            str(STANDALONE_PACKAGER),
            "--submission-dir",
            str(sub),
            "--task-id",
            "P08_demo",
            "--system-xml",
            str(sx),
            "--topology-pdb",
            str(tp),
            "--state-xml",
            str(st),
            "--prepared-structure",
            str(tp),
            "--extra-output",
            f"wt_prepared_structure.pdb={parent}",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (sub / "topology" / "system.xml").is_file()
    assert (sub / "topology" / "topology.pdb").is_file()
    assert (sub / "topology" / "state.xml").is_file()
    assert (sub / "prepared_structure.pdb").is_file()
    assert (sub / "wt_prepared_structure.pdb").is_file()
    assert not (sub / "manifest.json").exists()
    text = STANDALONE_PACKAGER.read_text()
    assert "import mdclaw" not in text
    assert "from mdclaw" not in text


@pytest.mark.parametrize(
    "missing",
    ["system_xml_file", "topology_pdb_file", "state_xml_file"],
)
def test_package_openmm_submission_reports_missing_inputs(
    tmp_path: Path,
    missing: str,
):
    sx, tp, st = _make_external_triple(tmp_path)
    kwargs = {
        "system_xml_file": str(sx),
        "topology_pdb_file": str(tp),
        "state_xml_file": str(st),
        "prepared_structure_file": str(tp),
    }
    kwargs[missing] = str(tmp_path / "does_not_exist")
    result = cli.package_openmm_submission(
        submission_dir=str(tmp_path / "submission"),
        task_id="P01_demo",
        **kwargs,
    )

    assert not result["success"]
    assert result["errors"]
