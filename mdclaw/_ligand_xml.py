"""Convert topology-time geostd ligand records to OpenMM ForceField XML.

``build_amber_system`` may find a residue in Amber's geostd database while
building a topology.  geostd stores a curated Tripos mol2 (atom names, GAFF2
atom types, partial charges) plus an Amber frcmod (bond / angle / dihedral /
vdW additions on top of the GAFF2 baseline).  This module converts that pair
into a self-contained OpenMM ForceField XML, so the residue can be supplied by
the force-field bundle while other ligands still use ``GAFFTemplateGenerator``.

The output XML carries (a) frcmod-derived parameter additions and (b) a
``<Residue>`` template whose atom names/types/charges/bonds come directly
from the geostd mol2. Base GAFF2 atom types (``c3``, ``p5``, ``c5``, and so
on) are provided by openmmforcefields' shipped ``gaff-2.2.20.xml`` through
:func:`mdclaw.forcefield_catalog.resolve_xml_bundle`'s ``gaff_base`` slot.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any, Optional


_GAFF_BASE_DEFAULT = "gaff-2.2.20"


def convert_geostd_ligand_to_openmm_xml(
    mol2_path: Path,
    frcmod_path: Path,
    residue_name: str,
    out_path: Path,
) -> dict[str, Any]:
    """Convert a geostd mol2 + frcmod pair to a self-contained OpenMM XML.

    Parameters
    ----------
    mol2_path
        Tripos mol2 carrying the geostd ligand residue (atom names, GAFF2 atom
        types, partial charges) for exactly one residue.
    frcmod_path
        Amber frcmod with any bond/angle/dihedral/vdW additions that the
        GAFF2 baseline does not already cover. May be empty (no DIHE etc.).
    residue_name
        The 3- or 4-character residue name to register in the ``<Residue>``
        block. Must match the residue name used by the merged prep PDB so
        OpenMM's template matcher can find it.
    out_path
        Output ``.xml`` path. Parent directory is created if missing.

    Returns
    -------
    dict
        ``{"success": bool, "xml_path": str|None, "residue_name": str,
        "atom_count": int|None, "bond_count": int|None,
        "warnings": [...], "errors": [...], "code": Optional[str]}``.

        Failure ``code`` values:

        * ``ligand_xml_mol2_missing`` / ``ligand_xml_frcmod_missing``
        * ``ligand_xml_load_failed`` — ParmEd could not parse the mol2.
        * ``ligand_xml_frcmod_invalid`` — frcmod parse failed.
        * ``ligand_xml_residue_template_failed`` — residue→template build
          failed.
        * ``ligand_xml_write_failed`` — ParmEd / filesystem write failed.
    """
    result: dict[str, Any] = {
        "success": False,
        "xml_path": None,
        "residue_name": residue_name,
        "atom_count": None,
        "bond_count": None,
        "warnings": [],
        "errors": [],
        "code": None,
    }

    mol2_path = Path(mol2_path)
    frcmod_path = Path(frcmod_path)

    if not mol2_path.exists():
        result["code"] = "ligand_xml_mol2_missing"
        result["errors"].append(f"mol2 not found: {mol2_path}")
        return result
    if not frcmod_path.exists():
        result["code"] = "ligand_xml_frcmod_missing"
        result["errors"].append(f"frcmod not found: {frcmod_path}")
        return result

    try:
        import parmed
        from parmed.amber import AmberParameterSet
        from parmed.modeller import ResidueTemplate
        from parmed.openmm import OpenMMParameterSet
    except ImportError as exc:
        result["code"] = "ligand_xml_parmed_missing"
        result["errors"].append(
            f"parmed not importable for ligand XML conversion: {exc}. "
            f"Run `conda env update -f environment.yml`."
        )
        return result

    try:
        params = AmberParameterSet.from_leaprc(
            StringIO(f"parm = loadamberparams {frcmod_path}")
        )
    except Exception as exc:  # noqa: BLE001
        result["code"] = "ligand_xml_frcmod_invalid"
        result["errors"].append(
            f"ParmEd could not parse frcmod {frcmod_path}: "
            f"{type(exc).__name__}: {exc}"
        )
        return result

    try:
        struct = parmed.load_file(str(mol2_path), structure=True)
    except Exception as exc:  # noqa: BLE001
        result["code"] = "ligand_xml_load_failed"
        result["errors"].append(
            f"ParmEd could not load mol2 {mol2_path}: "
            f"{type(exc).__name__}: {exc}"
        )
        return result

    if not struct.residues:
        result["code"] = "ligand_xml_load_failed"
        result["errors"].append(f"mol2 {mol2_path} contained no residues")
        return result
    if len(struct.residues) > 1:
        result["warnings"].append(
            f"mol2 {mol2_path} has {len(struct.residues)} residues; "
            f"only the first ({struct.residues[0].name!r}) is registered."
        )

    res = struct.residues[0]
    if res.name != residue_name:
        result["warnings"].append(
            f"mol2 residue name {res.name!r} != requested {residue_name!r}; "
            f"renaming for template registration."
        )
        res.name = residue_name

    try:
        rt = ResidueTemplate.from_residue(res)
    except Exception as exc:  # noqa: BLE001
        result["code"] = "ligand_xml_residue_template_failed"
        result["errors"].append(
            f"ResidueTemplate.from_residue failed for {residue_name!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        return result
    params.residues[rt.name] = rt

    try:
        omm = OpenMMParameterSet.from_parameterset(params, remediate_residues=False)
    except Exception as exc:  # noqa: BLE001
        result["code"] = "ligand_xml_write_failed"
        result["errors"].append(
            f"OpenMMParameterSet.from_parameterset failed: "
            f"{type(exc).__name__}: {exc}"
        )
        return result

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        omm.write(str(out_path), write_unused=True, skip_duplicates=True)
    except Exception as exc:  # noqa: BLE001
        result["code"] = "ligand_xml_write_failed"
        result["errors"].append(
            f"OpenMMParameterSet.write({out_path}) failed: "
            f"{type(exc).__name__}: {exc}"
        )
        return result

    result["success"] = True
    result["xml_path"] = str(out_path)
    result["atom_count"] = len(rt.atoms)
    result["bond_count"] = len(rt.bonds)
    return result


def get_gaff_base_xml_path(name: str = _GAFF_BASE_DEFAULT) -> Optional[str]:
    """Resolve a shipped openmmforcefields GAFF base XML by stem name.

    Parameters
    ----------
    name
        Stem of the GAFF XML to resolve (e.g. ``"gaff-2.2.20"`` or
        ``"gaff-2.11"``). The shipped layout is
        ``<openmmforcefields>/ffxml/amber/gaff/ffxml/<name>.xml``.

    Returns
    -------
    str or None
        Absolute path string, or ``None`` if the file is not present.
    """
    try:
        import importlib.resources

        candidate = (
            importlib.resources.files("openmmforcefields")
            / "ffxml"
            / "amber"
            / "gaff"
            / "ffxml"
            / f"{name}.xml"
        )
    except (ImportError, AttributeError):
        return None
    abspath = str(candidate)
    return abspath if Path(abspath).exists() else None
