"""Tests for detecting and rebuilding missing residues during preparation.

PDBFixer finds missing residues by comparing coordinates against the reference
sequence, so preparation can only see a gap if that sequence survives the chain
split. These tests build their inputs with gemmi rather than downloading them,
so they run on a compute node with no network.
"""

import shutil
from pathlib import Path

import pytest

gemmi = pytest.importorskip("gemmi")
pytest.importorskip("pdbfixer")

from mdclaw.structure.clean_protein import (  # noqa: E402
    MODELLER_REPAIR_RANDOM_SEED,
    _repair_missing_residues_with_modeller,
    _validate_modeller_repair_model,
    clean_protein,
)
from mdclaw.structure.protonation import (  # noqa: E402
    _normalize_protonation_state_overrides,
)
from mdclaw.structure.split import split_molecules  # noqa: E402


# Two chains with deliberately DIFFERENT sequences and lengths: a chain-to-entity
# mix-up survives a same-length check, so the assertions below compare contents.
CHAIN_A_SEQ = ["ALA", "GLY", "SER", "THR", "VAL", "LEU", "ILE", "PRO", "PHE", "TYR"]
CHAIN_B_SEQ = ["LYS", "ARG", "ASP", "GLU"]
# Chain A is missing SER/THR/VAL (SEQRES positions 3-5); chain B is complete.
CHAIN_A_OBSERVED = [(1, "ALA"), (2, "GLY"), (6, "LEU"), (7, "ILE"), (8, "PRO"), (9, "PHE"), (10, "TYR")]
CHAIN_B_OBSERVED = [(1, "LYS"), (2, "ARG"), (3, "ASP"), (4, "GLU")]


def _build_two_chain_structure(path):
    """Write a 2-chain PDB whose SEQRES declares residues the coordinates lack."""
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    for chain_name, observed in (("A", CHAIN_A_OBSERVED), ("B", CHAIN_B_OBSERVED)):
        chain = gemmi.Chain(chain_name)
        for offset, (seqnum, resname) in enumerate(observed):
            residue = gemmi.Residue()
            residue.name = resname
            residue.seqid = gemmi.SeqId(seqnum, " ")
            # A real backbone, so chain typing sees a polypeptide.
            for atom_name, element, dx in (
                ("N", "N", 0.0), ("CA", "C", 1.5), ("C", "C", 2.5), ("O", "O", 3.0),
            ):
                atom = gemmi.Atom()
                atom.name = atom_name
                atom.element = gemmi.Element(element)
                atom.pos = gemmi.Position(offset * 3.8 + dx, 0.0, 0.0)
                atom.occ = 1.0
                atom.b_iso = 20.0
                residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    for entity in structure.entities:
        if entity.entity_type != gemmi.EntityType.Polymer:
            continue
        subchains = list(entity.subchains)
        entity.full_sequence = CHAIN_A_SEQ if any(s.startswith("A") for s in subchains) else CHAIN_B_SEQ
    structure.write_pdb(str(path))
    return path


def _seqres_of(path):
    """Return the SEQRES residue names a PDB file declares, in order."""
    names = []
    for line in open(path):
        if line.startswith("SEQRES"):
            names.extend(line[19:].split())
    return names


def _write_model(path, residues):
    """Write a realistic single-chain model with author residue identities."""
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("A")
    for offset, (seqnum, icode, resname) in enumerate(residues):
        residue = gemmi.Residue()
        residue.name = resname
        residue.seqid = gemmi.SeqId(seqnum, icode)
        for atom_name, element, dx in (
            ("N", "N", 0.0),
            ("CA", "C", 1.5),
            ("C", "C", 2.5),
            ("O", "O", 3.0),
        ):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element(element)
            atom.pos = gemmi.Position(offset * 3.8 + dx, 0.0, 0.0)
            atom.occ, atom.b_iso = 1.0, 20.0
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    structure.write_pdb(str(path))
    return path


@pytest.fixture
def stub_amber_conversion(monkeypatch):
    """Keep missing-residue tests independent of host Amber executables."""
    import importlib

    clean_module = importlib.import_module("mdclaw.structure.clean_protein")

    def fake_pdb2pqr(args):
        output = Path(args[args.index("--pdb-output") + 1])
        shutil.copyfile(args[0], output)

    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "is_available", lambda: True)
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "run", fake_pdb2pqr)
    monkeypatch.setattr(
        clean_module.pdb4amber_wrapper,
        "is_available",
        lambda: pytest.fail("missing-residue test fell through to pdb4amber"),
    )


