"""The Amber protonation-variant restore must not rename unrelated residues.

``build_amber_system`` rewrites ASH/GLH/LYN/CYM to their CCD names so OpenFF
Pablo can identify them, then restores the Amber names on the loaded topology.
The restore is keyed on ``(chain, residue number)``, which is *not* unique in an
assembled system: lipids, ions and waters restart numbering, so a POPC tail can
sit at the same chain and number as a protein aspartate. Renaming that lipid to
ASH produced "No template found for residue N (ASH). The set of atoms matches
PA" from the force field, which was exactly right.
"""

import pytest

from mdclaw.amber.openmm_build import _restore_amber_variant_names
from mdclaw.amber.topology_validation import _validate_final_protonation_variants


class _Chain:
    def __init__(self, chain_id):
        self.id = chain_id


class _Atom:
    def __init__(self, name):
        self.name = name


class _Residue:
    def __init__(self, name, chain_id, res_id, atoms=()):
        self.name = name
        self.chain = _Chain(chain_id)
        self.id = res_id
        self._atoms = [_Atom(name) for name in atoms]

    def atoms(self):
        return iter(self._atoms)


class _Topology:
    def __init__(self, residues):
        self._residues = residues

    def residues(self):
        return iter(self._residues)


def _record(variant, base):
    return {"variant": variant, "base_name": base}


def test_restores_the_residue_it_renamed():
    asp = _Residue("ASP", "A", "97")
    report = _restore_amber_variant_names(
        _Topology([asp]), {("A", "97"): _record("ASH", "ASP")}
    )
    assert asp.name == "ASH"
    assert report["restored_count"] == report["expected_count"] == 1


def test_leaves_a_lipid_sharing_the_chain_and_number_alone():
    asp = _Residue("ASP", "A", "97")
    lipid = _Residue("PA", "A", "97")

    _restore_amber_variant_names(
        _Topology([asp, lipid]), {("A", "97"): _record("ASH", "ASP")}
    )

    assert asp.name == "ASH"
    assert lipid.name == "PA", "a lipid at the same key must not become ASH"


def test_leaves_water_and_ions_sharing_the_number_alone():
    residues = [
        _Residue("ASP", "A", "112"),
        _Residue("HOH", "A", "112"),
        _Residue("NA", "A", "112"),
    ]

    _restore_amber_variant_names(
        _Topology(residues), {("A", "112"): _record("ASH", "ASP")}
    )

    assert [r.name for r in residues] == ["ASH", "HOH", "NA"]


def test_does_not_cross_chains():
    other_chain = _Residue("ASP", "B", "97")
    report = _restore_amber_variant_names(
        _Topology([other_chain]), {("A", "97"): _record("ASH", "ASP")}
    )
    assert other_chain.name == "ASP"
    assert report["records"][0]["candidate_count"] == 0


@pytest.mark.parametrize(
    "variant,base", [("ASH", "ASP"), ("GLH", "GLU"), ("LYN", "LYS"), ("CYM", "CYS")]
)
def test_every_variant_round_trips(variant, base):
    residue = _Residue(base, "A", "10")
    _restore_amber_variant_names(
        _Topology([residue]), {("A", "10"): _record(variant, base)}
    )
    assert residue.name == variant


def test_no_records_is_a_no_op():
    residue = _Residue("ASP", "A", "97")
    report = _restore_amber_variant_names(_Topology([residue]), {})
    assert residue.name == "ASP"
    assert report == {"expected_count": 0, "restored_count": 0, "records": []}


def test_duplicate_same_key_base_candidates_are_not_renamed():
    residues = [
        _Residue("ASP", "A", "97"),
        _Residue("ASP", "A", "97"),
    ]

    report = _restore_amber_variant_names(
        _Topology(residues),
        {("A", "97"): _record("ASH", "ASP")},
    )

    assert [residue.name for residue in residues] == ["ASP", "ASP"]
    assert report["restored_count"] == 0
    assert report["records"][0]["candidate_count"] == 2


@pytest.mark.parametrize(
    "variant,atoms",
    [
        ("ASH", {"HD2"}),
        ("GLH", {"HE2"}),
        ("LYN", {"HZ1", "HZ2"}),
        ("CYM", {"SG"}),
    ],
)
def test_final_variant_validation_checks_unique_site_and_atom_contract(
    variant,
    atoms,
):
    residue = _Residue(variant, "A", "10", atoms)
    restore_report = {
        "expected_count": 1,
        "restored_count": 1,
        "records": [{
            "chain": "A",
            "resnum": "10",
            "variant": variant,
            "base_name": {
                "ASH": "ASP",
                "GLH": "GLU",
                "LYN": "LYS",
                "CYM": "CYS",
            }[variant],
            "candidate_count": 1,
            "restored": True,
        }],
    }

    report = _validate_final_protonation_variants(
        topology=_Topology([residue]),
        restore_report=restore_report,
    )

    assert report["status"] == "passed"
    assert report["validated_count"] == 1


def test_final_variant_validation_fails_restore_count_mismatch():
    report = _validate_final_protonation_variants(
        topology=_Topology([]),
        restore_report={
            "expected_count": 1,
            "restored_count": 0,
            "records": [{
                "chain": "A",
                "resnum": "97",
                "variant": "ASH",
                "base_name": "ASP",
                "candidate_count": 0,
                "restored": False,
            }],
        },
    )

    assert report["status"] == "failed"
    assert report["expected_count"] == 1
    assert report["restored_count"] == 0


def test_final_variant_validation_rejects_wrong_hydrogen_identity():
    residue = _Residue("LYN", "A", "10", {"HZ1", "HZ2", "HZ3"})
    report = _validate_final_protonation_variants(
        topology=_Topology([residue]),
        restore_report={
            "expected_count": 1,
            "restored_count": 1,
            "records": [{
                "chain": "A",
                "resnum": "10",
                "variant": "LYN",
                "base_name": "LYS",
                "candidate_count": 1,
                "restored": True,
            }],
        },
    )

    assert report["status"] == "failed"
    assert report["records"][0]["forbidden_atoms_present"] == ["HZ3"]
