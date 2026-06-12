"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (geostd XML when available, otherwise
  ``GAFFTemplateGenerator``), and emit a portable ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology resolves geostd templates first and
  falls back to ``GAFFTemplateGenerator`` for the remaining small molecules.
- Handling glycoproteins by converting deposited glycan residues to
  Amber/GLYCAM notation at topology time, preserving the generated bond plan,
  and completing only GLYCAM-specific hydrogens before System creation.

The XML triple is the only topology contract on the run side; tleap and
parm7/rst7 are not produced or consumed anywhere. AmberTools
(``pdb4amber`` and ``cpptraj``) remain available for structure-preparation
support; ligand parameterization is not a prep-stage mdclaw artifact.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory, BaseToolWrapper,
)
from mdclaw._common import get_timeout  # noqa: E402
from mdclaw import forcefield_catalog as _ff_catalog  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers.
# ``tleap`` is no longer used: the curated build path runs through
# ``openmmforcefields.SystemGenerator`` and emits the modern
# ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple (PR3 of the
# openmmforcefields-unification refactor). ``cpptraj`` is still used for
# the GLYCAM ``prepareforleap`` glycan conversion stage; see
# ``_prepare_glycam_pdb_with_cpptraj`` for context.
cpptraj_wrapper = BaseToolWrapper("cpptraj")


# =============================================================================
# Force Field Mappings (based on Amber Manual 2024 recommendations)
# =============================================================================

from mdclaw.amber.content_detection import _normalize_pdb_chain_id  # noqa: E402
from mdclaw.amber.forcefield_constants import _GLYCAM_LINKED_ASN_RESNAME, _GLYCAM_TOPOLOGY_RESNAMES  # noqa: E402
from mdclaw.amber.ligand_validation import _is_hydrogen_like_atom  # noqa: E402
from mdclaw.amber.topology_bonds import _topology_has_bond, _write_pdb_with_glycan_link_records  # noqa: E402


def _is_glycam_topology_residue(residue_name: str) -> bool:
    return str(residue_name or "").upper() in _GLYCAM_TOPOLOGY_RESNAMES


def _collect_pdb_residue_units(pdb_path: Path) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    last_key: tuple[str, str, str, str] | None = None
    try:
        lines = pdb_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return units
    for line in lines:
        if not line.startswith(("ATOM  ", "HETATM")) or len(line) < 27:
            continue
        key = (
            _normalize_pdb_chain_id(line[21:22]),
            line[22:26].strip(),
            line[26:27].strip(),
            line[17:20].strip(),
        )
        if key != last_key:
            units.append({
                "unit_index": len(units) + 1,
                "chain": key[0],
                "resnum": key[1],
                "icode": key[2],
                "resname": key[3],
                "atoms": set(),
            })
            last_key = key
        units[-1]["atoms"].add(line[12:16].strip())
    return units