def test_split_carries_each_chain_own_reference_sequence(tmp_path):
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    result = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )

    assert result["success"], result.get("errors")
    files = sorted(result["protein_files"])
    assert len(files) == 2

    written = [_seqres_of(path) for path in files]
    # The point of the test: each file carries ITS OWN sequence. Comparing only
    # lengths would pass even if both chains got chain A's entity.
    assert CHAIN_A_SEQ in written
    assert CHAIN_B_SEQ in written
    assert written[0] != written[1]


def test_split_output_makes_the_gap_visible_to_pdbfixer(tmp_path):
    from pdbfixer import PDBFixer

    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    result = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )

    gaps = {}
    for path in result["protein_files"]:
        fixer = PDBFixer(filename=path)
        fixer.findMissingResidues()
        seqres = tuple(_seqres_of(path))
        gaps[seqres] = sum(len(v) for v in fixer.missingResidues.values())

    # Chain A's 3-residue internal gap is now detectable; chain B has none.
    assert gaps[tuple(CHAIN_A_SEQ)] == 3
    assert gaps[tuple(CHAIN_B_SEQ)] == 0


def test_missing_gap_is_silent_without_a_reference_sequence(
    tmp_path,
    stub_amber_conversion,
):
    """A structure with no SEQRES reports 'not checked', not 'none found'."""
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    stripped = tmp_path / "no_seqres.pdb"
    stripped.write_text(
        "".join(line for line in open(source) if not line.startswith("SEQRES"))
    )

    result = clean_protein(pdb_file=str(stripped))

    detection = result["missing_residue_detection"]
    assert detection["reference_sequence_available"] is False
    assert detection["status"] == "not_detectable"
    assert any("means 'not checked'" in w for w in result["warnings"])


def test_auto_uses_pdbfixer_for_an_in_scope_gap(
    tmp_path,
    monkeypatch,
    stub_amber_conversion,
):
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    result = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )
    chain_a = next(
        path for path in result["protein_files"] if _seqres_of(path) == CHAIN_A_SEQ
    )
    import importlib

    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    monkeypatch.setattr(
        clean_module,
        "_repair_missing_residues_with_modeller",
        lambda *_args, **_kwargs: pytest.fail("auto invoked MODELLER in scope"),
    )

    cleaned = clean_protein(pdb_file=chain_a)

    assert cleaned["missing_residue_method_requested"] == "auto"
    assert cleaned["missing_residue_method_used"] == "pdbfixer"
    assert cleaned["missing_residue_method_escalated"] is False
    detection = cleaned["missing_residue_detection"]
    assert detection["reference_sequence_available"] is True
    assert detection["reference_sequence_length"] == len(CHAIN_A_SEQ)
    assert detection["modeled_residues"] == len(CHAIN_A_OBSERVED)


def test_invalid_missing_residue_method_is_rejected(tmp_path):
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")

    result = clean_protein(pdb_file=str(source), missing_residue_method="alphafold")

    assert result["success"] is False
    assert result["code"] == "invalid_missing_residue_method"


def test_out_of_scope_recommends_a_new_prep_node(tmp_path):
    """A failed prep is sealed, so recovery must create a sibling prep node."""
    from mdclaw.structure.clean_protein import _missing_residue_regeneration_recommendation

    recommendation = _missing_residue_regeneration_recommendation(
        {"segment_count": 1, "total_residues": 33, "max_segment_length": 33}
    )

    first = recommendation["options"][0]
    assert first["option"] == "repair_gaps_in_new_prep_node"
    assert first["flag"] == "--missing-residue-method modeller"
    assert recommendation["restart_stage"] == "prep"
    commands = recommendation["next_commands"]
    assert len(commands) == 2
    assert "create_node" in commands[0]
    assert "--node-type prep" in commands[0]
    assert "--parent-node-ids <completed_parent_node_id>" in commands[0]
    assert "--node-id <new_prep_node_id>" in commands[1]
    assert "--missing-residue-method modeller" in commands[1]
    serialized = str(recommendation).lower()
    assert "same node" not in serialized
    assert "re-run this" not in serialized


