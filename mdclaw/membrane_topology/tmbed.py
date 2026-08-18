"""Membrane topology prediction with TMbed.

TMbed (Bernhofer & Rost, BMC Bioinformatics 2022) labels every residue of a
sequence as transmembrane helix, transmembrane strand, signal peptide, or
non-membrane inside/outside, using ProtT5 embeddings plus a CNN, a Gaussian
filter and a Viterbi decoder. The decoder enforces that the inside/outside
assignment flips after every membrane crossing, so the topology it returns is
internally consistent by construction.

Why this matters here: MEMEMBED and PPM infer both the membrane slab *and* the
up/down direction from the 3D structure, and a large soluble domain can pull
that inference the wrong way — a receptor whose N-terminal domain is
extracellular can come out inserted upside down. TMbed works from sequence, so
it is unaffected by the extramembrane bulk, and supplies exactly the two things
the structure-based methods guess at: which residues cross the bilayer, and
which side everything else is on.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from mdclaw._common import ensure_directory, tail_for_agent  # noqa: E402
from mdclaw.sidechain_packer import PROTEIN_RESNAME_TO_ONE  # noqa: E402

# TMbed per-residue classes in ``--out-format 1`` (undirected segments plus the
# inside/outside assignment for everything that is not a membrane segment).
TMBED_TMH = "H"
TMBED_TMB = "B"
TMBED_SIGNAL = "S"
TMBED_INSIDE = "i"
TMBED_OUTSIDE = "o"
MEMBRANE_CLASSES = frozenset({TMBED_TMH, TMBED_TMB})

DEFAULT_MIN_SEGMENT_LENGTH = 5
TMBED_MODEL_DIR_ENV = "MDCLAW_TMBED_MODEL_DIR"


def _tmbed_model_dir(explicit: Optional[str]) -> Optional[str]:
    """Resolve the TMbed model directory (bundled in the image by default)."""
    for candidate in (explicit, os.environ.get(TMBED_MODEL_DIR_ENV)):
        if candidate and Path(candidate).is_dir():
            return str(Path(candidate).resolve())
    return None


def _chain_sequences(structure_file: Path) -> list[dict[str, Any]]:
    """Read one-letter sequences plus author residue numbers per protein chain."""
    chains: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, int, str]] = set()
    for line in Path(structure_file).read_text(
        encoding="utf-8", errors="ignore"
    ).splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        one = PROTEIN_RESNAME_TO_ONE.get(resname)
        if one is None:
            continue
        try:
            resseq = int(line[22:26])
        except ValueError:
            continue
        chain_id = line[21].strip() or "A"
        key = (chain_id, resseq, line[26].strip())
        if key in seen:
            continue
        seen.add(key)
        entry = chains.setdefault(chain_id, {"chain": chain_id, "sequence": "", "resseq": []})
        entry["sequence"] += one
        entry["resseq"].append(resseq)
    return [entry for entry in chains.values() if len(entry["sequence"]) >= 10]


def _write_fasta(path: Path, records: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    for record in records:
        lines.append(f">{record['chain']}")
        lines.append(record["sequence"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_tmbed_prediction(text: str) -> dict[str, str]:
    """Parse TMbed's FASTA-like prediction file into ``{id: label_string}``.

    TMbed writes a header, the sequence, and then the per-residue prediction.
    Some formats add further lines; the prediction is the last line of a block
    that has the same length as the sequence and uses only class characters.
    """
    blocks: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            current = line[1:].strip().split()[0]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)

    allowed = set("BbHhSio.")
    predictions: dict[str, str] = {}
    for name, body in blocks.items():
        if not body:
            continue
        sequence = body[0]
        for candidate in reversed(body[1:] or body):
            if len(candidate) == len(sequence) and set(candidate) <= allowed:
                predictions[name] = candidate
                break
    return predictions


def _segments_from_labels(
    labels: str,
    resseq: list[int],
    *,
    min_segment_length: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split per-residue labels into membrane segments and sided non-TM regions."""
    runs: list[tuple[str, int, int]] = []
    for index, label in enumerate(labels):
        canonical = label.upper() if label.upper() in MEMBRANE_CLASSES else label
        if runs and runs[-1][0] == canonical:
            runs[-1] = (canonical, runs[-1][1], index)
        else:
            runs.append((canonical, index, index))

    segments: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for canonical, start, end in runs:
        if start >= len(resseq) or end >= len(resseq):
            continue
        first, last = resseq[start], resseq[end]
        length = end - start + 1
        if canonical in MEMBRANE_CLASSES:
            if length < min_segment_length:
                continue
            segments.append({
                "start": first,
                "end": last,
                "length": length,
                "kind": "helix" if canonical == TMBED_TMH else "strand",
            })
        elif canonical in {TMBED_INSIDE, TMBED_OUTSIDE}:
            regions.append({
                "start": first,
                "end": last,
                "length": length,
                "side": "in" if canonical == TMBED_INSIDE else "out",
            })
    return segments, regions


