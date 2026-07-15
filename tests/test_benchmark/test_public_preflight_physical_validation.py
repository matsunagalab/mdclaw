"""Physical validation tests for the tool-neutral public preflight."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


pytest.importorskip("openmm")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "benchmarks" / "tools" / "validate_submission.py"


def _write_bundle(
    root: Path,
    *,
    state_x_nm: list[float],
    pdb_x_nm: list[float] | None = None,
    epsilon_kj_mol: float = 0.1,
    constant_energy_kj_mol: float | None = None,
) -> tuple[Path, Path]:
    from openmm import (
        Context,
        CustomExternalForce,
        NonbondedForce,
        Platform,
        System,
        Vec3,
        VerletIntegrator,
        XmlSerializer,
        unit,
    )
    from openmm.app import Element, PDBFile, Topology

    submission = root / "submission"
    topology_dir = submission / "topology"
    topology_dir.mkdir(parents=True)

    topology = Topology()
    chain = topology.addChain("A")
    system = System()
    nonbonded = NonbondedForce()
    for index in range(len(state_x_nm)):
        residue = topology.addResidue("ALA", chain, str(index + 1))
        topology.addAtom(f"C{index + 1}", Element.getBySymbol("C"), residue)
        system.addParticle(12.0)
        nonbonded.addParticle(0.0, 0.3, epsilon_kj_mol)
    system.addForce(nonbonded)

    if constant_energy_kj_mol is not None:
        constant_force = CustomExternalForce(str(constant_energy_kj_mol))
        constant_force.addParticle(0, [])
        system.addForce(constant_force)

    state_positions = [Vec3(x, 0.0, 0.0) for x in state_x_nm] * unit.nanometer
    integrator = VerletIntegrator(0.001 * unit.picoseconds)
    context = Context(
        system,
        integrator,
        Platform.getPlatformByName("Reference"),
    )
    context.setPositions(state_positions)
    state = context.getState(getPositions=True)

    (topology_dir / "system.xml").write_text(XmlSerializer.serialize(system))
    (topology_dir / "state.xml").write_text(XmlSerializer.serialize(state))
    pdb_positions = [
        Vec3(x, 0.0, 0.0) for x in (pdb_x_nm if pdb_x_nm is not None else state_x_nm)
    ] * unit.nanometer
    with (topology_dir / "topology.pdb").open("w") as handle:
        PDBFile.writeFile(topology, pdb_positions, handle, keepIds=True)

    contract = root / "submission_contract.json"
    contract.write_text(
        json.dumps(
            {
                "task_id": "P_test",
                "primary_score": "preparation",
                "required_outputs": [
                    "topology/system.xml",
                    "topology/topology.pdb",
                    "topology/state.xml",
                ],
            }
        )
    )
    return submission, contract


def _run_preflight(
    submission: Path,
    contract: Path,
    *extra_args: str,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        [
            sys.executable,
            str(PREFLIGHT),
            "--submission-dir",
            str(submission),
            "--submission-contract",
            str(contract),
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.stdout, completed.stderr
    return completed, json.loads(completed.stdout)


def _openmm_check(payload: dict) -> dict:
    return next(check for check in payload["checks"] if check["name"] == "openmm_bundle_loads")


def test_public_preflight_accepts_clean_bundle(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0, 1.0],
    )

    completed, payload = _run_preflight(submission, contract)

    assert completed.returncode == 0, payload
    assert payload["success"] is True
    check = _openmm_check(payload)
    assert check["passed"] is True
    assert check["particle_count"] == 2
    assert check["state_position_count"] == 2
    assert check["energy_is_finite"] is True
    assert check["energy_platform"] in {"CPU", "Reference"}
    assert check["clash_count"] == 0
    assert check["clash_examples"] == []


def test_public_preflight_scans_state_not_topology_pdb(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0, 0.05],
        pdb_x_nm=[0.0, 1.0],
        epsilon_kj_mol=1.0e-6,
    )

    completed, payload = _run_preflight(submission, contract)

    assert completed.returncode == 1
    assert payload["success"] is False
    assert payload["failure_class"] == "invalid_openmm_bundle"
    check = _openmm_check(payload)
    assert check["passed"] is False
    assert check["energy_is_finite"] is True
    assert check["abs_energy_per_particle_kj_mol"] <= check["max_abs_energy_per_particle_kj_mol"]
    assert check["clash_count"] > 0
    assert any(example.startswith("0-1 at") for example in check["clash_examples"])
    assert any("steric clash" in error for error in check["errors"])
    assert not any("physically implausible" in error for error in check["errors"])


def test_public_preflight_allows_positive_finite_energy(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0],
        epsilon_kj_mol=0.0,
        constant_energy_kj_mol=5.0,
    )

    completed, payload = _run_preflight(submission, contract)

    assert completed.returncode == 0, payload
    check = _openmm_check(payload)
    assert check["passed"] is True
    assert check["energy_is_finite"] is True
    assert check["energy_kj_mol"] == pytest.approx(5.0)
    assert check["abs_energy_per_particle_kj_mol"] == pytest.approx(5.0)
    assert check["clash_count"] == 0


def test_public_preflight_rejects_implausible_energy_per_particle(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0],
        epsilon_kj_mol=0.0,
        constant_energy_kj_mol=2.0e6,
    )

    completed, payload = _run_preflight(submission, contract)

    assert completed.returncode == 1
    assert payload["failure_class"] == "invalid_openmm_bundle"
    check = _openmm_check(payload)
    assert check["energy_is_finite"] is True
    assert check["abs_energy_per_particle_kj_mol"] == pytest.approx(2.0e6)
    assert check["clash_count"] == 0
    assert any("physically implausible" in error for error in check["errors"])


def test_skip_openmm_preserves_previous_behavior(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0, 0.05],
        pdb_x_nm=[0.0, 1.0],
    )

    completed, payload = _run_preflight(
        submission,
        contract,
        "--skip-openmm",
    )

    assert completed.returncode == 0, payload
    check = _openmm_check(payload)
    assert check == {
        "name": "openmm_bundle_loads",
        "passed": True,
        "skipped": True,
        "warnings": ["OpenMM validation skipped by --skip-openmm"],
        "errors": [],
    }


def test_public_preflight_rejects_symlinked_artifact(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0, 1.0],
    )
    state = submission / "topology" / "state.xml"
    outside = tmp_path / "private-state.xml"
    state.replace(outside)
    state.symlink_to(outside)

    completed, payload = _run_preflight(
        submission,
        contract,
        "--skip-openmm",
    )

    assert completed.returncode == 1
    assert payload["failure_class"] == "unsafe_submission_path"
    assert any("must not be a symlink" in err for err in payload["errors"])


def test_public_preflight_rejects_symlinked_submission_root(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0, 1.0],
    )
    real_submission = tmp_path / "real-submission"
    submission.rename(real_submission)
    submission.symlink_to(real_submission, target_is_directory=True)

    completed, payload = _run_preflight(
        submission,
        contract,
        "--skip-openmm",
    )

    assert completed.returncode == 1
    assert payload["failure_class"] == "unsafe_submission_path"
    assert any("submission_dir must not be a symlink" in err for err in payload["errors"])


def test_public_preflight_rejects_file_outside_raw_allowlist(tmp_path: Path):
    submission, contract = _write_bundle(
        tmp_path,
        state_x_nm=[0.0, 1.0],
    )
    (submission / "notes.json").write_text("{}\n")

    completed, payload = _run_preflight(
        submission,
        contract,
        "--skip-openmm",
    )

    assert completed.returncode == 1
    assert payload["failure_class"] == "unsafe_submission_path"
    assert any(
        "unexpected file outside public raw contract: notes.json" in err
        for err in payload["errors"]
    )


def test_public_preflight_allows_study_manifest_artifacts(tmp_path: Path):
    submission = tmp_path / "submission"
    trajectory = submission / "trajectories" / "trajectory_1.dcd"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_bytes(b"CORD\x00\x00\x00\x00")
    (submission / "manifest.json").write_text("{}\n")
    contract = tmp_path / "submission_contract.json"
    contract.write_text(json.dumps({
        "task_id": "S_test",
        "primary_score": "scientific_answer",
        "required_outputs": ["manifest.json"],
    }))

    completed, payload = _run_preflight(
        submission,
        contract,
        "--skip-openmm",
    )

    assert completed.returncode == 0, payload
    assert payload["success"] is True
