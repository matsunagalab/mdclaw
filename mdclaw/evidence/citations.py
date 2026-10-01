"""Small evidence-based selector; the audit is not an unconditional citation list."""

from pathlib import Path
import re

from mdclaw._common import sha256_file


_OPENMM = "https://docs.openmm.org/latest/userguide/introduction.html#referencing-openmm"
_THEORY = "https://docs.openmm.org/latest/userguide/theory/02_standard_forces.html"
_MIDDLE = "https://docs.openmm.org/latest/api-python/generated/openmm.openmm.LangevinMiddleIntegrator.html"
_PARAMETERS = {
    "ff14SB": "Maier2015ff14SB", "ff19SB": "Tian2020ff19SB",
    "tip3p": "Jorgensen1983TIP3P", "opc": "Izadi2014OPC",
    "opc3": "Izadi2016OPC3", "spce": "Berendsen1987SPCE",
    "tip4pew": "Horn2004TIP4PEw",
}
_PARAMETER_SOURCE = "https://docs.openmm.org/latest/userguide/application/02_running_sims.html#force-fields"
_FEP_DESIGN = "docs/research/fep-references.md"
# pymbar's README lists the papers to cite for MBAR and its timeseries module.
_PYMBAR = "https://github.com/choderalab/pymbar"
_PYMBAR_TIMESERIES = "https://github.com/choderalab/pymbar"
# Preparation, solvation and ligand parameterisation: each tool's own citation
# instructions; the mapping and its limits are in
# docs/research/citation-audit-2026-09-06.md.
_AMBER = "https://ambermd.org/CiteAmber.php"
_OPENMMFF = "https://github.com/openmm/openmmforcefields"
_PDBFIXER = "https://github.com/openmm/pdbfixer"
_PDB2PQR = "https://pdb2pqr.readthedocs.io/en/latest/supporting.html"
_PROPKA = "https://github.com/jensengroup/propka#references--citations"
_PACKMOL = "https://m3g.github.io/packmol/citation.shtml"
# clean_protein's protonation_baseline_method; "disabled" ran neither.
_PDB2PQR_KEYS = ("Dolinsky2004PDB2PQR", "Dolinsky2007PDB2PQR", "Jurrus2018APBSPDB2PQR")
_PROPKA_KEYS = ("Olsson2011PROPKA3", "Sondergaard2011PROPKA")
_PDB2PQR_BASELINES = ("pdb2pqr+propka", "pdb2pqr_no_prediction")
# NAGL models whose model card names the paper (Ash 1.0 links the AshGC working paper).
_NAGL_MODELS = {
    "openff-gnn-am1bcc-1.0.0.pt": (
        "Wang2025AshGCWorkingPaper",
        "https://github.com/openforcefield/openff-nagl-models/blob/main/docs/models/openff-gnn-am1bcc-1.0.0/index.md"),
}


def _packmol_memgen_remark(record):
    """Evidence that packmol-memgen wrote a solv node's box, for nodes recorded
    before solvate_structure named its backend: the REMARK it puts at the top of
    the solvated PDB the node lists. ``None`` when that line is not there."""
    value = record["artifacts"].get("solvated_pdb")
    if not isinstance(value, str):
        return None
    path = (Path(record["artifact_base_dir"]) / value).resolve()
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        head = [fh.readline() for _ in range(5)]
    if not any(line.startswith(b"REMARK") and b"Packmol Memgen" in line for line in head):
        return None
    return {"file": str(path), "sha256": sha256_file(path)}


