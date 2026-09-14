"""Membrane orientation with PPM3 (the code behind the OPM database).

PPM integrates a per-atom transfer free energy through an anisotropic solvation
profile and minimises it over the protein's rigid-body placement *and* the
bilayer thickness. MEMEMBED, by contrast, sums a per-residue statistical score
over fixed 1 A depth bins with the thickness pinned at +/-17.5 A.

Do not read published agreement with OPM as accuracy: OPM's entries are PPM
output, so scoring PPM against OPM scores it against itself. Against PDBTM,
which uses the independent TMDET algorithm, PPM3 lands 6.8 degrees from the
reference on 5L7D and MEMEMBED 8.8 — but the two reference databases disagree
with each other by 5.9 degrees, so that gap does not separate them. PPM is used
here for what it does differently: it fits the bilayer thickness instead of
assuming one, and it is deterministic where MEMEMBED runs a genetic algorithm.

The binary is ``immers``, bundled with packmol-memgen. It is driven entirely
through stdin — there are no command-line arguments — and it writes its output
beside the input under a fixed name, so it has to run in its own directory with
``res.lib`` copied in.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from mdclaw._common import tail_for_agent  # noqa: E402

PPM3_BINARY = "immers"
PPM3_INPUT_PDB = "ppm3tmp.pdb"
PPM3_OUTPUT_PDB = "ppm3tmpout.pdb"
PPM3_RESOURCE = "res.lib"
# PPM3 requires a topology value; this is its own convention, used only when the
# caller did not state one, and always reported as assumed.
PPM3_DEFAULT_SIDE = "out"
DEFAULT_PPM3_TIMEOUT_SECONDS = 3600
# The known-bad build prints the orientation it just computed through a FORMAT
# descriptor missing a comma and dies there, leaving no output PDB behind.
PPM3_FORMAT_BUG_MARKER = "Missing comma between descriptors"


def _ppm3_resource_dir() -> Optional[Path]:
    """Locate packmol-memgen's bundled ppm3 directory (holds ``res.lib``)."""
    try:
        import packmol_memgen
    except ImportError:
        return None
    candidate = Path(packmol_memgen.__file__).parent / "lib" / "ppm3"
    return candidate if candidate.is_dir() else None


def _chain_ids(pdb_file: Path) -> list[str]:
    seen: list[str] = []
    for line in pdb_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("ATOM"):
            chain = line[21]
            if chain not in seen:
                seen.append(chain)
    return seen


def _dummy_membrane(lines: list[str]) -> dict[str, Any]:
    zs = [
        float(line[46:54])
        for line in lines
        if line.startswith(("ATOM", "HETATM")) and line[17:20].strip() == "DUM"
    ]
    if not zs:
        return {"count": 0}
    center = sum(zs) / len(zs)
    return {
        "count": len(zs),
        "center_z": center,
        "z_min": min(zs),
        "z_max": max(zs),
        "thickness": max(zs) - min(zs),
    }


