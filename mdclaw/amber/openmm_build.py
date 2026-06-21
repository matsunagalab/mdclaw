"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (``GAFFTemplateGenerator``), and emit a portable
  ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology parameterizes the small molecules
  with ``GAFFTemplateGenerator``.
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

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable, Optional, Dict, Any, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    atomic_write_text_group,
    ensure_directory, BaseToolWrapper,
)
from mdclaw._lock import file_lock  # noqa: E402
from mdclaw import forcefield_catalog as _ff_catalog  # noqa: E402
from mdclaw import _topology_pablo  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# --- Ligand charge-fitting timeout ------------------------------------------
# ``SystemGenerator.create_system`` lazily triggers GAFF parameterization for
# small molecules, which runs antechamber + sqm (AM1-BCC charge fitting). For a
# large, highly charged ligand (e.g. AP5, the bis-adenosine pentaphosphate in
# 1AKE) sqm can run for many minutes. The step *does* converge given enough
# wall time, so the failure mode we guard against is a too-short timeout
# aborting an otherwise-healthy build, not an unbounded hang.
#
# The floor below is the minimum we ever allow. ``MDCLAW_CHARGE_FIT_TIMEOUT``
# may *raise* the ceiling for exceptionally large ligands but can never lower
# it below the floor — this is deliberate so an agent (including a weak LLM
# driving the CLI) cannot shorten the charge-fitting budget and induce the
# spurious ``SQM_timeout`` failures we have seen in the benchmark. There is no
# CLI / function argument for this value, only the floored env override.
_CHARGE_FIT_TIMEOUT_FLOOR_SECONDS = 1800  # 30 min


def _resolve_charge_fit_timeout() -> int:
    """Return the charge-fitting timeout in seconds, never below the floor."""
    raw = os.environ.get("MDCLAW_CHARGE_FIT_TIMEOUT")
    if raw is None or str(raw).strip() == "":
        return _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS
    try:
        requested = int(float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring non-numeric MDCLAW_CHARGE_FIT_TIMEOUT=%r; using floor %ds",
            raw,
            _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS,
        )
        return _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS
    if requested < _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS:
        logger.warning(
            "MDCLAW_CHARGE_FIT_TIMEOUT=%ds is below the floor; clamping up to "
            "%ds (the charge-fitting budget can be raised but not shortened)",
            requested,
            _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS,
        )
        return _CHARGE_FIT_TIMEOUT_FLOOR_SECONDS
    return requested


