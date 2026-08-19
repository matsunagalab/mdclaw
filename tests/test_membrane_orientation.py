"""Membrane orientation: OPM homolog transfer, PPM3, and the cascade between them."""

from __future__ import annotations

import importlib
import json
import math
import subprocess as sp
import time
from pathlib import Path

import pytest

from mdclaw.solvation import membrane, opm_orient, ppm_orient
from mdclaw.solvation.opm_orient import (
    _align_residues,
    _chain_residues,
    _kabsch,
    _kabsch_trimmed,
    orient_protein_with_opm_homolog,
)


def _atom(serial, name, resname, chain, resseq, x, y, z, record="ATOM") -> str:
    return (
        f"{record:<6}{serial:5d} {name:<4} {resname:>3} {chain:1}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C"
    )


_SEQ = "ACDEFGHIKLMNPQRSTVWY" * 4  # 80 residues, comfortably past the CA gate
_THREE = {v: k for k, v in opm_orient._THREE_TO_ONE.items() if len(k) == 3}


def _membrane_path(index: int) -> tuple[float, float, float]:
    """A residue position inside a +/-15 A bilayer slab, spanning all three axes.

    Deliberately not a planar curve. A path whose x and y are proportional lies
    in a plane, so its smallest principal spread is zero and the rotation about
    that plane's normal is undetermined — which the fit-conditioning gate
    rejects, correctly, but which would make every fixture here unusable as a
    stand-in for a real transmembrane bundle.
    """
    return (
        12.0 * math.cos(0.7 * index),
        12.0 * math.sin(0.7 * index),
        12.0 * math.sin(0.4 * index),
    )


def _write_chain(path: Path, sequence: str, *, chain="A", start=1,
                 offset=(0.0, 0.0, 0.0), dummy_z=None, scatter: float = 0.0,
                 extramembrane: int = 0, extramembrane_z: float = 60.0) -> Path:
    """Write a chain that sits in the membrane, optionally with a soluble domain.

    ``extramembrane`` appends residues far outside the slab, standing in for a
    domain a donor and target can share while still sitting differently in the
    bilayer.
    """
    rows = []
    serial = 1
    for index, letter in enumerate(sequence):
        if index < len(sequence) - extramembrane:
            x, y, z = _membrane_path(index)
        else:
            k = index - (len(sequence) - extramembrane)
            x, y, z = 2.0 * k, 3.0 * k, extramembrane_z + 1.2 * k
        if scatter:
            # A deterministic distortion, for standing in for a copy of a chain
            # that is modelled worse than its twin.
            x += scatter * math.sin(2.3 * index)
            y += scatter * math.cos(1.7 * index)
        rows.append(_atom(
            serial, "CA", _THREE[letter], chain, start + index,
            offset[0] + x, offset[1] + y, offset[2] + z,
        ))
        serial += 1
    if dummy_z is not None:
        for z in (-dummy_z, dummy_z):
            for k in range(4):
                rows.append(_atom(
                    serial, "N", "DUM", "X", 900 + k, 3.0 * k, 0.0, z, record="HETATM"
                ))
                serial += 1
    path.write_text("\n".join(rows) + "\nEND\n")
    return path


def _homolog_candidate(pdb_id="1abc", identity=0.95, coverage=0.9):
    return {
        "identifier": f"{pdb_id.upper()}_1", "pdb_id": pdb_id, "search_score": 1.0,
        "search_sequence_identity": identity, "search_query_coverage": coverage,
        "search_evalue": 0.0,
    }


def _bare_candidate(pdb_id="1abc"):
    """A hit as RCSB often returns it: ranked, but with no match context."""
    return {
        "identifier": f"{pdb_id.upper()}_1", "pdb_id": pdb_id, "search_score": 1.0,
        "search_sequence_identity": None, "search_query_coverage": None,
        "search_evalue": None,
    }


def _write_complex(path: Path, chains: list[tuple[str, str, dict]]) -> Path:
    """One PDB holding several chains, each rendered by the _write_chain rules."""
    rows: list[str] = []
    for chain_id, sequence, options in chains:
        part = path.parent / f"_chain_{chain_id}.pdb"
        _write_chain(part, sequence, chain=chain_id, **options)
        rows.extend(
            line for line in part.read_text().splitlines()
            if line.startswith(("ATOM", "HETATM"))
        )
        part.unlink()
    path.write_text("\n".join(rows) + "\nEND\n")
    return path


def _stub_search(monkeypatch, candidates=None, error=None, *,
                 by_sequence=None, calls=None):
    """Stand in for RCSB, optionally answering differently per query sequence."""
    def _search(sequence, **kwargs):
        if calls is not None:
            calls.append(sequence)
        if by_sequence is not None:
            return by_sequence.get(sequence, ([], None))
        return (candidates or []), error

    monkeypatch.setattr(opm_orient, "_search_opm_homologs", _search)


def _report(result) -> dict:
    return json.loads(Path(result["opm_homolog_search"]).read_text())


def _candidates(result) -> list[dict]:
    """Every candidate the report recorded, in the order they were tried."""
    return [
        candidate
        for chain in _report(result)["query_chains"]
        for candidate in chain["candidates"]
    ]


def _seed_cache(out_dir: Path, pdb_id: str, sequence: str, **kwargs) -> Path:
    cache = out_dir / "opm_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return _write_chain(cache / f"opm_{pdb_id}.pdb", sequence, **kwargs)


# ── sequence correspondence ──────────────────────────────────────────────────

def test_alignment_stays_in_register_across_an_insertion():
    """Gaps must be read from the CIGAR, not the match string.

    gemmi renders both kinds of gap as a space in the match string, so it cannot
    say which sequence a gap belongs to; consuming the wrong one shifts every
    correspondence after the first indel while still looking plausible.
    """
    def residues(seq, offset=0):
        return [
            {"chain": "A", "resseq": offset + i, "icode": "", "one": c,
             "ca": [float(i), 0.0, 0.0]}
            for i, c in enumerate(seq)
        ]

    pairs = _align_residues(residues("ACDEFXXXGHIKLMNP"), residues("ACDEFGHIKLMNP", 100))

    assert len(pairs) == 13
    assert all(q["one"] == t["one"] for q, t in pairs)


def test_chain_residues_reads_amber_protonation_variants(tmp_path):
    path = tmp_path / "p.pdb"
    path.write_text("\n".join([
        _atom(1, "CA", "HID", "A", 1, 0.0, 0.0, 0.0),
        _atom(2, "CA", "CYX", "A", 2, 1.0, 0.0, 0.0),
        _atom(3, "CA", "ASH", "A", 3, 2.0, 0.0, 0.0),
    ]) + "\nEND\n")

    chains = _chain_residues(path)

    assert [r["one"] for r in chains["A"]] == ["H", "C", "D"]


# ── superposition ────────────────────────────────────────────────────────────

def test_trimmed_fit_recovers_a_core_hidden_by_extra_domain():
    """A donor missing a domain the target has must still be usable.

    Fitting on every aligned pair lets the unmatched domain drag the whole
    superposition: on the real pair this measurement came from, an untrimmed fit
    reports 14.8 A and is rejected, while the shared core actually superposes at
    0.33 A. The cutoff is a multiple of the median deviation, which survives a
    minority of wrong correspondences.
    """
    core = [[float(i), 0.3 * i, 0.1 * i] for i in range(80)]
    # A domain present only on one side, of a size a real homolog would differ by.
    reference = list(core) + [[7.0 * k, 300.0, -11.0 * k] for k in range(10)]
    mobile = list(core) + [[-13.0 * k, -250.0, 5.0 * k] for k in range(10)]

    _, _, untrimmed, _ = _kabsch_trimmed(
        mobile, reference, min_retained=len(reference), rounds=0
    )
    _, _, trimmed, retained = _kabsch_trimmed(mobile, reference, min_retained=40)

    assert untrimmed > 10.0
    assert trimmed < 0.01
    assert retained >= 40


