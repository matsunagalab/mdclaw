"""Tests for exporting PDB coordinates from OpenMM state.xml files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_export_state_pdb_uses_state_positions(tmp_path: Path):
    pytest.importorskip("openmm")

    from openmm import Context, System, VerletIntegrator, XmlSerializer, unit
    from openmm.app import Element, PDBFile, Topology
    from mdclaw.simulation.platform import export_state_pdb

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
    from mdclaw.simulation.minimize import run_minimization

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


def test_the_shared_exporter_keeps_the_topology_s_ids(tmp_path):
    """min / eq / prod must not renumber what topology.pdb numbered.

    Without keepIds, PDBFile numbers every residue 1..N inside its chain and
    labels chains A, B, C... by index, so an export disagreed with the topology
    it was rendered from -- 5ZK8's protein is 18-458 there and came out 1-273 --
    and a system with more than 26 chains, which a solvated membrane easily has,
    reused chain letters.
    """
    from openmm.app import PDBFile

    from mdclaw.structure.pdb_utils import render_simulation_pdb_preserving_resnames

    source = tmp_path / "topology.pdb"
    source.write_text(
        "ATOM      1  N   ALA X  18      0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA X  18      1.450   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA X  18      2.900   0.000   0.000  1.00  0.00           C\n"
        "ATOM      4  N   GLY X 383      4.230   0.000   0.000  1.00  0.00           N\n"
        "ATOM      5  CA  GLY X 383      5.680   0.000   0.000  1.00  0.00           C\n"
        "ATOM      6  C   GLY X 383      7.130   0.000   0.000  1.00  0.00           C\n"
        "END\n")
    pdb = PDBFile(str(source))
    text = render_simulation_pdb_preserving_resnames(
        pdb.topology, pdb.positions, str(source))
    rendered = [(line[17:20].strip(), line[22:26].strip(), line[21])
                for line in text.splitlines() if line.startswith("ATOM")]
    assert [r[1] for r in rendered] == ["18"] * 3 + ["383"] * 3
    assert {r[2] for r in rendered} == {"X"}
    assert [r[0] for r in rendered] == ["ALA"] * 3 + ["GLY"] * 3


def test_export_state_pdb_keeps_the_names_the_reader_normalised(tmp_path):
    """The standalone export must not diverge from min / eq / prod.

    Written through PDBFile alone it lost exactly what that reader normalises on
    the way in -- measured on a real topology, HIE 17 came out HIS 17 and WAT
    34401 came out HOH 34401. This tool is documented as the way to produce a
    benchmark submission, where composition is compared residue by residue.
    """
    from openmm import Context, System, VerletIntegrator, XmlSerializer, unit
    from openmm.app import PDBFile

    from mdclaw.simulation.platform import export_state_pdb

    source = tmp_path / "topology.pdb"
    source.write_text(
        "ATOM      1  N   HIE X  17      0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  HIE X  17      1.450   0.900   0.000  1.00  0.00           C\n"
        "ATOM      3  C   HIE X  17      2.900   0.000   0.700  1.00  0.00           C\n"
        "ATOM      4  SG  CYX X  18      4.300   0.900   0.000  1.00  0.00           S\n"
        "END\n")
    pdb = PDBFile(str(source))
    system = System()
    for _ in range(pdb.topology.getNumAtoms()):
        system.addParticle(12.0 * unit.amu)
    context = Context(system, VerletIntegrator(1.0 * unit.femtosecond))
    context.setPositions(pdb.positions)
    state_file = tmp_path / "state.xml"
    state_file.write_text(
        XmlSerializer.serialize(context.getState(getPositions=True)))

    out = tmp_path / "exported.pdb"
    result = export_state_pdb(str(source), str(state_file), str(out))

    assert result["success"], result.get("errors")
    names = [line[17:20].strip() for line in out.read_text().splitlines()
             if line.startswith("ATOM")]
    assert names == ["HIE", "HIE", "HIE", "CYX"]
    numbers = {line[22:26].strip() for line in out.read_text().splitlines()
               if line.startswith("ATOM")}
    assert numbers == {"17", "18"}