def _parse_glycam_leap_bond_plan(
    leap_script: Path,
    prepared_pdb: Path,
) -> dict[str, Any]:
    """Parse cpptraj prepareforleap's bond commands into a stable JSON plan."""
    plan: dict[str, Any] = {
        "schema_version": "1.0",
        "source": "cpptraj_prepareforleap",
        "leap_script": str(leap_script),
        "prepared_pdb": str(prepared_pdb),
        "bond_count": 0,
        "bonds": [],
        "warnings": [],
        "errors": [],
    }
    units = _collect_pdb_residue_units(prepared_pdb)
    unit_by_index = {int(item["unit_index"]): item for item in units}
    if not units:
        plan["warnings"].append(
            f"Could not parse residue units from GLYCAM PDB {prepared_pdb}"
        )
    try:
        lines = leap_script.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        plan["errors"].append(f"Could not read GLYCAM LEaP bond plan: {exc}")
        return plan

    bond_re = re.compile(
        r"^\s*bond\s+mol\.(?P<u1>\d+)\.(?P<a1>[A-Za-z0-9'_*+-]+)\s+"
        r"mol\.(?P<u2>\d+)\.(?P<a2>[A-Za-z0-9'_*+-]+)\s*$"
    )
    for line_no, line in enumerate(lines, start=1):
        match = bond_re.match(line)
        if not match:
            continue
        u1 = int(match.group("u1"))
        u2 = int(match.group("u2"))
        a1 = match.group("a1")
        a2 = match.group("a2")
        left_unit = unit_by_index.get(u1)
        right_unit = unit_by_index.get(u2)
        left = {
            "unit_index": u1,
            "atom": a1,
            "chain": left_unit.get("chain") if left_unit else None,
            "resnum": left_unit.get("resnum") if left_unit else None,
            "icode": left_unit.get("icode") if left_unit else None,
            "resname": left_unit.get("resname") if left_unit else None,
        }
        right = {
            "unit_index": u2,
            "atom": a2,
            "chain": right_unit.get("chain") if right_unit else None,
            "resnum": right_unit.get("resnum") if right_unit else None,
            "icode": right_unit.get("icode") if right_unit else None,
            "resname": right_unit.get("resname") if right_unit else None,
        }
        errors: list[str] = []
        if left_unit is None:
            errors.append(f"left unit {u1} not found in {prepared_pdb.name}")
        elif a1 not in left_unit["atoms"]:
            errors.append(f"left atom mol.{u1}.{a1} not found in {prepared_pdb.name}")
        if right_unit is None:
            errors.append(f"right unit {u2} not found in {prepared_pdb.name}")
        elif a2 not in right_unit["atoms"]:
            errors.append(f"right atom mol.{u2}.{a2} not found in {prepared_pdb.name}")
        if errors:
            plan["errors"].extend(errors)
        plan["bonds"].append({
            "source_line": line,
            "source_line_number": line_no,
            "left": left,
            "right": right,
            "status": "parsed" if not errors else "unresolved",
        })
    plan["bond_count"] = len(plan["bonds"])
    return plan


def _protein_hydrogen_signature_for_glycam(omm_topology: Any) -> dict[tuple[Any, ...], tuple[str, ...]]:
    signature: dict[tuple[Any, ...], tuple[str, ...]] = {}
    for residue in omm_topology.residues():
        residue_name = str(residue.name or "")
        if residue_name == _GLYCAM_LINKED_ASN_RESNAME or _is_glycam_topology_residue(residue_name):
            continue
        hydrogens = tuple(
            sorted(atom.name for atom in residue.atoms() if _is_hydrogen_like_atom(atom))
        )
        signature[
            (
                residue.index,
                _normalize_pdb_chain_id(getattr(residue.chain, "id", "")),
                str(residue.id),
                residue_name,
            )
        ] = hydrogens
    return signature


def _add_glycam_backbone_bonds(
    omm_topology: Any,
    omm_positions: Any,
    unit_module: Any,
) -> int:
    try:
        positions_nm = [p.value_in_unit(unit_module.nanometer) for p in omm_positions]
    except Exception:  # noqa: BLE001
        return 0

    added = 0
    max_peptide_bond_nm = 0.18
    for chain in omm_topology.chains():
        residues = list(chain.residues())
        for left_residue, right_residue in zip(residues, residues[1:]):
            if (
                left_residue.name != _GLYCAM_LINKED_ASN_RESNAME
                and right_residue.name != _GLYCAM_LINKED_ASN_RESNAME
            ):
                continue
            left_c = next((a for a in left_residue.atoms() if a.name == "C"), None)
            right_n = next((a for a in right_residue.atoms() if a.name == "N"), None)
            if left_c is None or right_n is None:
                continue
            if _topology_has_bond(omm_topology, left_c, right_n):
                continue
            x1, y1, z1 = positions_nm[left_c.index]
            x2, y2, z2 = positions_nm[right_n.index]
            d2 = (x1 - x2) ** 2 + (y1 - y2) ** 2 + (z1 - z2) ** 2
            if d2 > max_peptide_bond_nm * max_peptide_bond_nm:
                continue
            omm_topology.addBond(left_c, right_n)
            added += 1
    return added