def test_trimming_cannot_shrink_below_the_caller_s_floor():
    """Trimming to a handful of atoms would make any donor look like a good fit."""
    core = [[float(i), 0.0, 0.0] for i in range(50)]
    reference = list(core)
    mobile = [[x, 3.0 * (i % 5), 0.0] for i, (x, _, _) in enumerate(core)]

    _, _, _, retained = _kabsch_trimmed(mobile, reference, min_retained=45)

    assert retained >= 45


def test_kabsch_is_a_proper_rotation():
    mobile = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    reference = [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]

    import numpy as np

    rotation, _, rmsd = _kabsch(mobile, reference)

    assert rmsd < 1e-6
    assert np.linalg.det(rotation) == pytest.approx(1.0, abs=1e-9)


# ── quality gates ────────────────────────────────────────────────────────────

def test_transfer_accepts_a_good_donor_and_centres_the_membrane(monkeypatch, tmp_path):
    query = _write_chain(tmp_path / "q.pdb", _SEQ, offset=(10.0, -5.0, 30.0))
    _seed_cache(tmp_path / "out", "1abc", _SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, [_homolog_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["success"], result
    assert result["membrane_center_z"] == 0.0
    homolog = result["opm_homolog"]
    assert homolog["pdb_id"] == "1abc"
    assert homolog["hydrophobic_thickness"] == pytest.approx(30.0)
    assert Path(result["oriented_pdb"]).is_file()
    report = _report(result)
    assert report["outcome"] == "accepted"
    assert report["accepted"]["fit_rmsd"] < 0.1
    assert report["accepted"]["membrane_ca"] >= 40


def test_gates_use_the_local_alignment_when_the_api_gives_no_context(
    monkeypatch, tmp_path
):
    """A hit with no match context must still be checked, not waved through.

    RCSB omits sequence_identity/query_coverage often enough that gating on them
    lets unvetted donors through; identity and coverage are computed from the
    alignment here and the search values kept only as provenance.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    unrelated = "".join("W" if i % 2 else "G" for i in range(len(_SEQ)))
    _seed_cache(tmp_path / "out", "1abc", unrelated, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert not result["success"]
    assert result["code"] == "opm_homolog_rejected"
    candidate = _candidates(result)[0]
    assert "local identity" in candidate["rejected"]
    assert candidate["search_sequence_identity"] is None
    assert candidate["local_identity"] < 0.5


def test_low_coverage_is_rejected_on_the_local_alignment(monkeypatch, tmp_path):
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _seed_cache(tmp_path / "out", "1abc", _SEQ[:20], dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out", min_corresponding_ca=5
    )

    assert not result["success"]
    candidate = _candidates(result)[0]
    assert candidate["local_query_coverage"] < 0.5
    assert "coverage" in candidate["rejected"]


def test_transfer_rejects_a_donor_without_membrane_markers(monkeypatch, tmp_path):
    """An OPM entry with no DUM atoms carries no frame to transfer."""
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _seed_cache(tmp_path / "out", "1abc", _SEQ)          # no dummy_z
    _stub_search(monkeypatch, [_homolog_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert not result["success"]
    assert "DUM" in _candidates(result)[0]["rejected"]


def test_transfer_rejects_a_poor_fit(monkeypatch, tmp_path):
    """Enough membrane residues, but a geometry that will not superpose."""
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    cache = tmp_path / "out" / "opm_cache"
    cache.mkdir(parents=True)
    rows, serial = [], 1
    for index, letter in enumerate(_SEQ):
        # inside the slab, but scrambled relative to the query's path
        rows.append(_atom(serial, "CA", _THREE[letter], "A", 1 + index,
                          (index % 5) * 6.0, (index % 7) * 5.0,
                          ((index * 37) % 25) - 12.0))
        serial += 1
    for k in range(8):
        rows.append(_atom(serial + k, "N", "DUM", "X", 900 + k, 0.0, 0.0,
                          15.0 if k % 2 else -15.0, record="HETATM"))
    (cache / "opm_1abc.pdb").write_text("\n".join(rows) + "\nEND\n")
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out", max_fit_rmsd=0.5
    )

    assert not result["success"]
    candidate = _candidates(result)[0]
    assert candidate["membrane_ca"] >= 40
    assert "RMSD" in candidate["rejected"]


def test_offline_search_is_a_fallback_not_a_failure(monkeypatch, tmp_path):
    """Being unable to reach RCSB must not fail the build."""
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _stub_search(monkeypatch, [], error="RCSB search unavailable: URLError")

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert not result["success"]
    assert result["fallback"] is True
    assert result["code"] == "opm_homolog_search_unavailable"
    assert "RCSB" in result["fallback_reason"]


def test_no_hit_is_a_fallback(monkeypatch, tmp_path):
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _stub_search(monkeypatch, [])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["code"] == "opm_homolog_no_match"
    assert result["fallback"] is True


# ── every chain is a query ───────────────────────────────────────────────────

_SOLUBLE_SEQ = "ACDEFGHIKLMNPQRSTVWY" * 8       # 160 residues, none in the slab
_SUBUNIT_SEQ = "WYFLIVMACGPSTNQHKRDE" * 4       # 80 residues, all in the slab


def test_a_soluble_longest_chain_does_not_hide_the_membrane_subunit(
    monkeypatch, tmp_path
):
    """The longest chain is not always the one with a membrane homolog.

    A large soluble partner bolted onto a small membrane subunit is the ordinary
    shape of a membrane complex. Searching only the longest chain would abandon
    the primary path on exactly the structures it exists for — and the soluble
    chain does find an OPM-annotated relative, it just has nothing in the
    bilayer, so it must be rejected rather than allowed to end the search.
    """
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SOLUBLE_SEQ, {"extramembrane": len(_SOLUBLE_SEQ)}),
        ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    out = tmp_path / "out"
    _seed_cache(out, "1sol", _SOLUBLE_SEQ, dummy_z=15.0,
                extramembrane=len(_SOLUBLE_SEQ))
    _seed_cache(out, "2mem", _SUBUNIT_SEQ, dummy_z=15.0)
    calls: list[str] = []
    _stub_search(monkeypatch, by_sequence={
        _SOLUBLE_SEQ: ([_bare_candidate("1sol")], None),
        _SUBUNIT_SEQ: ([_bare_candidate("2mem")], None),
    }, calls=calls)

    result = orient_protein_with_opm_homolog(protein_pdb=query, out_dir=out)

    assert result["success"], result
    assert result["opm_homolog"]["query_chain"] == "B"
    assert result["opm_homolog"]["pdb_id"] == "2mem"
    # longest first, and the second chain was only reached because the first
    # was not allowed to stop the search
    assert calls == [_SOLUBLE_SEQ, _SUBUNIT_SEQ]
    chains = _report(result)["query_chains"]
    assert [chain["chain"] for chain in chains] == ["A", "B"]
    assert chains[0]["outcome"] == "rejected"
    assert "bilayer" in chains[0]["candidates"][0]["rejected"]
    assert chains[1]["outcome"] == "accepted"


def test_one_query_chain_s_outage_does_not_end_the_search(monkeypatch, tmp_path):
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SOLUBLE_SEQ, {}),
        ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    out = tmp_path / "out"
    _seed_cache(out, "2mem", _SUBUNIT_SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, by_sequence={
        _SOLUBLE_SEQ: ([], "RCSB search returned HTTP 500"),
        _SUBUNIT_SEQ: ([_bare_candidate("2mem")], None),
    })

    result = orient_protein_with_opm_homolog(protein_pdb=query, out_dir=out)

    assert result["success"], result
    assert result["opm_homolog"]["query_chain"] == "B"
    chains = _report(result)["query_chains"]
    assert chains[0]["outcome"] == "search_error"
    assert "HTTP 500" in chains[0]["search_error"]


def test_partial_outage_with_a_completed_no_match_is_not_total_unavailability(
    monkeypatch, tmp_path
):
    """One chain answering "nothing matched" makes "nothing could be searched" false.

    Reporting an outage would hide a real answer; reporting a plain no-match
    would hide that the other chains were never checked. Both facts go in the
    reason.
    """
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SOLUBLE_SEQ, {}),
        ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    _stub_search(monkeypatch, by_sequence={
        _SOLUBLE_SEQ: ([], "RCSB search returned HTTP 500"),
        _SUBUNIT_SEQ: ([], None),
    })

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["fallback"] is True
    # not no_match: an agent branching on that would stop retrying, when in
    # fact a chain it never reached may hold the homolog
    assert result["code"] == "opm_homolog_evaluation_incomplete"
    reason = result["fallback_reason"]
    assert "1 chain(s) were never searched" in reason
    assert "HTTP 500" in reason


def test_only_an_outage_on_every_chain_is_reported_as_unreachable(
    monkeypatch, tmp_path
):
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SOLUBLE_SEQ, {}),
        ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    _stub_search(monkeypatch, error="RCSB search returned HTTP 500")

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["fallback"] is True
    assert result["code"] == "opm_homolog_search_unavailable"
    assert "chain A" in result["fallback_reason"]
    assert "chain B" in result["fallback_reason"]
    assert all(
        chain["outcome"] == "search_error"
        for chain in _report(result)["query_chains"]
    )


def test_one_donor_structure_is_fetched_once_for_the_whole_complex(
    monkeypatch, tmp_path
):
    """Two query chains hitting the same OPM entry must not download it twice."""
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SOLUBLE_SEQ, {}),
        ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    out = tmp_path / "out"
    _seed_cache(out, "1abc", _SUBUNIT_SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate("1abc")])
    fetched: list[str] = []
    real_fetch = opm_orient._fetch_opm_structure
    monkeypatch.setattr(
        opm_orient, "_fetch_opm_structure",
        lambda pdb_id, **kwargs: (fetched.append(pdb_id),
                                  real_fetch(pdb_id, **kwargs))[1],
    )

    result = orient_protein_with_opm_homolog(protein_pdb=query, out_dir=out)

    assert result["success"], result
    assert result["opm_homolog"]["query_chain"] == "B"
    assert fetched == ["1abc"]


def test_identical_chains_share_a_search_but_not_a_fit(monkeypatch, tmp_path):
    """Copies of one chain return identical hits but can have different coordinates.

    Collapsing them saves a redundant RCSB request. Collapsing the *fit* would
    mean that when the first copy is distorted or incompletely rebuilt, a donor
    the second copy would have accepted is never tried against it.
    """
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SUBUNIT_SEQ, {"scatter": 9.0}),          # a copy that will not fit
        ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    out = tmp_path / "out"
    _seed_cache(out, "1abc", _SUBUNIT_SEQ, dummy_z=15.0)
    calls: list[str] = []
    _stub_search(monkeypatch, [_bare_candidate()], calls=calls)

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=out, max_fit_rmsd=1.0
    )

    assert result["success"], result
    assert len(calls) == 1, "the identical sequence must not be searched twice"
    assert result["opm_homolog"]["query_chain"] == "B"
    chains = {c["chain"]: c for c in _report(result)["query_chains"]}
    assert chains["A"]["outcome"] == "rejected"
    assert chains["B"]["search_reused_from_identical_sequence"] is True


# ── donor chain selection ────────────────────────────────────────────────────

def _two_chain_donor(cache: Path, pdb_id: str = "1abc") -> Path:
    """A donor whose right chain fits loosely and whose wrong chain fits tightly.

    Chain P is the real counterpart: same sequence, same fold, displaced by a
    fraction of an angstrom per residue. Chain Q is an unrelated sequence laid
    exactly on the query's path, so it superposes perfectly over a stretch it
    has no business matching.
    """
    cache.mkdir(parents=True, exist_ok=True)
    decoy_seq = "".join(
        letter if index % 3 == 0 else "A" for index, letter in enumerate(_SEQ)
    )
    rows, serial = [], 1
    for chain_id, sequence, jitter in (("P", _SEQ, 0.45), ("Q", decoy_seq, 0.0)):
        for index, letter in enumerate(sequence):
            x, y, z = _membrane_path(index)
            rows.append(_atom(
                serial, "CA", _THREE[letter], chain_id, 1 + index,
                x + jitter * math.sin(index), y - jitter * math.cos(index), z,
            ))
            serial += 1
    for k in range(8):
        rows.append(_atom(serial + k, "N", "DUM", "X", 900 + k, 3.0 * k, 0.0,
                          15.0 if k % 2 else -15.0, record="HETATM"))
    path = cache / f"opm_{pdb_id}.pdb"
    path.write_text("\n".join(rows) + "\nEND\n")
    return path


def test_a_tighter_low_identity_chain_does_not_displace_the_real_one(
    monkeypatch, tmp_path
):
    """Gate every donor chain first, then rank the survivors by RMSD.

    Ranking on RMSD before gating lets a chain that aligns to a short unrelated
    stretch — tight precisely because it matches nothing meaningful — become the
    candidate's representative and drag the whole donor down with it.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ, offset=(10.0, -5.0, 0.0))
    strict, loose = tmp_path / "strict", tmp_path / "loose"
    _two_chain_donor(strict / "opm_cache")
    _two_chain_donor(loose / "opm_cache")
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(protein_pdb=query, out_dir=strict)

    assert result["success"], result
    assert result["opm_homolog"]["homolog_chain"] == "P"
    scored = {
        chain["homolog_chain"]: chain
        for chain in _report(result)["accepted"]["homolog_chains"]
    }
    assert "local identity" in scored["Q"]["rejected"]
    assert scored["Q"]["local_identity"] < 0.5
    assert "rejected" not in scored["P"]

    # The decoy really is the tighter fit: open the identity gate and it wins on
    # RMSD, which is why the gates have to come first.
    loosened = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=loose, min_identity=0.0
    )
    assert loosened["success"], loosened
    assert loosened["opm_homolog"]["homolog_chain"] == "Q"
    assert loosened["opm_homolog"]["fit_rmsd"] < result["opm_homolog"]["fit_rmsd"]


def test_every_donor_chain_keeps_its_numbers_when_all_are_rejected(
    monkeypatch, tmp_path
):
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _two_chain_donor(tmp_path / "out" / "opm_cache")
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out", max_fit_rmsd=0.05
    )

    assert not result["success"]
    assert result["code"] == "opm_homolog_rejected"
    scored = {
        chain["homolog_chain"]: chain
        for chain in _candidates(result)[0]["homolog_chains"]
    }
    assert set(scored) == {"P", "Q"}
    assert "RMSD" in scored["P"]["rejected"]
    assert "local identity" in scored["Q"]["rejected"]
    assert scored["P"]["membrane_ca"] >= 40
    assert scored["Q"]["local_identity"] < 0.5
    # the chain that got furthest is the one the candidate is rejected for
    assert "chain P" in _candidates(result)[0]["rejected"]


