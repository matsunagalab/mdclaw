"""The selected prep, including an empty plan, owns downstream chemistry."""

import json
import pytest
from mdclaw._node import create_node, resolve_node_inputs
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node


@pytest.mark.parametrize(
    "new_plan",
    [[], None, [{"cys1": {"chain": "Q", "resnum": 7}, "cys2": {"chain": "Q", "resnum": 29}}]],
)
def test_solv_and_topo_use_same_nearest_prep(tmp_path, new_plan):
    job = str(tmp_path)
    source = create_node(job, "source")["node_id"]
    complete_node(job, source, {"structure_file": "artifacts/source.pdb"})
    first = create_node(job, "prep", parent_node_ids=[source])["node_id"]
    complete_node(
        job, first, {"merged_pdb": "artifacts/merged.pdb", "disulfide_bonds": "artifacts/ss.json"}
    )
    (tmp_path / "nodes" / first / "artifacts/ss.json").write_text(
        json.dumps([{"old_branch": True}])
    )
    second = create_node(job, "prep", parent_node_ids=[first])["node_id"]
    artifacts = {"merged_pdb": "artifacts/merged.pdb"}
    if new_plan is not None:
        artifacts["disulfide_bonds"] = "artifacts/ss.json"
    complete_node(job, second, artifacts)
    if new_plan is not None:
        (tmp_path / "nodes" / second / "artifacts/ss.json").write_text(json.dumps(new_plan))
    solv = create_node(job, "solv", parent_node_ids=[second])["node_id"]
    inputs = resolve_node_inputs(job, solv, "solv")
    assert inputs.get("disulfide_bonds") == new_plan
    assert inputs["pdb_resolved_from_node_id"] == second
    complete_node(job, solv, {"solvated_pdb": "artifacts/solvated.pdb"})
    topo = create_node(job, "topo", parent_node_ids=[solv])["node_id"]
    assert resolve_node_inputs(job, topo, "topo").get("disulfide_bonds") == new_plan


def test_inspect_and_split_share_protonation_sequence(tmp_path):
    from mdclaw.research.inspection import inspect_molecules
    from mdclaw.structure.split import _inspect_molecules_impl
    from mdclaw.chemistry_constants import protein_sequence_symbol

    names = [
        "ACE",
        "HID",
        "HIE",
        "HIP",
        "ASH",
        "GLH",
        "LYN",
        "CYM",
        "CYX",
        "ALA",
        "NME",
        "MSE",
        "SEP",
        "TPO",
        "PTR",
        "UNK",
    ]
    rows = []
    for num, name in enumerate(names, 1):
        for atom, el in [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")]:
            rows.append(
                f"ATOM  {len(rows) + 1:5d} {atom:^4s} {name} Q{num:4d}    {num * 4.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {el:>2s}\n"
            )
    pdb = tmp_path / "variants.pdb"
    pdb.write_text("".join(rows) + "TER\nEND\n")
    expected = "HHHDEKCCAMSTYX"
    assert "".join(map(protein_sequence_symbol, names)) == expected
    for inspect in (inspect_molecules, _inspect_molecules_impl):
        r = inspect(str(pdb))
        assert r["success"], r
        chains = [c for c in r["chains"] if c.get("sequence")]
        assert "".join(c["sequence"] for c in chains) == expected, r
        assert sum(c["sequence_length"] for c in chains) == 14
        assert sum(c["cap_count"] for c in r["chains"]) == 2


@pytest.mark.parametrize(
    "charge,valid",
    [
        (0.0, True),
        (2.0, True),
        (-3.0, True),
        (0.4, False),
        (float("nan"), False),
        (float("inf"), False),
        (None, False),
    ],
)
def test_charge_comes_from_valid_nonbonded_force(tmp_path, monkeypatch, charge, valid):
    from openmm import System, NonbondedForce, XmlSerializer
    from mdclaw.solvation.membrane import _compute_membrane_net_charge

    system = System()
    system.addParticle(1)
    if charge is not None:
        force = NonbondedForce()
        force.addParticle(charge, 1, 0)
        system.addForce(force)
    path = tmp_path / "system.xml"
    path.write_text(XmlSerializer.serialize(system))
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return {"success": True, "system_xml": str(path)}

    monkeypatch.setattr("mdclaw.amber.build_system.build_amber_system", build)
    pairs = [{"cys1": {"chain": "Q", "resnum": 7}, "cys2": {"chain": "Q", "resnum": 29}}]
    r = _compute_membrane_net_charge(
        pdb_file=tmp_path / "input.pdb", box_dims={}, disulfide_bonds=pairs, water_model="tip3p"
    )
    assert r["success"] == valid, r
    assert captured["disulfide_bonds"] == pairs
    assert captured["forcefield"] == "ff14SB"
    if valid:
        assert r["net_charge"] == int(charge)
    else:
        assert r["code"] == "net_charge_invalid"
