"""OpenMM-XML / TorchForce research-mode topology builder.

Companion to ``mdclaw.amber_server.build_amber_system``. Where
``build_amber_system`` is the curated Amber-catalog path (ff19SB / ff14SB /
phosaa / lipid21 / GLYCAM, all routed through the openmmforcefields
catalog), ``build_openmm_system`` is the research-mode escape hatch: the
user supplies a list of OpenMM ``ForceField`` XML files (e.g.
``GB99dms.xml`` from the Greener group) together with optional ligand
molecules, and the tool emits the same ``system.xml + topology.pdb +
state.xml`` artifact triple so eq/prod can consume the result through the
same DAG resolver.

The tool is intentionally permissive — there is no FF×water guardrail
matrix here, because by definition the user is bringing their own XML
that mdclaw's Amber25 catalog does not know about. We only block on
critical correctness conditions (e.g. GB99dms requires OpenMM 8.0+).

TorchForce / .pt overlays (garnet-style ML potentials) are explicitly out
of scope for this PR.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,  # noqa: F401  (kept for parity / future extension)
    create_file_not_found_error,
    create_tool_not_available_error,
    create_unique_subdir,
    create_validation_error,
    ensure_directory,
    setup_logger,
)
from mdclaw import _topology_pablo  # noqa: E402

logger = setup_logger(__name__)

WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> Optional[str]:
    try:
        import hashlib
        with path.open("rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return None


def _xml_entry_matches_catalog(entry: str, catalog_path: str) -> bool:
    """Decide whether a user-supplied ``forcefield_xml`` entry refers to the
    same shipped XML as ``catalog_path`` (e.g. ``"implicit/obc2.xml"``).

    Three forms are recognised, all case-insensitive on the basename:
    - exact string equality (the standard recipe);
    - any path that ends in the same filename (``/extras/obc2.xml`` or
      ``…/implicit/obc2.xml``);
    so a user pinning the XML by absolute path can still declare which GB
    model they meant. We do *not* accept a bare ``obc2.xml`` without an
    ``implicit/`` directory component, to avoid colliding with unrelated
    XMLs that happen to share a basename.
    """
    if entry == catalog_path:
        return True
    catalog_basename = Path(catalog_path).name.lower()
    entry_path = Path(entry)
    if entry_path.name.lower() != catalog_basename:
        return False
    parts_lower = {p.lower() for p in entry_path.parts}
    return "implicit" in parts_lower


def _detect_implicit_solvent_xml(
    forcefield_xml: List[str],
) -> tuple[Optional[str], list[str]]:
    """Scan ``forcefield_xml`` for shipped ``implicit/*.xml`` entries.

    Returns a tuple ``(canonical_or_none, matched_models)``. When exactly
    one shipped model is present, ``canonical_or_none`` is its canonical
    name (``"OBC2"`` etc.). Multiple matches return ``(None, [...])`` so
    the caller can raise ``implicit_solvent_xml_ambiguous``. Zero matches
    return ``(None, [])`` — third-party GB XML (``GB99dms.xml``) is
    intentionally not inferable, matching the spec's escape-hatch policy.
    """
    from mdclaw import forcefield_catalog as _fc

    matched: list[str] = []
    for canonical, catalog_path in _fc.IMPLICIT_SOLVENT_XML.items():
        if any(_xml_entry_matches_catalog(e, catalog_path) for e in forcefield_xml):
            matched.append(canonical)
    if len(matched) == 1:
        return matched[0], matched
    return None, matched


def _check_gb99_openmm_version_compatible(forcefield_xml: List[str]) -> Optional[str]:
    """Return an error message if any GB99* XML is paired with OpenMM < 8.0.

    GB99dms is the Greener group's GBNeck2-derived implicit-solvent FF; it
    requires OpenMM >= 8.0 because earlier versions silently miscompute the
    GB integral for the parameter set.
    """
    needs_openmm_8 = any(
        "gb99" in (Path(p).name.lower()) for p in forcefield_xml
    )
    if not needs_openmm_8:
        return None
    try:
        import openmm
        major = int(openmm.version.short_version.split(".")[0])
    except Exception:  # noqa: BLE001
        return None
    if major < 8:
        return (
            f"GB99dms-style implicit-solvent XML requires OpenMM >= 8.0; "
            f"current OpenMM is {openmm.version.full_version}. Upgrade via "
            f"`conda env update -f environment.yml`."
        )
    return None


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


def build_openmm_system(
    pdb_file: Optional[str] = None,
    forcefield_xml: Optional[List[str]] = None,
    additional_smiles: Optional[List[List[str]]] = None,
    nonbonded_method: str = "PME",
    nonbonded_cutoff_nm: float = 1.0,
    constraints: str = "HBonds",
    rigid_water: bool = True,
    hmr: bool = True,
    implicit_solvent: Optional[str] = None,
    minimize: bool = True,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an OpenMM ``System`` from arbitrary user-supplied ForceField XML.

    This is the research-mode counterpart to ``build_amber_system``. It does
    not consult mdclaw's Amber25 force-field catalog or guardrail matrix —
    by definition the user is bringing third-party / experimental XML that
    sits outside the catalog (e.g. ``GB99dms.xml``).

    Args:
        pdb_file: Path to the prepared (hydrogenated) PDB. Loaded via Pablo
            with a ``openmm.app.PDBFile`` fallback for residues Pablo cannot
            match.
        forcefield_xml: List of OpenMM ForceField XML paths or names. Looked
            up against ``openmm.app.ForceField``'s search path; absolute
            paths work too. Order matters when residue templates overlap.
        additional_smiles: ``[(residue_name, smiles), ...]`` pairs for
            non-standard residues so Pablo can match them via
            ``ResidueDefinition.anon_from_smiles``.
        nonbonded_method: ``"PME"`` (periodic), ``"NoCutoff"`` (gas-phase or
            implicit), or ``"CutoffNonPeriodic"``.
        nonbonded_cutoff_nm: Real-space cutoff in nm; ignored for
            ``NoCutoff``.
        constraints: ``"HBonds"`` (default) / ``"AllBonds"`` / ``"None"``.
        rigid_water: Pass-through to ``ForceField.createSystem``.
        hmr: When ``True`` (default), bakes ``hydrogenMass=4 amu`` into
             ``system.xml`` so a downstream ``run_equilibration`` /
             ``run_production`` invocation with the default ``hmr=True``
             does not trip the modern-system contract check. Defaults
             match ``build_amber_system`` so a ``build_openmm_system →
             run_equilibration → run_production`` chain works without
             extra kwargs. Pass ``hmr=False`` to keep standard hydrogen
             masses (use a 2 fs timestep on the run_* side).
        implicit_solvent: Canonical GB model name (case-insensitive;
             ``HCT`` / ``OBC1`` / ``OBC2`` / ``GBn`` / ``GBn2``, with
             ``gbneck2`` / ``igb1``–``igb8`` aliases). When set, the
             matching ``implicit/<model>.xml`` must already appear in
             ``forcefield_xml`` — this builder is the research escape
             hatch and will not silently inject XMLs the caller did not
             ask for; missing entries fail-fast with
             ``implicit_solvent_xml_missing``. When omitted, the builder
             scans ``forcefield_xml`` for shipped ``implicit/*.xml``
             entries and stamps the inferred canonical name on the topo
             node's metadata so the run-side topology guard recognises
             the build choice. Multiple shipped GB XMLs trigger
             ``implicit_solvent_xml_ambiguous``. Third-party GB XML
             (e.g. ``GB99dms.xml``) is *not* inferable — pass
             ``implicit_solvent`` explicitly only when the corresponding
             shipped XML is also in ``forcefield_xml``; for purely
             custom GB XML, leave the metadata as ``None`` and accept
             that the run-side topology guard cannot match.
        minimize: Run a short LocalEnergyMinimizer pass before
            serializing the state. Disable for debugging.
        output_name: Stem for the artifact file names.
        output_dir / job_dir / node_id: Standard mdclaw I/O knobs.

    Returns: dict with ``success``, ``errors``, ``warnings``, plus on
    success ``system_xml``, ``topology_pdb``, ``state_xml``,
    ``num_atoms``, ``num_residues``, ``forcefield_provenance``.
    """
    result: Dict[str, Any] = {
        "success": False,
        "errors": [],
        "warnings": [],
        "parameters": {
            "forcefield_xml": list(forcefield_xml or []),
            "nonbonded_method": nonbonded_method,
            "nonbonded_cutoff_nm": nonbonded_cutoff_nm,
            "constraints": constraints,
            "rigid_water": rigid_water,
            "hmr": hmr,
            "implicit_solvent": None,    # filled in after canonicalization
            "minimize": minimize,
        },
    }

    _node_mode = bool(job_dir and node_id)

    # Helper that surfaces a structured error AND, when running under a node,
    # marks the node as failed so the DAG never sees a half-built artifact.
    def _emit_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
        if _node_mode:
            from mdclaw._node import fail_node
            fail_node(
                job_dir, node_id,
                errors=payload.get("errors") or [payload.get("message", "build_openmm_system failed")],
            )
        return payload

    # In node mode the topo node owns the artifact location; auto-resolve the
    # input PDB from the prep ancestor when the user didn't supply one
    # explicitly. ``begin_node`` flips the node into ``running`` so subsequent
    # failures can be surfaced via fail_node().
    if _node_mode:
        from mdclaw._node import (
            begin_node,
            resolve_node_inputs,
            validate_node_execution_context,
        )

        # Surface the build-time choices into actual_conditions so the topo
        # node can declare them via ``create_node(... conditions={...})`` and
        # have ``validate_node_execution_context`` enforce the match.
        # Mirrors the contract ``build_amber_system`` keeps with its own
        # actual_conditions so research-mode and curated builders behave
        # identically under DAG condition checks.
        _ctx = validate_node_execution_context(
            job_dir, node_id, "topo",
            actual_conditions={
                "forcefield_xml": list(forcefield_xml or []),
                "nonbonded_method": nonbonded_method,
                "nonbonded_cutoff_nm": nonbonded_cutoff_nm,
                "constraints": constraints,
                "rigid_water": rigid_water,
                "hmr": hmr,
                "implicit_solvent": implicit_solvent,
                "output_name": output_name,
            },
        )
        if not _ctx["success"]:
            return _emit_failure({
                "success": False,
                "error_type": "ValidationError",
                "errors": _ctx.get("errors", []),
                **_ctx,
            })

        if not pdb_file:
            _inputs = resolve_node_inputs(job_dir, node_id, "topo")
            if "input_resolution_error" in _inputs:
                return _emit_failure({
                    **result,
                    "errors": result["errors"] + [_inputs["input_resolution_error"]],
                    "code": "input_resolution_blocked",
                })
            pdb_file = _inputs.get("pdb_file")

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    elif output_dir:
        out_dir = Path(output_dir)
        ensure_directory(out_dir)
    else:
        out_dir = create_unique_subdir(WORKING_DIR, "openmm_system")
    result["output_dir"] = str(out_dir)

    if not pdb_file:
        # ``pdb_file is None`` (or empty string) is a structural error, not a
        # missing file — distinguish them so users / agents see "supply
        # pdb_file" instead of "file None not found". In node mode, the
        # error fires only after auto-resolve has had a chance to populate
        # pdb_file from the prep ancestor.
        return _emit_failure(create_validation_error(
            "pdb_file",
            "pdb_file is required",
            expected="An absolute or working-directory-relative path to a "
                     "prepared (hydrogenated) PDB",
            actual=repr(pdb_file),
            hints=[
                "Pass --pdb-file <path>, or run in node mode with a "
                "completed prep ancestor so resolve_node_inputs can "
                "auto-resolve merged_pdb.",
            ],
            code="missing_pdb_file",
        ))

    pdb_path = Path(pdb_file)
    if not pdb_path.is_file():
        return _emit_failure(create_file_not_found_error(str(pdb_path), file_type="pdb_file"))

    if not forcefield_xml:
        result["errors"].append(
            "forcefield_xml is required: supply at least one OpenMM ForceField XML."
        )
        return _emit_failure(result)

    # --- Implicit-solvent: canonicalize, validate, optionally infer -------
    # Three failure modes here, all fail-fast with structured codes:
    #   * unknown declared model name (typo)
    #     -> ``implicit_solvent_model_unsupported``
    #   * declared model but the matching ``implicit/<model>.xml`` is not
    #     in ``forcefield_xml`` (the escape hatch never silently injects
    #     XML the caller did not ask for)
    #     -> ``implicit_solvent_xml_missing``
    #   * no declaration but multiple shipped ``implicit/*.xml`` are
    #     bundled (cannot decide which model the System actually carries)
    #     -> ``implicit_solvent_xml_ambiguous``
    from mdclaw import forcefield_catalog as _fc

    canonical_implicit: Optional[str] = None
    if implicit_solvent is not None:
        if str(implicit_solvent).strip().lower() == "custom":
            canonical_implicit = "custom"
        else:
            canonical = _fc.normalize_implicit_solvent(implicit_solvent)
            if canonical not in _fc.IMPLICIT_SOLVENT_XML:
                supported = ", ".join((*_fc.supported_implicit_solvent_models(), "custom"))
                result["errors"].append(
                    f"Unknown implicit_solvent={implicit_solvent!r}. "
                    f"Supported: {supported}."
                )
                return _emit_failure({
                    **result,
                    "code": "implicit_solvent_model_unsupported",
                })
            expected_xml = _fc.IMPLICIT_SOLVENT_XML[canonical]
            if not any(
                _xml_entry_matches_catalog(entry, expected_xml)
                for entry in forcefield_xml
            ):
                result["errors"].append(
                    f"build_openmm_system requested implicit_solvent={canonical!r}, "
                    f"but forcefield_xml does not include {expected_xml!r}. "
                    f"Add it to forcefield_xml, or use "
                    f"build_amber_system --implicit-solvent {canonical}."
                )
                return _emit_failure({
                    **result,
                    "code": "implicit_solvent_xml_missing",
                })
            canonical_implicit = canonical
    else:
        inferred, matched = _detect_implicit_solvent_xml(forcefield_xml)
        if len(matched) > 1:
            result["errors"].append(
                f"forcefield_xml contains multiple shipped implicit-solvent "
                f"XMLs ({', '.join(matched)}); pass implicit_solvent=<one of "
                f"{', '.join(matched)}> to disambiguate, or remove the "
                f"unwanted XML from forcefield_xml."
            )
            return _emit_failure({
                **result,
                "code": "implicit_solvent_xml_ambiguous",
            })
        canonical_implicit = inferred  # may be None when no shipped GB XML

    result["parameters"]["implicit_solvent"] = canonical_implicit

    try:
        import openmmforcefields  # noqa: F401
    except ImportError:
        return _emit_failure(create_tool_not_available_error(
            "openmmforcefields",
            "Run `conda env update -f environment.yml` to install the openmmforcefields-unification deps",
        ))

    incompat = _check_gb99_openmm_version_compatible(forcefield_xml)
    if incompat:
        result["errors"].append(incompat)
        return _emit_failure({
            **result,
            "code": "openmm_version_too_old",
        })

    system_xml_file = out_dir / f"{output_name}.system.xml"
    topology_pdb_file = out_dir / f"{output_name}.topology.pdb"
    state_xml_file = out_dir / f"{output_name}.state.xml"

    try:
        from openmm import app, unit, XmlSerializer, LangevinIntegrator
        from openmm.app import ForceField, Modeller, PDBFile, Simulation
    except ImportError as exc:
        result["errors"].append(
            f"OpenMM stack not importable: {exc}. Run `conda env update -f environment.yml`."
        )
        return _emit_failure(result)

    extra_smiles_pairs: List[tuple[str, str]] = []
    for item in additional_smiles or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            result["warnings"].append(
                f"additional_smiles entry must be a 2-element [residue_name, smiles]; "
                f"got {item!r}"
            )
            continue
        extra_smiles_pairs.append((str(item[0]), str(item[1])))

    pablo_result = _topology_pablo.load_topology(
        pdb_path, extra_smiles=extra_smiles_pairs
    )
    result["warnings"].extend(pablo_result.warnings)
    omm_topology = pablo_result.topology
    omm_positions = pablo_result.positions

    nb_method_map = {
        "PME": app.PME,
        "NoCutoff": app.NoCutoff,
        "CutoffNonPeriodic": app.CutoffNonPeriodic,
        "Ewald": app.Ewald,
        "CutoffPeriodic": app.CutoffPeriodic,
    }
    if nonbonded_method not in nb_method_map:
        result["errors"].append(
            f"nonbonded_method={nonbonded_method!r} not recognized; "
            f"choose from {sorted(nb_method_map)}."
        )
        return _emit_failure(result)

    constraints_map = {
        "HBonds": app.HBonds,
        "AllBonds": app.AllBonds,
        "None": None,
        None: None,
    }
    if constraints not in constraints_map:
        result["errors"].append(
            f"constraints={constraints!r} not recognized; "
            f"choose from HBonds | AllBonds | None."
        )
        return _emit_failure(result)

    try:
        ff = ForceField(*forcefield_xml)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"ForceField init failed: {type(exc).__name__}: {exc}. "
            f"Bundle: {forcefield_xml}"
        )
        return _emit_failure(result)

    modeller = Modeller(omm_topology, omm_positions)
    try:
        modeller.addExtraParticles(ff)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"addExtraParticles failed (continuing without virtual sites): "
            f"{type(exc).__name__}: {exc}"
        )

    create_system_kwargs: Dict[str, Any] = {
        "nonbondedMethod": nb_method_map[nonbonded_method],
        "constraints": constraints_map[constraints],
        "rigidWater": rigid_water,
    }
    if nonbonded_method != "NoCutoff":
        create_system_kwargs["nonbondedCutoff"] = nonbonded_cutoff_nm * unit.nanometer
    # HMR is a build-time decision: bake ``hydrogenMass=4 amu`` into the
    # System so the run-side XML system validator accepts the default
    # ``hmr=True`` from run_equilibration / run_production.
    if hmr:
        create_system_kwargs["hydrogenMass"] = 4.0 * unit.amu

    try:
        system = ff.createSystem(modeller.topology, **create_system_kwargs)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"ForceField.createSystem failed: {type(exc).__name__}: {exc}"
        )
        return _emit_failure(result)

    # When an implicit-solvent model was declared or inferred, the built
    # System must actually carry a Generalized-Born force. Otherwise the
    # XML loaded but never produced a ``CustomGBForce`` (e.g. residue
    # template overrode the implicit definitions), and the run-side shim
    # would later run vacuum dynamics under the GB label. Mirror of the
    # ``build_amber_system`` ``implicit_solvent_force_missing`` guard.
    gb_force_classes = (
        "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
    )
    present_forces = {type(f).__name__ for f in system.getForces()}
    has_gb_force = bool(present_forces & set(gb_force_classes))
    if canonical_implicit is not None:
        if not has_gb_force:
            result["errors"].append(
                f"implicit_solvent={canonical_implicit!r} requested but the "
                f"built System carries no Generalized-Born force "
                f"(expected one of {', '.join(gb_force_classes)}). "
                f"Check that the GB XML was loaded after the "
                f"protein force field XML."
            )
            return _emit_failure({
                **result,
                "code": "implicit_solvent_force_missing",
            })
    elif has_gb_force:
        canonical_implicit = "custom"
        result["parameters"]["implicit_solvent"] = canonical_implicit
        result["warnings"].append(
            "System carries a GB / implicit-solvent force from custom XML; "
            "recording implicit_solvent='custom'. Downstream run_* calls must "
            "also pass --implicit-solvent custom."
        )

    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 2.0 * unit.femtoseconds
        )
        simulation = Simulation(modeller.topology, system, integrator)
        simulation.context.setPositions(modeller.positions)
        if minimize:
            simulation.minimizeEnergy(maxIterations=200)
        state = simulation.context.getState(
            getPositions=True, getVelocities=True,
            enforcePeriodicBox=(nonbonded_method == "PME"),
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Energy minimization failed: {type(exc).__name__}: {exc}"
        )
        return _emit_failure(result)

    # Coerce Pablo's int residue.id to str so PDBFile.writeFile(keepIds=True)
    # doesn't choke on `len(int_id)`.
    for res in modeller.topology.residues():
        if not isinstance(res.id, str):
            res.id = str(res.id)

    try:
        with system_xml_file.open("w") as fh:
            fh.write(XmlSerializer.serialize(system))
        with state_xml_file.open("w") as fh:
            fh.write(XmlSerializer.serialize(state))
        with topology_pdb_file.open("w") as fh:
            PDBFile.writeFile(modeller.topology, state.getPositions(), fh, keepIds=True)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Serialization failed: {type(exc).__name__}: {exc}"
        )
        return _emit_failure(result)

    sha256_table: Dict[str, str] = {}
    for xml_path in forcefield_xml:
        candidate = Path(xml_path)
        if candidate.is_file():
            digest = _hash_file(candidate)
            if digest:
                sha256_table[xml_path] = digest

    # Solvent classification for provenance / node metadata. ``implicit``
    # wins because the GB force on the System defines the regime; ``explicit``
    # tracks periodic nonbonded methods (PME / Ewald / CutoffPeriodic);
    # everything else (NoCutoff, CutoffNonPeriodic without GB) is vacuum.
    if canonical_implicit:
        solvent_type = "implicit"
    elif nonbonded_method in ("PME", "Ewald", "CutoffPeriodic"):
        solvent_type = "explicit"
    else:
        solvent_type = "vacuum"

    provenance: Dict[str, Any] = {
        "kind": "openmm_xml",
        "forcefield_xml": list(forcefield_xml),
        "extra_smiles": extra_smiles_pairs,
        "sha256": sha256_table,
        "method": {
            "solvent_type": solvent_type,
            "implicit_solvent": canonical_implicit,
            "nonbonded": nonbonded_method,
            "cutoff_nm": nonbonded_cutoff_nm if nonbonded_method != "NoCutoff" else None,
            "constraints": constraints,
            "rigid_water": rigid_water,
            "hmr": bool(hmr),
            "hydrogen_mass_amu": 4.0 if hmr else 1.008,
            "barostat": None,
            "includes_restraints": False,
        },
        "addExtraParticles": True,
    }
    try:
        import openmm
        provenance["openmm_version"] = openmm.version.full_version
    except Exception:  # noqa: BLE001
        pass
    try:
        from openff.toolkit import __version__ as off_ver
        provenance["openff_toolkit_version"] = off_ver
    except Exception:  # noqa: BLE001
        pass

    num_atoms = modeller.topology.getNumAtoms()
    num_residues = sum(1 for _ in modeller.topology.residues())

    result.update({
        "success": True,
        "system_xml": str(system_xml_file),
        "topology_pdb": str(topology_pdb_file),
        "state_xml": str(state_xml_file),
        "num_atoms": num_atoms,
        "num_residues": num_residues,
        "forcefield_provenance": provenance,
        "code": "openmm_system_built",
    })

    if _node_mode:
        from mdclaw._node import complete_node
        artifacts = {
            "system_xml": f"artifacts/{output_name}.system.xml",
            "topology_pdb": f"artifacts/{output_name}.topology.pdb",
            "state_xml": f"artifacts/{output_name}.state.xml",
        }
        # The eq/prod resolver reads ``metadata.implicit_solvent``,
        # ``metadata.solvent_type``, and ``metadata.hmr`` so the run-side
        # topology guard can match build-time choices to runtime kwargs.
        # Mirror of ``build_amber_system``'s metadata stamp so curated
        # and research-mode topo nodes are interchangeable downstream.
        complete_node(
            job_dir, node_id,
            artifacts=artifacts,
            metadata={
                "system_artifact_kind": "openmm_system_xml",
                "forcefield_provenance": provenance,
                "forcefield_xml": list(forcefield_xml),
                "implicit_solvent": canonical_implicit,
                "solvent_type": solvent_type,
                "hmr": bool(hmr),
            },
        )

    logger.info(
        "Built OpenMM System via custom XML: %d atoms, %d residues, bundle=%s",
        num_atoms, num_residues, forcefield_xml,
    )
    return result


# =============================================================================
# Tool registry
# =============================================================================

TOOLS = {
    "build_openmm_system": build_openmm_system,
}