# ── choosing among acceptable donors ─────────────────────────────────────────

def _rank(**numbers):
    return opm_orient._evidence_rank(numbers)


def test_a_one_residue_support_difference_does_not_outrank_an_exact_fit():
    """Measured on 5L7D against live RCSB results.

    The query's own OPM entry superposes at 0.000 A over 200 membrane CA. A
    sister structure had 201 — one more residue — and 0.325 A. Ranking on the
    raw count put the sister first, which is backwards: 0.5% more support is
    not evidence, and a fit that is not exact when an exact one exists is.
    """
    exact = _rank(membrane_identity=1.0, membrane_ca=200, fit_rmsd=0.0)
    sister = _rank(membrane_identity=1.0, membrane_ca=201, fit_rmsd=0.325)

    assert exact > sister


def test_a_tight_fit_over_few_residues_does_not_outrank_a_broadly_supported_one():
    thin = _rank(membrane_identity=1.0, membrane_ca=45, fit_rmsd=0.1)
    broad = _rank(membrane_identity=1.0, membrane_ca=200, fit_rmsd=0.9)

    assert broad > thin


def test_a_perfect_sparse_match_does_not_outrank_a_near_perfect_broad_one():
    """40 of 40 is a weaker claim than 198 of 200, not a stronger one.

    Comparing raw proportions puts the sparse match first — it is 100% against
    99% — even though it rests on a fifth of the observations. The Wilson lower
    bound discounts it to 0.91 against 0.96 and the broad match wins.
    """
    sparse = _rank(membrane_identity=1.00, membrane_ca=40, fit_rmsd=2.9)
    broad = _rank(membrane_identity=0.99, membrane_ca=198, fit_rmsd=0.1)

    assert broad > sparse