def _glycam_residue_id_matches(residue: Any, resnum: str, icode: str) -> bool:
    residue_id = str(getattr(residue, "id", "") or "").strip()
    if not resnum:
        return True
    expected = f"{resnum}{icode}".strip()
    return residue_id in {resnum, expected}


def _glycam_residue_identity_payload(residue: Any) -> dict[str, str]:
    return {
        "chain": _normalize_pdb_chain_id(getattr(residue.chain, "id", "")),
        "resnum": str(getattr(residue, "id", "") or "").strip(),
        "resname": str(getattr(residue, "name", "") or "").strip().upper(),
    }


def _resolve_glycam_bond_endpoint_residue(
    endpoint: dict[str, Any],
    residues: list[Any],
) -> tuple[Any | None, str | None, dict[str, Any] | None]:
    unit_index = None
    try:
        unit_index = int(endpoint.get("unit_index"))
    except (TypeError, ValueError):
        pass

    expected_resname = str(endpoint.get("resname") or "").strip().upper()
    expected_chain = _normalize_pdb_chain_id(endpoint.get("chain"))
    expected_resnum = str(endpoint.get("resnum") or "").strip()
    expected_icode = str(endpoint.get("icode") or "").strip()
    has_identity = bool(expected_chain or expected_resnum)

    if has_identity:
        matches = [
            residue
            for residue in residues
            if (
                not expected_resname
                or str(residue.name or "").upper() == expected_resname
            )
            and (
                not expected_chain
                or _normalize_pdb_chain_id(getattr(residue.chain, "id", ""))
                == expected_chain
            )
            and _glycam_residue_id_matches(residue, expected_resnum, expected_icode)
        ]
        if len(matches) == 1:
            residue = matches[0]
            warning = None
            if unit_index is None:
                warning = {
                    "code": "glycam_bond_plan_unit_index_invalid_but_identity_resolved",
                    "unit_index": endpoint.get("unit_index"),
                    "resolved_residue": _glycam_residue_identity_payload(residue),
                    "message": (
                        "GLYCAM bond endpoint unit_index is invalid, but "
                        "identity lookup resolved the residue."
                    ),
                }
            elif unit_index < 1 or unit_index > len(residues):
                warning = {
                    "code": "glycam_bond_plan_unit_index_out_of_range_but_identity_resolved",
                    "unit_index": unit_index,
                    "resolved_residue": _glycam_residue_identity_payload(residue),
                    "message": (
                        "GLYCAM bond endpoint unit_index is out of range, but "
                        "identity lookup resolved the residue."
                    ),
                }
            elif residues[unit_index - 1] is not residue:
                warning = {
                    "code": "glycam_bond_plan_unit_index_drift",
                    "unit_index": unit_index,
                    "unit_index_residue": _glycam_residue_identity_payload(
                        residues[unit_index - 1]
                    ),
                    "resolved_residue": _glycam_residue_identity_payload(residue),
                    "message": (
                        "GLYCAM bond endpoint unit_index drifted from "
                        "identity lookup; using the identity-matched residue."
                    ),
                }
            return residue, None, warning
        if not matches:
            return (
                None,
                "GLYCAM bond endpoint identity mismatch for "
                f"unit {endpoint.get('unit_index')!r}: chain={expected_chain!r} "
                f"resnum={expected_resnum!r} icode={expected_icode!r} "
                f"resname={expected_resname!r}",
                None,
            )
        return (
            None,
            "GLYCAM bond endpoint identity is ambiguous for "
            f"unit {endpoint.get('unit_index')!r}: chain={expected_chain!r} "
            f"resnum={expected_resnum!r} icode={expected_icode!r} "
            f"resname={expected_resname!r}; matched {len(matches)} residues",
            None,
        )

    if unit_index is None:
        return None, f"Invalid GLYCAM bond endpoint: {endpoint}", None
    if unit_index < 1 or unit_index > len(residues):
        return None, f"GLYCAM bond endpoint unit {unit_index} not found", None
    residue = residues[unit_index - 1]
    if expected_resname and str(residue.name or "").upper() != expected_resname:
        return (
            None,
            "GLYCAM bond endpoint unit-index fallback residue mismatch for "
            f"unit {unit_index}: topology residue {residue.name}#{residue.id} "
            f"!= expected {expected_resname}",
            None,
        )
    return residue, None, None


