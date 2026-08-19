"""Membrane orientation transferred from an experimentally oriented OPM homolog.

OPM records, for thousands of membrane proteins, where the bilayer sits relative
to the coordinates. When a close homolog is in there, its frame can be carried
over: superpose the two structures and apply the same rigid transform to the
input. Nothing is predicted — the answer comes from an entry someone curated.

Every protein chain is a query, longest first, and the search stops at the
first donor that clears the gates. A complex is often a large soluble partner
bolted onto a small membrane subunit, and only the subunit has an OPM homolog;
searching the longest chain alone would abandon the primary path on exactly the
structures it exists for. One chain failing to reach the search service does not
end the attempt either — the chain that has a homolog is frequently a later one.

Two things decide whether that is trustworthy, and they are gated separately.

How much of the query this donor accounts for, and how well, is measured from
the alignment computed here rather than from the search service's match context.
RCSB drops the match context entirely on some hits, reports identity out of 100
rather than as a fraction, and never reports coverage at all — only the aligned
range it can be derived from. Gating on any of that would wave through
candidates that were never checked, so the search numbers are normalised onto
the same 0-1 scale and kept as provenance only.

A search that matches nothing answers 204 with an empty body, which urllib
counts as success rather than as an error. That has to be read as "no homolog
for this chain": treating the unparseable body as a transport failure would file
a protein that simply has no OPM relative as an outage.

Where the fit is taken matters as much as how good it is. The superposition uses
only residues the donor places inside its own bilayer, read from the DUM markers
in its OPM entry. A homolog can share a large extramembrane domain and still sit
differently in the membrane; fitting across everything — or trimming to whatever
subset fits best — lets that domain choose the frame, which is precisely what a
membrane transfer must not do.

Among a donor's chains, only those that clear every gate compete, and the
lowest-RMSD one of those wins. Ranking on RMSD first would let a chain matching
a short, unrelated stretch — tight precisely because it is short — displace the
chain that genuinely corresponds to the query.

The transfer inherits the donor's frame *and* the residual of the superposition,
and which donor is used matters far more than how tightly it superposes.
Measured against PDBTM, whose TMDET algorithm is independent of the PPM code
behind OPM: 5L7D's frame taken from 4JKV superposes at 0.81 A over the
transmembrane region and still lands 11.8 degrees from the reference, while the
top hit the live search actually returns, 6OT0, superposes at only 1.93 A and
lands 6.3 — as good as running PPM3 on the target directly. OPM's own
annotations for two structures of one protein differ by roughly 5 degrees,
because PPM optimises each structure separately. So read fit RMSD as a
sanity check on the correspondence, not as a predictor of frame accuracy, and
read the gates as bounding how bad a donor may be rather than ranking donors.
"""

import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
OPM_PDB_URL = "https://storage.googleapis.com/opm-assets/pdb/{pdb_id}.pdb"
OPM_ANNOTATION_TYPE = "OPM"

DEFAULT_MIN_IDENTITY = 0.5
DEFAULT_MIN_COVERAGE = 0.5
DEFAULT_MIN_CORRESPONDING_CA = 40
DEFAULT_MAX_FIT_RMSD = 3.0
DEFAULT_MAX_CANDIDATES = 10
# Superposition is refined by discarding the worst-fitting pairs and refitting.
# Without it, a donor that merely lacks a domain the target has — a 7TM-only
# construct against a receptor that also has its extracellular domain — drags
# the whole fit and is rejected even though its membrane region matches well.
DEFAULT_TRIM_ROUNDS = 10
DEFAULT_TRIM_CUTOFF_MULTIPLE = 2.0
# How far past the donor's own bilayer boundary a residue may sit and still
# count as membrane-embedded for the fit. Small on purpose: the point of
# restricting the fit is that residues outside the membrane must not decide
# where the membrane goes.
DEFAULT_SLAB_MARGIN = 2.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 120
# Ceiling on the whole backend, not on each request. Ten chains times ten
# candidates times a per-request timeout is hours, and the caller was promised a
# prompt drop to PPM3 rather than an unbounded wait on an unreachable network.
DEFAULT_TOTAL_BUDGET_SECONDS = 600
# Smallest principal spread of the fitted CA cloud, relative to the largest,
# for the rotation to be determined. Measured: real membrane subsets sit at
# 0.69-0.70 and even a single ideal 40-residue helix at 0.093, while collinear
# and coplanar sets are 0.000 -- so this rejects rank deficiency with an order
# of magnitude of margin on both sides, not thin-but-real geometry.
DEFAULT_MIN_FIT_CONDITION = 0.01

_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
    # Amber/protonation variants carry the same identity for alignment.
    "HID": "H", "HIE": "H", "HIP": "H", "HSD": "H", "HSE": "H", "HSP": "H",
    "CYX": "C", "CYM": "C", "ASH": "D", "GLH": "E", "LYN": "K",
}


def _occupancy(line: str) -> float:
    try:
        return float(line[54:60])
    except ValueError:
        return 1.0


