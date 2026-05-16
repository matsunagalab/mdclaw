"""Side-chain packing backend adapter.

The public workflow uses HPacker for mutation-sidechain reconstruction and
surrogate candidate side-chain packing.  This module isolates HPacker's API and
keeps PDB normalization/validation close to the backend boundary.
"""

from __future__ import annotations

import importlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


AA_ONE_TO_THREE = {
    "A": "ALA",
    "C": "CYS",
    "D": "ASP",
    "E": "GLU",
    "F": "PHE",
    "G": "GLY",
    "H": "HIS",
    "I": "ILE",
    "K": "LYS",
    "L": "LEU",
    "M": "MET",
    "N": "ASN",
    "P": "PRO",
    "Q": "GLN",
    "R": "ARG",
    "S": "SER",
    "T": "THR",
    "V": "VAL",
    "W": "TRP",
    "Y": "TYR",
}
AA_THREE_TO_ONE = {v: k for k, v in AA_ONE_TO_THREE.items()}
PROTEIN_VARIANT_TO_STANDARD = {
    "HID": "HIS",
    "HIE": "HIS",
    "HIP": "HIS",
    "HSD": "HIS",
    "HSE": "HIS",
    "HSP": "HIS",
    "CYX": "CYS",
    "CYM": "CYS",
    "ASH": "ASP",
    "GLH": "GLU",
    "LYN": "LYS",
}
PROTEIN_RESNAME_TO_ONE = dict(AA_THREE_TO_ONE)
PROTEIN_RESNAME_TO_ONE.update(
    {variant: AA_THREE_TO_ONE[standard] for variant, standard in PROTEIN_VARIANT_TO_STANDARD.items()}
)
STANDARD_AA = set(PROTEIN_RESNAME_TO_ONE)


class HPackerUnavailableError(RuntimeError):
    """Raised when HPacker cannot be imported."""


class HPackerExecutionError(RuntimeError):
    """Raised when HPacker fails or emits invalid output."""


@dataclass(frozen=True)
class ProteinResidue:
    chain: str
    resseq: int
    icode: str
    resname: str
    index: int

    @property
    def hpacker_id(self) -> tuple[str, int, str]:
        return (self.chain or " ", self.resseq, self.icode or " ")

    @property
    def one_letter(self) -> str:
        return PROTEIN_RESNAME_TO_ONE.get(self.resname, "X")


@dataclass
class HPackerRunResult:
    success: bool
    output_path: str | None = None
    mutation_specs: list[str] = field(default_factory=list)
    mutation_map: dict[str, str] = field(default_factory=dict)
    repack_radius_angstrom: float | None = None
    refinement_iterations: int = 5
    hpacker_version: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    code: str | None = None


def _load_hpacker_class():
    try:
        module = importlib.import_module("hpacker")
    except ImportError as exc:
        raise HPackerUnavailableError(
            "HPacker is not installed. Install the project environment with "
            "hpacker and e3nn==0.5.0."
        ) from exc
    try:
        return module.HPacker, getattr(module, "__version__", "unknown")
    except AttributeError as exc:
        raise HPackerUnavailableError("hpacker.HPacker is not available") from exc


def _is_pdb_atom(line: str) -> bool:
    return line.startswith(("ATOM  ", "HETATM"))


def _is_standard_protein_atom(line: str) -> bool:
    if not line.startswith("ATOM  "):
        return False
    return line[17:20].strip().upper() in STANDARD_AA


def _canonical_hpacker_resname(resname: str) -> str:
    return PROTEIN_VARIANT_TO_STANDARD.get(resname.upper(), resname.upper())


def _parse_resseq(line: str) -> int | None:
    try:
        return int(line[22:26].strip())
    except ValueError:
        return None


def _line_hpacker_id(line: str) -> tuple[str, int, str] | None:
    if not _is_standard_protein_atom(line):
        return None
    resseq = _parse_resseq(line)
    if resseq is None:
        return None
    return (line[21].strip() or " ", resseq, line[26].strip() or " ")


def _protein_residues_from_lines(lines: Iterable[str]) -> list[ProteinResidue]:
    residues: list[ProteinResidue] = []
    seen: set[tuple[str, int, str, str]] = set()
    for line in lines:
        if not _is_standard_protein_atom(line):
            continue
        resseq = _parse_resseq(line)
        if resseq is None:
            continue
        chain = line[21].strip()
        icode = line[26].strip()
        resname = line[17:20].strip().upper()
        key = (chain, resseq, icode, resname)
        if key in seen:
            continue
        seen.add(key)
        residues.append(
            ProteinResidue(
                chain=chain,
                resseq=resseq,
                icode=icode,
                resname=resname,
                index=len(residues),
            )
        )
    return residues