def test_membrane_identity_leads_the_ordering():
    closer = _rank(membrane_identity=0.98, membrane_ca=100, fit_rmsd=2.0)
    weaker = _rank(membrane_identity=0.90, membrane_ca=200, fit_rmsd=0.1)

    assert closer > weaker


def test_every_candidate_is_judged_before_one_is_chosen(monkeypatch, tmp_path):
    """RCSB orders by search relevance, which is not orientation quality.

    Stopping at the first candidate that clears the gates lets a barely
    acceptable donor set the membrane frame while a far better one sits
    unexamined in the same result page.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ, offset=(4.0, -2.0, 0.0))
    out = tmp_path / "out"
    _seed_cache(out, "1weak", _SEQ, dummy_z=15.0, scatter=1.4)   # ranked first
    _seed_cache(out, "2good", _SEQ, dummy_z=15.0)                # exact match
    _stub_search(monkeypatch, [_bare_candidate("1weak"), _bare_candidate("2good")])

    result = orient_protein_with_opm_homolog(protein_pdb=query, out_dir=out)

    assert result["success"], result
    assert result["opm_homolog"]["pdb_id"] == "2good"
    assert result["opm_homolog"]["accepted_candidates"] == 2
    assert result["evaluation_complete"] is True
    ranking = _report(result)["ranking"]
    assert [item["pdb_id"] for item in ranking] == ["2good", "1weak"]
    assert ranking[0]["fit_rmsd"] < ranking[1]["fit_rmsd"]


# ── refusing a frame that is not actually determined ─────────────────────────

def test_a_donor_matching_only_outside_the_membrane_is_rejected(
    monkeypatch, tmp_path
):
    """Whole-chain identity is not enough when the fit happens in the bilayer.

    Two proteins can share a large soluble domain — enough to clear a
    whole-chain identity and coverage gate — while their membrane domains are
    unrelated. The frame is fitted to the membrane subset, so that subset is
    what has to correspond; otherwise a coincidentally similar helical bundle
    donates its bilayer to a protein that sits in the membrane differently.
    """
    shared = "ACDEFGHIKLMNPQRSTVWY" * 5                # 100-residue soluble domain
    query_tm = "WYFLIVMACGPSTNQHKRDE" * 3              # 60 residues in the slab
    decoy_tm = "".join("G" if i % 2 else "S" for i in range(60))
    query = _write_chain(tmp_path / "q.pdb", query_tm + shared,
                         extramembrane=len(shared))
    _seed_cache(tmp_path / "out", "1abc", decoy_tm + shared, dummy_z=15.0,
                extramembrane=len(shared))
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert not result["success"]
    candidate = _candidates(result)[0]
    # the whole-chain gates were satisfied; the membrane subset is what failed
    assert candidate["local_identity"] >= 0.5
    assert candidate["membrane_identity"] < 0.5
    assert "membrane-subset identity" in candidate["rejected"]


def test_a_degenerate_fit_is_rejected_rather_than_spun_arbitrarily(
    monkeypatch, tmp_path
):
    """Collinear CA atoms fit at RMSD 0 with a proper rotation matrix.

    The determinant correction rules out a reflection but says nothing about
    whether the rotation exists. If the fitted cloud is a line, the spin about
    that line is free, and whatever it happens to be is applied to every
    soluble domain and binding partner in the input.
    """
    line = "ACDEFGHIKLMNPQRSTVWY" * 4
    query = tmp_path / "q.pdb"
    rows = [_atom(i + 1, "CA", _THREE[c], "A", i + 1, 0.9 * i, 0.0, 0.0)
            for i, c in enumerate(line)]
    query.write_text("\n".join(rows) + "\nEND\n")
    cache = tmp_path / "out" / "opm_cache"
    cache.mkdir(parents=True)
    rows = [_atom(i + 1, "CA", _THREE[c], "A", i + 1, 0.0, 0.9 * i, 0.0)
            for i, c in enumerate(line)]
    rows += [_atom(200 + k, "N", "DUM", "X", 900 + k, 3.0 * k, 0.0,
                   15.0 if k % 2 else -15.0, record="HETATM") for k in range(8)]
    (cache / "opm_1abc.pdb").write_text("\n".join(rows) + "\nEND\n")
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert not result["success"]
    candidate = _candidates(result)[0]
    assert candidate["fit_condition"] == 0.0
    assert "degenerate" in candidate["rejected"]
    assert candidate["fit_rmsd"] is None      # never fitted, so never scored


def test_a_real_membrane_subset_is_well_conditioned():
    """The conditioning floor must not reject ordinary transmembrane geometry."""
    points = [list(_membrane_path(i)) for i in range(80)]

    assert opm_orient._fit_condition(points) > 10 * opm_orient.DEFAULT_MIN_FIT_CONDITION


@pytest.mark.parametrize("override, expected", [
    ({"min_corresponding_ca": 0}, "at least 3"),
    ({"min_corresponding_ca": -5}, "at least 3"),
    ({"max_fit_rmsd": float("nan")}, "finite"),
    ({"max_fit_rmsd": 0.0}, "positive"),
    ({"min_identity": 1.5}, "[0, 1]"),
    ({"min_identity": float("inf")}, "finite"),
    ({"slab_margin": -1.0}, "negative"),
    ({"max_candidates": 0}, "positive"),
    ({"total_budget_seconds": 0}, "positive"),
])
def test_an_out_of_range_gate_fails_instead_of_disabling_the_gate(
    monkeypatch, tmp_path, override, expected
):
    """A threshold that cannot tighten anything must not silently loosen everything.

    ``min_corresponding_ca=0`` lets an empty pair list reach the superposition,
    and any NaN threshold makes ``value > threshold`` false for every value, so
    a 39 A fit is accepted. Both are caller errors and are reported as such —
    not as a fallback, which would quietly orient by another method.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _seed_cache(tmp_path / "out", "1abc", _SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out", **override
    )

    assert not result["success"]
    assert result["fallback"] is False          # a bad argument is not a fallback
    assert result["code"] == "opm_homolog_gates_invalid"
    assert expected in result["fallback_reason"]


def test_an_invalid_gate_is_not_papered_over_by_ppm(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        ppm_orient, "orient_protein_with_ppm",
        lambda **kwargs: pytest.fail("a caller error must not fall through to PPM3"),
    )
    gates = {"min_identity": 0.5, "min_coverage": 0.5, "min_corresponding_ca": 0,
             "max_fit_rmsd": 3.0, "max_candidates": 10, "timeout_seconds": 120}

    outcome, payload = _cascade(tmp_path, opm_gates=gates)

    assert not outcome["success"]
    assert outcome["code"] == "opm_homolog_gates_invalid"


# ── outage vs. verdict ───────────────────────────────────────────────────────

