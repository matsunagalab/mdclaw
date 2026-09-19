"""Mutant structure generation for the hybrid topology.

``build_hybrid_system`` needs a mutant PDB that is *identical* to the
solvated wild-type PDB everywhere except the mutated residue. This module
produces it by modelling the new side chain (HPacker when available,
PDBFixer otherwise), taking only the mutated residue's atoms from that
model, and splicing them into a copy of the wild-type file. Environment
atoms — the rest of the protein, ligands, ions, water — keep their order,
names and coordinates, which is what makes the atom mapping trivial.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mdclaw.sidechain_packer import (
    AA_THREE_TO_ONE,
    PROTEIN_VARIANT_TO_STANDARD,
    TERMINAL_CAP_RESNAMES,
    _is_standard_protein_atom,
    _line_residue_key,
    parse_mutation_specs,
    read_protein_residues,
)

logger = logging.getLogger(__name__)

MUTANT_BACKENDS = ("auto", "hpacker", "pdbfixer")


class MutantBuildError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MutationSpec:
    chain_id: str            # PDB chain id ("" when blank)
    resseq: int
    icode: str               # "" when none
    wt_resname: str          # as written in the PDB (may be HIE, CYX, ...)
    mut_resname: str         # standard three-letter name
    label: str               # normalized "A:L99A"

    @property
    def residue_key(self) -> tuple[str, int, str]:
        return (self.chain_id or " ", self.resseq, self.icode or " ")

    @property
    def residue_id(self) -> str:
        return f"{self.resseq}{self.icode}"

    @property
    def wt_one(self) -> str:
        return AA_THREE_TO_ONE.get(PROTEIN_VARIANT_TO_STANDARD.get(self.wt_resname, self.wt_resname), "X")

    @property
    def mut_one(self) -> str:
        return AA_THREE_TO_ONE.get(self.mut_resname, "X")

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "chain_id": self.chain_id,
            "resseq": self.resseq,
            "icode": self.icode,
            "wt_resname": self.wt_resname,
            "mut_resname": self.mut_resname,
        }


def parse_single_mutation(spec: str, pdb_file: str | Path) -> MutationSpec:
    """Parse ``L99A`` / ``A:L99A`` against the residues present in ``pdb_file``."""
    residues = read_protein_residues(pdb_file)
    if not residues:
        raise MutantBuildError(code="fep_mutation_spec_invalid", message=f"{pdb_file} contains no standard protein residues")
    try:
        mutation_map, normalized = parse_mutation_specs([spec], residues)
    except ValueError as exc:
        code = "fep_mutation_residue_not_found" if "not found" in str(exc) else "fep_mutation_spec_invalid"
        raise MutantBuildError(code=code, message=str(exc)) from exc
    if len(mutation_map) != 1:
        raise MutantBuildError(
            code="fep_mutation_spec_invalid", message=f"exactly one point mutation is supported per hybrid topology, got {len(mutation_map)}: {spec!r}",
        )
    (chain, resseq, icode), mut_resname = next(iter(mutation_map.items()))
    residue = next(r for r in residues if r.hpacker_id == (chain, resseq, icode))
    if PROTEIN_VARIANT_TO_STANDARD.get(residue.resname, residue.resname) == mut_resname:
        raise MutantBuildError(
            code="fep_mutation_spec_invalid", message=f"{normalized[0]} is not a mutation: residue is already {residue.resname}",
        )
    return MutationSpec(
        chain_id=chain.strip(),
        resseq=resseq,
        icode=icode.strip(),
        wt_resname=residue.resname,
        mut_resname=mut_resname,
        label=normalized[0],
    )


# --------------------------------------------------------------------------- #
# PDB line helpers                                                              #
# --------------------------------------------------------------------------- #

def _format_atom_line(serial: int, name: str, resname: str, chain: str, resseq: int,
                      icode: str, x: float, y: float, z: float, element: str) -> str:
    # Atom-name column convention: 1-2 letter elements start in column 14.
    if len(name) < 4 and (len(element) == 1):
        name_field = f" {name:<3}"
    else:
        name_field = f"{name:<4}"
    return (
        f"ATOM  {serial % 100000:5d} {name_field} {resname:>3} {chain[:1] or ' '}"
        f"{resseq:4d}{icode[:1] or ' '}   {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00"
        f"          {element:>2}"
    )


def _is_protein_or_cap_line(line: str) -> bool:
    if not line.startswith(("ATOM  ", "HETATM")):
        return False
    resname = line[17:20].strip().upper()
    return _is_standard_protein_atom(line) or resname in TERMINAL_CAP_RESNAMES


def _canonical_protein_fragment(pdb_lines: list[str]) -> list[str]:
    """Protein + cap ATOM lines with protonation-variant names canonicalised.

    Only the mutated residue is taken back from the model, so the rest of the
    fragment just has to be something the side-chain modeller understands.
    """
    out = []
    prev_chain = None
    for line in pdb_lines:
        if line.startswith("TER"):
            out.append(line)
            continue
        if not _is_protein_or_cap_line(line):
            continue
        resname = line[17:20].strip().upper()
        canon = PROTEIN_VARIANT_TO_STANDARD.get(resname, resname)
        line = "ATOM  " + line[6:17] + f"{canon:>3}" + line[20:]
        chain = line[21]
        if prev_chain is not None and chain != prev_chain and not out[-1].startswith("TER"):
            out.append("TER")
        prev_chain = chain
        out.append(line)
    out.append("END")
    return out


def _residue_lines_from_topology(topology, positions, spec: MutationSpec) -> list[str]:
    from openmm import unit

    pos = positions.value_in_unit(unit.angstrom)
    lines = []
    for chain in topology.chains():
        if (str(chain.id).strip() or "") != spec.chain_id:
            continue
        for residue in chain.residues():
            if str(residue.id).strip() != str(spec.resseq):
                continue
            if (getattr(residue, "insertionCode", "") or "").strip() != spec.icode:
                continue
            # Solvent residues can share (chain, number) with the protein;
            # only the modelled mutant residue itself is wanted.
            if PROTEIN_VARIANT_TO_STANDARD.get(residue.name, residue.name) != spec.mut_resname:
                continue
            for atom in residue.atoms():
                x, y, z = pos[atom.index]
                element = atom.element.symbol if atom.element is not None else atom.name[0]
                lines.append(_format_atom_line(
                    len(lines) + 1, atom.name, spec.mut_resname, spec.chain_id, spec.resseq,
                    spec.icode, float(x), float(y), float(z), element,
                ))
    if not lines:
        raise MutantBuildError(
            code="fep_mutant_model_failed", message=f"residue {spec.chain_id}:{spec.residue_id} is missing from the side-chain model",
        )
    return lines


# --------------------------------------------------------------------------- #
# Backends                                                                      #
# --------------------------------------------------------------------------- #

def _model_with_pdbfixer(pdb_lines: list[str], spec: MutationSpec, work_dir: Path) -> list[str]:
    from pdbfixer import PDBFixer

    fragment = work_dir / "pdbfixer_fragment.pdb"
    fragment.write_text("\n".join(_canonical_protein_fragment(pdb_lines)) + "\n")
    fixer = PDBFixer(filename=str(fragment))
    wt_canon = PROTEIN_VARIANT_TO_STANDARD.get(spec.wt_resname, spec.wt_resname)
    try:
        fixer.applyMutations([f"{wt_canon}-{spec.residue_id}-{spec.mut_resname}"], spec.chain_id or " ")
    except (ValueError, KeyError) as exc:
        raise MutantBuildError(code="fep_mutant_model_failed", message=f"PDBFixer could not apply {spec.label}: {exc}") from exc
    fixer.findMissingResidues()
    fixer.missingResidues = {}
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.0)
    return _residue_lines_from_topology(fixer.topology, fixer.positions, spec)


def _model_with_hpacker(pdb_path: Path, spec: MutationSpec, work_dir: Path) -> list[str]:
    from openmm.app import PDBFile

    from mdclaw.sidechain_packer import run_hpacker_mutation
    from mdclaw.structure.mutation import _canonicalise_variants_for_hpacker

    packer_input = work_dir / "hpacker_input.pdb"
    packer_output = work_dir / "hpacker_output.pdb"
    _canonicalise_variants_for_hpacker(pdb_path, packer_input)
    result = run_hpacker_mutation(packer_input, packer_output, mutations=[spec.label])
    if not result.success:
        raise MutantBuildError(
            code=result.code or "hpacker_failed",
            message="; ".join(result.errors) or "HPacker failed without a message",
        )
    pdb = PDBFile(str(packer_output))
    return _residue_lines_from_topology(pdb.topology, pdb.positions, spec)


def model_mutant_residue(
    pdb_path: Path,
    spec: MutationSpec,
    *,
    backend: str = "auto",
    work_dir: Optional[Path] = None,
) -> tuple[list[str], str, list[str]]:
    """Return ``(atom_lines, backend_used, warnings)`` for the mutated residue."""
    if backend not in MUTANT_BACKENDS:
        raise MutantBuildError(
            code="fep_mutation_spec_invalid", message=f"mutant_backend must be one of {MUTANT_BACKENDS}, got {backend!r}",
        )
    pdb_lines = pdb_path.read_text().splitlines()
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mdclaw_fep_mutant_") as tmp:
        wd = Path(work_dir) if work_dir else Path(tmp)
        wd.mkdir(parents=True, exist_ok=True)
        if backend in ("auto", "hpacker"):
            try:
                return _model_with_hpacker(pdb_path, spec, wd), "hpacker", warnings
            except Exception as exc:  # noqa: BLE001 - fall back for auto only
                if backend == "hpacker":
                    if isinstance(exc, MutantBuildError):
                        raise
                    raise MutantBuildError(code="fep_mutant_model_failed", message=f"HPacker failed: {exc}") from exc
                code = getattr(exc, "code", type(exc).__name__)
                warnings.append(f"HPacker unavailable or failed ({code}); side chain modelled with PDBFixer.")
                logger.info("HPacker fallback to PDBFixer for %s: %s", spec.label, exc)
        try:
            return _model_with_pdbfixer(pdb_lines, spec, wd), "pdbfixer", warnings
        except MutantBuildError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MutantBuildError(code="fep_mutant_model_failed", message=f"PDBFixer failed: {type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Splicing                                                                      #
# --------------------------------------------------------------------------- #

def splice_residue(pdb_lines: list[str], spec: MutationSpec, new_residue_lines: list[str]) -> tuple[list[str], dict]:
    """Replace the mutated residue's atom lines, renumber serials, remap CONECT."""
    key = spec.residue_key
    target = [i for i, line in enumerate(pdb_lines)
              if _is_standard_protein_atom(line) and _line_residue_key(line) == key]
    if not target:
        raise MutantBuildError(code="fep_mutation_residue_not_found", message=f"{spec.label}: residue not found in the wild-type PDB")
    first, last = target[0], target[-1]
    if last - first + 1 != len(target):
        raise MutantBuildError(code="fep_mutation_residue_ambiguous", message=f"{spec.label}: residue atoms are not contiguous")
    removed_serials = {pdb_lines[i][6:11].strip() for i in target}
    body = pdb_lines[:first] + new_residue_lines + pdb_lines[last + 1:]

    serial_map: dict[str, int] = {}
    duplicate_serials = False
    out: list[str] = []
    serial = 0
    for line in body:
        if line.startswith(("ATOM  ", "HETATM")):
            serial += 1
            old = line[6:11].strip()
            if old in serial_map:
                duplicate_serials = True
            serial_map[old] = serial
            line = f"{line[:6]}{serial % 100000:5d}{line[11:]}"
        elif line.startswith("TER"):
            serial += 1
            line = f"TER   {serial % 100000:5d}{line[11:]}" if len(line) > 11 else "TER"
        out.append(line)

    conect_kept = conect_dropped = 0
    final: list[str] = []
    for line in out:
        if not line.startswith("CONECT"):
            final.append(line)
            continue
        fields = [line[6 + 5 * i:11 + 5 * i].strip() for i in range(8)]
        fields = [f for f in fields if f]
        if duplicate_serials or any(f in removed_serials or f not in serial_map for f in fields):
            conect_dropped += 1
            continue
        final.append("CONECT" + "".join(f"{serial_map[f]:5d}" for f in fields))
        conect_kept += 1
    report = {
        "atoms_removed": len(target),
        "atoms_added": len(new_residue_lines),
        "total_atoms": serial - sum(1 for line in out if line.startswith("TER")),
        "conect_kept": conect_kept,
        "conect_dropped": conect_dropped,
    }
    return final, report