def read_protein_residues(pdb_file: str | Path) -> list[ProteinResidue]:
    return _protein_residues_from_lines(Path(pdb_file).read_text().splitlines())


_MUTATION_RE = re.compile(r"^(?:(?P<chain>[^:]):)?(?P<from>[A-Z])(?P<resseq>-?\d+)(?P<icode>[A-Z]?)(?P<to>[A-Z])$")


def parse_mutation_specs(
    mutation_specs: list[str] | tuple[str, ...] | None,
    residues: list[ProteinResidue],
) -> tuple[dict[tuple[str, int, str], str], list[str]]:
    """Parse CLI mutation strings into HPacker ``res_id -> resname`` mapping."""
    if not mutation_specs:
        return {}, []
    parsed: dict[tuple[str, int, str], str] = {}
    normalized_specs: list[str] = []
    for raw in mutation_specs:
        spec = raw.strip().upper()
        if not spec:
            continue
        match = _MUTATION_RE.match(spec)
        if not match:
            raise ValueError(
                f"Invalid mutation spec '{raw}'. Use L99A or A:L99A notation."
            )
        from_code = match.group("from")
        to_code = match.group("to")
        if from_code not in AA_ONE_TO_THREE or to_code not in AA_ONE_TO_THREE:
            raise ValueError(f"Unsupported amino-acid code in mutation spec '{raw}'")
        chain = match.group("chain")
        resseq = int(match.group("resseq"))
        icode = match.group("icode") or ""
        candidates = [
            residue for residue in residues
            if residue.resseq == resseq
            and residue.icode.upper() == icode
            and (chain is None or residue.chain.upper() == chain.upper())
        ]
        if not candidates:
            raise ValueError(f"Mutation target not found in input PDB: {raw}")
        if chain is None and len(candidates) > 1:
            chains = ", ".join(sorted({res.chain or "<blank>" for res in candidates}))
            raise ValueError(
                f"Mutation target '{raw}' is ambiguous across chains: {chains}. "
                "Use chain-qualified notation such as A:L99A."
            )
        residue = candidates[0]
        if residue.one_letter != from_code:
            raise ValueError(
                f"Mutation spec '{raw}' expects {from_code} at {residue.chain or '<blank>'}:"
                f"{residue.resseq}{residue.icode}, but input has {residue.one_letter}"
            )
        parsed[residue.hpacker_id] = AA_ONE_TO_THREE[to_code]
        normalized_specs.append(
            f"{residue.chain + ':' if residue.chain else ''}"
            f"{from_code}{residue.resseq}{residue.icode}{to_code}"
        )
    return parsed, normalized_specs


def mutation_map_from_sequence(
    sequence: str,
    residues: list[ProteinResidue],
) -> tuple[dict[tuple[str, int, str], str], list[str]]:
    """Map legacy mixed-case sequence input to HPacker mutation targets.

    The legacy convention used lowercase as "keep" and uppercase as "mutate to
    this residue".  We preserve it as input compatibility only.
    """
    compact = "".join(sequence.split())
    if len(compact) != len(residues):
        raise ValueError(
            f"sequence length ({len(compact)}) must match standard protein residue "
            f"count ({len(residues)})"
        )
    parsed: dict[tuple[str, int, str], str] = {}
    normalized_specs: list[str] = []
    for code, residue in zip(compact, residues):
        upper = code.upper()
        if upper not in AA_ONE_TO_THREE:
            raise ValueError(f"Unsupported amino-acid code in sequence: {code}")
        if code.isupper():
            parsed[residue.hpacker_id] = AA_ONE_TO_THREE[upper]
            normalized_specs.append(
                f"{residue.chain + ':' if residue.chain else ''}"
                f"{residue.one_letter}{residue.resseq}{residue.icode}{upper}"
            )
    return parsed, normalized_specs


def _write_protein_input(original_lines: list[str], output_path: Path) -> None:
    out: list[str] = []
    for line in original_lines:
        if not _is_standard_protein_atom(line):
            continue
        resname = line[17:20].strip().upper()
        canonical = _canonical_hpacker_resname(resname)
        out.append(line[:17] + f"{canonical:>3}" + line[20:])
    out.append("END")
    output_path.write_text("\n".join(out) + "\n")


