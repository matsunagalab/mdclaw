"""Comma-joined id lists are read as lists; the multi-candidate refusal names its flag."""

from mdclaw.structure.split import _split_listed_ids


def test_comma_and_space_joined_ids_are_split():
    assert _split_listed_ids(["A,B", " C"]) == ["A", "B", "C"]
    assert _split_listed_ids("A, B") == ["A", "B"]
    assert _split_listed_ids(["A:AMH:90,B:AMH:91"]) == ["A:AMH:90", "B:AMH:91"]
    assert _split_listed_ids(None) is None
    assert _split_listed_ids([]) == []


def test_multi_candidate_refusal_names_the_flag():
    import pytest

    from mdclaw.source_bundle import select_source_structure

    bundle = {"structures": [{"structure_id": "candidate_001"}, {"structure_id": "candidate_002"}]}
    with pytest.raises(ValueError) as excinfo:
        select_source_structure(bundle)
    assert "--source-structure-id" in str(excinfo.value) and "list_source_candidates" in str(excinfo.value)
