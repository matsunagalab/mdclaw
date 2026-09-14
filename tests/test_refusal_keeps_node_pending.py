"""A refusal before begin_node leaves the node pending and rerunnable.

Campaign v2 spent a prep node on every declared --conditions key the call did
not carry (chains, solvent_regime, ligands, ligand_net_charge), each time
followed by a fresh node with the same arguments minus the conditions.
"""

from mdclaw._node import create_node, read_node, update_job_params
from mdclaw.node.failure import trace_failure
from mdclaw.structure.prepare_complex import prepare_complex


def _prep_with_declared_chains(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    update_job_params(str(job_dir), {"solvent_regime": "explicit"})
    # no source: the context check (declared conditions, parents) runs first
    return job_dir, create_node(str(job_dir), "prep", conditions={"chains": ["A"]})["node_id"]


def test_a_declared_condition_the_call_lacks_leaves_the_node_pending(tmp_path):
    job_dir, prep = _prep_with_declared_chains(tmp_path)
    result = prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"])
    assert result["success"] is False
    assert result["code"] == "node_execution_context_invalid"
    assert result["node_status"] == "pending"
    assert "still pending" in result["hints"][0]
    node = read_node(str(job_dir), prep)
    assert node["status"] == "pending"
    assert node["metadata"]["last_refusal"]["code"] == "node_execution_context_invalid"
    assert (job_dir / "nodes" / prep / node["artifacts"]["failure"]).is_file()
    trace = trace_failure(str(job_dir), prep)
    assert any(o["action"] == "run_node" and o["reason"] == "refused_before_start" for o in trace["recovery_options"])


def test_the_same_node_can_be_run_again_after_the_refusal(tmp_path):
    job_dir, prep = _prep_with_declared_chains(tmp_path)
    prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"])
    # a second refusal for a different reason still leaves it pending
    second = prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"])
    assert second["code"] == "node_execution_context_invalid"
    assert read_node(str(job_dir), prep)["status"] == "pending"


# A split-stage refusal: the selection is described, nothing is prepared.
# The synthetic deposit is the one tests/test_chain_selection.py uses for the
# ligand-selection guardrail: protein label chain A, ligand AP5 in label chain
# C, both author chain A.
_CIF_PROTEIN_A_LIGAND_C_AUTH_A = """\
data_TEST
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.pdbx_PDB_ins_code
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.pdbx_formal_charge
_atom_site.auth_seq_id
_atom_site.auth_comp_id
_atom_site.auth_asym_id
_atom_site.auth_atom_id
_atom_site.pdbx_PDB_model_num
ATOM   1 N N   . ALA A 1 1   ? 0.0 0.0 0.0 1.0 0.0 ? 1   ALA A N   1
ATOM   2 C CA  . ALA A 1 1   ? 1.5 0.0 0.0 1.0 0.0 ? 1   ALA A CA  1
ATOM   3 C C   . ALA A 1 1   ? 2.0 1.5 0.0 1.0 0.0 ? 1   ALA A C   1
ATOM   4 O O   . ALA A 1 1   ? 1.5 2.5 0.0 1.0 0.0 ? 1   ALA A O   1
HETATM 5 P P1  . AP5 C 2 215 ? 5.0 0.0 0.0 1.0 0.0 ? 215 AP5 A P1  1
HETATM 6 O O1  . AP5 C 2 215 ? 6.5 0.0 0.0 1.0 0.0 ? 215 AP5 A O1  1
#
"""


def _prep_on_registered_deposit(tmp_path):
    from mdclaw.research.source_node import register_local_structure

    deposit = tmp_path / "protein_a_ligand_c_auth_a.cif"
    deposit.write_text(_CIF_PROTEIN_A_LIGAND_C_AUTH_A, encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    update_job_params(str(job_dir), {"solvent_regime": "explicit"})
    source = create_node(str(job_dir), "source")["node_id"]
    registered = register_local_structure(str(deposit), str(job_dir), source)
    assert registered["success"], registered
    return job_dir, create_node(str(job_dir), "prep")["node_id"]


def _event_names(job_dir, node_id):
    import json

    return [record["event_type"] for record in (
        json.loads(p.read_text()) for p in sorted((job_dir / "events").iterdir()))
        if record.get("node_id") == node_id]


def test_a_split_refusal_leaves_the_prep_node_pending(tmp_path):
    """associated_ligands_require_selection spent 39 prep nodes in campaign v2."""
    job_dir, prep = _prep_on_registered_deposit(tmp_path)
    result = prepare_complex(job_dir=str(job_dir), node_id=prep,
                             select_chains=["A"], include_types=["protein", "ligand"])
    assert result["success"] is False
    assert result["code"] == "associated_ligands_require_selection"
    assert result["ligand_selection"]["recommended_include_ligand_ids"] == ["A:AP5:215"]
    assert result["node_status"] == "pending"
    assert "still pending" in result["hints"][0]
    node = read_node(str(job_dir), prep)
    assert node["status"] == "pending"
    assert node["metadata"]["last_refusal"]["code"] == "associated_ligands_require_selection"
    assert "tool_started" not in _event_names(job_dir, prep)
    assert "node_refused_before_start" in _event_names(job_dir, prep)
    trace = trace_failure(str(job_dir), prep)
    assert any(o["action"] == "run_node" and o["reason"] == "refused_before_start"
               for o in trace["recovery_options"])

    # A second refusal, for a selection that names an absent chain, still
    # leaves the same node pending with the newer code on record.
    second = prepare_complex(job_dir=str(job_dir), node_id=prep,
                             select_chains=["A"], include_types=["protein"],
                             residue_ranges="Z:1-4")
    assert second["success"] is False
    assert second["code"] == "residue_range_chain_not_found"
    assert read_node(str(job_dir), prep)["status"] == "pending"
    assert read_node(str(job_dir), prep)["metadata"]["last_refusal"]["code"] == "residue_range_chain_not_found"


def test_the_node_is_begun_once_the_split_delivers_the_selection(tmp_path, monkeypatch):
    """After the split, a failure is a run that failed: the node is sealed."""
    import importlib

    module = importlib.import_module("mdclaw.structure.prepare_complex")

    def boom(*args, **kwargs):
        raise RuntimeError("cleaning crashed after the split")

    monkeypatch.setattr(module, "clean_protein", boom)
    job_dir, prep = _prep_on_registered_deposit(tmp_path)
    result = prepare_complex(job_dir=str(job_dir), node_id=prep,
                             select_chains=["A"], include_types=["protein"],
                             protonation_method="standard")
    assert result["success"] is False
    assert read_node(str(job_dir), prep)["status"] == "failed"
    assert "tool_started" in _event_names(job_dir, prep)