def _restore_reference_resnames(
    pdb_file: Path,
    reference_residues: list[ProteinResidue],
    mutation_map: dict[tuple[str, int, str], str],
) -> None:
    """Restore Amber-style residue names before hydrogen rebuilding."""
    resnames = {residue.hpacker_id: residue.resname for residue in reference_residues}
    resnames.update(mutation_map)
    out: list[str] = []
    for line in pdb_file.read_text().splitlines():
        key = _line_hpacker_id(line)
        if key is not None and key in resnames:
            out.append(line[:17] + f"{resnames[key]:>3}" + line[20:])
        else:
            out.append(line)
    pdb_file.write_text("\n".join(out) + "\n")


def _split_hpacker_and_nonprotein_lines(
    hpacker_output: Path,
    original_lines: list[str],
) -> list[str]:
    lines: list[str] = []
    for line in hpacker_output.read_text().splitlines():
        if line.startswith(("ATOM  ", "TER")):
            lines.append(line)
    for line in original_lines:
        if line.startswith("HETATM") or (
            line.startswith("ATOM  ") and not _is_standard_protein_atom(line)
        ):
            lines.append(line)
    conect = [line for line in original_lines if line.startswith("CONECT")]
    lines.extend(conect)
    lines.append("END")
    return lines


def _infer_element(line: str) -> str:
    atom_name = line[12:16].strip()
    raw = line[76:78].strip().upper() if len(line) >= 78 else ""
    if raw == "D" and atom_name.startswith("H"):
        return "H"
    if raw:
        return raw[:2].rjust(2)
    stripped = atom_name.lstrip("0123456789").upper()
    if len(stripped) >= 2 and stripped[:2] in {"CL", "BR", "NA", "MG", "ZN", "CA", "FE", "MN", "CU", "CO", "NI", "K"}:
        return stripped[:2].title().rjust(2)
    return (stripped[:1] or " ").rjust(2)


def _normalize_atom_line(line: str, serial: int) -> str:
    padded = line.rstrip("\n")
    if len(padded) < 80:
        padded = padded.ljust(80)
    element = _infer_element(padded)
    return f"{padded[:6]}{serial:5d}{padded[11:76]}{element:>2}{padded[78:80]}"


def _normalize_pdb_lines(lines: list[str]) -> list[str]:
    atom_serial_map: dict[int, int] = {}
    out: list[str] = []
    serial = 1
    for line in lines:
        if _is_pdb_atom(line):
            try:
                old_serial = int(line[6:11].strip())
            except ValueError:
                old_serial = -serial
            atom_serial_map[old_serial] = serial
            out.append(_normalize_atom_line(line, serial))
            serial += 1
        elif line.startswith("CONECT"):
            fields = line.split()
            if len(fields) < 2:
                continue
            try:
                src_old = int(fields[1])
            except ValueError:
                continue
            if src_old not in atom_serial_map:
                continue
            targets: list[int] = []
            for field in fields[2:]:
                try:
                    old_target = int(field)
                except ValueError:
                    continue
                if old_target in atom_serial_map:
                    targets.append(atom_serial_map[old_target])
            if targets:
                out.append(
                    f"CONECT{atom_serial_map[src_old]:5d}"
                    + "".join(f"{target:5d}" for target in targets)
                )
        elif line.startswith("END"):
            continue
        else:
            out.append(line)
    out.append("END")
    return out


def _write_normalized_output(lines: list[str], output_path: Path) -> None:
    output_path.write_text("\n".join(_normalize_pdb_lines(lines)) + "\n")


def _rebuild_protein_hydrogens(
    input_pdb: Path,
    output_pdb: Path,
    *,
    reference_pdb: Path | None = None,
) -> None:
    """Rebuild protein hydrogens after HPacker's heavy-atom side-chain pass."""
    from openmm.app import PDBFile
    from pdbfixer import PDBFixer

    fixer = PDBFixer(filename=str(input_pdb))
    fixer.addMissingHydrogens(7.0)
    raw_output = output_pdb.with_name(f"{output_pdb.stem}.raw{output_pdb.suffix}")
    with raw_output.open("w") as handle:
        PDBFile.writeFile(fixer.topology, fixer.positions, handle, keepIds=True)
    _sort_protein_atoms_like_reference(raw_output, reference_pdb or input_pdb, output_pdb)


