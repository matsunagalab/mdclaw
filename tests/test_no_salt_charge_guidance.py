"""--no-salt on a charged solute is named as such, at embed time and at the topology.

Campaign v2 (001_membrane_5yc8 cli_skill_sif r2, 006 cli_sif r1, 008 cli_skill_sif r1):
agents read --no-salt as "no bulk salt" for a "neutralised" request, embedded a
+6 to +18 e receptor without counter-ions, and the topology refused with
"Explicit solvation requested neutralization", which nothing had.
"""

from mdclaw.solvation.membrane import _estimate_protein_net_charge


def _residue(serial, resname, resnum, chain="A"):
    return (f"ATOM  {serial:5d}  CA  {resname:>3s} {chain}{resnum:4d}    "
            f"{0.0:8.3f}{0.0:8.3f}{float(resnum):8.3f}  1.00  0.00           C\n")


def test_residue_names_give_the_net_charge(tmp_path):
    pdb = tmp_path / "p.pdb"
    pdb.write_text("".join([
        _residue(1, "ARG", 1), _residue(2, "LYS", 2), _residue(3, "HIP", 3), _residue(4, "HID", 4),
        _residue(5, "ASP", 5), _residue(6, "GLH", 6), _residue(7, "LYN", 7), _residue(8, "CYM", 8),
        _residue(9, "ALA", 9), _residue(10, "ARG", 1),   # same residue twice: counted once
    ]) + "END\n")
    assert _estimate_protein_net_charge(pdb) == 1


def test_a_neutral_protein_estimates_zero(tmp_path):
    pdb = tmp_path / "p.pdb"
    pdb.write_text(_residue(1, "LYS", 1) + _residue(2, "GLU", 2) + "END\n")
    assert _estimate_protein_net_charge(pdb) == 0


def test_the_topology_message_names_the_missing_ions():
    from mdclaw.guardrail_codes import GUARDRAIL_CODES

    assert "system_net_charge_without_ions" in GUARDRAIL_CODES
    assert "--saltcon 0" in GUARDRAIL_CODES["system_net_charge_without_ions"]