def _carry_input_atoms_into_ppm_frame(
    input_pdb: Path, oriented_heavy_lines: list[str], max_rmsd: float = 0.5
) -> tuple[Optional[list[str]], dict[str, Any]]:
    """Apply the rigid transform PPM3 used to every atom of the input file.

    Heavy atoms are matched by chain, residue number, insertion code and atom
    name. Returns ``(lines, fit)``; ``lines`` is ``None`` when fewer than three
    atoms match or the fit is not rigid (PPM3 is a rigid re-orientation, so a
    poor fit means the output is not the input and must not be trusted).
    """
    def key(line):
        return (line[21], line[22:26].strip(), line[26].strip(), line[12:16].strip())

    def xyz(line):
        return [float(line[30:38]), float(line[38:46]), float(line[46:54])]

    reference = {}
    for line in oriented_heavy_lines:
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 54:
            reference.setdefault(key(line), xyz(line))
    input_lines = Path(input_pdb).read_text(encoding="utf-8", errors="ignore").splitlines()
    mobile, target = [], []
    for line in input_lines:
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 54 and key(line) in reference:
            mobile.append(xyz(line))
            target.append(reference[key(line)])
    if len(mobile) < 3:
        return None, {"matched_atoms": len(mobile), "reason": "fewer than 3 atoms matched"}
    from mdclaw.solvation.opm_orient import _kabsch

    rotation, translation, rmsd = _kabsch(mobile, target)
    fit = {"matched_atoms": len(mobile), "rmsd": round(float(rmsd), 4)}
    if rmsd > max_rmsd:
        fit["reason"] = f"fit RMSD {rmsd:.2f} A is not a rigid re-orientation"
        return None, fit
    import numpy as np

    out = []
    for line in input_lines:
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 54:
            moved = rotation @ np.array(xyz(line)) + translation
            out.append(f"{line[:30]}{moved[0]:8.3f}{moved[1]:8.3f}{moved[2]:8.3f}{line[54:]}")
        elif line.startswith("TER"):
            out.append(line)
    return out, fit