def _ter_line(serial: int, residue: ProteinResidue) -> str:
    chain = residue.chain or " "
    icode = residue.icode or " "
    return f"TER   {serial:5d}      {residue.resname:>3} {chain:1}{residue.resseq:4d}{icode:1}"


def _sort_protein_atoms_like_reference(
    rebuilt_pdb: Path,
    reference_pdb: Path,
    output_pdb: Path,
) -> None:
    """Restore reference residue order after PDBFixer hydrogen rebuilding.

    PDBFixer keeps residue IDs when asked, but can emit some residues out of
    sequence after adding hydrogens.  Downstream solvation/topology tools treat
    PDB order as chain order, so put each protein residue back in the HPacker
    reference order while preserving the rebuilt atoms within the residue.
    """
    reference_residues = read_protein_residues(reference_pdb)
    groups: dict[tuple[str, int, str], list[str]] = {}
    preamble: list[str] = []
    for line in rebuilt_pdb.read_text().splitlines():
        if line.startswith("ATOM"):
            key = _line_hpacker_id(line)
            if key is None:
                continue
            groups.setdefault(key, []).append(line)
        elif line.startswith(("TER", "END")):
            continue
        else:
            preamble.append(line)

    ordered: list[str] = list(preamble)
    serial = 1
    previous_chain: str | None = None
    previous_residue: ProteinResidue | None = None
    for residue in reference_residues:
        if previous_chain is not None and residue.chain != previous_chain and previous_residue:
            ordered.append(_ter_line(serial, previous_residue))
            serial += 1
        ordered.extend(groups.get(residue.hpacker_id, []))
        serial += len(groups.get(residue.hpacker_id, []))
        previous_chain = residue.chain
        previous_residue = residue
    if previous_residue:
        ordered.append(_ter_line(serial, previous_residue))
    ordered.append("END")
    output_pdb.write_text("\n".join(ordered) + "\n")


def _resname_by_hpacker_id(pdb_file: Path) -> dict[tuple[str, int, str], str]:
    return {residue.hpacker_id: residue.resname for residue in read_protein_residues(pdb_file)}


def _count_nonprotein_atoms(lines: Iterable[str]) -> int:
    return sum(
        1 for line in lines
        if line.startswith("HETATM") or (
            line.startswith("ATOM  ") and not _is_standard_protein_atom(line)
        )
    )


def _validate_output(
    input_lines: list[str],
    output_path: Path,
    mutation_map: dict[tuple[str, int, str], str],
) -> list[str]:
    errors: list[str] = []
    output_lines = output_path.read_text().splitlines()
    serials: set[int] = set()
    for line in output_lines:
        if not _is_pdb_atom(line):
            continue
        try:
            serial = int(line[6:11].strip())
        except ValueError:
            errors.append(f"Invalid atom serial in output line: {line[:16].rstrip()}")
            continue
        if serial in serials:
            errors.append(f"Duplicate atom serial in HPacker output: {serial}")
        serials.add(serial)
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip()
        if element == "D" and atom_name.startswith("H"):
            errors.append(f"Hydrogen atom {atom_name} was written with element D")
    output_resnames = _resname_by_hpacker_id(output_path)
    for res_id, expected_resname in mutation_map.items():
        if output_resnames.get(res_id) != expected_resname:
            errors.append(
                f"Mutation target {res_id} expected {expected_resname}, "
                f"found {output_resnames.get(res_id)}"
            )
    if _count_nonprotein_atoms(input_lines) != _count_nonprotein_atoms(output_lines):
        errors.append("Non-protein atom count changed during HPacker merge")
    return errors