def _record_glycam_resolver_warning(
    report: dict[str, Any],
    warning: dict[str, Any] | None,
) -> None:
    if not warning:
        return
    drift = report.setdefault("unit_index_drift", [])
    if warning not in drift:
        drift.append(warning)
    message = warning.get("message")
    if message and message not in report["warnings"]:
        report["warnings"].append(message)


def _normalize_glycam_topology(
    *,
    omm_topology: Any,
    omm_positions: Any,
    glycam_bond_plan: dict[str, Any],
    protein_forcefield: str,
    phosaa_name: Optional[str],
    dna_name: Optional[str],
    rna_name: Optional[str],
    glycan_name: Optional[str],
    lipid_name: Optional[str],
    app_module: Any,
    unit_module: Any,
) -> tuple[Any, Any, dict[str, Any]]:
    """Apply explicit GLYCAM topology normalization after prepareforleap."""
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "stage": "glycam_topology_normalization",
        "attempted": True,
        "completed": False,
        "glycam_bond_plan": {
            "bond_count": len(glycam_bond_plan.get("bonds", []) or []),
            "applied_count": 0,
            "failed_count": 0,
        },
        "removed_substituted_h_count": 0,
        "backbone_bonds_added_count": 0,
        "hydrogen_completion": {
            "attempted": False,
            "added_atom_count": 0,
        },
        "protein_hydrogen_set_preserved": None,
        "unit_index_drift": [],
        "warnings": list(glycam_bond_plan.get("warnings", []) or []),
        "errors": list(glycam_bond_plan.get("errors", []) or []),
    }
    if report["errors"]:
        report["code"] = "glycam_bond_plan_apply_failed"
        return omm_topology, omm_positions, report

    linked_nln_endpoints: list[dict[str, Any]] = []
    for bond in glycam_bond_plan.get("bonds", []) or []:
        left = bond.get("left") or {}
        right = bond.get("right") or {}
        endpoints = (left, right)
        if any(
            str(endpoint.get("resname") or "") == _GLYCAM_LINKED_ASN_RESNAME
            and str(endpoint.get("atom") or "") == "ND2"
            for endpoint in endpoints
        ) and any(
            _is_glycam_topology_residue(str(endpoint.get("resname") or ""))
            and str(endpoint.get("atom") or "") == "C1"
            for endpoint in endpoints
        ):
            for endpoint in endpoints:
                if str(endpoint.get("resname") or "") == _GLYCAM_LINKED_ASN_RESNAME:
                    linked_nln_endpoints.append(endpoint)

    if linked_nln_endpoints:
        residues = list(omm_topology.residues())
        hd22_atoms = []
        seen_residue_indices: set[int] = set()
        for endpoint in linked_nln_endpoints:
            residue, error, warning = _resolve_glycam_bond_endpoint_residue(
                endpoint,
                residues,
            )
            _record_glycam_resolver_warning(report, warning)
            if error:
                report["errors"].append(error)
                continue
            if residue.index in seen_residue_indices:
                continue
            seen_residue_indices.add(residue.index)
            hd22 = next((atom for atom in residue.atoms() if atom.name == "HD22"), None)
            if hd22 is not None:
                hd22_atoms.append(hd22)
        if report["errors"]:
            report["code"] = "glycam_bond_plan_apply_failed"
            return omm_topology, omm_positions, report
        if hd22_atoms:
            modeller = app_module.Modeller(omm_topology, omm_positions)
            modeller.delete(hd22_atoms)
            omm_topology = modeller.topology
            omm_positions = modeller.positions
            report["removed_substituted_h_count"] = len(hd22_atoms)

    residues = list(omm_topology.residues())
    for bond in glycam_bond_plan.get("bonds", []) or []:
        endpoint_atoms = []
        for side in ("left", "right"):
            endpoint = bond.get(side) or {}
            residue, error, warning = _resolve_glycam_bond_endpoint_residue(
                endpoint,
                residues,
            )
            _record_glycam_resolver_warning(report, warning)
            if error:
                report["errors"].append(error)
                continue
            unit_index = endpoint.get("unit_index")
            atom_name = str(endpoint.get("atom") or "")
            atom = next((candidate for candidate in residue.atoms() if candidate.name == atom_name), None)
            if atom is None:
                report["errors"].append(
                    f"GLYCAM bond endpoint atom mol.{unit_index}.{atom_name} "
                    f"not found in residue {residue.name}#{residue.id}"
                )
                continue
            endpoint_atoms.append(atom)
        if len(endpoint_atoms) != 2:
            continue
        atom1, atom2 = endpoint_atoms
        if not _topology_has_bond(omm_topology, atom1, atom2):
            omm_topology.addBond(atom1, atom2)
        report["glycam_bond_plan"]["applied_count"] += 1

    if report["errors"]:
        report["glycam_bond_plan"]["failed_count"] = (
            report["glycam_bond_plan"]["bond_count"]
            - report["glycam_bond_plan"]["applied_count"]
        )
        report["code"] = "glycam_bond_plan_apply_failed"
        return omm_topology, omm_positions, report

    report["backbone_bonds_added_count"] = _add_glycam_backbone_bonds(
        omm_topology,
        omm_positions,
        unit_module,
    )

    before_signature = _protein_hydrogen_signature_for_glycam(omm_topology)
    before_atom_count = omm_topology.getNumAtoms()
    report["hydrogen_completion"]["attempted"] = True
    try:
        app_module.Modeller.loadHydrogenDefinitions("glycam-hydrogens.xml")
        hydrogen_xml = _ff_catalog.resolve_xml_bundle(
            protein=protein_forcefield,
            water="tip3p",
            phosaa=phosaa_name,
            dna=dna_name,
            rna=rna_name,
            glycan=glycan_name,
            lipid=lipid_name,
        )
        hydrogen_ff = app_module.ForceField(*hydrogen_xml)
        modeller = app_module.Modeller(omm_topology, omm_positions)
        modeller.addHydrogens(hydrogen_ff, pH=7.0)
        omm_topology = modeller.topology
        omm_positions = modeller.positions
        report["hydrogen_completion"]["forcefield_xml"] = hydrogen_xml
        report["hydrogen_completion"]["added_atom_count"] = (
            omm_topology.getNumAtoms() - before_atom_count
        )
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(
            f"GLYCAM hydrogen completion failed: {type(exc).__name__}: {exc}"
        )
        report["code"] = "glycam_hydrogen_completion_failed"
        return omm_topology, omm_positions, report

    after_signature = _protein_hydrogen_signature_for_glycam(omm_topology)
    report["protein_hydrogen_set_preserved"] = before_signature == after_signature
    if before_signature != after_signature:
        report["errors"].append(
            "GLYCAM hydrogen completion changed non-GLYCAM protein/water "
            "hydrogen atom sets; topology build does not perform generic H repair."
        )
        report["code"] = "glycam_normalization_changed_protein_hydrogens"
        return omm_topology, omm_positions, report

    report["completed"] = True
    return omm_topology, omm_positions, report