def test_undownloadable_candidates_are_not_reported_as_gate_failures(
    monkeypatch, tmp_path
):
    """A donor that was never fetched was never judged.

    Reporting `opm_homolog_rejected` would tell an agent that these structures
    were examined and found wanting, sending it to pick different search terms
    when the real problem is that OPM's asset host is unreachable.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _stub_search(monkeypatch, [_bare_candidate("1abc"), _bare_candidate("2xyz")])
    monkeypatch.setattr(
        opm_orient, "_fetch_opm_structure",
        lambda pdb_id, **kwargs: (None, f"could not fetch {pdb_id}: timed out", None),
    )

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert not result["success"]
    assert result["fallback"] is True
    assert result["code"] == "opm_homolog_fetch_unavailable"
    assert all(c.get("fetch_failed") for c in _candidates(result))


def test_a_download_outage_alongside_a_real_rejection_is_not_a_verdict(
    monkeypatch, tmp_path
):
    """One donor judged and one never fetched is not "all of them failed"."""
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    unrelated = "".join("W" if i % 2 else "G" for i in range(len(_SEQ)))
    _seed_cache(tmp_path / "out", "1abc", unrelated, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate("1abc"), _bare_candidate("2xyz")])
    real_fetch = opm_orient._fetch_opm_structure
    monkeypatch.setattr(
        opm_orient, "_fetch_opm_structure",
        lambda pdb_id, **kwargs: (
            (None, "could not fetch 2xyz: timed out", None) if pdb_id == "2xyz"
            else real_fetch(pdb_id, **kwargs)
        ),
    )

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["code"] == "opm_homolog_evaluation_incomplete"
    reason = result["fallback_reason"]
    assert "1 candidate(s) failed the gates" in reason
    assert "1 could not be downloaded" in reason


def test_the_backend_stops_at_its_total_budget(monkeypatch, tmp_path):
    """Per-request timeouts do not bound a complex; ten chains multiply them."""
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", _SOLUBLE_SEQ, {}), ("B", _SUBUNIT_SEQ, {"start": 500}),
    ])
    searched: list[str] = []

    def _slow(sequence, **kwargs):
        searched.append(sequence)
        time.sleep(0.35)
        return [], "RCSB search unavailable: TimeoutError: timed out"

    monkeypatch.setattr(opm_orient, "_search_opm_homologs", _slow)

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out", total_budget_seconds=0.3
    )

    assert not result["success"]
    assert result["fallback"] is True
    assert len(searched) == 1, "the budget must stop the second chain"
    skipped = [c for c in _report(result)["query_chains"] if "skipped" in c]
    assert "budget" in skipped[0]["skipped"]


# ── one conformer, one model ─────────────────────────────────────────────────

def test_only_the_first_model_is_read_and_written(monkeypatch, tmp_path):
    """An NMR ensemble must not be fitted in one frame and written in several.

    Assigning CA coordinates before the residue-seen check lets the last model
    silently win the fit while the file still carries every model's atoms, so
    the packed system becomes a superposition of frames.
    """
    ensemble = tmp_path / "q.pdb"
    rows = ["MODEL        1"]
    for i, letter in enumerate(_SEQ):
        x, y, z = _membrane_path(i)
        rows.append(_atom(i + 1, "CA", _THREE[letter], "A", i + 1, x, y, z))
    rows += ["ENDMDL", "MODEL        2"]
    for i, letter in enumerate(_SEQ):
        x, y, z = _membrane_path(i)
        rows.append(_atom(i + 1, "CA", _THREE[letter], "A", i + 1, x, y, z + 500.0))
    rows += ["ENDMDL", "END"]
    ensemble.write_text("\n".join(rows) + "\n")

    residues = _chain_residues(ensemble)["A"]
    assert max(r["ca"][2] for r in residues) < 100.0, "model 2 leaked into the fit"

    _seed_cache(tmp_path / "out", "1abc", _SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate()])
    result = orient_protein_with_opm_homolog(
        protein_pdb=ensemble, out_dir=tmp_path / "out"
    )

    assert result["success"], result
    written = [line for line in Path(result["oriented_pdb"]).read_text().splitlines()
               if line.startswith("ATOM")]
    assert len(written) == len(_SEQ), "both models were written into one frame"
    assert any("models" in w for w in result["warnings"])


def test_the_highest_occupancy_conformer_wins(tmp_path):
    """Last-altLoc-wins picks by file order; occupancy is the actual evidence."""
    def row(serial, alt, z, occupancy):
        return (
            "ATOM  %5d  CA %1sALA A%4d    %8.3f%8.3f%8.3f%6.2f  0.00           C"
            % (serial, alt, 1, 0.0, 0.0, z, occupancy)
        )

    structure = tmp_path / "altloc.pdb"
    structure.write_text("\n".join([
        row(1, "A", 0.0, 0.70), row(2, "B", 50.0, 0.30), "END",
    ]) + "\n")

    residues = _chain_residues(structure)["A"]
    kept, dropped = opm_orient._first_model_atoms(structure)

    assert [r["ca"][2] for r in residues] == [0.0]
    assert len(kept) == 1 and dropped["altloc_atoms"] == 1


# ── donor cache ──────────────────────────────────────────────────────────────

def test_a_corrupt_cache_entry_is_replaced_not_blamed_on_the_donor(tmp_path):
    """A local accident must not be reported as a property of the OPM entry."""
    cache = tmp_path / "opm_cache"
    cache.mkdir(parents=True)
    (cache / "opm_1abc.pdb").write_text("HEADER only, no atoms\n")
    calls: list[str] = []

    class _Response:
        def read(self):
            calls.append("download")
            return _write_chain(tmp_path / "d.pdb", _SEQ,
                                dummy_z=15.0).read_bytes()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    opm_orient.urllib.request.urlopen = lambda url, timeout=None: _Response()
    try:
        path, error, digest = opm_orient._fetch_opm_structure(
            "1abc", cache_dir=cache, timeout_seconds=5
        )
    finally:
        importlib.reload(opm_orient.urllib.request)

    assert error is None and calls == ["download"]
    assert digest and len(digest) == 64
    assert "DUM" in Path(path).read_text()


def test_a_download_is_renamed_into_place(monkeypatch, tmp_path):
    """A kill between write and rename must not leave a partial file to trust."""
    cache = tmp_path / "opm_cache"
    payload = _write_chain(tmp_path / "d.pdb", _SEQ, dummy_z=15.0).read_bytes()
    seen: dict = {}
    real_replace = opm_orient.os.replace

    def _watch(src, dst):
        seen["from"] = Path(src).suffix
        return real_replace(src, dst)

    monkeypatch.setattr(opm_orient.os, "replace", _watch)
    _stub_urlopen(monkeypatch, body=payload)

    path, error, _ = opm_orient._fetch_opm_structure(
        "1abc", cache_dir=cache, timeout_seconds=5
    )

    assert error is None and Path(path).is_file()
    assert seen["from"] == ".part"


# ── round-2 review: identifiability, coherence, and honest codes ────────────

def test_a_coplanar_fit_is_accepted_because_it_does_determine_a_rotation():
    """Rank 2 is enough; only collinear data leave a free spin.

    Three or more non-collinear points fix two in-plane basis vectors, and the
    proper-rotation constraint fixes the normal. Gating on the *third* singular
    value would reject a coplanar cloud that recovers a known general rotation
    to within 3e-16 — a valid donor thrown away.
    """
    import numpy as np

    angles = np.linspace(0.0, 2 * math.pi, 40, endpoint=False)
    flat = np.stack(
        [12 * np.cos(angles), 12 * np.sin(angles), np.zeros_like(angles)], axis=1
    )
    spin = np.array([[math.cos(0.7), -math.sin(0.7), 0.0],
                     [math.sin(0.7), math.cos(0.7), 0.0],
                     [0.0, 0.0, 1.0]])
    tilt = np.array([[1.0, 0.0, 0.0],
                     [0.0, math.cos(0.5), -math.sin(0.5)],
                     [0.0, math.sin(0.5), math.cos(0.5)]])
    truth = tilt @ spin
    moved = (truth @ flat.T).T + np.array([3.0, -2.0, 5.0])

    rotation, _, rmsd = _kabsch(flat.tolist(), moved.tolist())

    assert rmsd < 1e-9
    assert np.abs(rotation - truth).max() < 1e-9
    assert opm_orient._fit_condition(flat.tolist()) > \
        opm_orient.DEFAULT_MIN_FIT_CONDITION


def test_a_collinear_cloud_is_still_refused():
    line = [[float(i), 0.0, 0.0] for i in range(40)]

    assert opm_orient._fit_condition(line) < opm_orient.DEFAULT_MIN_FIT_CONDITION


def test_a_single_helix_clears_the_conditioning_floor():
    """The tightest realistic geometry: one ideal transmembrane helix."""
    helix = [
        [2.3 * math.cos(i * 100 * math.pi / 180),
         2.3 * math.sin(i * 100 * math.pi / 180), 1.5 * i]
        for i in range(40)
    ]

    assert opm_orient._fit_condition(helix) > opm_orient.DEFAULT_MIN_FIT_CONDITION


def test_one_altloc_is_chosen_per_residue_not_per_atom(tmp_path):
    """A per-atom choice can assemble a side chain that exists in no structure.

    Here conformer A holds the backbone and conformer B the side chain by
    occupancy. Choosing atom by atom takes CA from A and CB from B — a hybrid
    residue. The label with the most occupancy across the residue takes it all.
    """
    def row(serial, name, alt, z, occupancy):
        return (
            "ATOM  %5d  %-3s%1sALA A%4d    %8.3f%8.3f%8.3f%6.2f  0.00           C"
            % (serial, name, alt, 1, 0.0, 0.0, z, occupancy)
        )

    structure = tmp_path / "altloc.pdb"
    structure.write_text("\n".join([
        row(1, "CA", "A", 0.0, 0.70), row(2, "CA", "B", 50.0, 0.30),
        row(3, "CB", "A", 1.5, 0.30), row(4, "CB", "B", 51.5, 0.70),
        "END",
    ]) + "\n")

    kept, dropped = opm_orient._first_model_atoms(structure)
    labels = {line[16] for line in kept}

    assert labels == {"A"}, "the residue must come from one conformer"
    assert len(kept) == 2 and dropped["altloc_atoms"] == 2


def test_a_chain_break_survives_the_transform(monkeypatch, tmp_path):
    """TER marks where a polymer stops; dropping it fuses two segments.

    Two segments sharing a chain id are read downstream as one chain and pick
    up connectivity that was never in the input.
    """
    query = tmp_path / "q.pdb"
    rows = []
    for index, letter in enumerate(_SEQ):
        x, y, z = _membrane_path(index)
        rows.append(_atom(index + 1, "CA", _THREE[letter], "A", index + 1, x, y, z))
        if index == 39:
            rows.append("TER    %5d      %3s A%4d" % (900, _THREE[letter], 40))
    query.write_text("\n".join(rows) + "\nEND\n")
    _seed_cache(tmp_path / "out", "1abc", _SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["success"], result
    written = Path(result["oriented_pdb"]).read_text().splitlines()
    assert sum(1 for line in written if line.startswith("TER")) == 1
    assert sum(1 for line in written if line.startswith("ATOM")) == len(_SEQ)


def test_a_truncated_atom_record_does_not_raise(tmp_path):
    """Fixed-column parsing must validate width before indexing it."""
    structure = tmp_path / "short.pdb"
    structure.write_text(
        "ATOM\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00\n"
    )

    kept, dropped = opm_orient._first_model_atoms(structure)

    assert len(kept) == 1
    assert dropped["malformed_records"] == 1


@pytest.mark.parametrize("override, expected", [
    ({"max_candidates": 1.5}, "whole number"),
    ({"min_corresponding_ca": 3.9}, "whole number"),
    ({"min_fit_condition": 0.0}, "re-enables"),
    ({"min_fit_condition": 1.5}, "[0, 1]"),
])
def test_count_and_condition_controls_must_be_usable(
    monkeypatch, tmp_path, override, expected
):
    """A fractional page count reaches RCSB as a malformed request.

    The caller's mistake then comes back as a search outage, which is the wrong
    diagnosis entirely. ``min_fit_condition=0`` likewise re-enables the exact
    degenerate fits the gate was added to reject.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out", **override
    )

    assert result["code"] == "opm_homolog_gates_invalid"
    assert expected in result["fallback_reason"]


