"""Read residue-template contracts from the active OpenMM force-field XML."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class ResidueTemplate:
    """Minimal residue information needed before System creation."""

    name: str
    net_charge: float
    atom_names: frozenset[str]
    external_bond_atoms: frozenset[str]


@dataclass(frozen=True)
class NucleicTemplateFamily:
    """Internal and terminal templates for one canonical nucleotide."""

    internal: str
    five_prime: str
    three_prime: str
    single: str

    @property
    def residue_names(self) -> frozenset[str]:
        return frozenset(
            {self.internal, self.five_prime, self.three_prime, self.single}
        )


@dataclass(frozen=True)
class LipidTemplateContract:
    """Whole-residue and modular Lipid21 template roles."""

    modular_names: frozenset[str]
    full_names: frozenset[str]
    external_names: frozenset[str]
    head_names: frozenset[str]
    tail_names: frozenset[str]

    @property
    def fragment_names(self) -> frozenset[str]:
        return self.head_names | self.tail_names

    @property
    def known_names(self) -> frozenset[str]:
        return self.modular_names | self.full_names


@lru_cache(maxsize=None)
def resolve_forcefield_xml_path(xml_name: str) -> Path:
    """Resolve an XML name using the same package roots as OpenMM."""
    requested = Path(str(xml_name))
    if requested.is_file():
        return requested.resolve()

    roots: list[Path] = []
    try:
        import openmmforcefields

        roots.append(Path(openmmforcefields.__file__).resolve().parent / "ffxml")
    except ImportError:
        pass
    try:
        import openmm

        roots.append(Path(openmm.__file__).resolve().parent / "app" / "data")
    except ImportError:
        pass

    for root in roots:
        candidate = root / requested
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Could not resolve force-field XML {xml_name!r} under: "
        + ", ".join(str(root) for root in roots)
    )


@lru_cache(maxsize=None)
def load_residue_templates(xml_name: str) -> dict[str, ResidueTemplate]:
    """Return residue templates declared directly by one force-field XML."""
    xml_path = resolve_forcefield_xml_path(xml_name)
    root = ET.parse(xml_path).getroot()
    templates: dict[str, ResidueTemplate] = {}
    for residue in root.findall("./Residues/Residue"):
        name = str(residue.get("name") or "").strip()
        if not name:
            continue
        atoms = residue.findall("Atom")
        templates[name] = ResidueTemplate(
            name=name,
            net_charge=sum(float(atom.get("charge", "0")) for atom in atoms),
            atom_names=frozenset(
                atom_name
                for atom in atoms
                if (atom_name := str(atom.get("name") or "").strip())
            ),
            external_bond_atoms=frozenset(
                bond_name
                for bond in residue.findall("ExternalBond")
                if (bond_name := str(bond.get("atomName") or "").strip())
            ),
        )
    return templates


@lru_cache(maxsize=None)
def load_nucleic_template_families(
    xml_name: str,
) -> dict[str, NucleicTemplateFamily]:
    """Discover complete internal/5'/3'/single nucleotide template families."""
    templates = load_residue_templates(xml_name)
    template_names = set(templates)
    families: dict[str, NucleicTemplateFamily] = {}
    for internal in templates:
        five_prime = f"{internal}5"
        three_prime = f"{internal}3"
        single = f"{internal}N"
        if {five_prime, three_prime, single} <= template_names:
            families[internal] = NucleicTemplateFamily(
                internal=internal,
                five_prime=five_prime,
                three_prime=three_prime,
                single=single,
            )
    if not families:
        raise ValueError(f"No complete nucleotide template families in {xml_name!r}")
    return families


@lru_cache(maxsize=None)
def nucleic_residue_name_map(xml_name: str) -> dict[str, NucleicTemplateFamily]:
    """Map every template name in a complete nucleotide family to that family."""
    result: dict[str, NucleicTemplateFamily] = {}
    for family in load_nucleic_template_families(xml_name).values():
        for name in family.residue_names:
            result[name] = family
    return result


@lru_cache(maxsize=None)
def load_lipid_template_contract(
    modular_xml: str,
    full_xml: str,
) -> LipidTemplateContract:
    """Classify Lipid21 templates by their declared external-bond count."""
    modular = load_residue_templates(modular_xml)
    full = load_residue_templates(full_xml)
    if not modular or not full:
        raise ValueError(
            "Lipid template XML is empty: "
            f"modular={modular_xml!r}, full={full_xml!r}"
        )
    external = {
        name for name, template in modular.items() if template.external_bond_atoms
    }
    contract = LipidTemplateContract(
        modular_names=frozenset(modular),
        full_names=frozenset(full),
        external_names=frozenset(external),
        head_names=frozenset(
            name
            for name, template in modular.items()
            if len(template.external_bond_atoms) == 2
        ),
        tail_names=frozenset(
            name
            for name, template in modular.items()
            if len(template.external_bond_atoms) == 1
        ),
    )
    if not contract.head_names or not contract.tail_names:
        raise ValueError(f"No modular lipid head/tail templates in {modular_xml!r}")
    return contract