def _first_model_atoms(pdb_file: Path) -> tuple[list[str], dict[str, int]]:
    """Records of the first model, with one coherent conformer per residue.

    An NMR ensemble and an alternate-conformation crystal structure both break
    a naive fixed-column read in the same way: later records overwrite earlier
    ones for the same residue, so the fit can end up using one model's
    coordinates while the file still carries every model's atoms. Both the
    superposition and the transformed output have to come from the same single
    conformer, so the selection happens once, here.

    The altLoc choice is made per *residue*, not per atom. Picking the
    highest-occupancy record atom by atom can take CA from conformer A and CB
    from conformer B and emit a hybrid side chain that exists in no structure;
    the label with the most occupancy across the whole residue wins instead,
    ties broken alphabetically so the result does not depend on file order.

    ``TER`` records are carried through untransformed. They mark chain breaks,
    and dropping them merges two polymer segments — especially when they share
    a chain id — into one chain with connectivity that was never there.

    Returns the kept lines and what was left out, for the report.
    """
    raw = Path(pdb_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    dropped = {"models": 0, "altloc_atoms": 0, "malformed_records": 0}

    model: list[str] = []
    in_model = False
    for line in raw:
        if line.startswith("MODEL"):
            if in_model or model:
                break
            in_model = True
            continue
        if line.startswith("ENDMDL"):
            break
        if line.startswith("TER"):
            model.append(line)
            continue
        if not line.startswith(("ATOM", "HETATM")):
            continue
        if len(line) < 54:
            # Too short to hold coordinates; indexing it would raise out of the
            # backend instead of producing a structured result.
            dropped["malformed_records"] += 1
            continue
        model.append(line)
    if in_model:
        dropped["models"] = max(
            0, sum(1 for line in raw if line.startswith("MODEL")) - 1
        )

    # Which altLoc label carries this residue, by total occupancy.
    occupancy_by_label: dict[tuple[str, str, str], dict[str, float]] = {}
    for line in model:
        if line.startswith("TER"):
            continue
        label = line[16].strip()
        if not label:
            continue
        residue = (line[21], line[22:26], line[26])
        occupancy_by_label.setdefault(residue, {}).setdefault(label, 0.0)
        occupancy_by_label[residue][label] += _occupancy(line)
    chosen = {
        residue: max(labels, key=lambda name: (labels[name], [-ord(c) for c in name]))
        for residue, labels in occupancy_by_label.items()
    }

    kept: list[str] = []
    seen_atoms: set[tuple[str, str, str, str]] = set()
    for line in model:
        if line.startswith("TER"):
            kept.append(line)
            continue
        label = line[16].strip()
        residue = (line[21], line[22:26], line[26])
        if label and label != chosen.get(residue):
            dropped["altloc_atoms"] += 1
            continue
        atom = (line[21], line[22:26], line[26], line[12:16])
        if atom in seen_atoms:
            # A blank and a labelled record for the same atom, or a repeat.
            dropped["altloc_atoms"] += 1
            continue
        seen_atoms.add(atom)
        kept.append(line)
    return kept, dropped


def _chain_residues(pdb_file: Path) -> dict[str, list[dict[str, Any]]]:
    """Per-chain ordered residues with their one-letter code and CA position."""
    lines, _ = _first_model_atoms(Path(pdb_file))
    return _residues_from_lines(lines)


def _residues_from_lines(lines: list[str]) -> dict[str, list[dict[str, Any]]]:
    chains: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, int, str]] = set()
    ca: dict[tuple[str, int, str], list[float]] = {}
    for line in lines:
        if not line.startswith(("ATOM", "HETATM")):
            continue
        one = _THREE_TO_ONE.get(line[17:20].strip().upper())
        if one is None:
            continue
        try:
            resseq = int(line[22:26])
        except ValueError:
            continue
        key = (line[21].strip() or "A", resseq, line[26].strip())
        if line[12:16].strip() == "CA" and key not in ca:
            try:
                ca[key] = [
                    float(line[30:38]), float(line[38:46]), float(line[46:54])
                ]
            except ValueError:
                pass
        if key in seen:
            continue
        seen.add(key)
        chains.setdefault(key[0], []).append(
            {"chain": key[0], "resseq": resseq, "icode": key[2], "one": one}
        )
    for residues in chains.values():
        for residue in residues:
            residue["ca"] = ca.get(
                (residue["chain"], residue["resseq"], residue["icode"])
            )
    return chains


def _sequence(residues: list[dict[str, Any]]) -> str:
    return "".join(residue["one"] for residue in residues)


