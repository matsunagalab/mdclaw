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


class _Chain:
    def __init__(self, chain_id):
        self.id = chain_id


class _Residue:
    def __init__(self, name, chain_id, res_id):
        self.name = name
        self.chain = _Chain(chain_id)
        self.id = res_id


class _Topology:
    def __init__(self, residues):
        self._residues = residues

    def residues(self):
        return iter(self._residues)


def _record(variant, base, chain):
    return {"variant": variant, "base_name": base, "chain": chain}


def test_restores_the_residue_it_renamed():
    asp = _Residue("ASP", "A", "97")
    _restore_amber_variant_names(
        _Topology([asp]), {("A", "97"): _record("ASH", "ASP", "A")}
    )
    assert asp.name == "ASH"


def test_leaves_a_lipid_sharing_the_chain_and_number_alone():
    asp = _Residue("ASP", "A", "97")
    lipid = _Residue("PA", "A", "97")

    _restore_amber_variant_names(
        _Topology([asp, lipid]), {("A", "97"): _record("ASH", "ASP", "A")}
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
        _Topology(residues), {("A", "112"): _record("ASH", "ASP", "A")}
    )

    assert [r.name for r in residues] == ["ASH", "HOH", "NA"]


def test_does_not_cross_chains():
    other_chain = _Residue("ASP", "B", "97")
    _restore_amber_variant_names(
        _Topology([other_chain]), {("A", "97"): _record("ASH", "ASP", "A")}
    )
    assert other_chain.name == "ASP"


@pytest.mark.parametrize(
    "variant,base", [("ASH", "ASP"), ("GLH", "GLU"), ("LYN", "LYS"), ("CYM", "CYS")]
)
def test_every_variant_round_trips(variant, base):
    residue = _Residue(base, "A", "10")
    _restore_amber_variant_names(
        _Topology([residue]), {("A", "10"): _record(variant, base, "A")}
    )
    assert residue.name == variant


def test_no_records_is_a_no_op():
    residue = _Residue("ASP", "A", "97")
    _restore_amber_variant_names(_Topology([residue]), {})
    assert residue.name == "ASP"