def write_mutant_pdb(
    wt_pdb: str | Path,
    spec: MutationSpec,
    out_pdb: str | Path,
    *,
    backend: str = "auto",
    work_dir: Optional[str | Path] = None,
) -> dict:
    """Model the mutant residue and write the spliced mutant PDB."""
    wt_path = Path(wt_pdb)
    out_path = Path(out_pdb)
    residue_lines, used, warnings = model_mutant_residue(
        wt_path, spec, backend=backend, work_dir=Path(work_dir) if work_dir else None,
    )
    lines, report = splice_residue(wt_path.read_text().splitlines(), spec, residue_lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    if report["conect_dropped"]:
        warnings.append(
            f"{report['conect_dropped']} CONECT record(s) dropped while renumbering the mutant PDB; "
            f"prep-stage disulfide/glycan records still drive bond creation.",
        )
    return {
        "mutant_pdb": str(out_path),
        "backend": used,
        "mutation": spec.to_json(),
        "residue_atom_names": [line[12:16].strip() for line in residue_lines],
        "warnings": warnings,
        **report,
    }


__all__ = [
    "MUTANT_BACKENDS", "MutantBuildError", "MutationSpec",
    "model_mutant_residue", "parse_single_mutation", "splice_residue", "write_mutant_pdb",
]