def test_the_best_donor_may_belong_to_a_later_query_chain(monkeypatch, tmp_path):
    """Chain order is not evidence.

    A long membrane-associated partner with a barely acceptable relative must
    not set the frame for the whole complex while the actual transmembrane
    subunit, which has an exact donor, goes unused.
    """
    long_seq = "WYFLIVMACGPSTNQHKRDE" * 6                # 120 residues
    short_seq = "ACDEFGHIKLMNPQRSTVWY" * 4               # 80 residues
    # A distant relative of the long chain: clears the 0.5 identity gate and
    # fits loosely, which is exactly the donor that must not win by seniority.
    weak_seq = "".join(
        letter if index % 3 else "G" for index, letter in enumerate(long_seq)
    )
    query = _write_complex(tmp_path / "q.pdb", [
        ("A", long_seq, {}),
        ("B", short_seq, {"start": 500}),
    ])
    out = tmp_path / "out"
    _seed_cache(out, "1weak", weak_seq, dummy_z=15.0, scatter=1.2)
    _seed_cache(out, "2exact", short_seq, dummy_z=15.0)              # exact
    _stub_search(monkeypatch, by_sequence={
        long_seq: ([_bare_candidate("1weak")], None),
        short_seq: ([_bare_candidate("2exact")], None),
    })

    result = orient_protein_with_opm_homolog(protein_pdb=query, out_dir=out)

    assert result["success"], result
    assert result["opm_homolog"]["query_chain"] == "B"
    assert result["opm_homolog"]["pdb_id"] == "2exact"
    ranking = _report(result)["ranking"]
    assert [item["query_chain"] for item in ranking] == ["B", "A"]
    # the loose donor really was acceptable — it lost on evidence, not on a gate
    assert ranking[1]["membrane_identity"] >= 0.5


def test_a_truncated_candidate_field_is_flagged_not_presented_as_best(
    monkeypatch, tmp_path
):
    """Adopting a gate-passing donor is fine; calling it the best is not.

    The budget can stop the search before every candidate is judged. The frame
    that was chosen still cleared every gate, so it beats dropping to another
    method — but the claim that nothing better was available is unsupported and
    must not be made silently.
    """
    query = _write_chain(tmp_path / "q.pdb", _SEQ)
    out = tmp_path / "out"
    _seed_cache(out, "1ok", _SEQ, dummy_z=15.0)
    _stub_search(monkeypatch, [_bare_candidate("1ok"), _bare_candidate("2never")])
    real_consider = opm_orient._consider_candidate

    def _slow(entry, **kwargs):
        outcome = real_consider(entry, **kwargs)
        time.sleep(0.3)
        return outcome

    monkeypatch.setattr(opm_orient, "_consider_candidate", _slow)

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=out, total_budget_seconds=0.25
    )

    assert result["success"], result
    assert result["opm_homolog"]["pdb_id"] == "1ok"
    assert result["evaluation_complete"] is False
    assert any("unjudged" in w for w in result["warnings"])
    assert "unjudged" in _report(result)["query_chains"][0]["truncated"]


# ── PPM3 ─────────────────────────────────────────────────────────────────────

def _stub_ppm(monkeypatch, tmp_path, stdout=""):
    captured: dict = {}

    def _capture(cmd, **kwargs):
        captured["stdin"] = kwargs.get("input")
        return sp.CompletedProcess(cmd, 0, stdout=stdout)

    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: "/usr/bin/immers")
    resources = tmp_path / "res"
    resources.mkdir(exist_ok=True)
    (resources / "res.lib").write_text("")
    monkeypatch.setattr(ppm_orient, "_ppm3_resource_dir", lambda: resources)
    monkeypatch.setattr(ppm_orient.subprocess, "run", _capture)
    return captured


