"""Tests for exporting PDB coordinates from OpenMM state.xml files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_export_state_pdb_uses_state_positions(tmp_path: Path):
    pytest.importorskip("openmm")

    from openmm import Context, System, VerletIntegrator, XmlSerializer, unit
    from openmm.app import Element, PDBFile, Topology
    from mdclaw.md_simulation_server import export_state_pdb

    topology = Topology()
    chain = topology.addChain("A")
    residue = topology.addResidue("ALA", chain, id="1")
    topology.addAtom("CA", Element.getBySymbol("C"), residue, id="1")

    topology_pdb = tmp_path / "topology.pdb"
    with topology_pdb.open("w") as fh:
        PDBFile.writeFile(
            topology,
            unit.Quantity([(0.0, 0.0, 0.0)], unit.nanometer),
            fh,
            keepIds=True,
        )

    system = System()
    system.addParticle(12.0)
    integrator = VerletIntegrator(0.001)
    context = Context(system, integrator)
    context.setPositions(unit.Quantity([(0.1, 0.2, 0.3)], unit.nanometer))
    state = context.getState(getPositions=True)
    state_xml = tmp_path / "state.xml"
    state_xml.write_text(XmlSerializer.serialize(state))
    del context
    del integrator

    output_pdb = tmp_path / "minimized_structure.pdb"
    result = export_state_pdb(
        topology_pdb_file=str(topology_pdb),
        state_xml_file=str(state_xml),
        output_pdb_file=str(output_pdb),
    )

    assert result["success"], result
    assert result["used_state_xml_positions"] is True
    assert result["atom_count"] == 1
    assert result["position_count"] == 1
    text = output_pdb.read_text()
    assert "  1.000" in text
    assert "  2.000" in text
    assert "  3.000" in text


def test_run_minimization_writes_state_structure_and_report(tmp_path: Path):
    pytest.importorskip("openmm")

    from openmm import Context, System, VerletIntegrator, XmlSerializer, unit
    from openmm.app import Element, PDBFile, Topology
    from mdclaw.md_simulation_server import run_minimization

    topology = Topology()
    chain = topology.addChain("A")
    residue = topology.addResidue("ALA", chain, id="1")
    topology.addAtom("CA", Element.getBySymbol("C"), residue, id="1")

    positions = unit.Quantity([(0.1, 0.2, 0.3)], unit.nanometer)
    topology_pdb = tmp_path / "topology.pdb"
    with topology_pdb.open("w") as fh:
        PDBFile.writeFile(topology, positions, fh, keepIds=True)

    system = System()
    system.addParticle(12.0)
    system_xml = tmp_path / "system.xml"
    system_xml.write_text(XmlSerializer.serialize(system))

    integrator = VerletIntegrator(0.001)
    context = Context(system, integrator)
    context.setPositions(positions)
    state_xml = tmp_path / "state.xml"
    state_xml.write_text(XmlSerializer.serialize(context.getState(getPositions=True)))
    del context
    del integrator

    result = run_minimization(
        system_xml_file=str(system_xml),
        topology_pdb_file=str(topology_pdb),
        state_xml_file=str(state_xml),
        output_dir=str(tmp_path / "out"),
        max_iterations=1,
        restraint_atoms="CA",
        restraint_force_constant=0.0,
    )

    assert result["success"], result
    minimized_structure = Path(result["minimized_structure"])
    minimized_state = Path(result["state_file"])
    report_file = Path(result["minimization_report"])
    assert minimized_structure.is_file()
    assert minimized_state.is_file()
    assert report_file.is_file()

    report = json.loads(report_file.read_text())
    assert report["minimization"]["completed"] is True
    assert report["minimization"]["energy_is_finite"] is True
    assert report["minimization"]["atom_count_preserved"] is True