def _as_fraction(value: Any) -> Optional[float]:
    """RCSB reports sequence identity out of 100; the gates work in 0-1."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number / 100.0, 4) if number > 1.0 else round(number, 4)


def _search_query_coverage(context: dict[str, Any]) -> Optional[float]:
    """Coverage of RCSB's own alignment, which it reports only as a range."""
    beg, end = context.get("query_beg"), context.get("query_end")
    length = context.get("query_length")
    if beg is None or end is None or not length:
        return None
    try:
        return round((float(end) - float(beg) + 1.0) / float(length), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _search_opm_homologs(
    sequence: str,
    *,
    max_candidates: int,
    min_identity: float,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Ask RCSB for entities that both match the sequence and carry OPM data.

    Sent as a POST with a JSON body rather than a query string: membrane protein
    sequences run to hundreds of residues, and URL-encoding one into a GET
    reliably exceeds what the service accepts. ``results_verbosity=verbose`` is
    requested so the per-hit match context comes back at all — without it there
    is nothing to record about why a candidate scored as it did.
    """
    query = {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "sequence",
                    "parameters": {
                        "evalue_cutoff": 1,
                        "identity_cutoff": min_identity,
                        "sequence_type": "protein",
                        "value": sequence,
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_annotation.type",
                        "operator": "exact_match",
                        "value": OPM_ANNOTATION_TYPE,
                    },
                },
            ],
        },
        "return_type": "polymer_entity",
        "request_options": {
            "paginate": {"start": 0, "rows": max_candidates},
            "results_content_type": ["experimental"],
            "results_verbosity": "verbose",
        },
    }
    request = urllib.request.Request(
        RCSB_SEARCH_URL,
        data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            return [], None
        # Say what the service said. Without the body a 500 is indistinguishable
        # from a malformed query, and the difference decides whether to retry.
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return [], (
            f"RCSB search returned HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        )
    except Exception as exc:  # noqa: BLE001
        return [], f"RCSB search unavailable: {type(exc).__name__}: {exc}"

    # Zero hits come back as 204 with an empty body, and urllib treats 2xx as
    # success — so this never reaches the HTTPError branch above. Parsing it
    # would raise, and the generic handler would then report "no OPM homolog
    # for this chain" as though the service were unreachable.
    if status == 204:
        return [], None
    if not raw.strip():
        # A 200 with nothing in it is not RCSB's no-hit answer; it is a
        # truncated response from something in between. Calling it "no homolog"
        # would turn an outage into a scientific conclusion.
        return [], f"RCSB search returned an empty body with HTTP {status}"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return [], f"RCSB search returned unparseable JSON: {exc}"

    candidates: list[dict[str, Any]] = []
    for entry in payload.get("result_set", []):
        identifier = str(entry.get("identifier", ""))
        pdb_id = identifier.split("_")[0].lower()
        context: dict[str, Any] = {}
        for service in entry.get("services", []):
            if service.get("service_type") != "sequence":
                continue
            for node in service.get("nodes", []):
                for match in node.get("match_context", []):
                    context = match
                    break
        candidates.append({
            "identifier": identifier,
            "pdb_id": pdb_id,
            "search_score": entry.get("score"),
            # Provenance only. RCSB reports identity as a percentage and does
            # not report coverage at all, so both are put on the same 0-1 scale
            # as the locally computed gates rather than sitting beside them in
            # a different unit. They stay ungated: the match context is missing
            # often enough that trusting it would wave candidates through.
            "search_sequence_identity": _as_fraction(
                context.get("sequence_identity")
            ),
            "search_query_coverage": _search_query_coverage(context),
            "search_alignment_length": context.get("alignment_length"),
            "search_evalue": context.get("evalue"),
        })
    return candidates, None


def _usable_opm_structure(data: bytes) -> Optional[str]:
    """Why this payload cannot serve as a donor, or ``None`` if it can.

    Integrity only — whether the bytes are a structure at all. Whether the
    entry carries a bilayer is a scientific question about the donor and is
    answered later, so that a real OPM entry without DUM markers is reported as
    an unusable donor rather than as a broken download.

    Truncation is prevented rather than detected: the download is renamed into
    place, so a process killed mid-write cannot leave a partial file at the
    cache path for later runs to trust. This check still runs on cache hits,
    because a file put there by anything else has made no such promise.
    """
    lines = data.decode("utf-8", errors="ignore").splitlines()
    if not any(line.startswith("ATOM") for line in lines):
        return "no ATOM records"
    return None


def _fetch_opm_structure(
    pdb_id: str, *, cache_dir: Path, timeout_seconds: int
) -> tuple[Optional[Path], Optional[str], Optional[str]]:
    """Return the cached donor path, an error, and the payload's SHA-256."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"opm_{pdb_id}.pdb"
    if target.is_file():
        data = target.read_bytes()
        defect = _usable_opm_structure(data)
        if defect is None:
            return target, None, hashlib.sha256(data).hexdigest()
        # A bad cache entry is a local accident, so replace it rather than
        # reporting it as a property of the OPM entry.
        logger.warning("Discarding cached %s (%s); refetching", target.name, defect)
        target.unlink()
    url = OPM_PDB_URL.format(pdb_id=pdb_id)
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            data = response.read()
    except Exception as exc:  # noqa: BLE001
        return None, f"could not fetch {url}: {type(exc).__name__}: {exc}", None
    defect = _usable_opm_structure(data)
    if defect is not None:
        return None, f"{url} is not a usable OPM entry: {defect}", None
    # Rename into place so a kill between write and use cannot leave a
    # half-written file that later runs would accept.
    partial = target.with_suffix(".part")
    partial.write_bytes(data)
    os.replace(partial, target)
    return target, None, hashlib.sha256(data).hexdigest()


def _align_residues(
    query: list[dict[str, Any]], target: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair residues through a gemmi global sequence alignment.

    Read from the CIGAR rather than the match string: gemmi renders both kinds
    of gap as a space there, so the match string cannot say which sequence the
    gap belongs to, and consuming the wrong one silently shifts every
    correspondence after the first indel. In gemmi's convention ``I`` advances
    the query alone and ``D`` the target alone.
    """
    import re

    import gemmi

    query_seq = _sequence(query)
    target_seq = _sequence(target)
    if not query_seq or not target_seq:
        return []
    alignment = gemmi.align_string_sequences(
        list(query_seq), list(target_seq), []
    )
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    qi = ti = 0
    for count, op in re.findall(r"(\d+)([MIDNSHP=X])", alignment.cigar_str()):
        count = int(count)
        if op in "M=X":
            for _ in range(count):
                if qi < len(query) and ti < len(target):
                    pairs.append((query[qi], target[ti]))
                qi += 1
                ti += 1
        elif op == "I":
            qi += count
        elif op == "D":
            ti += count
    return pairs


def _kabsch_trimmed(
    mobile: list[list[float]],
    reference: list[list[float]],
    *,
    min_retained: int,
    rounds: int = DEFAULT_TRIM_ROUNDS,
    cutoff_multiple: float = DEFAULT_TRIM_CUTOFF_MULTIPLE,
) -> tuple[Any, Any, float, int]:
    """Kabsch fit refined by dropping pairs that deviate far from the median.

    **Not used for membrane transfer.** Trimming picks whichever subset fits
    best, and for two proteins that share a large soluble domain but sit
    differently in the bilayer, that subset can be the soluble domain — which
    would transfer a frame decided by residues that never touch the membrane.
    The transfer restricts the fit to the donor's own bilayer instead. This is
    kept as a helper for comparing superpositions where no membrane is involved.

    Returns the transform, the RMSD over retained pairs, and how many were kept.
    """
    import numpy as np

    P_all = np.asarray(mobile, dtype=float)
    Q_all = np.asarray(reference, dtype=float)
    keep = np.arange(len(P_all))
    rotation, translation, rmsd = _kabsch(mobile, reference)
    for _ in range(max(0, rounds)):
        P, Q = P_all[keep], Q_all[keep]
        rotation, translation, rmsd = _kabsch(P.tolist(), Q.tolist())
        deviations = np.linalg.norm((rotation @ P.T).T + translation - Q, axis=1)
        median = float(np.median(deviations))
        if median <= 1e-9:
            break
        survivors = keep[deviations <= cutoff_multiple * median]
        if len(survivors) < min_retained or len(survivors) == len(keep):
            break
        keep = survivors
    P, Q = P_all[keep], Q_all[keep]
    rotation, translation, rmsd = _kabsch(P.tolist(), Q.tolist())
    return rotation, translation, rmsd, int(len(keep))


def _fit_condition(*point_sets: list[list[float]]) -> float:
    """How well the point clouds pin down a rotation: second/largest spread.

    Kabsch's determinant correction rules out a reflection but says nothing
    about whether the rotation is determined at all. Collinear CA atoms fit at
    RMSD 0 with a proper rotation matrix while the spin about their axis is
    free — and that free spin is then applied to every soluble domain and
    binding partner in the input.

    Rank 2 is enough, which is why this is the *second* singular value and not
    the third. Three or more non-collinear points fix two in-plane basis
    vectors, and the proper-rotation constraint then fixes the normal: a
    perfectly coplanar cloud recovers a known general rotation to 3e-16 here.
    Testing the third value would reject that valid fit. Measured ratios —
    collinear 0.000, coplanar 1.000, a single ideal 40-residue helix 0.095,
    real membrane CA subsets 0.86-0.87. Both clouds are checked; the worse one
    governs.
    """
    import numpy as np

    worst = 1.0
    for points in point_sets:
        P = np.asarray(points, dtype=float)
        if P.ndim != 2 or len(P) < 3:
            return 0.0
        singular = np.linalg.svd(P - P.mean(axis=0), compute_uv=False)
        if singular[0] <= 0.0:
            return 0.0
        worst = min(worst, float(singular[1] / singular[0]))
    return worst


def _kabsch(
    mobile: list[list[float]], reference: list[list[float]]
) -> tuple[Any, Any, float]:
    """Rotation + translation taking ``mobile`` onto ``reference``, with RMSD."""
    import numpy as np

    P = np.asarray(mobile, dtype=float)
    Q = np.asarray(reference, dtype=float)
    if P.ndim != 2 or P.shape != Q.shape or len(P) < 3:
        # Reached only when a caller's own guard is wrong; raising a bare
        # LinAlgError from inside numpy would tell nobody which input was bad.
        raise ValueError(
            f"Kabsch needs at least 3 paired 3-D points, got "
            f"{P.shape} and {Q.shape}"
        )
    p_centre, q_centre = P.mean(axis=0), Q.mean(axis=0)
    Pc, Qc = P - p_centre, Q - q_centre
    U, _, Vt = np.linalg.svd(Pc.T @ Qc)
    handedness = np.sign(np.linalg.det(Vt.T @ U.T))
    rotation = Vt.T @ np.diag([1.0, 1.0, handedness]) @ U.T
    fitted = (rotation @ Pc.T).T
    rmsd = float(np.sqrt(((fitted - Qc) ** 2).sum(axis=1).mean()))
    translation = q_centre - rotation @ p_centre
    return rotation, translation, rmsd


def _dummy_membrane(pdb_file: Path) -> dict[str, Any]:
    zs = [
        float(line[46:54])
        for line in pdb_file.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()
        if line.startswith(("ATOM", "HETATM")) and line[17:20].strip() == "DUM"
    ]
    if not zs:
        return {"count": 0}
    centre = sum(zs) / len(zs)
    return {
        "count": len(zs),
        "center_z": centre,
        "z_min": min(zs),
        "z_max": max(zs),
        "thickness": max(zs) - min(zs),
    }


def _fit_donor_chain(
    query_with_ca: list[dict[str, Any]],
    target_with_ca: list[dict[str, Any]],
    chain_id: str,
    *,
    slab_low: float,
    slab_high: float,
    min_identity: float,
    min_coverage: float,
    min_corresponding_ca: int,
    max_fit_rmsd: float,
    min_fit_condition: float = DEFAULT_MIN_FIT_CONDITION,
) -> dict[str, Any]:
    """Score one donor chain against the query and say whether it clears the gates.

    Every gate is applied here, before any chain can be called the best one.
    Ranking on fit RMSD first and gating afterwards lets a chain that aligns
    over a short, low-identity stretch — which superposes tightly precisely
    because it is short — push aside the chain that actually corresponds to the
    query.

    The numbers come back either way; ``rejected`` is set when a gate fails and
    ``rotation``/``translation`` only when none did.
    """
    record: dict[str, Any] = {"homolog_chain": chain_id, "gates_passed": 0}
    pairs = [
        (q, t)
        for q, t in _align_residues(query_with_ca, target_with_ca)
        if q.get("ca") and t.get("ca")
    ]
    if not pairs:
        record["aligned_ca"] = 0
        record["rejected"] = "no residues aligned"
        return record

    # Identity and coverage over the whole alignment: how well this donor chain
    # matches the query, and how much of the query it accounts for.
    matched = sum(1 for q, t in pairs if q["one"] == t["one"])
    identity = matched / len(pairs)
    coverage = len(pairs) / len(query_with_ca)
    # Fit only on residues the donor places inside its own bilayer. A homolog
    # can share a large soluble domain and still sit differently in the
    # membrane; fitting on everything lets that domain decide the frame, which
    # is the one thing the transfer must not do.
    membrane_pairs = [
        (q, t) for q, t in pairs if slab_low <= t["ca"][2] <= slab_high
    ]
    # Identity over the membrane subset is the one that matters, because that
    # subset is what the frame is fitted to. Two proteins sharing a large
    # soluble domain can clear a whole-chain gate while their membrane domains
    # are unrelated, and then 40 slab CA atoms of coincidentally similar
    # geometry are enough to transfer a frame from a protein that sits in the
    # bilayer differently. Whole-chain identity stays as the coarse filter.
    membrane_matched = sum(1 for q, t in membrane_pairs if q["one"] == t["one"])
    membrane_identity = (
        membrane_matched / len(membrane_pairs) if membrane_pairs else 0.0
    )
    record.update({
        "local_identity": round(identity, 4),
        "local_query_coverage": round(coverage, 4),
        "membrane_identity": round(membrane_identity, 4),
        "aligned_ca": len(pairs),
        "membrane_ca": len(membrane_pairs),
        "fit_condition": None,
        "fit_rmsd": None,
    })

    if identity < min_identity:
        record["rejected"] = f"local identity {identity:.3f} < {min_identity}"
        return record
    record["gates_passed"] = 1
    if coverage < min_coverage:
        record["rejected"] = (
            f"local query coverage {coverage:.3f} < {min_coverage}"
        )
        return record
    record["gates_passed"] = 2
    if len(membrane_pairs) < min_corresponding_ca:
        record["rejected"] = (
            f"only {len(membrane_pairs)} corresponding CA atoms lie inside the "
            f"donor's bilayer (need {min_corresponding_ca})"
        )
        return record
    record["gates_passed"] = 3
    if membrane_identity < min_identity:
        record["rejected"] = (
            f"membrane-subset identity {membrane_identity:.3f} < {min_identity}"
        )
        return record
    record["gates_passed"] = 4

    mobile = [q["ca"] for q, _ in membrane_pairs]
    reference = [t["ca"] for _, t in membrane_pairs]
    condition = _fit_condition(mobile, reference)
    record["fit_condition"] = round(condition, 4)
    if condition < min_fit_condition:
        record["rejected"] = (
            f"fitted CA cloud is degenerate (smallest/largest spread "
            f"{condition:.4f} < {min_fit_condition}); the rotation about that "
            "axis would be arbitrary"
        )
        return record
    record["gates_passed"] = 5

    rotation, translation, rmsd = _kabsch(mobile, reference)
    record["fit_rmsd"] = round(rmsd, 3)
    if rmsd > max_fit_rmsd:
        record["rejected"] = (
            f"membrane-slab fit RMSD {rmsd:.3f} A > {max_fit_rmsd} A"
        )
        return record
    record["gates_passed"] = 6
    record["rotation"] = rotation
    record["translation"] = translation
    return record


def _consider_candidate(
    entry: dict[str, Any],
    *,
    query_with_ca: list[dict[str, Any]],
    cache_dir: Path,
    timeout_seconds: int,
    slab_margin: float,
    min_identity: float,
    min_coverage: float,
    min_corresponding_ca: int,
    max_fit_rmsd: float,
    min_fit_condition: float,
    structure_cache: dict[str, dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], bool]:
    """Fetch one OPM donor and return the chain of it that can carry the frame.

    ``entry`` is annotated in place with the fetch outcome, the donor's DUM
    membrane, and every chain's numbers — including the ones that failed, so a
    rejection can be read without re-running the search. The first element of
    the return value is the acceptable chain with the lowest fit RMSD, or
    ``None``; the second says whether the donor was actually evaluated. A donor
    that could not be downloaded was never judged, and counting it as a gate
    rejection would report an outage as evidence against the candidates.

    Donors are parsed once per PDB id and reused: several query chains of one
    complex routinely hit the same OPM entry.
    """
    pdb_id = entry["pdb_id"]
    parsed = structure_cache.get(pdb_id)
    if parsed is None:
        homolog_path, fetch_error, digest = _fetch_opm_structure(
            pdb_id, cache_dir=cache_dir, timeout_seconds=timeout_seconds
        )
        parsed = {"error": fetch_error, "dummy": {}, "chains": {}, "sha256": digest}
        if homolog_path is not None:
            parsed["error"] = None
            parsed["dummy"] = _dummy_membrane(homolog_path)
            parsed["chains"] = _chain_residues(homolog_path)
        structure_cache[pdb_id] = parsed

    if parsed["error"]:
        entry["rejected"] = parsed["error"]
        entry["fetch_failed"] = True
        return None, False
    entry["opm_url"] = OPM_PDB_URL.format(pdb_id=pdb_id)
    entry["opm_sha256"] = parsed.get("sha256")

    dummy = parsed["dummy"]
    if not dummy.get("count"):
        entry["rejected"] = "OPM structure carried no DUM membrane markers"
        return None, True
    entry["dummy_membrane"] = dummy
    slab_low = float(dummy["z_min"]) - slab_margin
    slab_high = float(dummy["z_max"]) + slab_margin

    scored: list[dict[str, Any]] = []
    for chain_id, residues in parsed["chains"].items():
        target_with_ca = [r for r in residues if r.get("ca")]
        if not target_with_ca:
            continue
        scored.append(_fit_donor_chain(
            query_with_ca, target_with_ca, chain_id,
            slab_low=slab_low, slab_high=slab_high,
            min_identity=min_identity, min_coverage=min_coverage,
            min_corresponding_ca=min_corresponding_ca,
            max_fit_rmsd=max_fit_rmsd, min_fit_condition=min_fit_condition,
        ))
    if not scored:
        entry["rejected"] = "no homolog chain produced a usable alignment"
        return None, True

    entry["homolog_chains"] = [
        {k: v for k, v in record.items() if k not in {"rotation", "translation"}}
        for record in scored
    ]
    accepted = [record for record in scored if "rejected" not in record]
    if accepted:
        best = min(accepted, key=lambda record: record["fit_rmsd"])
    else:
        # Name the chain that got furthest through the gates, so the reason
        # points at the donor chain that came closest rather than a random one.
        best = max(scored, key=lambda record: (
            record["gates_passed"],
            record.get("membrane_ca") or 0,
            record.get("local_identity") or 0.0,
        ))
    entry.update({
        "homolog_chain": best["homolog_chain"],
        "local_identity": best.get("local_identity"),
        "local_query_coverage": best.get("local_query_coverage"),
        "membrane_identity": best.get("membrane_identity"),
        "aligned_ca": best.get("aligned_ca"),
        "membrane_ca": best.get("membrane_ca"),
        "fit_condition": best.get("fit_condition"),
        "fit_rmsd": best.get("fit_rmsd"),
    })
    if not accepted:
        entry["rejected"] = (
            f"chain {best['homolog_chain']}: {best['rejected']}"
            if len(scored) > 1 else best["rejected"]
        )
        return None, True
    return best, True


def _wilson_lower_bound(matched: int, total: int, z: float = 1.96) -> float:
    """Conservative estimate of an identity proportion, given how many residues.

    40 out of 40 is a weaker claim than 198 out of 200, even though the raw
    proportion is higher: it rests on a fifth of the observations. The Wilson
    interval's lower bound says so — 0.91 against 0.96 — where comparing
    proportions directly would rank the sparse match first and let it choose the
    membrane frame.
    """
    if total <= 0:
        return 0.0
    p = matched / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    margin = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total))
    return max(0.0, (centre - margin) / denominator)


def _evidence_rank(entry: dict[str, Any]) -> tuple[float, float]:
    """Ordering over accepted candidates: strongest evidence first.

    RCSB ranks by search relevance, which says nothing about how well a donor
    fixes a membrane frame. Rank instead on how much the membrane
    correspondence is worth — identity discounted by how many residues support
    it — and let fit RMSD separate candidates that claim is unable to tell
    apart.

    Both roundings matter, and both came from live data on 5L7D. Without the
    support discount, a 40/40 membrane match at 2.9 A outranked a 198/200 match
    at 0.1 A. Without rounding the bound to two decimals, a one-residue
    difference (201 against 200) put a sister structure ahead of the query's own
    OPM entry, which superposes at exactly 0.000 A.
    """
    support = int(entry.get("membrane_ca") or 0)
    identity = float(entry.get("membrane_identity") or 0.0)
    bound = _wilson_lower_bound(round(identity * support), support)
    rmsd = entry.get("fit_rmsd")
    return (round(bound, 2), -float(rmsd if rmsd is not None else 1e9))


def _validate_gates(values: dict[str, Any]) -> Optional[str]:
    """Reject thresholds that would disable a gate instead of tightening it.

    These are public arguments, so an out-of-range value has to fail loudly.
    ``min_corresponding_ca=0`` lets an empty pair list reach the superposition,
    and any NaN threshold makes every ``value > threshold`` comparison false —
    which silently accepts a 39 A fit rather than rejecting it.
    """
    def finite(name: str) -> Optional[str]:
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} must be a number, got {value!r}"
        if not math.isfinite(float(value)):
            return f"{name} must be finite, got {value!r}"
        return None

    for name in values:
        problem = finite(name)
        if problem:
            return problem
    for name in ("min_identity", "min_coverage"):
        if not 0.0 <= float(values[name]) <= 1.0:
            return f"{name} must lie in [0, 1], got {values[name]}"
    if not 0.0 <= float(values["min_fit_condition"]) <= 1.0:
        return (
            f"min_fit_condition must lie in [0, 1], got "
            f"{values['min_fit_condition']}"
        )
    if int(values["min_corresponding_ca"]) < 3:
        return (
            "min_corresponding_ca must be at least 3; fewer paired points "
            f"cannot determine a rotation, got {values['min_corresponding_ca']}"
        )
    if float(values["max_fit_rmsd"]) <= 0.0:
        return f"max_fit_rmsd must be positive, got {values['max_fit_rmsd']}"
    if float(values["slab_margin"]) < 0.0:
        return f"slab_margin must not be negative, got {values['slab_margin']}"
    for name in ("max_candidates", "timeout_seconds", "total_budget_seconds"):
        if float(values[name]) <= 0:
            return f"{name} must be positive, got {values[name]}"
    for name in ("max_candidates", "min_corresponding_ca"):
        # A fractional row count reaches RCSB as a malformed page request, so a
        # caller's mistake would come back as a search outage.
        if float(values[name]) != int(values[name]):
            return f"{name} must be a whole number, got {values[name]}"
    if float(values["min_fit_condition"]) <= 0.0:
        return (
            "min_fit_condition must be positive; 0 re-enables the degenerate "
            f"fits this gate exists to reject, got {values['min_fit_condition']}"
        )
    return None


def orient_protein_with_opm_homolog(
    *,
    protein_pdb: Path,
    out_dir: Path,
    min_identity: float = DEFAULT_MIN_IDENTITY,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    min_corresponding_ca: int = DEFAULT_MIN_CORRESPONDING_CA,
    max_fit_rmsd: float = DEFAULT_MAX_FIT_RMSD,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    slab_margin: float = DEFAULT_SLAB_MARGIN,
    min_fit_condition: float = DEFAULT_MIN_FIT_CONDITION,
    total_budget_seconds: float = DEFAULT_TOTAL_BUDGET_SECONDS,
) -> dict:
    """Transfer a membrane frame from the best acceptable OPM homolog.

    Returns the same shape as the other orientation backends. When no candidate
    clears the gates — including when the search cannot be reached at all — this
    is *not* an error: it returns ``success=False`` with ``fallback=True`` and a
    reason, so the caller can move on to a method that needs no database.
    """
    import numpy as np

    result: dict[str, Any] = {
        "success": False,
        "fallback": False,
        "warnings": [],
        "errors": [],
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gates = {
        "slab_margin": slab_margin,
        "min_identity": min_identity,
        "min_coverage": min_coverage,
        "min_corresponding_ca": min_corresponding_ca,
        "max_fit_rmsd": max_fit_rmsd,
        "min_fit_condition": min_fit_condition,
        "max_candidates": max_candidates,
        "timeout_seconds": timeout_seconds,
        "total_budget_seconds": total_budget_seconds,
    }
    report: dict[str, Any] = {
        "query_structure": str(protein_pdb),
        "gates": gates,
        "search_url": RCSB_SEARCH_URL,
        "selection_policy": (
            "every protein chain is searched longest-first; the first chain "
            "with any acceptable candidate wins, and among its candidates the "
            "every searchable chain is judged, then one ranking over every "
            "acceptable (chain, donor) pair decides: the Wilson lower bound of "
            "membrane-subset identity given its residue count, to 2 dp, then "
            "fit RMSD"
        ),
        "query_chains": [],
        "accepted": None,
    }

    def _write_report() -> str:
        path = out_dir / "opm_homolog_search.json"
        path.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        return str(path)

    def _finish(reason: str, *, code: str, fallback: bool = True) -> dict:
        report["outcome"] = reason
        result["opm_homolog_search"] = _write_report()
        result["fallback"] = fallback
        result["code"] = code
        result["fallback_reason"] = reason
        (result["warnings"] if fallback else result["errors"]).append(reason)
        return result

    invalid = _validate_gates(gates)
    if invalid:
        return _finish(invalid, code="opm_homolog_gates_invalid", fallback=False)

    atom_lines, dropped = _first_model_atoms(Path(protein_pdb))
    if dropped["models"] or dropped["altloc_atoms"]:
        note = (
            f"input carried {dropped['models']} models and "
            f"{dropped['altloc_atoms']} alternate-conformation atoms; the fit "
            "and the oriented output both use the first model and the "
            "highest-occupancy conformer"
        )
        report["input_selection"] = dropped
        result["warnings"].append(note)
        logger.warning("%s", note)
    chains = _residues_from_lines(atom_lines)
    if not chains:
        return _finish(
            f"no standard protein residues in {protein_pdb}",
            code="opm_homolog_no_protein", fallback=False,
        )

    # Every protein chain is a query, longest first. In a complex of a large
    # soluble partner with a small membrane subunit, only the short chain has an
    # OPM homolog at all, so searching the longest chain alone would give up on
    # exactly the structures this backend exists for. Copies of one chain share
    # a search but not a fit: identical sequences return identical hits, while
    # their coordinates can differ enough that one copy clears the slab-RMSD
    # gate and another does not.
    queries: list[dict[str, Any]] = []
    for chain_id, residues in sorted(
        chains.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        record: dict[str, Any] = {
            "chain": chain_id,
            "residues": len(residues),
            "residues_with_ca": sum(1 for r in residues if r.get("ca")),
            "outcome": "not_searched",
            "search_error": None,
            "candidates": [],
        }
        queries.append(
            {"record": record, "sequence": _sequence(residues),
             "residues": residues}
        )
    report["query_chains"] = [query["record"] for query in queries]

    search_cache: dict[str, tuple[list[dict[str, Any]], Optional[str]]] = {}
    structure_cache: dict[str, dict[str, Any]] = {}
    search_errors: list[str] = []
    completed_searches: list[str] = []
    accepted: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    unjudged_chains: list[str] = []
    evaluated_candidates = 0
    unjudged_candidates = 0
    fetch_failures = 0
    searched_chains = 0
    deadline = time.monotonic() + float(total_budget_seconds)

    def _remaining() -> float:
        return deadline - time.monotonic()

    def _request_timeout() -> float:
        # No floor: a grace period would let a request finish past the budget
        # the caller set, which makes a short explicit budget non-binding.
        return min(float(timeout_seconds), max(0.001, _remaining()))

    for query in queries:
        record = query["record"]
        query_with_ca = [r for r in query["residues"] if r.get("ca")]
        if len(query_with_ca) < min_corresponding_ca:
            record["skipped"] = (
                f"{len(query_with_ca)} residues with a CA is below the "
                f"{min_corresponding_ca} the membrane fit needs"
            )
            continue
        if _remaining() <= 0:
            record["skipped"] = "the OPM time budget was already exhausted"
            unjudged_chains.append(record["chain"])
            continue

        cached = search_cache.get(query["sequence"])
        if cached is None:
            searched_chains += 1
            cached = _search_opm_homologs(
                query["sequence"],
                max_candidates=max_candidates,
                min_identity=min_identity,
                timeout_seconds=_request_timeout(),
            )
            search_cache[query["sequence"]] = cached
        else:
            record["search_reused_from_identical_sequence"] = True
        candidates, search_error = cached

        if search_error:
            # One chain being unreachable says nothing about the others, and in
            # a complex the chain that has a homolog is often a later one. Keep
            # going; only an outage on every chain ends the search.
            record["outcome"] = "search_error"
            record["search_error"] = search_error
            search_errors.append(f"chain {record['chain']}: {search_error}")
            unjudged_chains.append(record["chain"])
            continue
        completed_searches.append(record["chain"])
        if not candidates:
            record["outcome"] = "no_match"
            continue

        record["outcome"] = "rejected"
        for candidate in candidates:
            if _remaining() <= 0:
                skipped = len(candidates) - len(record["candidates"])
                record["truncated"] = (
                    f"the OPM time budget ran out with {skipped} candidate(s) "
                    "of this chain unjudged"
                )
                unjudged_candidates += skipped
                break
            entry: dict[str, Any] = dict(candidate)
            entry["query_chain"] = record["chain"]
            record["candidates"].append(entry)
            best, evaluated = _consider_candidate(
                entry,
                query_with_ca=query_with_ca,
                cache_dir=out_dir / "opm_cache",
                timeout_seconds=_request_timeout(),
                slab_margin=slab_margin,
                min_identity=min_identity,
                min_coverage=min_coverage,
                min_corresponding_ca=min_corresponding_ca,
                max_fit_rmsd=max_fit_rmsd,
                min_fit_condition=min_fit_condition,
                structure_cache=structure_cache,
            )
            if evaluated:
                evaluated_candidates += 1
            else:
                fetch_failures += 1
            if best is not None:
                accepted.append((record, entry, best))
                record["accepted_candidates"] = (
                    record.get("accepted_candidates", 0) + 1
                )

    # Every searchable chain has been tried, so the winner is chosen across all
    # of them on evidence rather than by which chain happened to come first.
    # A long membrane-associated partner with a barely acceptable relative must
    # not set the frame while the actual transmembrane subunit goes unused.
    incomplete = bool(
        unjudged_candidates or unjudged_chains or fetch_failures
    )
    if accepted:
        ranked = sorted(
            accepted, key=lambda item: _evidence_rank(item[1]), reverse=True
        )
        report["ranking"] = [
            {
                "query_chain": item["query_chain"],
                "pdb_id": item["pdb_id"],
                "homolog_chain": item.get("homolog_chain"),
                "membrane_identity": item.get("membrane_identity"),
                "membrane_ca": item.get("membrane_ca"),
                "fit_rmsd": item.get("fit_rmsd"),
            }
            for _, item, _ in ranked
        ]
        record, entry, best = ranked[0]

        dummy = entry["dummy_membrane"]
        rotation, translation = best["rotation"], best["translation"]
        shift = float(dummy.get("center_z") or 0.0)
        oriented = out_dir / "oriented_protein.pdb"
        lines: list[str] = []
        for line in atom_lines:
            if line.startswith("TER"):
                lines.append(line)      # a chain break carries no coordinates
                continue
            try:
                point = np.array([
                    float(line[30:38]), float(line[38:46]), float(line[46:54])
                ])
            except ValueError:
                continue
            moved = rotation @ point + translation
            lines.append(
                f"{line[:30]}{moved[0]:8.3f}{moved[1]:8.3f}"
                f"{moved[2] - shift:8.3f}{line[54:]}"
            )
        if not any(line.startswith(("ATOM", "HETATM")) for line in lines):
            return _finish(
                "input had no transformable atom records",
                code="opm_homolog_no_protein", fallback=False,
            )
        oriented.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")

        entry["accepted"] = True
        entry["transform"] = {
            "rotation": [[float(v) for v in row] for row in rotation],
            "translation": [float(v) for v in translation],
            "membrane_center_shift_z": shift,
        }
        record["outcome"] = "accepted"
        report["accepted"] = entry
        report["outcome"] = "accepted"
        report["evaluation_complete"] = not incomplete
        if incomplete:
            # The donor cleared every gate, so the frame is defensible and worth
            # more than dropping to a different method entirely. What is not
            # defensible is calling it the best when part of the field was never
            # looked at, so say so rather than implying a complete comparison.
            note = (
                f"chose among {len(accepted)} acceptable donor(s), but "
                f"{unjudged_candidates} candidate(s) and "
                f"{len(unjudged_chains)} chain(s) went unjudged and "
                f"{fetch_failures} could not be downloaded; a better donor may "
                "exist among them"
            )
            result["warnings"].append(note)
            logger.warning("%s", note)
        result.update({
            "success": True,
            "oriented_pdb": str(oriented),
            "membrane_center_z": 0.0,
            "evaluation_complete": not incomplete,
            "opm_homolog": {
                "pdb_id": entry["pdb_id"],
                "identifier": entry["identifier"],
                "homolog_chain": best["homolog_chain"],
                "query_chain": record["chain"],
                "query_chains_searched": searched_chains,
                "candidates_evaluated": evaluated_candidates,
                "accepted_candidates": len(accepted),
                "evaluation_complete": not incomplete,
                "local_identity": entry["local_identity"],
                "local_query_coverage": entry["local_query_coverage"],
                "membrane_identity": entry["membrane_identity"],
                "aligned_ca": best["aligned_ca"],
                "membrane_ca": best["membrane_ca"],
                "fit_condition": best["fit_condition"],
                "fit_rmsd": best["fit_rmsd"],
                "hydrophobic_thickness": dummy.get("thickness"),
                "search_sequence_identity": entry.get(
                    "search_sequence_identity"
                ),
                "search_query_coverage": entry.get("search_query_coverage"),
                "opm_url": entry["opm_url"],
                "opm_sha256": entry.get("opm_sha256"),
                "transform": entry["transform"],
            },
        })
        result["opm_homolog_search"] = _write_report()
        logger.info(
            "Transferred membrane frame from OPM %s chain %s onto query "
            "chain %s (membrane identity %.2f, %d membrane CA, fit RMSD %.2f A; "
            "%d of %d candidate(s) acceptable across %d chain(s))",
            entry["pdb_id"], best["homolog_chain"], record["chain"],
            best["membrane_identity"], best["membrane_ca"], best["fit_rmsd"],
            len(accepted), evaluated_candidates, searched_chains,
        )
        return result

    report["evaluation_complete"] = not incomplete
    # Nothing was accepted. Only report a verdict if the field was actually
    # judged: an agent branches on the code, and "rejected"/"no_match" tell it
    # the donors were examined and found wanting, so it will not retry what was
    # in fact an outage. The most specific true cause wins.
    if not searched_chains:
        return _finish(
            f"no protein chain has the {min_corresponding_ca} CA atoms the "
            "membrane fit needs, so nothing was searched",
            code="opm_homolog_no_match",
        )
    shown = "; ".join(search_errors[:3])
    if len(search_errors) > 3:
        shown += f" (+{len(search_errors) - 3} more)"
    if _remaining() <= 0 and (unjudged_candidates or unjudged_chains):
        return _finish(
            f"the OPM backend's {total_budget_seconds:.0f}s budget ran out "
            f"with {unjudged_candidates} candidate(s) and "
            f"{len(unjudged_chains)} chain(s) unjudged",
            code="opm_homolog_budget_exhausted",
        )
    if not completed_searches:
        return _finish(
            f"no query chain could be searched ({shown})",
            code="opm_homolog_search_unavailable",
        )
    if fetch_failures and not evaluated_candidates:
        return _finish(
            f"none of the {fetch_failures} OPM candidate(s) could be "
            "downloaded, so no donor was judged",
            code="opm_homolog_fetch_unavailable",
        )
    if incomplete:
        parts = []
        if evaluated_candidates:
            parts.append(f"{evaluated_candidates} candidate(s) failed the gates")
        if fetch_failures:
            parts.append(f"{fetch_failures} could not be downloaded")
        if unjudged_candidates:
            parts.append(f"{unjudged_candidates} were never judged")
        if unjudged_chains:
            parts.append(f"{len(unjudged_chains)} chain(s) were never searched")
        return _finish(
            "no donor was accepted and the field was not fully judged: "
            + ", ".join(parts) + (f" ({shown})" if shown else ""),
            code="opm_homolog_evaluation_incomplete",
        )
    if evaluated_candidates:
        return _finish(
            f"all {evaluated_candidates} OPM candidate(s) judged for "
            f"{searched_chains} query chain(s) failed the quality gates",
            code="opm_homolog_rejected",
        )
    return _finish(
        "no OPM-annotated structure matched any of the "
        f"{searched_chains} query chain(s)",
        code="opm_homolog_no_match",
    )

def transfer_angle_between(normal_a: list[float], normal_b: list[float]) -> float:
    """Angle in degrees between two membrane normals, for cross-checking."""
    import numpy as np

    a = np.asarray(normal_a, dtype=float)
    b = np.asarray(normal_b, dtype=float)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 0.0:
        return float("nan")
    return math.degrees(math.acos(min(1.0, abs(float(a @ b) / denominator))))
