#!/usr/bin/env python3
"""MDClaw-free weak baseline: naive pdbfixer + one default force field.

This is the **no-MDClaw floor** reference for MDPrepBench comparisons. It
imports no ``mdclaw`` code (only stdlib, OpenMM, and pdbfixer), so its
``tooling_condition`` is ``mdclaw-free``. It deliberately does the minimum:

- download/load the prompt's primary PDB,
- run pdbfixer with default settings (add missing residues/atoms/hydrogens),
- apply one hard-coded force field + water model,
- minimize briefly,
- serialize the OpenMM triple and package a ``submission/``.

It has **no** chain-selection, ligand, ion-concentration, mutation, PTM,
disulfide, terminal-capping, glycan, membrane, or NMR-model-selection logic. It
is therefore expected to pass only trivial single-chain apo tasks (e.g. P01) and
to lose graded credit on discriminating tasks (P02, P07, P08, P11, P18, P22,
P24, P25, ...). That gap is the benchmark's discrimination evidence: a credible
MD-prep workflow must beat this floor on the discriminating capabilities.

It packages via the standalone no-MDClaw packager so the whole solver path stays
MDClaw-free; only the shared scorer (run separately) uses MDClaw.

Usage:

    $MDCLAW_BENCHMARK_STAGE_WRAPPER --stage min -- \\
      python benchmarks/baselines/naive_pdbfixer_prep.py \\
        --pdb-id 2LZM \\
        --submission-dir runs/<run_id>/tasks/P01_.../submission \\
        --task-id P01_prep_simple_monomer_t4l

This is a reference runner; running it across the suite is operator-driven.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

# A single fixed, generic protein force field + water model. The baseline does
# not adapt these to the task; fidelity tasks that request a different model are
# expected to lose credit here.
_DEFAULT_PROTEIN_FF = "amber14-all.xml"
_DEFAULT_WATER_FF = "amber14/tip3pfb.xml"
_DEFAULT_WATER_MODEL = "tip3p"


def _build(pdb_id: str, work: Path) -> tuple[Path, Path, Path, Path]:
    """Run the naive pdbfixer + default-FF + minimize pipeline.

    Returns paths to (system.xml, topology.pdb, state.xml, prepared_structure.pdb).
    """
    from openmm import LangevinMiddleIntegrator, Platform, XmlSerializer, unit
    from openmm.app import PME, ForceField, HBonds, Modeller, PDBFile, Simulation
    from pdbfixer import PDBFixer

    fixer = PDBFixer(pdbid=pdb_id)
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.0)

    prepared_structure = work / "prepared_structure.pdb"
    with prepared_structure.open("w") as fh:
        PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)

    forcefield = ForceField(_DEFAULT_PROTEIN_FF, _DEFAULT_WATER_FF)
    modeller = Modeller(fixer.topology, fixer.positions)
    modeller.addSolvent(forcefield, model=_DEFAULT_WATER_MODEL,
                        padding=1.0 * unit.nanometer)

    system = forcefield.createSystem(
        modeller.topology,
        nonbondedMethod=PME,
        nonbondedCutoff=1.0 * unit.nanometer,
        constraints=HBonds,
    )
    integrator = LangevinMiddleIntegrator(
        300 * unit.kelvin, 1.0 / unit.picosecond, 0.002 * unit.picoseconds
    )
    try:
        platform = Platform.getPlatformByName("CPU")
    except Exception:  # noqa: BLE001
        platform = None
    simulation = (
        Simulation(modeller.topology, system, integrator, platform)
        if platform is not None
        else Simulation(modeller.topology, system, integrator)
    )
    simulation.context.setPositions(modeller.positions)
    simulation.minimizeEnergy(maxIterations=200)

    state = simulation.context.getState(getPositions=True, getVelocities=True)
    system_xml = work / "system.xml"
    topology_pdb = work / "topology.pdb"
    state_xml = work / "state.xml"
    system_xml.write_text(XmlSerializer.serialize(system))
    state_xml.write_text(XmlSerializer.serialize(state))
    with topology_pdb.open("w") as fh:
        PDBFile.writeFile(modeller.topology, state.getPositions(), fh, keepIds=True)
    return system_xml, topology_pdb, state_xml, prepared_structure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdb-id", required=True,
                        help="Primary PDB ID named in the public prompt.")
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="naive_pdbfixer_") as temp_dir:
        work = Path(temp_dir)
        try:
            system_xml, topology_pdb, state_xml, prepared_structure = _build(
                args.pdb_id,
                work,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"naive pdbfixer build failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

        # Package via the raw-only standalone helper to keep the solver path
        # fully MDClaw-free.
        packager = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "package_submission.py"
        )
        cmd = [
            sys.executable, str(packager),
            "--submission-dir", args.submission_dir,
            "--task-id", args.task_id,
            "--system-xml", str(system_xml),
            "--topology-pdb", str(topology_pdb),
            "--state-xml", str(state_xml),
            "--prepared-structure", str(prepared_structure),
        ]
        return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