def predict_membrane_topology(
    structure_file: Optional[str] = None,
    sequence: Optional[str] = None,
    fasta_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    model_dir: Optional[str] = None,
    use_gpu: bool = True,
    min_segment_length: int = DEFAULT_MIN_SEGMENT_LENGTH,
) -> dict:
    """Predict transmembrane segments and inside/outside topology with TMbed.

    Supply a structure (author residue numbering is carried through to the
    result so downstream membrane tools can address the same residues), a raw
    sequence, or a FASTA file.

    Returns a dict with ``chains``, each holding ``labels`` (per-residue TMbed
    classes), ``segments`` (membrane-crossing residue ranges) and ``regions``
    (non-membrane stretches labelled ``in``/``out``), plus the top-level
    ``n_terminal_side`` that ``embed_in_membrane`` accepts directly.
    """
    result: dict[str, Any] = {
        "success": False,
        "chains": [],
        "n_terminal_side": None,
        "is_transmembrane": False,
        "backend": "tmbed",
        "code": None,
        "errors": [],
        "warnings": [],
    }

    provided = [value for value in (structure_file, sequence, fasta_file) if value]
    if len(provided) != 1:
        result["code"] = "membrane_topology_input_invalid"
        result["errors"].append(
            "Provide exactly one of structure_file, sequence, or fasta_file."
        )
        return result

    out_dir = ensure_directory(Path(output_dir) if output_dir else Path("."))

    records: list[dict[str, Any]]
    if structure_file:
        path = Path(structure_file).expanduser().resolve()
        if not path.is_file():
            result["code"] = "file_not_found"
            result["errors"].append(f"structure_file not found: {structure_file}")
            return result
        records = _chain_sequences(path)
        if not records:
            result["code"] = "membrane_topology_no_protein"
            result["errors"].append(
                "No protein chain with at least 10 standard residues in "
                f"{structure_file}"
            )
            return result
    elif sequence:
        clean = "".join(str(sequence).split()).upper()
        records = [{"chain": "A", "sequence": clean, "resseq": list(range(1, len(clean) + 1))}]
    else:
        path = Path(fasta_file).expanduser().resolve()
        if not path.is_file():
            result["code"] = "file_not_found"
            result["errors"].append(f"fasta_file not found: {fasta_file}")
            return result
        records = []
        name, buffer = None, ""
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith(">"):
                if name is not None and buffer:
                    records.append({"chain": name, "sequence": buffer,
                                    "resseq": list(range(1, len(buffer) + 1))})
                name, buffer = line[1:].strip().split()[0] if line[1:].strip() else "A", ""
            elif line:
                buffer += line.upper()
        if name is not None and buffer:
            records.append({"chain": name, "sequence": buffer,
                            "resseq": list(range(1, len(buffer) + 1))})
        if not records:
            result["code"] = "membrane_topology_input_invalid"
            result["errors"].append(f"No sequences parsed from {fasta_file}")
            return result

    if not shutil.which("python") and not sys.executable:
        result["code"] = "tmbed_unavailable"
        result["errors"].append("No Python interpreter available to run TMbed.")
        return result

    fasta_path = out_dir / "membrane_topology_input.fasta"
    pred_path = out_dir / "membrane_topology.pred"
    _write_fasta(fasta_path, records)

    cmd = [
        sys.executable, "-m", "tmbed", "predict",
        "-f", str(fasta_path),
        "-p", str(pred_path),
        "--out-format", "1",
    ]
    resolved_model_dir = _tmbed_model_dir(model_dir)
    if resolved_model_dir:
        cmd += ["-m", resolved_model_dir]
    cmd.append("--use-gpu" if use_gpu else "--no-use-gpu")

    logger.info("Predicting membrane topology with TMbed: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as exc:
        result["code"] = "tmbed_unavailable"
        result["errors"].append(f"TMbed could not be launched: {exc}")
        return result

    if proc.returncode != 0 or not pred_path.is_file():
        unavailable = "No module named" in (proc.stderr or "")
        result["code"] = "tmbed_unavailable" if unavailable else "tmbed_failed"
        result["errors"].append(
            ("TMbed is not installed in this runtime; membrane topology "
             "prediction is unavailable. "
             if unavailable else "TMbed prediction failed. ")
            + "stderr tail: " + tail_for_agent(proc.stderr)
        )
        return result

    predictions = _parse_tmbed_prediction(
        pred_path.read_text(encoding="utf-8", errors="ignore")
    )
    if not predictions:
        result["code"] = "tmbed_output_unparsed"
        result["errors"].append(
            f"Could not parse any per-residue prediction from {pred_path}"
        )
        return result

    all_segments: list[dict[str, Any]] = []
    all_regions: list[dict[str, Any]] = []
    for record in records:
        labels = predictions.get(record["chain"])
        if labels is None or len(labels) != len(record["sequence"]):
            result["warnings"].append(
                f"chain {record['chain']}: no usable TMbed prediction; skipped"
            )
            continue
        segments, regions = _segments_from_labels(
            labels, record["resseq"], min_segment_length=min_segment_length
        )
        for entry in segments:
            entry["chain"] = record["chain"]
        for entry in regions:
            entry["chain"] = record["chain"]
        all_segments += segments
        all_regions += regions
        result["chains"].append({
            "chain": record["chain"],
            "length": len(record["sequence"]),
            "labels": labels,
            "segments": segments,
            "regions": regions,
        })

    if not result["chains"]:
        result["code"] = "tmbed_output_unparsed"
        result["errors"].append("TMbed returned no usable prediction for any chain")
        return result

    first = result["chains"][0]
    leading = [r for r in first["regions"] if r["start"] == min(
        entry["start"] for entry in first["regions"])] if first["regions"] else []
    result["segments"] = all_segments
    result["regions"] = all_regions
    result["is_transmembrane"] = bool(all_segments)
    result["n_terminal_side"] = leading[0]["side"] if leading else None
    result["prediction_file"] = str(pred_path)

    topology_file = out_dir / "membrane_topology.json"
    payload = {
        "backend": "tmbed",
        "n_terminal_side": result["n_terminal_side"],
        "is_transmembrane": result["is_transmembrane"],
        "segments": all_segments,
        "regions": all_regions,
        "chains": result["chains"],
    }
    topology_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    result["membrane_topology_file"] = str(topology_file)
    result["success"] = True

    if not all_segments:
        result["warnings"].append(
            "TMbed predicted no transmembrane segment; this does not look like "
            "a membrane protein."
        )
    return result
