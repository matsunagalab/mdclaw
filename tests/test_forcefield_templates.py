import pytest

from mdclaw.forcefield_catalog import (
    DNA_XML,
    LIPID_XML,
    OPENMM_APP_LIPID_XML,
    RNA_XML,
)
from mdclaw.forcefield_templates import (
    load_lipid_template_contract,
    load_nucleic_template_families,
    load_residue_templates,
)


def test_residue_template_parser_reads_charge_atoms_and_external_bonds(tmp_path):
    xml = tmp_path / "tiny.xml"
    xml.write_text(
        """<ForceField><Residues><Residue name="X">
        <Atom name="A" charge="0.25"/><Atom name="B" charge="-1.25"/>
        <ExternalBond atomName="A"/></Residue></Residues></ForceField>"""
    )

    template = load_residue_templates(str(xml))["X"]

    assert template.net_charge == -1.0
    assert template.atom_names == {"A", "B"}
    assert template.external_bond_atoms == {"A"}


def test_specialized_template_contracts_reject_empty_xml(tmp_path):
    xml = tmp_path / "empty.xml"
    xml.write_text("<ForceField><Residues/></ForceField>")

    with pytest.raises(ValueError, match="No complete nucleotide"):
        load_nucleic_template_families(str(xml))
    with pytest.raises(ValueError, match="Lipid template XML is empty"):
        load_lipid_template_contract(str(xml), str(xml))


def test_shipped_nucleic_templates_define_complete_standard_families():
    dna = load_nucleic_template_families(DNA_XML["OL15"])
    rna = load_nucleic_template_families(RNA_XML["OL3"])

    assert set(dna) == {"DA", "DC", "DG", "DT"}
    assert set(rna) == {"A", "C", "G", "U"}
    assert "DI" not in dna
    assert "I" not in rna


def test_shipped_lipid21_contract_is_derived_from_all_templates():
    contract = load_lipid_template_contract(
        LIPID_XML["lipid21"],
        OPENMM_APP_LIPID_XML["lipid21_full"],
    )

    assert {"PGR", "PS", "PH-", "SPM"} <= contract.head_names
    assert {"AR", "DHA", "LAL", "MY", "SA", "ST"} <= contract.tail_names
    assert {"DOPC", "DOPG", "POPS", "DAPA", "SDPS"} <= contract.full_names