def test_repair_models_only_the_observed_span(tmp_path, monkeypatch):
    """Unresolved termini stay out of the repair.

    The reference sequence covers them, so passing it whole would have MODELLER
    grow long de-novo tails -- the same disorder the terminal filter decided to
    leave alone. MODELLER itself is stubbed: what is under test is which
    sequence it is asked to build.
    """
    # 3 residues missing at the N terminus, 2 at the C terminus, 3 inside.
    observed = [(4, "THR"), (5, "VAL"), (9, "PHE"), (10, "TYR")]
    full_sequence = ["ALA", "GLY", "SER", "THR", "VAL", "LEU", "ILE", "PRO", "PHE", "TYR", "TRP", "MET"]

    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("A")
    for offset, (seqnum, resname) in enumerate(observed):
        residue = gemmi.Residue()
        residue.name = resname
        residue.seqid = gemmi.SeqId(seqnum, " ")
        for atom_name, element, dx in (("N", "N", 0.0), ("CA", "C", 1.5), ("C", "C", 2.5), ("O", "O", 3.0)):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element(element)
            atom.pos = gemmi.Position(offset * 3.8 + dx, 0.0, 0.0)
            atom.occ, atom.b_iso = 1.0, 20.0
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    for entity in structure.entities:
        if entity.entity_type == gemmi.EntityType.Polymer:
            entity.full_sequence = full_sequence
    source = tmp_path / "gapped.pdb"
    structure.write_pdb(str(source))

    captured = {}
    model_file = tmp_path / "model.pdb"
    _write_model(
        model_file,
        [
            (4, " ", "THR"),
            (5, " ", "VAL"),
            (6, " ", "LEU"),
            (7, " ", "ILE"),
            (8, " ", "PRO"),
            (9, " ", "PHE"),
            (10, " ", "TYR"),
        ],
    )

    def fake_modeller(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "warnings": [],
            "errors": [],
            "selected_model": {
                "path": str(model_file),
                "template_frame": {"applied": True, "residues_renumbered": 7},
            },
        }

    monkeypatch.setattr(
        "mdclaw.genesis.modeller.modeller_from_alignment", fake_modeller
    )

    outcome = _repair_missing_residues_with_modeller(source)

    assert outcome["applied"] is True
    # ALA GLY SER at the N terminus and TRP MET at the C terminus are excluded;
    # what is left is the observed span THR..TYR, internal gap included.
    assert captured["target_sequence"] == "TVLIPFY"
    assert captured["loop_refinement"] is True
    assert captured["template_frame"] is True
    assert captured["random_seed"] == MODELLER_REPAIR_RANDOM_SEED
    assert any("unresolved terminal residue" in w for w in outcome["warnings"])


def test_repair_records_seed_and_template_identity(tmp_path, monkeypatch):
    """A rebuilt loop is only reproducible if the seed and template are kept."""
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    split = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )
    chain_a = next(p for p in split["protein_files"] if _seqres_of(p) == CHAIN_A_SEQ)

    model_file = tmp_path / "model.pdb"
    _write_model(
        model_file,
        [(index, " ", name) for index, name in enumerate(CHAIN_A_SEQ, 1)],
    )
    monkeypatch.setattr(
        "mdclaw.genesis.modeller.modeller_from_alignment",
        lambda **kwargs: {
            "success": True, "warnings": [], "errors": [],
            "selected_model": {
                "path": str(model_file),
                "template_frame": {
                    "applied": True,
                    "residues_renumbered": len(CHAIN_A_SEQ),
                },
            },
        },
    )

    outcome = _repair_missing_residues_with_modeller(chain_a)

    assert outcome["random_seed"] == MODELLER_REPAIR_RANDOM_SEED
    assert outcome["template"]["file"] == chain_a
    assert len(outcome["template"]["sha256"]) == 64
    assert outcome["operation"]["total_residues"] == 3