def test_ppm_passes_an_explicit_side_through(monkeypatch, tmp_path):
    captured = _stub_ppm(monkeypatch, tmp_path)
    structure = _write_chain(tmp_path / "p.pdb", _SEQ)

    result = ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path, n_terminal_side="in"
    )

    lines = captured["stdin"].strip().split("\n")
    assert len(lines) == 8
    assert lines[6] == "in"
    assert not any("undetermined" in w for w in result["warnings"])


def test_ppm_flags_an_unstated_side_instead_of_deciding(monkeypatch, tmp_path):
    """PPM3 needs a value, but silence must not be recorded as a choice."""
    captured = _stub_ppm(monkeypatch, tmp_path)
    structure = _write_chain(tmp_path / "p.pdb", _SEQ)

    result = ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path
    )

    assert captured["stdin"].strip().split("\n")[6] == ppm_orient.PPM3_DEFAULT_SIDE
    assert any("undetermined" in w for w in result["warnings"])


def test_ppm_names_the_known_format_bug(monkeypatch, tmp_path):
    """The stock build crashes printing its own result; say which failure it is."""
    _stub_ppm(monkeypatch, tmp_path,
              stdout="Fortran runtime error: Missing comma between descriptors\n")
    structure = _write_chain(tmp_path / "p.pdb", _SEQ)

    result = ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path
    )

    assert result["code"] == "ppm3_format_bug"
    assert "rebuild" in result["errors"][0].lower()


def test_ppm_requires_the_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: None)
    structure = _write_chain(tmp_path / "p.pdb", _SEQ)

    result = ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path
    )

    assert result["code"] == "ppm3_unavailable"


# ── cascade and provenance ───────────────────────────────────────────────────

def _cascade(tmp_path, result=None, **kwargs):
    payload = result if result is not None else {"warnings": [], "parameters": {}}
    payload.setdefault("warnings", [])
    payload.setdefault("parameters", {})
    defaults = dict(
        protein_pdb=tmp_path / "q.pdb", out_dir=tmp_path / "out", result=payload,
        method="auto", opm_homolog_search=True,
        opm_gates={"min_identity": 0.5, "min_coverage": 0.5,
                   "min_corresponding_ca": 40, "max_fit_rmsd": 3.0,
                   "max_candidates": 10, "timeout_seconds": 120},
        beta_barrel=False, force_span=False, n_terminal_side=None, search_type=3,
    )
    defaults.update(kwargs)
    return membrane._orient_for_membrane(**defaults), payload


def test_auto_uses_the_homolog_when_it_passes(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        membrane, "_orient_protein_with_memembed",
        lambda **kwargs: pytest.fail("MEMEMBED must not run in auto"),
    )
    monkeypatch.setattr(
        opm_orient, "orient_protein_with_opm_homolog",
        lambda **kwargs: {"success": True, "oriented_pdb": "o.pdb",
                          "membrane_center_z": 0.0, "warnings": [],
                          "opm_homolog": {"pdb_id": "1abc"}},
    )

    outcome, payload = _cascade(tmp_path)

    assert outcome["success"]
    assert payload["orientation"]["method"] == "opm-homolog"
    assert payload["parameters"]["orientation_backend_used"] == "opm-homolog"


def test_auto_falls_back_to_ppm_and_records_why(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        opm_orient, "orient_protein_with_opm_homolog",
        lambda **kwargs: {"success": False, "fallback": True,
                          "code": "opm_homolog_no_match", "warnings": [],
                          "fallback_reason": "no OPM-annotated structure matched"},
    )
    monkeypatch.setattr(
        ppm_orient, "orient_protein_with_ppm",
        lambda **kwargs: {"success": True, "oriented_pdb": "o.pdb",
                          "membrane_center_z": 0.0, "warnings": [],
                          "ppm": {"n_terminal_side_assumed": True}},
    )

    outcome, payload = _cascade(tmp_path)

    assert outcome["success"]
    orientation = payload["orientation"]
    assert orientation["method"] == "ppm"
    assert [a["backend"] for a in orientation["attempts"]] == ["opm-homolog", "ppm"]
    assert orientation["attempts"][0]["code"] == "opm_homolog_no_match"
    assert any("no OPM-annotated structure matched" in w for w in payload["warnings"])


def test_disabling_the_lookup_skips_straight_to_ppm(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        opm_orient, "orient_protein_with_opm_homolog",
        lambda **kwargs: pytest.fail("the OPM search must not run when disabled"),
    )
    monkeypatch.setattr(
        ppm_orient, "orient_protein_with_ppm",
        lambda **kwargs: {"success": True, "oriented_pdb": "o.pdb",
                          "membrane_center_z": 0.0, "warnings": [], "ppm": {}},
    )

    outcome, payload = _cascade(tmp_path, opm_homolog_search=False)

    assert outcome["success"]
    assert payload["orientation"]["method"] == "ppm"
    assert payload["orientation"]["attempts"][0]["code"] == "opm_homolog_search_disabled"


def test_explicit_homolog_request_does_not_silently_fall_back(monkeypatch, tmp_path):
    """Asking for a backend by name and getting another one would be a lie."""
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        opm_orient, "orient_protein_with_opm_homolog",
        lambda **kwargs: {"success": False, "fallback": True,
                          "code": "opm_homolog_no_match", "warnings": [],
                          "fallback_reason": "no match"},
    )
    monkeypatch.setattr(
        ppm_orient, "orient_protein_with_ppm",
        lambda **kwargs: pytest.fail("PPM must not run for an explicit request"),
    )

    outcome, payload = _cascade(tmp_path, method="opm-homolog")

    assert not outcome["success"]
    assert payload["orientation"]["method"] == "opm-homolog"


def test_explicit_memembed_is_honoured(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        membrane, "_orient_protein_with_memembed",
        lambda **kwargs: {"success": True, "oriented_pdb": "o.pdb",
                          "membrane_center_z": 0.0, "warnings": [], "memembed": {}},
    )
    monkeypatch.setattr(
        opm_orient, "orient_protein_with_opm_homolog",
        lambda **kwargs: pytest.fail("the OPM search must not run for memembed"),
    )

    outcome, payload = _cascade(tmp_path, method="memembed")

    assert outcome["success"]
    assert payload["orientation"]["method"] == "memembed"


def test_embed_rejects_an_unknown_orientation_method(tmp_path):
    structure = _write_chain(tmp_path / "p.pdb", _SEQ)

    result = membrane.embed_in_membrane(
        pdb_file=str(structure), output_dir=str(tmp_path / "out"),
        orientation_method="sideways",
    )

    assert result["code"] == "membrane_orientation_method_invalid"


def test_packing_receives_an_already_oriented_structure(monkeypatch, tmp_path):
    """Orientation must stay independent of which packing backend runs."""
    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"success": False, "code": "stopped", "errors": ["x"], "warnings": []}

    monkeypatch.setattr(membrane, "embed_with_membrane_patch_tiles", _capture)
    monkeypatch.setattr(
        opm_orient, "orient_protein_with_opm_homolog",
        lambda **kwargs: {"success": False, "fallback": True, "warnings": [],
                          "code": "opm_homolog_no_match", "fallback_reason": "none"},
    )
    oriented = tmp_path / "oriented_protein.pdb"

    def _ppm(**kwargs):
        Path(kwargs["out_dir"]).mkdir(parents=True, exist_ok=True)
        target = Path(kwargs["out_dir"]) / "oriented_protein.pdb"
        target.write_text(Path(kwargs["protein_pdb"]).read_text())
        return {"success": True, "oriented_pdb": str(target),
                "membrane_center_z": 0.0, "warnings": [], "ppm": {}}

    monkeypatch.setattr(ppm_orient, "orient_protein_with_ppm", _ppm)
    structure = _write_chain(tmp_path / "p.pdb", _SEQ)

    membrane.embed_in_membrane(
        pdb_file=str(structure), output_dir=str(tmp_path / "out"),
    )

    assert seen["preoriented"] is True
    assert seen["membrane_center_z"] == 0.0
    assert seen["orient_fn"] is None
    assert seen["protein_pdb"].name == oriented.name