def select_citations(subjects):
    selected, unresolved, documentation = {}, [], []

    def add(key, subject, record, field, role, source, evidence=None):
        evidence = evidence or record["source"]
        reason = {"label": subject["label"], "node_id": record["node_id"],
                  "evidence_field": field, "evidence_file": evidence["file"],
                  "evidence_sha256": evidence["sha256"],
                  "role": role, "citation_source": source}
        selected.setdefault(key, []).append(reason)

    for subject in subjects:
        for record in subject["history"]:
            if record["status"] != "completed":
                continue
            metadata, runtime = record["recorded_metadata"], record["runtime"]
            ident = {"label": subject["label"], "node_id": record["node_id"]}
            integrator = runtime.get("integrator", {}).get("type")
            integrator_source = record["runtime_sources"].get("integrator") if integrator else None
            integrator_field = "/Integrator/@type" if integrator else "/metadata/integrator_signature/integrator"
            signature = metadata.get("integrator_signature", {})
            if not integrator and isinstance(signature, dict):
                integrator = signature.get("integrator")
            # The runtime System of a production node, else the State / System
            # XML a min, eq or topo node wrote: both carry the OpenMM version.
            version, version_field, version_source = "", None, None
            for key in ("runtime_system", "openmm_serialization"):
                if (runtime.get(key) or {}).get("openmm_version"):
                    root = "System" if key == "runtime_system" else runtime[key]["root"]
                    version, version_field = runtime[key]["openmm_version"], f"/{root}/@openmmVersion"
                    version_source = record["runtime_sources"][key]
                    break
            if version.startswith("8."):
                add("Eastman2024OpenMM8", subject, record, version_field, "software", _OPENMM, version_source)
            elif version:
                unresolved.append({**ident, "method": "OpenMM_version", "value": version,
                                   "reason": "No verified paper mapping for this OpenMM version"})
            elif runtime or integrator:
                unresolved.append({**ident, "method": "OpenMM_version",
                                   "reason": "Software paper selection requires recorded version"})
            if integrator == "LangevinMiddleIntegrator":
                add("Zhang2019LFMiddle", subject, record, integrator_field,
                    "official_method", _MIDDLE, integrator_source)
                add("Leimkuhler2016BAOAB", subject, record, integrator_field,
                    "related_method_not_separate_execution", _MIDDLE, integrator_source)
            elif integrator:
                unresolved.append({**ident, "method": integrator, "reason": "Integrator mapping not verified"})
            system = runtime.get("runtime_system", {})
            for force in system.get("forces", []):
                name = force.get("type", "unknown")
                if name in ("MonteCarloBarostat", "MonteCarloMembraneBarostat"):
                    for key in ("Chow1995MCBarostat", "Aqvist2004MCBarostat"):
                        add(key, subject, record, f"/System/Forces/Force[@type='{name}']",
                            "official_method" if name == "MonteCarloBarostat" else "base_method",
                            _THEORY + "#montecarlobarostat", record["runtime_sources"]["runtime_system"])
                    if name == "MonteCarloMembraneBarostat":
                        documentation.append({**ident, "method": name,
                                              "source": _THEORY + "#montecarlomembranebarostat",
                                              "dedicated_paper": None})
                elif name.startswith("Custom") or name in ("TorchForce", "PlumedForce"):
                    unresolved.append({**ident, "method": name,
                                       "reason": "Requires potential/action-specific provenance"})
            if system.get("constraint_count", 0):
                unresolved.append({**ident, "method": "constraint_solver",
                                   "reason": "Constraint count does not identify SETTLE/SHAKE/CCMA use"})
            for field in ("effective_forcefield", "water_model"):
                value = metadata.get(field)
                if isinstance(value, str) and value in _PARAMETERS:
                    add(_PARAMETERS[value], subject, record, "/metadata/" + field,
                        "recorded_parameter_selection", _PARAMETER_SOURCE)
                elif value:
                    unresolved.append({**ident, "method": field, "value": value,
                                       "reason": "Exact parameter mapping not automated"})
            if metadata.get("hmr") is True:
                add("Hopkins2015HMR", subject, record, "/metadata/hmr", "method",
                    "https://doi.org/10.1021/ct5010406")
            # Structure preparation: protonation (PDB2PQR, with PROPKA unless
            # no-prediction) and PDBFixer, which has no dedicated paper.
            baseline = metadata.get("protonation_baseline_method")
            if baseline in _PDB2PQR_BASELINES:
                for key in _PDB2PQR_KEYS:
                    add(key, subject, record, "/metadata/protonation_baseline_method", "software", _PDB2PQR)
                if baseline == "pdb2pqr+propka":
                    for key in _PROPKA_KEYS:
                        add(key, subject, record, "/metadata/protonation_baseline_method", "official_method", _PROPKA)
            elif isinstance(baseline, str) and baseline != "disabled":
                unresolved.append({**ident, "method": "protonation_baseline_method", "value": baseline,
                                   "reason": "Protonation tool mapping not automated"})
            if metadata.get("pdbfixer_version"):
                documentation.append({**ident, "method": "PDBFixer", "version": metadata["pdbfixer_version"],
                                      "source": _PDBFIXER, "dedicated_paper": None})
            # Solvation: packmol-memgen (which runs PACKMOL) when the node names
            # it; a node recorded before solvate_structure named its backend is
            # judged by the REMARK packmol-memgen wrote into its solvated PDB.
            if record["node_type"] == "solv":
                named = next((f for f in ("backend", "membrane_backend") if metadata.get(f)), None)
                evidence = None if named else _packmol_memgen_remark(record)
                if (named and metadata[named] == "packmol-memgen") or evidence:
                    field = f"/metadata/{named}" if named else "solvated_pdb REMARK (Packmol Memgen)"
                    add("SchottVerdugo2019PackmolMemgen", subject, record, field, "software", _AMBER, evidence)
                    add("Martinez2009Packmol", subject, record, field, "base_method", _PACKMOL, evidence)
            # Ligand parameterisation: GAFF atom types from antechamber (GAFF
            # template generator), partial charges from NAGL or AM1-BCC. GAFF2
            # has no standalone paper; its exact version stays on record.
            provenance = metadata.get("forcefield_provenance")
            if record["node_type"] == "topo" and isinstance(provenance, dict):
                field = "/metadata/forcefield_provenance"
                if provenance.get("kind") == "amber_via_openmmforcefields":
                    documentation.append({**ident, "method": "openmmforcefields",
                                          "version": provenance.get("openmmforcefields_version"),
                                          "source": _OPENMMFF, "dedicated_paper": None})
                small = provenance.get("small_molecule_forcefield")
                gaff = [lig for lig in provenance.get("ligand_molecules") or [] if isinstance(lig, dict)
                        and lig.get("topology_parameter_source") == "topology_gaff_template_generator"]
                if gaff and isinstance(small, str) and small.startswith("gaff"):
                    add("Wang2004GAFF", subject, record, field + "/small_molecule_forcefield",
                        "recorded_parameter_selection", _AMBER)
                    add("Wang2006Antechamber", subject, record, field + "/ligand_molecules", "software", _OPENMMFF)
                    add("Case2023AmberTools", subject, record, field + "/small_molecule_forcefield", "software", _AMBER)
                    if small.startswith("gaff-2"):
                        documentation.append({**ident, "method": "GAFF2 parameter set", "version": small,
                                              "source": _AMBER, "dedicated_paper": None})
                elif gaff:
                    unresolved.append({**ident, "method": "small_molecule_forcefield", "value": small,
                                       "reason": "Exact parameter mapping not automated"})
                charges = [c for c in provenance.get("ligand_charge_assignment") or [] if isinstance(c, dict)]
                for model in sorted({str(c.get("nagl_model")) for c in charges if c.get("method") == "nagl"}):
                    if model in _NAGL_MODELS:
                        key, card = _NAGL_MODELS[model]
                        add(key, subject, record, field + "/ligand_charge_assignment", "official_method", card)
                        documentation.append({**ident, "method": "OpenFF NAGL model", "version": model,
                                              "source": card, "dedicated_paper": None})
                    else:
                        unresolved.append({**ident, "method": "nagl_model", "value": model,
                                           "reason": "Model-to-paper mapping not verified"})
                if any(c.get("method") == "am1bcc_fallback" for c in charges):
                    for key in ("Jakalian2000AM1BCC", "Jakalian2002AM1BCC"):
                        add(key, subject, record, field + "/ligand_charge_assignment", "official_method", _AMBER)
                for method in sorted({str(c.get("method")) for c in charges} - {"nagl", "am1bcc_fallback"}):
                    unresolved.append({**ident, "method": "ligand_charge_method", "value": method,
                                       "reason": "Charge-method mapping not automated"})
            # Alchemical stages: the hybrid topology (single-residue hybrid
            # after pmx, Beutler soft-core LJ on the dummies), the MBAR leg
            # (estimator, equilibration detection / subsampling, overlap
            # diagnostic) and the folding-stability cycle (capped tripeptide
            # as the unfolded state). All keyed on recorded metadata.
            if record["node_type"] == "topo" and isinstance(metadata.get("fep"), dict):
                # A ligand-decoupling topology (absolute binding) is not a
                # pmx-style hybrid; it shares only the soft core.
                if metadata["fep"].get("kind") == "abfe_decouple":
                    if metadata["fep"].get("restraint") == "boresch":
                        add("Boresch2003AbsoluteBinding", subject, record, "/metadata/fep/restraint", "method",
                            _FEP_DESIGN)
                else:
                    add("Gapsys2015pmx", subject, record, "/metadata/fep", "base_method", _FEP_DESIGN)
                add("Beutler1994SoftCore", subject, record, "/metadata/fep", "method", _FEP_DESIGN)
                if metadata["fep"].get("charge_correction") == "coalchemical_ion":
                    add("Chen2018ChargeChangingFEP", subject, record, "/metadata/fep/charge_correction",
                        "method", _FEP_DESIGN)
            analysis = metadata.get("analysis")
            if record["node_type"] == "analyze" and analysis == "abfe_binding":
                # double decoupling with a standard-state term; the symmetry
                # correction follows the T4 lysozyme model-site work.
                add("Gilson1997BindingAffinities", subject, record, "/metadata/analysis", "base_method", _FEP_DESIGN)
                add("Boresch2003AbsoluteBinding", subject, record, "/metadata/terms_kj_mol", "method", _FEP_DESIGN)
                if (metadata.get("ligand_symmetry_number") or 1) > 1:
                    add("Mobley2007ModelSite", subject, record, "/metadata/ligand_symmetry_number", "method",
                        _FEP_DESIGN)
            if record["node_type"] == "analyze" and analysis == "fep_mbar":
                add("Shirts2008MBAR", subject, record, "/metadata/analysis", "official_method", _PYMBAR)
                add("Klimovich2015Guidelines", subject, record, "/metadata/min_neighbour_overlap",
                    "related_method_not_separate_execution", _FEP_DESIGN)
                if metadata.get("subsampled") is True:
                    add("Chodera2016Equilibration", subject, record, "/metadata/subsampled", "method", _PYMBAR_TIMESERIES)
                    add("Chodera2007Timeseries", subject, record, "/metadata/subsampled", "method", _PYMBAR_TIMESERIES)
            if record["node_type"] == "analyze" and analysis == "fep_ddg" and metadata.get("cycle", "folding") == "folding":
                add("Seeliger2010Thermostability", subject, record, "/metadata/cycle", "base_method", _FEP_DESIGN)
            if record["node_type"] == "prep" and metadata.get("leg_role") == "unfolded":
                add("Seeliger2010Thermostability", subject, record, "/metadata/leg_role", "base_method", _FEP_DESIGN)
            # Every other stage stays visible; no claim of exhaustive automatic mapping.
            unresolved.append({**ident, "method": "remaining_stage_methods",
                               "reason": "Stage methods outside the automated mappings require review; automated: "
                                         "OpenMM version and selected forces/integrator, recorded protein/water "
                                         "parameters and HMR, protonation, solvation, ligand parameters and charges, "
                                         "and the alchemical/MBAR methods"})
    library = Path(__file__).with_name("references.bib").read_text()
    entries = {m.group(1): m.group(0) for m in
               re.finditer(r"@\w+\{([^,]+),\n.*?^\}", library, re.M | re.S)}
    return {"selected": [{"key": key, "reasons": selected[key]} for key in sorted(selected)],
            "unresolved": unresolved, "documentation": documentation,
            "coverage_complete": False,
            "bibtex": "\n\n".join(entries[key] for key in sorted(selected)) + ("\n" if selected else "")}