def test_repair_rejects_a_truncated_successful_model(tmp_path, monkeypatch):
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    split = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )
    chain_a = next(p for p in split["protein_files"] if _seqres_of(p) == CHAIN_A_SEQ)
    truncated = tmp_path / "truncated.pdb"
    _write_model(truncated, [(1, " ", "ALA")])
    monkeypatch.setattr(
        "mdclaw.genesis.modeller.modeller_from_alignment",
        lambda **kwargs: {
            "success": True,
            "warnings": [],
            "errors": [],
            "selected_model": {
                "path": str(truncated),
                "template_frame": {"applied": True, "residues_renumbered": 1},
            },
        },
    )

    outcome = _repair_missing_residues_with_modeller(chain_a)

    assert outcome["success"] is False
    assert outcome["applied"] is False
    assert outcome["code"] == "modeller_missing_residue_repair_validation_failed"
    assert outcome["validation"]["observed_residue_count"] == 1
    assert outcome["validation"]["expected_residue_count"] == len(CHAIN_A_SEQ)
    assert outcome["validation"]["missing_observed_residues"]


def test_repair_validation_rejects_unrestored_template_numbering(tmp_path):
    residues = [(index, " ", name) for index, name in enumerate(CHAIN_B_SEQ, 1)]
    template = _write_model(tmp_path / "template.pdb", residues)
    model = _write_model(tmp_path / "model.pdb", residues)

    validation = _validate_modeller_repair_model(
        template,
        model,
        "KRDE",
        # ``applied`` only says coordinates were fitted. A collision leaves
        # author numbering untouched while still reporting applied=True.
        {"applied": True, "residues_renumbered": 0},
    )

    assert validation["success"] is False
    assert validation["template_numbering_restored"] is False
    assert any("author numbering" in error for error in validation["errors"])


def test_repair_preserves_insertion_coded_protonation_site(tmp_path, monkeypatch):
    source = tmp_path / "insertion_gap.pdb"
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("A")
    observed = [
        (99, " ", "ALA"),
        (100, "A", "ASP"),
        (102, " ", "SER"),
    ]
    for offset, (seqnum, icode, resname) in enumerate(observed):
        residue = gemmi.Residue()
        residue.name = resname
        residue.seqid = gemmi.SeqId(seqnum, icode)
        for atom_name, element, dx in (
            ("N", "N", 0.0),
            ("CA", "C", 1.5),
            ("C", "C", 2.5),
            ("O", "O", 3.0),
        ):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element(element)
            atom.pos = gemmi.Position(offset * 3.8 + dx, 0.0, 0.0)
            atom.occ, atom.b_iso = 1.0, 20.0
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    next(
        entity
        for entity in structure.entities
        if entity.entity_type == gemmi.EntityType.Polymer
    ).full_sequence = ["ALA", "ASP", "GLY", "SER"]
    structure.write_pdb(str(source))

    # PDBFixer's gap finder does not report this synthetic insertion-coded
    # alignment, so isolate the repair/identity contract by supplying the gap
    # record it would receive for an experimental structure.
    from pdbfixer import PDBFixer

    probe = PDBFixer(filename=str(source))

    class GapAwareFixer:
        def __init__(self, filename):
            self.topology = probe.topology
            self.sequences = probe.sequences
            self.missingResidues = {}

        def findMissingResidues(self):
            self.missingResidues = {(0, 2): ["GLY"]}

    monkeypatch.setattr("pdbfixer.PDBFixer", GapAwareFixer)

    repaired = tmp_path / "repaired.pdb"
    _write_model(
        repaired,
        [
            (99, " ", "ALA"),
            (100, "A", "ASP"),
            (101, " ", "GLY"),
            (102, " ", "SER"),
        ],
    )
    monkeypatch.setattr(
        "mdclaw.genesis.modeller.modeller_from_alignment",
        lambda **kwargs: {
            "success": True,
            "warnings": [],
            "errors": [],
            "selected_model": {
                "path": str(repaired),
                "template_frame": {"applied": True, "residues_renumbered": 4},
            },
        },
    )

    outcome = _repair_missing_residues_with_modeller(source)
    override = _normalize_protonation_state_overrides(
        protonation_states={"A:100:A": "ASH"}
    )[0]
    model_sites = {
        (line[21], line[22:26].strip(), line[26].strip())
        for line in repaired.read_text().splitlines()
        if line.startswith("ATOM")
    }

    assert outcome["success"] is True
    assert outcome["applied"] is True
    assert outcome["validation"]["template_numbering_restored"] is True
    assert (override["chain"], override["resnum"], override["icode"]) in model_sites