def test_fit_ignores_a_shared_domain_outside_the_membrane(monkeypatch, tmp_path):
    """A domain both structures share, but outside the bilayer, must not set the frame.

    Two proteins can agree closely on a soluble domain and still sit differently
    in the membrane. Fitting across everything — or trimming to whichever subset
    fits best — would let that domain choose the orientation, which is exactly
    what a membrane transfer must not do. Only residues the donor places inside
    its own bilayer are fitted.
    """
    membrane_len = len(_SEQ)
    sequence = _SEQ + "ACDEFGHIKLMNPQRSTVWY" * 2      # + 40-residue soluble domain
    query = _write_chain(tmp_path / "q.pdb", sequence, extramembrane=40)

    # Donor: identical soluble domain, membrane region rotated 90 degrees in xy.
    cache = tmp_path / "out" / "opm_cache"
    cache.mkdir(parents=True)
    rows, serial = [], 1
    for index, letter in enumerate(sequence):
        if index < membrane_len:
            x, y, z = _membrane_path(index)
            x, y = -y, x                              # the part that differs
        else:
            k = index - membrane_len
            x, y, z = 2.0 * k, 3.0 * k, 60.0 + 1.2 * k   # identical to the query
        rows.append(_atom(serial, "CA", _THREE[letter], "A", 1 + index, x, y, z))
        serial += 1
    for k in range(8):
        rows.append(_atom(serial + k, "N", "DUM", "X", 900 + k, 3.0 * k, 0.0,
                          15.0 if k % 2 else -15.0, record="HETATM"))
    (cache / "opm_1abc.pdb").write_text("\n".join(rows) + "\nEND\n")
    _stub_search(monkeypatch, [_bare_candidate()])

    result = orient_protein_with_opm_homolog(
        protein_pdb=query, out_dir=tmp_path / "out"
    )

    assert result["success"], result
    homolog = result["opm_homolog"]
    # The fit used only membrane residues, so the shared soluble domain — 40 of
    # the 120 aligned pairs — is excluded rather than dominating.
    assert homolog["aligned_ca"] == len(sequence)
    assert homolog["membrane_ca"] <= membrane_len
    assert homolog["fit_rmsd"] < 1.0

    # and the transferred frame follows the membrane region, not the domain
    import numpy as np

    rotation = np.asarray(homolog["transform"]["rotation"], dtype=float)
    assert abs(float(rotation[0][0])) < 0.5          # a real xy rotation happened


def test_search_is_posted_as_json_with_verbose_results(monkeypatch):
    """A membrane protein sequence does not fit in a URL query string."""
    captured: dict = {}

    class _Response:
        def read(self):
            return json.dumps({"result_set": []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(opm_orient.urllib.request, "urlopen", _urlopen)

    opm_orient._search_opm_homologs(
        "M" * 900, max_candidates=7, min_identity=0.4, timeout_seconds=90
    )

    request = captured["request"]
    assert request.get_method() == "POST"
    assert request.full_url == opm_orient.RCSB_SEARCH_URL
    assert request.headers["Content-type"] == "application/json"
    assert captured["timeout"] == 90
    body = json.loads(request.data.decode())
    options = body["request_options"]
    assert options["results_verbosity"] == "verbose"
    assert options["paginate"]["rows"] == 7
    nodes = body["query"]["nodes"]
    assert nodes[0]["parameters"]["identity_cutoff"] == 0.4
    assert nodes[0]["parameters"]["value"] == "M" * 900
    assert nodes[1]["parameters"]["value"] == opm_orient.OPM_ANNOTATION_TYPE


def _stub_urlopen(monkeypatch, *, status=200, body=b"", http_error=None):
    class _Response:
        status = None

        def __init__(self, status, body):
            self.status, self._body = status, body

        def read(self):
            return self._body

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        if http_error is not None:
            raise http_error
        return _Response(status, body)

    monkeypatch.setattr(opm_orient.urllib.request, "urlopen", _urlopen)


def test_zero_hits_come_back_as_no_match_not_as_an_outage(monkeypatch):
    """RCSB answers "nothing matched" with 204 and an empty body.

    urllib counts 2xx as success, so this never reaches the HTTPError branch;
    parsing it raises, and a generic handler would then report a chain with no
    homolog as though the search service were unreachable. With every chain
    reporting that, a complex that simply has no OPM relative would be filed as
    an outage.
    """
    _stub_urlopen(monkeypatch, status=204, body=b"")

    candidates, error = opm_orient._search_opm_homologs(
        "M" * 100, max_candidates=5, min_identity=0.5, timeout_seconds=10
    )

    assert candidates == []
    assert error is None


def test_an_empty_body_with_a_200_is_a_broken_response_not_a_no_match(monkeypatch):
    """Only 204 is RCSB's "nothing matched"; an empty 200 came from in between.

    Treating a truncated proxy or CDN response as "this chain has no homolog"
    turns an outage into a scientific conclusion, and across every chain it
    would report a protein as having no OPM relative at all.
    """
    _stub_urlopen(monkeypatch, status=200, body=b"\n")

    candidates, error = opm_orient._search_opm_homologs(
        "M" * 100, max_candidates=5, min_identity=0.5, timeout_seconds=10
    )

    assert candidates == []
    assert "empty body" in error


def test_a_server_error_carries_what_the_server_said(monkeypatch):
    """A 500 and a rejected query are the same status; only the body separates them."""
    import io

    error = opm_orient.urllib.error.HTTPError(
        opm_orient.RCSB_SEARCH_URL, 500, "Server Error", {},
        io.BytesIO(b'{"message":"did not complete ticketId within 30000 ms"}'),
    )
    _stub_urlopen(monkeypatch, http_error=error)

    candidates, reason = opm_orient._search_opm_homologs(
        "M" * 100, max_candidates=5, min_identity=0.5, timeout_seconds=10
    )

    assert candidates == []
    assert "HTTP 500" in reason
    assert "ticketId" in reason


def test_search_numbers_are_recorded_on_the_same_scale_as_the_gates(monkeypatch):
    """RCSB reports identity out of 100 and no coverage at all.

    Left as returned, a 95.5 would sit in the report beside a local_identity of
    0.81 in a different unit; and query_coverage would always be null even
    though the alignment range RCSB found says what it was.
    """
    payload = {"result_set": [{
        "identifier": "6OT0_1", "score": 1.0,
        "services": [{"service_type": "sequence", "nodes": [{"match_context": [{
            "sequence_identity": 95.5, "evalue": 0.0, "alignment_length": 496,
            "query_beg": 1, "query_end": 380, "query_length": 475,
        }]}]}],
    }]}
    _stub_urlopen(monkeypatch, body=json.dumps(payload).encode())

    candidates, error = opm_orient._search_opm_homologs(
        "M" * 475, max_candidates=5, min_identity=0.5, timeout_seconds=10
    )

    assert error is None
    hit = candidates[0]
    assert hit["pdb_id"] == "6ot0"
    assert hit["search_sequence_identity"] == pytest.approx(0.955)
    assert hit["search_query_coverage"] == pytest.approx(380 / 475, abs=1e-4)
    assert hit["search_alignment_length"] == 496


def test_search_reports_transport_failure_as_a_reason(monkeypatch):
    def _urlopen(request, timeout=None):
        raise OSError("network is unreachable")

    monkeypatch.setattr(opm_orient.urllib.request, "urlopen", _urlopen)

    candidates, error = opm_orient._search_opm_homologs(
        "M" * 100, max_candidates=5, min_identity=0.5, timeout_seconds=10
    )

    assert candidates == []
    assert "network is unreachable" in error