def orient_protein_with_ppm(
    *,
    protein_pdb: Path,
    out_dir: Path,
    n_terminal_side: Optional[str] = None,
    keep_ligands: bool = True,
    timeout_seconds: int = DEFAULT_PPM3_TIMEOUT_SECONDS,
) -> dict:
    """Orient a protein into the membrane frame with PPM3.

    Mirrors the return contract of the other orientation backends so the caller
    can swap between them.
    """
    result: dict[str, Any] = {"success": False, "warnings": [], "errors": []}
    binary = shutil.which(PPM3_BINARY)
    if not binary:
        result["code"] = "ppm3_unavailable"
        result["errors"].append(
            f"{PPM3_BINARY} not found in PATH; orient from an OPM homolog or "
            "with MEMEMBED instead."
        )
        return result

    resource_dir = _ppm3_resource_dir()
    if resource_dir is None or not (resource_dir / PPM3_RESOURCE).is_file():
        result["code"] = "ppm3_resources_missing"
        result["errors"].append(
            f"PPM3 needs {PPM3_RESOURCE} from packmol-memgen's ppm3 directory, "
            "which is not present in this runtime."
        )
        return result

    protein_pdb = Path(protein_pdb).resolve()
    chains = _chain_ids(protein_pdb)
    if not chains:
        result["code"] = "ppm3_no_protein_atoms"
        result["errors"].append(f"No ATOM records in {protein_pdb}")
        return result

    # PPM3 writes beside its input under a fixed name, so give it a scratch dir.
    work_dir = Path(out_dir) / "ppm3"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(protein_pdb, work_dir / PPM3_INPUT_PDB)
    shutil.copy(resource_dir / PPM3_RESOURCE, work_dir / PPM3_RESOURCE)

    # PPM3 always needs a value here, but an unstated side must not be dressed
    # up as a decision: assuming "out" would silently pick a leaflet for the
    # caller, and inserting a protein upside down is exactly the failure this
    # code exists to avoid. Use PPM's own convention and say that we did.
    requested_side = (n_terminal_side or "").strip().lower()
    side_is_assumed = requested_side not in {"in", "out"}
    side = PPM3_DEFAULT_SIDE if side_is_assumed else requested_side
    if side_is_assumed:
        result["warnings"].append(
            "n_terminal_side was not given, so which leaflet the N-terminus "
            f"faces is undetermined; PPM3 was run with its own '{side}' "
            "convention and the resulting up/down assignment is unverified."
        )
    stdin_script = "\n".join([
        "2",                                     # input mode: single PDB
        "yes" if keep_ligands else "no",         # keep heteroatoms
        PPM3_INPUT_PDB,
        "1",                                     # one bilayer
        "    ",                                  # default membrane type
        "planar",
        side,                                    # which side the N-terminus faces
        ",".join(chains),
    ]) + "\n"
    (work_dir / "ppm.inp").write_text(stdin_script, encoding="utf-8")

    logger.info("Orienting %s with PPM3 (n_terminal_side=%s)", protein_pdb.name, side)
    try:
        proc = subprocess.run(
            [binary],
            cwd=str(work_dir),
            input=stdin_script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        result["code"] = "ppm3_timeout"
        result["errors"].append(
            f"PPM3 did not finish within {timeout_seconds}s."
        )
        return result

    log_text = proc.stdout or ""
    (work_dir / "ppm3.log").write_text(log_text, encoding="utf-8")

    raw_output = work_dir / PPM3_OUTPUT_PDB
    if not raw_output.is_file():
        if PPM3_FORMAT_BUG_MARKER in log_text:
            result["code"] = "ppm3_format_bug"
            result["errors"].append(
                "This PPM3 build computes the orientation and then crashes "
                "printing it: opm.f has a FORMAT descriptor missing a comma. "
                "Rebuild immers from the patched source (the container does "
                "this at build time)."
            )
            return result
        result["code"] = "ppm3_no_output"
        result["errors"].append(
            "PPM3 wrote no oriented PDB. log tail: " + tail_for_agent(log_text)
        )
        return result

    oriented_lines = raw_output.read_text(encoding="utf-8", errors="ignore").splitlines()
    dummy = _dummy_membrane(oriented_lines)
    kept = [
        line
        for line in oriented_lines
        if line.startswith(("ATOM", "HETATM")) and line[17:20].strip() != "DUM"
    ]
    if not kept:
        result["code"] = "ppm3_empty_output"
        result["errors"].append("PPM3 output had no solute atoms after removing DUM")
        return result

    # PPM already centres the bilayer on z = 0; shift only if this build did not.
    center_z = float(dummy.get("center_z") or 0.0)
    if abs(center_z) > 1e-6:
        kept = [
            f"{line[:46]}{float(line[46:54]) - center_z:8.3f}{line[54:]}"
            for line in kept
        ]

    # PPM3 writes heavy atoms only. Recover the rigid transform it applied from
    # the atoms it kept and move the *input* file, hydrogens and all, the same
    # way (6ME3: the heavy-atom file reached the exact net-charge evaluation,
    # which built no template for a PRO "missing 7 H atoms" and failed the
    # solv node three times).
    heavy_only = Path(out_dir) / "ppm3_oriented_heavy.pdb"
    heavy_only.write_text("\n".join(kept) + "\nEND\n", encoding="utf-8")
    oriented = Path(out_dir) / "oriented_protein.pdb"
    carried, input_fit = _carry_input_atoms_into_ppm_frame(protein_pdb, kept)
    if carried is None:
        result["warnings"].append(
            "PPM3's oriented atoms could not be matched to the input "
            f"({input_fit.get('reason')}); the oriented file carries PPM3's heavy "
            "atoms only and hydrogens were lost."
        )
        oriented.write_text("\n".join(kept) + "\nEND\n", encoding="utf-8")
    else:
        oriented.write_text("\n".join(carried) + "\nEND\n", encoding="utf-8")

    result.update({
        "success": True,
        "oriented_pdb": str(oriented),
        "membrane_center_z": 0.0,
        "ppm": {
            "input_fit": input_fit,
            "heavy_atom_output": str(heavy_only),
            "n_terminal_side": side,
            "n_terminal_side_requested": n_terminal_side,
            "n_terminal_side_assumed": side_is_assumed,
            "chains": chains,
            "dummy_membrane": dummy,
            "hydrophobic_thickness": dummy.get("thickness"),
            "log_file": str(work_dir / "ppm3.log"),
        },
    })
    logger.info(
        "PPM3 oriented %s; bilayer thickness %.1f A",
        protein_pdb.name, float(dummy.get("thickness") or 0.0),
    )
    return result