@contextlib.contextmanager
def _charge_fit_timeout_guard(seconds: int):
    """Raise ``TimeoutError`` if the wrapped block runs longer than ``seconds``.

    Uses ``signal.alarm``, which is only available on the main thread; off the
    main thread (e.g. a threaded server) we skip the alarm rather than crash,
    accepting that the build can then run unbounded in that uncommon path.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.debug(
            "charge-fit timeout guard skipped (not main thread); build will "
            "run without a wall-clock limit"
        )
        yield
        return

    def _on_alarm(signum, frame):  # noqa: ANN001
        raise TimeoutError(
            f"ligand charge fitting (antechamber/sqm AM1-BCC) exceeded "
            f"{seconds}s; raise MDCLAW_CHARGE_FIT_TIMEOUT if the ligand is "
            f"exceptionally large"
        )

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

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

from mdclaw.amber.content_detection import _canonical_pablo_ion_resname, _normalize_pdb_chain_id, _rewrite_pablo_ion_pdb_line  # noqa: E402
from mdclaw.amber.forcefield_constants import _GLYCAM_LINKED_ASN_RESNAME  # noqa: E402
from mdclaw.amber.glycam_topology import _is_glycam_topology_residue, _normalize_glycam_topology  # noqa: E402
from mdclaw.amber.topology_bonds import _patch_ligand_molecule_internal_bonds, _patch_template_internal_bonds  # noqa: E402
from mdclaw.amber.topology_validation import _build_topology_validation_report, _unique_messages  # noqa: E402


_LIPID21_EXTERNAL_PAIR_KEYS = {
    frozenset((("PC", "C11"), ("PA", "C12"))),
    frozenset((("PE", "C11"), ("PA", "C12"))),
    frozenset((("PC", "C21"), ("OL", "C12"))),
    frozenset((("PE", "C21"), ("OL", "C12"))),
}
_LIPID21_EXTERNAL_RESNAMES = {"PC", "PE", "PA", "OL"}


def _lipid21_external_key(atom: Any) -> tuple[str, str] | None:
    """Return the lipid21 modular external key for ``atom``, if any."""
    residue_name = getattr(getattr(atom, "residue", None), "name", None)
    atom_name = getattr(atom, "name", None)
    key = (residue_name, atom_name)
    if residue_name in _LIPID21_EXTERNAL_RESNAMES:
        return key
    return None


def _lipid21_external_pair_allowed(atom_a: Any, atom_b: Any) -> bool:
    """Return whether a pair is chemically valid for lipid21 modular residues.

    Non-lipid pairs remain governed by the generic template-external-bond
    search. If either side is a lipid21 modular residue, both sides must be a
    known lipid21 external pair; this avoids random close contacts stealing a
    lipid head/tail bond budget before the chemically intended partner is seen.
    """
    key_a = _lipid21_external_key(atom_a)
    key_b = _lipid21_external_key(atom_b)
    if key_a is None and key_b is None:
        return True
    if key_a is None or key_b is None:
        return False
    return frozenset((key_a, key_b)) in _LIPID21_EXTERNAL_PAIR_KEYS


def _same_residue_identity(atom_a: Any, atom_b: Any) -> bool:
    """Return whether two atoms share chain id and residue id labels."""
    res_a = getattr(atom_a, "residue", None)
    res_b = getattr(atom_b, "residue", None)
    chain_a = getattr(getattr(res_a, "chain", None), "id", None)
    chain_b = getattr(getattr(res_b, "chain", None), "id", None)
    return (
        chain_a == chain_b
        and getattr(res_a, "id", None) == getattr(res_b, "id", None)
    )


def _external_pair_priority(atom_a: Any, atom_b: Any) -> int:
    """Lower priority value means a better external-bond match."""
    if _lipid21_external_key(atom_a) is None and _lipid21_external_key(atom_b) is None:
        return 0
    return 0 if _same_residue_identity(atom_a, atom_b) else 1


def _resolve_dna_name_from_libraries(nucleic_libraries: list[str]) -> Optional[str]:
    """Map a leaprc-style DNA library list to a forcefield_catalog DNA key."""
    for lib in nucleic_libraries:
        lower = (lib or "").lower()
        if "dna.ol15" in lower:
            return "OL15"
        if "dna.ol21" in lower:
            return "OL21"
        if "dna.bsc0" in lower:
            return "bsc0"
        if "dna.bsc1" in lower:
            return "bsc1"
    return None


def _resolve_rna_name_from_libraries(nucleic_libraries: list[str]) -> Optional[str]:
    """Map a leaprc-style RNA library list to a forcefield_catalog RNA key."""
    for lib in nucleic_libraries:
        lower = (lib or "").lower()
        if "rna.ol3" in lower:
            return "OL3"
        if "rna.roc" in lower:
            return "ROC"
        if "rna.yil" in lower:
            return "YIL"
    return None


def _resolve_phosaa_name_from_library(phosaa_library: Optional[str]) -> Optional[str]:
    """Map ``leaprc.phosaa19SB`` → ``"phosaa19SB"`` (catalog key)."""
    if not phosaa_library:
        return None
    lower = phosaa_library.lower()
    for key in ("phosaa19sb", "phosaa14sb", "phosaa10", "phosfb18"):
        if key in lower:
            return {"phosaa19sb": "phosaa19SB", "phosaa14sb": "phosaa14SB",
                    "phosaa10": "phosaa10", "phosfb18": "phosfb18"}[key]
    return None


def _resolve_glycan_name_from_library(glycan_library: Optional[str]) -> Optional[str]:
    if not glycan_library:
        return None
    if "06j-1" in glycan_library.lower():
        return "GLYCAM_06j-1"
    return None


def _hash_file(path: Path) -> Optional[str]:
    try:
        import hashlib
        with path.open("rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return None


def _positions_are_finite_for_report(positions: Any, unit_module: Any) -> bool:
    try:
        values = positions.value_in_unit(unit_module.nanometer)
    except AttributeError:
        values = positions

    def _walk(value: Any) -> bool:
        if isinstance(value, (str, bytes)):
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            pass
        try:
            return all(_walk(item) for item in value)
        except TypeError:
            return False

    return _walk(values)


def _position_count_for_report(positions: Any, unit_module: Any) -> Optional[int]:
    try:
        values = positions.value_in_unit(unit_module.nanometer)
    except AttributeError:
        values = positions
    try:
        return len(values)
    except TypeError:
        return None


def _record_topology_build_stage(
    job_dir: Optional[str],
    node_id: Optional[str],
    stage: str,
) -> None:
    """Best-effort progress breadcrumb for long topology builds."""
    if not (job_dir and node_id):
        return
    node_dir = Path(job_dir) / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with file_lock(node_dir / "node.lock"):
            data = json.loads(node_json.read_text())
            metadata = data.setdefault("metadata", {})
            metadata["topology_build_stage"] = stage
            metadata["topology_build_stage_updated_at"] = timestamp
            history = metadata.setdefault("topology_build_stage_history", [])
            if not history or history[-1].get("stage") != stage:
                history.append({"stage": stage, "updated_at": timestamp})
            tmp = node_json.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            os.replace(str(tmp), str(node_json))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not record topology build stage %s: %s", stage, exc)


def _run_openmmforcefields_build(
    *,
    pdb_path: Path,
    output_name: str,
    out_dir: Path,
    system_xml_file: Path,
    topology_pdb_file: Path,
    state_xml_file: Path,
    forcefield: str,
    water_model: Optional[str],
    phosaa_library: Optional[str],
    nucleic_libraries: list[str],
    glycan_library: Optional[str],
    is_membrane: bool,
    box_dimensions: Optional[Dict[str, float]],
    valid_ligands: list[Dict[str, Any]],
    valid_metal_params: list[Dict[str, Any]],
    valid_modxna_params: list[Dict[str, Any]],
    disulfide_bonds: Optional[list[Dict[str, Any]]],
    glycam_bond_plan: Optional[dict[str, Any]] = None,
    glycam_normalization_file: Optional[Path] = None,
    hmr: bool = True,
    implicit_solvent: Optional[str] = None,
    extra_xml: Optional[list[str]] = None,
    extra_smiles: Optional[list[Tuple[str, str]]] = None,
    stage_callback: Optional[Callable[[str], None]] = None,
    minimization_report_file: Optional[Path] = None,
    minimize_max_iterations: int = 1000,
) -> Dict[str, Any]:
    """Build an OpenMM ``System`` for the given prepared PDB.

    Replaces the legacy tleap path. Returns a dict shaped like the
    ``build_amber_system`` partial-result, with these keys:

    - ``success`` (bool)
    - ``errors`` (list[str])
    - ``warnings`` (list[str])
    - ``system_xml`` / ``topology_pdb`` / ``state_xml`` (str paths) on success
    - ``num_atoms`` / ``num_residues`` (int) on success
    - ``forcefield_provenance`` (dict) on success
    """
    result: Dict[str, Any] = {
        "success": False,
        "errors": [],
        "warnings": [],
        "topology_notes": [],
    }
    demotable_warnings: list[str] = []
    patch_summary: Dict[str, Any] = {
        "ligand_molecule_internal_bonds_added": 0,
        "template_internal_bonds_added": 0,
        "external_bonds_added": 0,
        "unpaired_external_atom_count": 0,
        "unpaired_lipid21_external_atom_count": 0,
        "nln_renamed_to_asn_count": 0,
        "orphan_glycam_residues_dropped_count": 0,
        "add_extra_particles_completed": None,
    }
    manual_disulfide_added_count = 0

    def _record_demotable_warning(message: str) -> None:
        """Keep context during failures, but demote to notes after validation."""
        result["warnings"].append(message)
        demotable_warnings.append(message)
        result["topology_notes"].append(message)

    has_explicit_glycam_plan = bool(glycam_bond_plan and glycam_bond_plan.get("bonds"))
    if glycam_bond_plan:
        result["glycam_bond_plan"] = glycam_bond_plan
    if minimization_report_file is None:
        minimization_report_file = out_dir / f"{output_name}.minimization_report.json"
    if glycam_normalization_file is None:
        glycam_normalization_file = out_dir / f"{output_name}.glycam_normalization.json"
    extra_xml = list(extra_xml or [])
    extra_smiles = list(extra_smiles or [])

    def _stage(stage: str) -> None:
        if stage_callback:
            stage_callback(stage)

    # --- 1. Resolve OpenMM XML bundle via the catalog --------------------
    _stage("resolve_forcefield_xml")
    # Implicit-solvent (GB) systems load an extra ``implicit/*.xml`` from
    # the openmmforcefields shipped tree, which contributes the
    # ``CustomGBForce`` (HCT / OBC1 / OBC2 / GBn / GBn2) that
    # ``XmlSerializer`` then bakes into ``system.xml``. The run-side shim
    # verifies that force is present before honoring an
    # ``implicitSolvent`` request, so a missing GB force after build is a
    # structured-failure case (``implicit_solvent_force_missing``).
    canon_protein = _ff_catalog.normalize_protein(forcefield) or forcefield
    canon_water = _ff_catalog.normalize_water(water_model) if water_model else None
    canon_implicit = (
        _ff_catalog.normalize_implicit_solvent(implicit_solvent)
        if implicit_solvent
        else None
    )
    phosaa_name = _resolve_phosaa_name_from_library(phosaa_library)
    dna_name = _resolve_dna_name_from_libraries(nucleic_libraries)
    rna_name = _resolve_rna_name_from_libraries(nucleic_libraries)
    glycan_name = _resolve_glycan_name_from_library(glycan_library)
    lipid_name = "lipid21" if is_membrane else None

    if canon_implicit and canon_implicit not in _ff_catalog.IMPLICIT_SOLVENT_XML:
        # The public ``build_amber_system`` already guards this path, but
        # direct callers of this helper still get a clean structured code.
        supported = ", ".join(_ff_catalog.supported_implicit_solvent_models())
        result["errors"].append(
            f"Unknown implicit-solvent model {implicit_solvent!r}. "
            f"Supported: {supported}."
        )
        result["code"] = "implicit_solvent_model_unsupported"
        return result

    # All ligands are parameterized at topology time by GAFFTemplateGenerator
    # (GAFF2 / AM1-BCC) via SystemGenerator below.
    for rec in valid_ligands or []:
        if str(rec.get("residue_name") or "").upper():
            rec["topology_parameter_source"] = "topology_gaff_template_generator"

    xml_bundle = _ff_catalog.resolve_xml_bundle(
        protein=canon_protein,
        water=canon_water,
        phosaa=phosaa_name,
        dna=dna_name,
        rna=rna_name,
        glycan=glycan_name,
        lipid=lipid_name,
        implicit_solvent=canon_implicit,
        extra_xml=list(extra_xml),
    )
    if not xml_bundle:
        result["errors"].append(
            f"Could not resolve any OpenMM ForceField XML for forcefield={forcefield!r} "
            f"water={water_model!r}. Use extra_xml to supply specialty FFs."
        )
        return result

    # --- 2. Validate prepared input ownership + Pablo load -----------------
    # Topology generation must not repair or rewrite the prepared structure.
    # Missing atoms/hydrogens are a prep-stage problem; here they should surface
    # as structured topology/template failures instead of being silently
    # patched by PDBFixer. This preserves prep-owned residue names such as GLH.
    _stage("topology_input_ready")
    pablo_input = pdb_path

    # Load ligand chemistry into OpenFF Molecules early so we can (a) feed
    # Pablo SMILES for non-CCD ligands like BEN, and (b) hand the
    # molecules to ``SystemGenerator`` / ``GAFFTemplateGenerator`` below.
    # Standard prep emits SDF chemistry records; SMILES is the fallback when
    # no coordinate-bearing SDF is available.
    try:
        from openff.toolkit import Molecule as _Molecule  # local import
    except ImportError as exc:
        result["errors"].append(
            f"openff-toolkit not importable for ligand load: {exc}. "
            f"Run `conda env update -f environment.yml`."
        )
        return result

    def _load_ligand_molecule(ligand_entry: Dict[str, Any]) -> Any:
        sdf_path = (
            ligand_entry.get("sdf")
            or ligand_entry.get("sdf_file")
            or ligand_entry.get("coordinate_file")
        )
        smiles = ligand_entry.get("smiles") or ligand_entry.get("smiles_used")
        if sdf_path:
            try:
                return _Molecule.from_file(
                    str(sdf_path),
                    allow_undefined_stereo=True,
                )
            except TypeError:
                return _Molecule.from_file(str(sdf_path))
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(
                    f"Could not build OpenFF Molecule for ligand "
                    f"{ligand_entry.get('residue_name', '?')!r} from stored "
                    f"SDF {sdf_path!r}; trying SMILES fallback: "
                    f"{type(exc).__name__}: {exc}"
                )
        if smiles:
            try:
                mol = _Molecule.from_smiles(
                    str(smiles),
                    hydrogens_are_explicit=False,
                    allow_undefined_stereo=True,
                    name=str(ligand_entry.get("residue_name") or ""),
                )
                mol.generate_conformers(n_conformers=1)
                return mol
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(
                    f"Could not build OpenFF Molecule for ligand "
                    f"{ligand_entry.get('residue_name', '?')!r} from stored SMILES: "
                    f"{type(exc).__name__}: {exc}"
                )
        raise ValueError(
            f"Ligand {ligand_entry.get('residue_name', '?')!r} has no usable "
            "SDF/SMILES chemistry record"
        )

    _stage("load_ligand_molecules")
    ligand_molecules: list[Any] = []
    for lig in valid_ligands or []:
        sdf = lig.get("sdf") or lig.get("sdf_file") or lig.get("coordinate_file")
        smiles = lig.get("smiles") or lig.get("smiles_used")
        if not (sdf or smiles):
            result["errors"].append(
                f"Ligand entry {lig.get('residue_name', '?')!r} is missing "
                f"chemistry input — expected SDF/SMILES from ligand_chemistry."
            )
            return result
        try:
            ligand_molecules.append(_load_ligand_molecule(lig))
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                f"Failed to load ligand chemistry for "
                f"{lig.get('residue_name', '?')!r}: "
                f"{type(exc).__name__}: {exc}. The OpenFF GAFF generator "
                f"needs every ligand as a Molecule; without it the topology "
                f"build fails downstream with 'No template found'."
            )
            result["code"] = "ligand_molecule_load_failed"
            return result

    # Hand the loaded ligands to Pablo as ``(residue_name, smiles)`` pairs so
    # its CCD matcher sees the GAFF-typed ligand as a registered
    # additional definition. Without this, Pablo's PDBFile fallback emits a
    # topology with zero internal bonds for unknown ligand residues, and
    # ``SystemGenerator.create_system`` then fails with "No template found"
    # (graph-isomorphism cannot match an unbonded residue).
    ligand_extra_smiles: list[Tuple[str, str]] = []
    for lig, mol in zip(valid_ligands or [], ligand_molecules):
        residue_name = lig.get("residue_name")
        if residue_name and mol is not None:
            try:
                ligand_extra_smiles.append((residue_name, mol.to_smiles()))
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(
                    f"Could not derive SMILES for ligand {residue_name!r}: "
                    f"{type(exc).__name__}: {exc}; Pablo may fall back to "
                    f"PDBFile and emit an under-bonded topology."
                )

    pablo_smiles = list(extra_smiles) + ligand_extra_smiles

    # Sanitize residue names that Pablo's CCD-based loader does not
    # recognise: packmol-memgen / Amber emit ions as ``Na+`` / ``Cl-`` /
    # ``K+`` (3-letter residue name carries the charge sigil), but CCD
    # ships only the bare element codes ``NA`` / ``CL`` / ``K``. Without
    # this rewrite Pablo bails on the entire topology and falls back to
    # ``openmm.app.PDBFile``, which then leaves ligand residues like BEN
    # without internal bonds — ``SystemGenerator.create_system`` then
    # fails with the cryptic "No template found for residue 223 (BEN)".
    # Round-trip Amber's protonation variants through CCD-canonical residue
    # names for Pablo, then restore Amber names after load so
    # ``protein.ff*.xml``'s protonation-specific templates apply.
    # Map non-CCD ion names → CCD canonical (residue + atom). PDBFixer
    # often re-aligns these fields, so match on stripped value rather than
    # exact bytes and re-emit with PDB-format padding.
    _HIS_AMBER_VARIANTS = ("HID", "HIE", "HIP", "HSD", "HSE", "HSP")
    _PABLO_AMBER_VARIANT_BASES = {
        "ASH": "ASP",
        "GLH": "GLU",
        "LYN": "LYS",
        "CYM": "CYS",
    }

    his_amber_resids: set[tuple[str, str]] = set()
    amber_variant_resids: dict[tuple[str, str], str] = {}
    sanitized_input = pablo_input
    needs_sanitize = False
    try:
        with pablo_input.open() as fh:
            for line in fh:
                if line.startswith(("ATOM  ", "HETATM")):
                    rn = line[17:20].strip()
                    if (_canonical_pablo_ion_resname(rn) is not None
                            or rn in _HIS_AMBER_VARIANTS
                            or rn in _PABLO_AMBER_VARIANT_BASES):
                        needs_sanitize = True
                        break
    except OSError:
        pass

    if needs_sanitize:
        sanitized_input = out_dir / f"{output_name}.pablo_input.pdb"
        with pablo_input.open() as fh_in, sanitized_input.open("w") as fh_out:
            for line in fh_in:
                if line.startswith(("ATOM  ", "HETATM")):
                    raw_resname = line[17:20]
                    rn_strip = raw_resname.strip()
                    rewritten, ion_changed = _rewrite_pablo_ion_pdb_line(line)
                    if ion_changed:
                        line = rewritten
                    elif rn_strip in _HIS_AMBER_VARIANTS:
                        chain_id = _normalize_pdb_chain_id(line[21:22])
                        resseq = line[22:26]
                        his_amber_resids.add((chain_id, resseq.strip()))
                        line = line[:17] + "HIS" + line[20:]
                    elif rn_strip in _PABLO_AMBER_VARIANT_BASES:
                        chain_id = _normalize_pdb_chain_id(line[21:22])
                        resseq = line[22:26]
                        amber_variant_resids[(chain_id, resseq.strip())] = rn_strip
                        base_name = _PABLO_AMBER_VARIANT_BASES[rn_strip]
                        line = line[:17] + f"{base_name:>3}" + line[20:]
                fh_out.write(line)

    _stage("pablo_load")
    pablo_result = _topology_pablo.load_topology(
        sanitized_input, extra_smiles=pablo_smiles
    )
    for warning in pablo_result.warnings:
        _record_demotable_warning(warning)
    omm_topology = pablo_result.topology
    omm_positions = pablo_result.positions

    # Restore Amber HID/HIE/HIP residue names on the loaded topology so
    # ``protein.ff*.xml``'s protonation-specific templates apply. Pablo
    # loaded these as canonical HIS via the CCD; pick the variant from the
    # H atoms that survived the load (``protein.ff19SB.xml`` lacks a HIS
    # template entirely so leaving them as HIS would crash create_system).
    if his_amber_resids:
        for residue in omm_topology.residues():
            if residue.name != "HIS":
                continue
            chain_id = _normalize_pdb_chain_id(residue.chain.id)
            if (chain_id, str(residue.id)) not in his_amber_resids:
                continue
            atoms = {a.name for a in residue.atoms()}
            if "HD1" in atoms and "HE2" in atoms:
                residue.name = "HIP"
            elif "HD1" in atoms:
                residue.name = "HID"
            elif "HE2" in atoms:
                residue.name = "HIE"
            else:
                residue.name = "HID"

    if amber_variant_resids:
        for residue in omm_topology.residues():
            chain_id = _normalize_pdb_chain_id(residue.chain.id)
            variant = amber_variant_resids.get((chain_id, str(residue.id)))
            if variant:
                residue.name = variant

    # Strip the HOP2 / HOP3 protons that ``phosphorylate_residues`` added
    # only so Pablo's CCD-shipped (protonated) PHOSPHOSERINE /
    # PHOSPHOTHREONINE / PHOSPHOTYROSINE template would match. Amber's
    # phosaa19SB / phosaa14SB / phosaa10 templates are dianion (no proton
    # on phosphate oxygens); keeping HOP2 / HOP3 would now make
    # ``SystemGenerator.create_system`` fail with "Unknown atom names:
    # HOP2 / HOP3" for the topology side.
    _PHOSPHO_DROP_HS = {"HOP2", "HOP3", "HOP1"}
    _PHOSPHO_RES_NAMES = {"SEP", "TPO", "PTR"}
    drop_atoms = [
        atom for atom in omm_topology.atoms()
        if atom.residue.name in _PHOSPHO_RES_NAMES
        and atom.name in _PHOSPHO_DROP_HS
    ]
    if drop_atoms:
        from openmm.app import Modeller as _Modeller
        modeller = _Modeller(omm_topology, omm_positions)
        modeller.delete(drop_atoms)
        omm_topology = modeller.topology
        omm_positions = modeller.positions

    # --- 3. Disulfide bonds (Pablo does not auto-detect) -----------------
    if disulfide_bonds:
        manual_disulfide_added_count = _topology_pablo.add_disulfide_bonds(
            omm_topology,
            disulfide_bonds,
        )

    # --- 4. Set unit cell for explicit solvent ---------------------------
    if not box_dimensions:
        # Implicit / vacuum builds must not carry a periodic box, otherwise
        # SystemGenerator picks PME and the typical small CRYST1 placeholder
        # in the input PDB triggers a "cutoff > half box" error during
        # minimization.
        try:
            omm_topology.setPeriodicBoxVectors(None)
        except Exception:  # noqa: BLE001
            pass

    if box_dimensions:
        try:
            from openmm import unit, Vec3
            box_a = box_dimensions.get("box_a", 0)
            box_b = box_dimensions.get("box_b", 0)
            box_c = box_dimensions.get("box_c", 0)
            if box_a > 0 and box_b > 0 and box_c > 0:
                # PBC-safe margin (matches the legacy 2.0 Å buffer policy).
                pbc_margin = 2.0
                box_a += pbc_margin
                box_b += pbc_margin
                box_c += pbc_margin
                # Box dims arrive in Å; convert to nm and wrap as a single
                # Quantity so OpenMM's serializer keeps the float / unit
                # split consistent (Vec3-Quantity-of-Quantity drops floats).
                box_vectors = unit.Quantity(
                    value=[
                        Vec3(box_a / 10.0, 0.0, 0.0),
                        Vec3(0.0, box_b / 10.0, 0.0),
                        Vec3(0.0, 0.0, box_c / 10.0),
                    ],
                    unit=unit.nanometer,
                )
                omm_topology.setPeriodicBoxVectors(box_vectors)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(
                f"Could not set periodic box: {type(exc).__name__}: {exc}"
            )

    # --- 5. SystemGenerator + Modeller (extra particles, ligand mols) ----
    try:
        from openmm import app, unit, XmlSerializer, LangevinIntegrator
        from openmm.app import Modeller, PDBFile, Simulation
        from openmmforcefields.generators import SystemGenerator
    except ImportError as exc:
        result["errors"].append(
            f"openmmforcefields stack not importable: {exc}. "
            f"Run `conda env update -f environment.yml`."
        )
        return result

    # SystemGenerator splits the kwargs by periodicity so the same generator
    # can build either kind of System. HMR is a build-time decision: when
    # the user opts in we bake ``hydrogenMass=4 amu`` into every System this
    # generator emits, and the same value is recorded in the provenance dict
    # so the run-side XML system validator can match it later.
    common_kwargs: Dict[str, Any] = {"constraints": app.HBonds, "rigidWater": True}
    if hmr:
        common_kwargs["hydrogenMass"] = 4.0 * unit.amu
    periodic_kwargs: Dict[str, Any] = {
        "nonbondedMethod": app.PME,
        "nonbondedCutoff": 1.0 * unit.nanometer,
    }
    nonperiodic_kwargs: Dict[str, Any] = {"nonbondedMethod": app.NoCutoff}

    ligand_molecules_for_gaff = list(ligand_molecules)

    _stage("system_generator_init")
    try:
        sg = SystemGenerator(
            forcefields=xml_bundle,
            small_molecule_forcefield="gaff-2.11",
            molecules=ligand_molecules_for_gaff or None,
            forcefield_kwargs=common_kwargs,
            periodic_forcefield_kwargs=periodic_kwargs,
            nonperiodic_forcefield_kwargs=nonperiodic_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"SystemGenerator init failed: {type(exc).__name__}: {exc}. "
            f"Bundle: {xml_bundle}"
        )
        return result

    # Metal frcmod+mol2 and modXNA frcmod+lib are NOT yet routed through
    # SystemGenerator: under the openmmforcefields path they would silently
    # fall through to the ForceField unmatched, eventually crashing inside
    # ``create_system`` with an opaque ``No template found`` error. Fail-fast
    # with a structured ``code`` so callers can route the user toward
    # ``build_openmm_system`` with a pre-built OpenMM ForceField XML port
    # of the metal / modXNA parameters until the ParmEd → OpenMM XML
    # bridge ships in ``forcefield_catalog``.
    if valid_metal_params:
        result["errors"].append(
            f"Metal parameters detected ({len(valid_metal_params)} sets) but the "
            f"openmmforcefields path does not yet provide a ParmEd → OpenMM XML "
            f"bridge from frcmod+mol2. Use ``build_openmm_system`` with a "
            f"pre-converted OpenMM ForceField XML for the metal residue "
            f"(research escape hatch); the same system.xml + topology.pdb + "
            f"state.xml triple flows to min/eq/prod."
        )
        result["code"] = "metal_openmm_xml_required"
        return result

    if valid_modxna_params:
        result["errors"].append(
            f"modXNA parameters detected ({len(valid_modxna_params)} sets) but the "
            f"openmmforcefields path does not yet provide a ParmEd → OpenMM XML "
            f"bridge from frcmod+lib. Use ``build_openmm_system`` with a "
            f"pre-converted OpenMM ForceField XML for the modified residue "
            f"(research escape hatch); the same system.xml + topology.pdb + "
            f"state.xml triple flows to min/eq/prod."
        )
        result["code"] = "modxna_openmm_xml_required"
        return result

    # Patch missing intra-residue bonds for residues that the loaded
    # forcefield knows but Pablo / PDBFile under-bonded. ``packmol-memgen``
    # does not emit CONECT records for lipid21 residues, and ``cpptraj
    # prepareforleap`` can leave GLYCAM/NLN residues either wholly unbonded
    # or partially bonded depending on which CONECT records survived the
    # PDB round trip. Copy any missing template bond onto the topology so
    # ``SystemGenerator.create_system`` sees the same residue graph as the
    # loaded force field.
    ligand_molecule_bonds_added = _patch_ligand_molecule_internal_bonds(
        omm_topology,
        valid_ligands or [],
        ligand_molecules,
    )
    if ligand_molecule_bonds_added:
        patch_summary["ligand_molecule_internal_bonds_added"] = (
            ligand_molecule_bonds_added
        )
        _record_demotable_warning(
            f"Patched {ligand_molecule_bonds_added} ligand bond(s) "
            f"from ligand_chemistry OpenFF Molecule records so "
            f"GAFFTemplateGenerator can match the residue graph."
        )

    bonds_added = _patch_template_internal_bonds(omm_topology, sg.forcefield)
    if bonds_added:
        patch_summary["template_internal_bonds_added"] = bonds_added
        _record_demotable_warning(
            f"Patched {bonds_added} intra-residue bond(s) onto topology "
            f"residues whose Pablo / PDBFile load left under-bonded "
            f"(lipid21 / GLYCAM templates supply the missing bonds)."
        )

    if has_explicit_glycam_plan:
        _stage("glycam_topology_normalization")
        omm_topology, omm_positions, glycam_normalization = _normalize_glycam_topology(
            omm_topology=omm_topology,
            omm_positions=omm_positions,
            glycam_bond_plan=glycam_bond_plan or {},
            protein_forcefield=canon_protein,
            phosaa_name=phosaa_name,
            dna_name=dna_name,
            rna_name=rna_name,
            glycan_name=glycan_name,
            lipid_name=lipid_name,
            app_module=app,
            unit_module=unit,
        )
        result["glycam_normalization"] = glycam_normalization
        glycam_normalization_file.write_text(
            json.dumps(glycam_normalization, indent=2),
            encoding="utf-8",
        )
        if not glycam_normalization.get("completed"):
            result["errors"].extend(glycam_normalization.get("errors", []))
            result["warnings"].extend(glycam_normalization.get("warnings", []))
            result["code"] = (
                glycam_normalization.get("code")
                or "glycam_topology_normalization_failed"
            )
            return result
        result["warnings"].extend(glycam_normalization.get("warnings", []))
        _record_demotable_warning(
            "Applied GLYCAM topology normalization from cpptraj prepareforleap "
            f"bond plan ({glycam_normalization['glycam_bond_plan']['applied_count']} "
            "bond(s)); glycan hydrogens were completed without generic protein repair."
        )

    # Patch missing inter-residue (external) bonds. ``packmol-memgen`` and
    # ``cpptraj prepareforleap`` write residues with the right geometry but
    # rely on tleap/parmed-side bond inference to connect them. The
    # template's ``externalBonds`` field tells us which atom in each
    # residue is supposed to dangle out to a neighbor; a small spatial
    # search (2.0 Å heavy-atom cutoff) wires them
    # up. Without this, ``SystemGenerator.create_system`` fails with the
    # protein-FF "missing 1 C atom externally bonded" cascade once the
    # adjacent residue (LEU next to a glycan, PA next to PC, etc.) cannot
    # complete its peptide / lipid linkage.
    try:
        from openmm import unit as _unit
    except ImportError:
        _unit = None
    if _unit is not None:
        # Per-atom external-bond budget so we never exceed what the
        # template advertises (the budget already reflects existing
        # cross-residue bonds Pablo / PDBFile produced).
        existing_bonds: set[tuple[int, int]] = set()
        cross_bonds_per_atom: dict[int, int] = {}
        for bond in omm_topology.bonds():
            i1, i2 = sorted((bond.atom1.index, bond.atom2.index))
            existing_bonds.add((i1, i2))
            if bond.atom1.residue.index != bond.atom2.residue.index:
                cross_bonds_per_atom[bond.atom1.index] = (
                    cross_bonds_per_atom.get(bond.atom1.index, 0) + 1
                )
                cross_bonds_per_atom[bond.atom2.index] = (
                    cross_bonds_per_atom.get(bond.atom2.index, 0) + 1
                )
        ext_candidates: list[tuple[Any, int, str]] = []
        ext_budget: dict[int, int] = {}
        for residue in omm_topology.residues():
            if has_explicit_glycam_plan and (
                residue.name == _GLYCAM_LINKED_ASN_RESNAME
                or _is_glycam_topology_residue(residue.name)
            ):
                continue
            template = sg.forcefield._templates.get(residue.name)
            if template is None or not template.externalBonds:
                continue
            atom_by_name = {a.name: a for a in residue.atoms()}
            template_external_count: dict[str, int] = {}
            for ti in template.externalBonds:
                name = template.atoms[ti].name
                template_external_count[name] = template_external_count.get(name, 0) + 1
            for name, expected in template_external_count.items():
                atom = atom_by_name.get(name)
                if atom is None:
                    continue
                remaining = expected - cross_bonds_per_atom.get(atom.index, 0)
                if remaining <= 0:
                    continue
                ext_budget[atom.index] = remaining
                ext_candidates.append((atom, atom.residue.index, name))
        positions_nm = [p.value_in_unit(_unit.nanometer) for p in omm_positions]
        box_lengths_nm: tuple[float, float, float] | None = None
        if box_dimensions:
            try:
                box_lengths_nm = (
                    float(box_dimensions["box_a"]) * 0.1,
                    float(box_dimensions["box_b"]) * 0.1,
                    float(box_dimensions["box_c"]) * 0.1,
                )
            except (KeyError, TypeError, ValueError):
                box_lengths_nm = None

        def _distance_sq_nm(atom_a: Any, atom_b: Any) -> float:
            xa, ya, za = positions_nm[atom_a.index]
            xb, yb, zb = positions_nm[atom_b.index]
            dx = xa - xb
            dy = ya - yb
            dz = za - zb
            # Modular lipid fragments can straddle the periodic boundary in
            # packmol-memgen output. Use minimum-image distances only for
            # lipid-lipid external candidates; protein/glycan linkages stay
            # in ordinary coordinate space to avoid inventing terminal bonds
            # across the box.
            if (
                box_lengths_nm is not None
                and _lipid21_external_key(atom_a) is not None
                and _lipid21_external_key(atom_b) is not None
            ):
                lx, ly, lz = box_lengths_nm
                if lx > 0:
                    dx -= round(dx / lx) * lx
                if ly > 0:
                    dy -= round(dy / ly) * ly
                if lz > 0:
                    dz -= round(dz / lz) * lz
            return dx * dx + dy * dy + dz * dz

        ext_bonds_added = 0
        seen_pairs: set[tuple[int, int]] = set()
        # Two-pass global matching: first pass only considers candidates whose
        # residue names differ, so chemically meaningful lipid21 pairs like
        # ``PC.C21 ↔ OL.C12`` win over same-name overlaps between nearby
        # leaflet lipids. Same-name pairings are still permitted on the second
        # pass for legitimate glycan-glycan polymerisation (``0YB ↔ 0YB`` etc.).
        # Build all pair options and sort by a chemistry-aware priority before
        # consuming budgets; a pure per-atom nearest-neighbour loop can steal a
        # lipid modular-residue external bond from its same lipid id partner in
        # dense imperfect Packmol layouts.
        # 2.0 Å heavy-atom cutoff for both passes — covers C-O / C-C ester
        # linkages in lipid21 and the GLYCAM glycosidic O-C bond.
        for restrict_cross_name in (True, False):
            pair_options: list[tuple[int, float, int, int, Any, Any]] = []
            for i, (atom_a, res_a, _name_a) in enumerate(ext_candidates):
                for j in range(i + 1, len(ext_candidates)):
                    atom_b, res_b, _name_b = ext_candidates[j]
                    if res_a == res_b:
                        continue
                    if restrict_cross_name and atom_a.residue.name == atom_b.residue.name:
                        continue
                    if not _lipid21_external_pair_allowed(atom_a, atom_b):
                        continue
                    k = tuple(sorted((atom_a.index, atom_b.index)))
                    if k in existing_bonds or k in seen_pairs:
                        continue
                    d2 = _distance_sq_nm(atom_a, atom_b)
                    if d2 >= 0.20 * 0.20:
                        continue
                    pair_options.append(
                        (
                            _external_pair_priority(atom_a, atom_b),
                            d2,
                            atom_a.index,
                            atom_b.index,
                            atom_a,
                            atom_b,
                        )
                    )
            pair_options.sort()
            for _priority, _d2, _idx_a, _idx_b, atom_a, atom_b in pair_options:
                if ext_budget.get(atom_a.index, 0) <= 0:
                    continue
                if ext_budget.get(atom_b.index, 0) <= 0:
                    continue
                k = tuple(sorted((atom_a.index, atom_b.index)))
                if k in existing_bonds or k in seen_pairs:
                    continue
                omm_topology.addBond(atom_a, atom_b)
                seen_pairs.add(k)
                ext_bonds_added += 1
                ext_budget[atom_a.index] -= 1
                ext_budget[atom_b.index] = ext_budget.get(atom_b.index, 0) - 1
        if ext_bonds_added:
            patch_summary["external_bonds_added"] = ext_bonds_added
            _record_demotable_warning(
                f"Patched {ext_bonds_added} inter-residue bond(s) connecting "
                f"residues whose templates declare external bonds but the "
                f"loader emitted them unconnected (lipid21 head/tail or "
                f"GLYCAM glycan-glycan linkages)."
            )
        # Debug: residues whose external-bond budget remained > 0 after the
        # patcher pass — these will fail downstream with "missing N C atom
        # externally bonded" so surface them as a warning the caller can act
        # on (typically a packmol-memgen layout where headgroups are too far
        # apart to bond, or a glycan branch with an unexpected partner).
        unbonded_externals: list[str] = []
        unbonded_lipid21_externals: list[str] = []
        for atom_idx, remaining in ext_budget.items():
            if remaining > 0:
                atom = next(
                    (a for a in omm_topology.atoms() if a.index == atom_idx),
                    None,
                )
                if atom is not None:
                    label = f"{atom.residue.name}#{atom.residue.id}.{atom.name}"
                    unbonded_externals.append(label)
                    if atom.residue.name in _LIPID21_EXTERNAL_RESNAMES:
                        unbonded_lipid21_externals.append(label)
        patch_summary["unpaired_external_atom_count"] = len(unbonded_externals)
        patch_summary["unpaired_lipid21_external_atom_count"] = len(
            unbonded_lipid21_externals
        )
        if unbonded_externals:
            result["warnings"].append(
                f"External-bond patcher could not pair {len(unbonded_externals)} "
                f"atom(s) within the 2.0 Å heavy-atom cutoff: "
                f"{unbonded_externals[:5]}"
                f"{'...' if len(unbonded_externals) > 5 else ''}"
            )
        if unbonded_lipid21_externals:
            result["code"] = "lipid21_external_bond_patching_failed"
            result["recommended_next_action"] = (
                "retry_membrane_embedding_with_new_layout_or_larger_lateral_box"
            )

        # Salvage NLN residues whose glycan partner was missing from the
        # prep output (cpptraj's ``prepareforleap`` writes NLN at every
        # detected N-glycan site but the matching glycan chain may be
        # spatially detached after the merge). With no glycan to bond to,
        # the residue is functionally a plain ASN — rename it so
        # ``addHydrogens`` can place HD22 from the ASN template and
        # ``protein.ff*.xml`` matches the side chain.
        if not has_explicit_glycam_plan:
            nln_renamed = 0
            for residue in omm_topology.residues():
                if residue.name != "NLN":
                    continue
                nd2 = next((a for a in residue.atoms() if a.name == "ND2"), None)
                if nd2 is None:
                    continue
                if ext_budget.get(nd2.index, 0) > 0:
                    residue.name = "ASN"
                    nln_renamed += 1
            if nln_renamed:
                patch_summary["nln_renamed_to_asn_count"] = nln_renamed
                result["warnings"].append(
                    f"Renamed {nln_renamed} NLN residue(s) without a matched "
                    f"glycan partner back to ASN (addHydrogens fills in HD22 "
                    f"and the protein FF treats them as plain asparagine)."
                )

        # Drop orphan GLYCAM residues whose external bonds are still
        # unpaired — these arise when ``cpptraj prepareforleap`` lays out
        # a glycan chain whose attachment-site partner (NLN, another
        # glycan) was relocated by the merge step beyond bond range.
        # Without a partner the residue is unbonded and the run-side FF
        # has no template that matches it; ``Modeller.delete`` removes
        # the dangling residue (and any waters / ions caught by chain
        # continuity).
        _GLYCAN_RESNAMES = {
            "0YB", "4YA", "4YB", "0LB", "VMB", "0MB", "0fA", "2MA", "0LA",
            "BMA", "MAN", "NAG", "0YA", "4YS", "0LS",
        }
        # Iterate: dropping one orphan glycan can leave its neighbour
        # glycans with their own unpaired external bonds. Recompute the
        # actual cross-residue bond count from the topology each pass and
        # delete any GLYCAM residue whose realised external-bond count is
        # less than its template demands. Cap at a few iterations so a
        # bug here cannot loop indefinitely on a healthy glycan tree.
        if not has_explicit_glycam_plan:
            from openmm.app import Modeller as _ModellerForOrphans
            all_dropped: list[str] = []
            for _orphan_pass in range(8):
                cross_bonds_now: dict[int, int] = {}
                for bond in omm_topology.bonds():
                    if bond.atom1.residue.index != bond.atom2.residue.index:
                        cross_bonds_now[bond.atom1.index] = (
                            cross_bonds_now.get(bond.atom1.index, 0) + 1
                        )
                        cross_bonds_now[bond.atom2.index] = (
                            cross_bonds_now.get(bond.atom2.index, 0) + 1
                        )
                this_round: list[Any] = []
                for residue in omm_topology.residues():
                    if residue.name not in _GLYCAN_RESNAMES:
                        continue
                    template = sg.forcefield._templates.get(residue.name)
                    if template is None or not template.externalBonds:
                        continue
                    atom_by_name = {a.name: a for a in residue.atoms()}
                    template_external_count: dict[str, int] = {}
                    for ti in template.externalBonds:
                        name = template.atoms[ti].name
                        template_external_count[name] = (
                            template_external_count.get(name, 0) + 1
                        )
                    unpaired = False
                    for name, expected in template_external_count.items():
                        atom = atom_by_name.get(name)
                        if atom is None:
                            continue
                        if cross_bonds_now.get(atom.index, 0) < expected:
                            unpaired = True
                            break
                    if unpaired:
                        this_round.append(residue)
                if not this_round:
                    break
                mod = _ModellerForOrphans(omm_topology, omm_positions)
                mod.delete([a for r in this_round for a in r.atoms()])
                omm_topology = mod.topology
                omm_positions = mod.positions
                all_dropped.extend(f"{r.name}#{r.id}" for r in this_round)
            if all_dropped:
                patch_summary["orphan_glycam_residues_dropped_count"] = len(
                    all_dropped
                )
                result["warnings"].append(
                    f"Dropped {len(all_dropped)} orphan GLYCAM residue(s) whose "
                    f"external bond partner was missing from the prep output: "
                    f"{all_dropped[:5]}"
                    f"{'...' if len(all_dropped) > 5 else ''}"
                )

    _stage("modeller_prepare")
    modeller = Modeller(omm_topology, omm_positions)
    # Do not call Modeller.addHydrogens here. Prep owns atom/H completeness and
    # protonation-state labels; topology build only adds force-field-required
    # extra particles (for example OPC virtual sites) and then validates by
    # attempting SystemGenerator.create_system.
    try:
        modeller.addExtraParticles(sg.forcefield)
        patch_summary["add_extra_particles_completed"] = True
    except Exception as exc:  # noqa: BLE001
        patch_summary["add_extra_particles_completed"] = False
        result["warnings"].append(
            f"addExtraParticles failed (continuing without virtual sites): "
            f"{type(exc).__name__}: {exc}"
        )

    _stage("system_generator_create_system")
    try:
        with _charge_fit_timeout_guard(_resolve_charge_fit_timeout()):
            system = sg.create_system(
                modeller.topology, molecules=ligand_molecules_for_gaff or None
            )
    except TimeoutError:
        # Propagate to ``build_amber_system``'s handler (code
        # ``openmmforcefields_build_timeout``).
        raise
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"SystemGenerator.create_system failed: {type(exc).__name__}: {exc}"
        )
        return result

    # Verify the GB force is actually attached when implicit_solvent was
    # requested. If the catalog XML loaded but no Generalized-Born force
    # ended up in the System (e.g. the protein force field overrode the
    # implicit residue templates), fail-fast rather than save a System
    # that the run-side shim would later reject as vacuum-disguised-as-GB.
    if canon_implicit:
        gb_force_classes = (
            "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
        )
        present = {type(f).__name__ for f in system.getForces()}
        if not (present & set(gb_force_classes)):
            result["errors"].append(
                f"implicit_solvent={canon_implicit!r} requested but the built "
                f"System carries no Generalized-Born force "
                f"(expected one of {', '.join(gb_force_classes)}). "
                f"This usually means the protein force field XML overrode "
                f"the implicit residue templates; try forcefield='ff14SBonlysc'."
            )
            result["code"] = "implicit_solvent_force_missing"
            return result

    # --- 6. Minimize + serialize ----------------------------------------
    _stage("initial_minimization")
    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 2.0 * unit.femtoseconds
        )
        simulation = Simulation(modeller.topology, system, integrator)
        simulation.context.setPositions(modeller.positions)
        initial_state = simulation.context.getState(
            getEnergy=True,
            getPositions=True,
            enforcePeriodicBox=bool(box_dimensions),
        )
        energy_initial_kj_mol = float(
            initial_state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        )
        simulation.minimizeEnergy(maxIterations=minimize_max_iterations)
        state = simulation.context.getState(
            getEnergy=True,
            getPositions=True,
            getVelocities=True,
            enforcePeriodicBox=bool(box_dimensions),
        )
        energy_final_kj_mol = float(
            state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Energy minimization failed: {type(exc).__name__}: {exc}"
        )
        return result

    final_positions = state.getPositions(asNumpy=True)
    position_count = _position_count_for_report(final_positions, unit)
    minimization_report = {
        "schema_version": "1.0",
        "minimization": {
            "attempted": True,
            "completed": True,
            "backend": "openmm",
            "max_iterations": minimize_max_iterations,
            "energy_initial_kj_mol": energy_initial_kj_mol,
            "energy_final_kj_mol": energy_final_kj_mol,
            "energy_is_finite": (
                math.isfinite(energy_initial_kj_mol)
                and math.isfinite(energy_final_kj_mol)
            ),
            "positions_are_finite": _positions_are_finite_for_report(final_positions, unit),
            "atom_count_preserved": (
                position_count == modeller.topology.getNumAtoms()
                and position_count == system.getNumParticles()
            ),
        },
    }
    topology_validation = _build_topology_validation_report(
        topology=modeller.topology,
        system=system,
        position_count=position_count,
        minimization=minimization_report["minimization"],
        box_dimensions=box_dimensions,
        canon_implicit=canon_implicit,
        pablo_used=pablo_result.used_pablo,
        pablo_guardrail_codes=pablo_result.guardrail_codes,
        patch_summary=patch_summary,
        disulfide_bonds=disulfide_bonds,
        manual_disulfide_added_count=manual_disulfide_added_count,
        non_authoritative_notes=result["topology_notes"],
    )
    disulfide_notes = topology_validation["disulfides"].get(
        "non_authoritative_notes",
        [],
    )
    if disulfide_notes:
        result["topology_notes"].extend(disulfide_notes)
        topology_validation["non_authoritative_notes"] = _unique_messages(
            topology_validation.get("non_authoritative_notes", []) + disulfide_notes
        )
    result["topology_validation"] = topology_validation
    if topology_validation["status"] != "passed":
        disulfides = topology_validation["disulfides"]
        result["errors"].append(
            "Final topology validation failed: "
            f"core={topology_validation['core']['status']}, "
            f"disulfides={disulfides['status']} "
            f"(expected {disulfides['expected_count']}, "
            f"topology observed "
            f"{disulfides['observed_topology_sg_sg_bond_count']}, "
            f"system observed "
            f"{disulfides['observed_system_harmonic_sg_sg_bond_count']})."
        )
        result["code"] = "topology_validation_failed"
        return result
    if demotable_warnings:
        demote_set = set(demotable_warnings)
        result["warnings"] = [
            warning for warning in result["warnings"]
            if warning not in demote_set
        ]
    result["topology_notes"] = _unique_messages(result["topology_notes"])

    # Coerce Pablo's int residue.id to str so PDBFile.writeFile(keepIds=True)
    # doesn't choke on `len(int_id)`.
    for res in modeller.topology.residues():
        if not isinstance(res.id, str):
            res.id = str(res.id)

    _stage("serialization")
    try:
        topology_buffer = io.StringIO()
        PDBFile.writeFile(
            modeller.topology,
            state.getPositions(),
            topology_buffer,
            keepIds=True,
        )
        from mdclaw.structure.pdb_utils import (
            preserve_long_resnames_in_pdb_text,
        )
        topology_pdb_text = preserve_long_resnames_in_pdb_text(
            topology_buffer.getvalue(), modeller.topology
        )
        atomic_write_text_group([
            (system_xml_file, XmlSerializer.serialize(system)),
            (state_xml_file, XmlSerializer.serialize(state)),
            (topology_pdb_file, topology_pdb_text),
            (minimization_report_file, json.dumps(minimization_report, indent=2)),
        ])
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Serialization failed: {type(exc).__name__}: {exc}"
        )
        return result

    _stage("collect_provenance")
    # --- 7. Statistics + provenance -------------------------------------
    num_atoms = modeller.topology.getNumAtoms()
    num_residues = sum(1 for _ in modeller.topology.residues())

    sha256_table: Dict[str, str] = {}
    for xml_path in xml_bundle:
        # Resolve under openmmforcefields if it's a relative-to-package path;
        # otherwise treat as user-supplied.
        try:
            import openmmforcefields  # local import keeps top-of-file slim
            ff_root = Path(openmmforcefields.__file__).parent / "ffxml"
            candidate = ff_root / xml_path
            if candidate.is_file():
                digest = _hash_file(candidate)
                if digest:
                    sha256_table[xml_path] = digest
                continue
        except Exception:  # noqa: BLE001
            pass
        candidate = Path(xml_path)
        if candidate.is_file():
            digest = _hash_file(candidate)
            if digest:
                sha256_table[xml_path] = digest

    if box_dimensions:
        provenance_solvent_type = "explicit"
    elif canon_implicit:
        provenance_solvent_type = "implicit"
    else:
        provenance_solvent_type = "vacuum"

    provenance: Dict[str, Any] = {
        "kind": "amber_via_openmmforcefields",
        "openmm_xml": list(xml_bundle),
        "extra_xml": list(extra_xml),
        "small_molecule_forcefield": "gaff-2.11",
        "ligand_molecules": [
            {
                "sdf": str(lig.get("sdf") or lig.get("sdf_file") or "")
                if (lig.get("sdf") or lig.get("sdf_file"))
                else None,
                "smiles_source": lig.get("smiles_source"),
                "topology_parameter_source": lig.get("topology_parameter_source"),
                "residue_name": lig.get("residue_name"),
            }
            for lig in (valid_ligands or [])
        ],
        "sha256": sha256_table,
        "method": {
            "solvent_type": provenance_solvent_type,
            "protein_forcefield": canon_protein,
            "nonbonded": "PME" if box_dimensions else "NoCutoff",
            "cutoff_nm": 1.0 if box_dimensions else None,
            "constraints": "HBonds",
            "rigid_water": True,
            "hmr": bool(hmr),
            "hydrogen_mass_amu": 4.0 if hmr else 1.008,
            "implicit_solvent": canon_implicit,
            "barostat": None,
            "includes_restraints": False,
        },
        "addExtraParticles": True,
        "manual_bonds": {
            "disulfides": list(disulfide_bonds or []),
            "glycam": glycam_bond_plan if has_explicit_glycam_plan else None,
        },
    }
    try:
        import openmm
        provenance["openmm_version"] = openmm.version.full_version
    except Exception:  # noqa: BLE001
        pass
    try:
        import openmmforcefields
        provenance["openmmforcefields_version"] = getattr(
            openmmforcefields, "__version__", "unknown"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from openff.toolkit import __version__ as off_ver
        provenance["openff_toolkit_version"] = off_ver
    except Exception:  # noqa: BLE001
        pass

    result.update({
        "success": True,
        "system_xml": str(system_xml_file),
        "topology_pdb": str(topology_pdb_file),
        "state_xml": str(state_xml_file),
        "minimization_report": str(minimization_report_file),
        "minimization": minimization_report["minimization"],
        "topology_validation": topology_validation,
        "topology_notes": result["topology_notes"],
        "num_atoms": num_atoms,
        "num_residues": num_residues,
        "forcefield_provenance": provenance,
    })
    return result


# =============================================================================
# Tool Registry
# =============================================================================