def _prepare_glycam_pdb_with_cpptraj(
    pdb_path: Path,
    out_dir: Path,
    output_name: str,
    glycan_linkages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Use cpptraj prepareforleap to convert PDB glycans to GLYCAM notation.

    This is intentionally scoped to the carbohydrate conversion step. Protein
    protonation, missing-residue handling, and disulfide planning stay in the
    existing MDClaw preparation path.
    """
    result: Dict[str, Any] = {
        "success": False,
        "code": None,
        "prepared_pdb": None,
        "leap_script": None,
        "glycam_bond_plan": None,
        "glycam_bond_plan_file": None,
        "cpptraj_input": None,
        "cpptraj_pdb_input": None,
        "cpptraj_log": None,
        "link_records": [],
        "errors": [],
        "warnings": [],
    }
    if not cpptraj_wrapper.is_available():
        result["errors"].append("cpptraj is required for GLYCAM glycan preparation")
        return result

    prepared_pdb = out_dir / f"{output_name}.glycam.pdb"
    generated_leap = out_dir / f"{output_name}.glycam.leap.in"
    bond_plan_file = out_dir / f"{output_name}.glycam_bond_plan.json"
    cpptraj_input = out_dir / f"{output_name}.prepareforleap.in"
    cpptraj_pdb_input = out_dir / f"{output_name}.prepareforleap.pdb"
    cpptraj_log = out_dir / f"{output_name}.prepareforleap.log"
    linked_pdb = _write_pdb_with_glycan_link_records(
        pdb_path=pdb_path,
        output_path=cpptraj_pdb_input,
        glycan_linkages=glycan_linkages,
    )
    result["warnings"].extend(linked_pdb["warnings"])
    result["errors"].extend(linked_pdb.get("errors", []))
    result["link_records"] = linked_pdb["link_records"]
    result["conect_records"] = linked_pdb["conect_records"]
    result["link_injection"] = {
        key: linked_pdb.get(key)
        for key in (
            "success",
            "expected_linkage_count",
            "emitted_link_count",
            "emitted_conect_pair_count",
            "missing_link_count",
            "path",
        )
    }
    if not linked_pdb.get("success", True):
        result["code"] = "glycan_linkage_mapping_failed"
        return result
    pdb_path = cpptraj_pdb_input

    cpptraj_input.write_text(
        "\n".join([
            f"parm {pdb_path}",
            f"loadcrd {pdb_path} name MDClawCrd",
            (
                "prepareforleap crdset MDClawCrd name MDClawPrepared "
                f"out {generated_leap} leapunitname mol pdbout {prepared_pdb} "
                "skiperrors nowat keepaltloc highestocc nohisdetect nodisulfides"
            ),
            "go",
            "quit",
            "",
        ]),
        encoding="utf-8",
    )

    try:
        proc_result = cpptraj_wrapper.run(
            ["-i", str(cpptraj_input)],
            cwd=out_dir,
            timeout=get_timeout("amber"),
        )
    except Exception as e:
        result["errors"].append(f"cpptraj prepareforleap failed: {type(e).__name__}: {e}")
        return result

    cpptraj_log.write_text(
        (proc_result.stdout or "")
        + ("\n--- STDERR ---\n" + proc_result.stderr if proc_result.stderr else ""),
        encoding="utf-8",
    )

    result.update({
        "prepared_pdb": str(prepared_pdb),
        "leap_script": str(generated_leap),
        "cpptraj_input": str(cpptraj_input),
        "cpptraj_pdb_input": str(cpptraj_pdb_input),
        "cpptraj_log": str(cpptraj_log),
    })
    if not prepared_pdb.exists():
        result["errors"].append("cpptraj prepareforleap completed but prepared PDB was not created")
    if not generated_leap.exists():
        result["errors"].append("cpptraj prepareforleap completed but LEaP command file was not created")
    if result["errors"]:
        return result

    bond_plan = _parse_glycam_leap_bond_plan(generated_leap, prepared_pdb)
    result["glycam_bond_plan"] = bond_plan
    result["glycam_bond_plan_file"] = str(bond_plan_file)
    bond_plan_file.write_text(json.dumps(bond_plan, indent=2), encoding="utf-8")

    result["success"] = True
    return result