def run_hpacker(
    input_pdb: str | Path,
    output_pdb: str | Path,
    *,
    mutations: list[str] | tuple[str, ...] | None = None,
    sequence: str | None = None,
    seq_file: str | Path | None = None,
    reconstruct_all_sidechains: bool = False,
    repack_radius_angstrom: float = 8.0,
    refinement_iterations: int = 5,
) -> HPackerRunResult:
    """Run HPacker and write a normalized PDB file."""
    input_path = Path(input_pdb).expanduser().resolve()
    output_path = Path(output_pdb).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    original_lines = input_path.read_text().splitlines()
    residues = _protein_residues_from_lines(original_lines)
    if not residues:
        return HPackerRunResult(
            success=False,
            errors=["Input PDB contains no standard protein residues for HPacker"],
            code="hpacker_no_protein_residues",
        )

    try:
        mutation_map, normalized_specs = parse_mutation_specs(mutations, residues)
        if seq_file is not None:
            if sequence is not None:
                raise ValueError("Provide only one of sequence or seq_file")
            sequence = Path(seq_file).expanduser().resolve().read_text()
        if sequence is not None:
            seq_map, seq_specs = mutation_map_from_sequence(sequence, residues)
            if mutation_map:
                raise ValueError("Provide either mutations or sequence/seq_file, not both")
            mutation_map = seq_map
            normalized_specs = seq_specs
    except Exception as exc:
        return HPackerRunResult(
            success=False,
            errors=[str(exc)],
            code="mutation_spec_invalid",
        )

    try:
        hpacker_cls, hpacker_version = _load_hpacker_class()
    except HPackerUnavailableError as exc:
        return HPackerRunResult(
            success=False,
            errors=[str(exc)],
            code="hpacker_not_available",
        )

    with tempfile.TemporaryDirectory(prefix="mdclaw_hpacker_") as tmp:
        tmp_dir = Path(tmp)
        protein_input = tmp_dir / "protein_input.pdb"
        hpacker_output = tmp_dir / "hpacker_output.pdb"
        hydrogenated_output = tmp_dir / "hpacker_hydrogenated.pdb"
        _write_protein_input(original_lines, protein_input)
        try:
            hpacker = hpacker_cls(str(protein_input))
            hpacker.reconstruct_sidechains(
                num_refinement_iterations=refinement_iterations,
                res_id_to_resname=mutation_map or None,
                reconstruct_all_sidechains=reconstruct_all_sidechains,
                proximity_cutoff_for_refinement=repack_radius_angstrom,
            )
            hpacker.write_pdb(str(hpacker_output))
            _restore_reference_resnames(hpacker_output, residues, mutation_map)
        except Exception as exc:
            return HPackerRunResult(
                success=False,
                errors=[f"HPacker failed: {type(exc).__name__}: {exc}"],
                code="hpacker_failed",
            )
        if not hpacker_output.exists():
            return HPackerRunResult(
                success=False,
                errors=["HPacker produced no PDB output"],
                code="hpacker_no_output",
            )
        try:
            _rebuild_protein_hydrogens(
                hpacker_output,
                hydrogenated_output,
                reference_pdb=protein_input,
            )
        except Exception as exc:
            return HPackerRunResult(
                success=False,
                errors=[f"Protein hydrogen rebuild after HPacker failed: {type(exc).__name__}: {exc}"],
                code="hpacker_hydrogen_rebuild_failed",
            )
        merged_lines = _split_hpacker_and_nonprotein_lines(hydrogenated_output, original_lines)
        _write_normalized_output(merged_lines, output_path)

    validation_errors = _validate_output(original_lines, output_path, mutation_map)
    if validation_errors:
        return HPackerRunResult(
            success=False,
            output_path=str(output_path),
            mutation_specs=normalized_specs,
            mutation_map={str(key): value for key, value in mutation_map.items()},
            repack_radius_angstrom=repack_radius_angstrom,
            refinement_iterations=refinement_iterations,
            hpacker_version=hpacker_version,
            errors=validation_errors,
            code="mutation_validation_failed",
        )
    return HPackerRunResult(
        success=True,
        output_path=str(output_path),
        mutation_specs=normalized_specs,
        mutation_map={str(key): value for key, value in mutation_map.items()},
        repack_radius_angstrom=repack_radius_angstrom,
        refinement_iterations=refinement_iterations,
        hpacker_version=hpacker_version,
    )


def run_hpacker_mutation(
    input_pdb: str | Path,
    output_pdb: str | Path,
    *,
    mutations: list[str] | tuple[str, ...] | None = None,
    sequence: str | None = None,
    seq_file: str | Path | None = None,
    repack_radius_angstrom: float = 8.0,
    refinement_iterations: int = 5,
) -> HPackerRunResult:
    return run_hpacker(
        input_pdb,
        output_pdb,
        mutations=mutations,
        sequence=sequence,
        seq_file=seq_file,
        reconstruct_all_sidechains=False,
        repack_radius_angstrom=repack_radius_angstrom,
        refinement_iterations=refinement_iterations,
    )


def run_hpacker_full_repack(
    input_pdb: str | Path,
    output_pdb: str | Path,
    *,
    refinement_iterations: int = 5,
) -> HPackerRunResult:
    return run_hpacker(
        input_pdb,
        output_pdb,
        reconstruct_all_sidechains=True,
        refinement_iterations=refinement_iterations,
    )