def test_repair_declines_when_there_is_nothing_internal_to_fill(tmp_path, monkeypatch):
    """No internal gap is not an error: the normal PDBFixer path stays in charge."""
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    split = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )
    chain_b = next(p for p in split["protein_files"] if _seqres_of(p) == CHAIN_B_SEQ)

    def explode(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("MODELLER was invoked for a chain with no gaps")

    monkeypatch.setattr("mdclaw.genesis.modeller.modeller_from_alignment", explode)

    outcome = _repair_missing_residues_with_modeller(chain_b)

    assert outcome["applied"] is False
    assert outcome["success"] is True
    assert outcome["errors"] == []


@pytest.mark.parametrize("method", ["modeller", "auto"])
def test_detection_describes_the_structure_before_repair(
    tmp_path,
    monkeypatch,
    method,
    stub_amber_conversion,
):
    """A repaired chain must not report its gaps as 'never checked'.

    MODELLER writes a model without SEQRES, so detection run on the repaired
    file would say the reference sequence is unavailable — printed directly
    under a line stating how many residues were just rebuilt.
    """
    source = _build_two_chain_structure(tmp_path / "two_chains.pdb")
    split = split_molecules(
        structure_file=str(source),
        output_dir=str(tmp_path / "split"),
        include_types=["protein"],
    )
    chain_a = next(p for p in split["protein_files"] if _seqres_of(p) == CHAIN_A_SEQ)

    # A model with the gap filled and, like MODELLER's output, no SEQRES.
    filled = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("A")
    for offset, resname in enumerate(CHAIN_A_SEQ):
        residue = gemmi.Residue()
        residue.name = resname
        residue.seqid = gemmi.SeqId(offset + 1, " ")
        for atom_name, element, dx in (("N", "N", 0.0), ("CA", "C", 1.5), ("C", "C", 2.5), ("O", "O", 3.0)):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element(element)
            atom.pos = gemmi.Position(offset * 3.8 + dx, 0.0, 0.0)
            atom.occ, atom.b_iso = 1.0, 20.0
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    filled.add_model(model)
    model_file = tmp_path / "filled.pdb"
    filled.write_pdb(str(model_file))

    monkeypatch.setattr(
        "mdclaw.genesis.modeller.modeller_from_alignment",
        lambda **kwargs: {
            "success": True, "warnings": [], "errors": [],
            "selected_model": {
                "path": str(model_file),
                "template_frame": {
                    "applied": True,
                    "residues_renumbered": len(CHAIN_A_SEQ),
                },
            },
        },
    )
    scanned_inputs = []

    def record_raw_input(path):
        scanned_inputs.append(str(path))
        return []

    import importlib

    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    monkeypatch.setattr(
        clean_module,
        "_extract_input_protonation_state_overrides",
        record_raw_input,
    )
    if method == "auto":
        monkeypatch.setattr(
            clean_module,
            "PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH",
            2,
        )
        # What is under test is the detection record after an escalated
        # repair, not whether this machine has MODELLER. Without pinning the
        # usability probe the assertions below read the ambient
        # ``KEY_MODELLER*``: the test passes wherever a key happens to be
        # exported and fails everywhere else.
        monkeypatch.setattr(
            clean_module,
            "_modeller_repair_usability",
            lambda: {
                "usable": True,
                "license_env_present": True,
                "modeller_importable": True,
                "import_error": None,
            },
        )

    result = clean_protein(pdb_file=chain_a, missing_residue_method=method)

    detection = result["missing_residue_detection"]
    assert detection["status"] == "detected"
    assert detection["reference_sequence_available"] is True
    assert detection["reference_sequence_length"] == len(CHAIN_A_SEQ)
    # The count is the pre-repair one: what was measured, not what was built.
    assert detection["modeled_residues"] == len(CHAIN_A_OBSERVED)
    assert not any("means 'not checked'" in w for w in result["warnings"])
    assert scanned_inputs == [str(chain_a)]
    assert scanned_inputs[0] != str(model_file)
    assert result["missing_residue_method_requested"] == method
    assert result["missing_residue_method_used"] == "modeller"
    assert result["missing_residue_repair"]["escalated"] is (method == "auto")
    if method == "auto":
        assert any("escalated from PDBFixer to MODELLER" in w for w in result["warnings"])
