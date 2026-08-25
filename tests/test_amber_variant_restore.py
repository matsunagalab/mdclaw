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
        _Topology([asp]), {("A", "97", ""): _record("ASH", "ASP")}
    )
    assert asp.name == "ASH"
    assert report["restored_count"] == report["expected_count"] == 1


def test_leaves_a_lipid_sharing_the_chain_and_number_alone():
    asp = _Residue("ASP", "A", "97")
    lipid = _Residue("PA", "A", "97")

    _restore_amber_variant_names(
        _Topology([asp, lipid]), {("A", "97", ""): _record("ASH", "ASP")}
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
        _Topology(residues), {("A", "112", ""): _record("ASH", "ASP")}
    )

    assert [r.name for r in residues] == ["ASH", "HOH", "NA"]


def test_does_not_cross_chains():
    other_chain = _Residue("ASP", "B", "97")
    report = _restore_amber_variant_names(
        _Topology([other_chain]), {("A", "97", ""): _record("ASH", "ASP")}
    )
    assert other_chain.name == "ASP"
    assert report["records"][0]["candidate_count"] == 0


@pytest.mark.parametrize(
    "variant,base", [("ASH", "ASP"), ("GLH", "GLU"), ("LYN", "LYS"), ("CYM", "CYS")]
)
def test_every_variant_round_trips(variant, base):
    residue = _Residue(base, "A", "10")
    _restore_amber_variant_names(
        _Topology([residue]), {("A", "10", ""): _record(variant, base)}
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
        {("A", "97", ""): _record("ASH", "ASP")},
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


# --- a disulfide cysteine is CYX and must come back as one --------------------
# OpenMM's PDB reader renames CYX to CYS on load, and the sanitizer's restore
# table held only the four titratable variants, so nothing put it back. Measured
# on a real membrane build before this: system.prepared.pdb carried CYX 4 and
# system.topology.pdb carried CYX 0, CYS 10. The System was never wrong -- its
# two S-S bonds were there either way -- but every artifact read from that file
# reported the cysteines as free, and a re-prep from it would let pdb2pqr put HG
# back on all four.

def test_cyx_is_restored_but_is_not_a_protonation_state():
    from mdclaw.chemistry_constants import (
        AMBER_NONDEFAULT_PROTONATION_VARIANT_BASES,
        AMBER_RESTORED_VARIANT_BASES,
    )
    assert AMBER_RESTORED_VARIANT_BASES["CYX"] == "CYS"
    assert "CYX" not in AMBER_NONDEFAULT_PROTONATION_VARIANT_BASES, (
        "a disulfide is the disulfide contract's decision, not a titration one")
    for variant in ("ASH", "GLH", "LYN", "CYM"):
        assert variant in AMBER_RESTORED_VARIANT_BASES


def test_the_protonation_extractor_leaves_structural_cysteines_alone(tmp_path):
    """CYX and metal-site CYM belong to structural chemistry, not titration."""
    from mdclaw.structure.protonation import (
        _extract_input_protonation_state_overrides,
    )
    pdb = tmp_path / "cyx.pdb"
    pdb.write_text(
        "ATOM      1  SG  CYX C 300       0.000   0.000   0.000  1.00  0.00           S\n"
        "ATOM      2  SG  CYM C 301       4.000   0.000   0.000  1.00  0.00           S\n"
        "TER\nEND\n")
    states = _extract_input_protonation_state_overrides(pdb)
    assert states == []


# --- 52 and 52A are two residues ---------------------------------------------
# The substitute-and-restore key carried chain and residue number and not the
# insertion code, so a second variant at the same number overwrote the first
# record. While only the four titratable variants went through it that took two
# of them at one number; adding CYX made it reachable with one of each, and then
# an ASH at 52 followed by a CYX at 52A left a single record saying CYX,
# restored it, and reported expected 1 / restored 1 / passed while the ASH came
# out ASP with nothing to say so.

class _FakeChain:
    def __init__(self, cid):
        self.id = cid


class _FakeResidue:
    def __init__(self, name, chain, rid, insertion_code=""):
        self.name, self.chain, self.id = name, chain, rid
        self.insertionCode = insertion_code


class _FakeTopology:
    def __init__(self, residues):
        self._residues = residues

    def residues(self):
        return iter(self._residues)


def test_two_variants_at_one_number_are_told_apart_by_the_insertion_code():
    from mdclaw.amber.openmm_build import _restore_amber_variant_names
    chain = _FakeChain("A")
    residues = [_FakeResidue("ASP", chain, "52", ""),
                _FakeResidue("CYS", chain, "52", "A")]
    report = _restore_amber_variant_names(_FakeTopology(residues), {
        ("A", "52", ""): {"variant": "ASH", "base_name": "ASP"},
        ("A", "52", "A"): {"variant": "CYX", "base_name": "CYS"},
    })
    assert report["expected_count"] == 2 and report["restored_count"] == 2
    assert [r.name for r in residues] == ["ASH", "CYX"]


def test_a_variant_the_topology_does_not_carry_is_reported_not_guessed():
    from mdclaw.amber.openmm_build import _restore_amber_variant_names
    chain = _FakeChain("A")
    residues = [_FakeResidue("ASP", chain, "52", "")]
    report = _restore_amber_variant_names(_FakeTopology(residues), {
        ("A", "52", ""): {"variant": "ASH", "base_name": "ASP"},
        ("A", "99", ""): {"variant": "CYX", "base_name": "CYS"},
    })
    assert report["expected_count"] == 2 and report["restored_count"] == 1


def test_cyx_has_an_atom_contract():
    """Without one, a CYX carrying HG validated as passed."""
    from mdclaw.amber.topology_validation import _AMBER_VARIANT_ATOM_CONTRACTS
    assert _AMBER_VARIANT_ATOM_CONTRACTS["CYX"]["forbidden"] == {"HG"}
    assert not _AMBER_VARIANT_ATOM_CONTRACTS["CYX"]["required"]
