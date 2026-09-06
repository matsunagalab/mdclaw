"""Small evidence-based selector; the audit is not an unconditional citation list."""

from pathlib import Path
import re


_THEORY = "https://docs.openmm.org/latest/userguide/theory/02_standard_forces.html"
_MIDDLE = "https://docs.openmm.org/latest/api-python/generated/openmm.openmm.LangevinMiddleIntegrator.html"
_PARAMETERS = {
    "ff14SB": "Maier2015ff14SB", "ff19SB": "Tian2020ff19SB",
    "tip3p": "Jorgensen1983TIP3P", "opc": "Izadi2014OPC",
    "opc3": "Izadi2016OPC3", "spce": "Berendsen1987SPCE",
    "tip4pew": "Horn2004TIP4PEw",
}
_PARAMETER_SOURCE = "https://docs.openmm.org/latest/userguide/application/02_running_sims.html#force-fields"


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
            version = runtime.get("runtime_system", {}).get("openmm_version", "") or ""
            if version.startswith("8."):
                add("Eastman2024OpenMM8", subject, record, "/System/@openmmVersion",
                    "software", "https://docs.openmm.org/latest/userguide/introduction.html#referencing-openmm",
                    record["runtime_sources"]["runtime_system"])
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
            # Every other stage stays visible; no claim of exhaustive automatic mapping.
            unresolved.append({**ident, "method": "remaining_stage_methods",
                               "reason": "Force-field, preparation and analysis provenance requires review; "
                                         "only explicit OpenMM and selected parameter/HMR mappings are automated"})
    library = Path(__file__).with_name("references.bib").read_text()
    entries = {m.group(1): m.group(0) for m in
               re.finditer(r"@\w+\{([^,]+),\n.*?^\}", library, re.M | re.S)}
    return {"selected": [{"key": key, "reasons": selected[key]} for key in sorted(selected)],
            "unresolved": unresolved, "documentation": documentation,
            "coverage_complete": False,
            "bibtex": "\n\n".join(entries[key] for key in sorted(selected)) + ("\n" if selected else "")}
