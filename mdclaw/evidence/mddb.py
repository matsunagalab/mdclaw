"""Offline MDDB-workflow bundles: selected trajectories, paired PDBs, YAML and provenance."""

import csv
import contextlib
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Optional

from mdclaw.evidence.reporting import generate_md_report


_UPSTREAM = "https://github.com/mmb-irb/MDDB-workflow/blob/4e6dceeee67ce83650eed4aa2cfffe10107e2564/"
_SOLVENT = {"SOL", "WAT", "HOH", "TIP", "TP3", "SWM4", "W"}
_COUNTER_IONS = {"K", "NA", "SOD", "POT", "CL", "CLA"}
_HUMAN_FIELDS = {"name", "description", "authors", "groups", "contact", "license", "linkcense",
                 "citation", "thanks", "pdb_ids", "collections", "ligands", "forced_references"}
_MD_FIELDS = {"method", "program", "version", "framestep", "timestep", "temp", "ensemble", "ff", "wat", "boxtype"}
_REQUIRED = {"name", "authors", "contact", "license", "linkcense", "method"}


def _reference_metadata(metadata):
    """Validate MDDB reference shapes offline, without changing caller-owned data."""
    def nonempty_string(value):
        return isinstance(value, str) and bool(value.strip())

    metadata = dict(metadata)
    ligands = metadata.get("ligands")
    if ligands is not None:
        if not isinstance(ligands, list):
            raise ValueError("metadata.ligands must be a list of ligand entries")
        normalized = []
        for index, entry in enumerate(ligands):
            field = f"metadata.ligands[{index}]"
            if type(entry) is int or isinstance(entry, str):
                entry = {"pubchem": str(entry)}
            if not isinstance(entry, dict) or set(entry) - {"pubchem", "drugbank", "chembl", "vmd_selection"}:
                raise ValueError(f"{field} accepts pubchem, drugbank, chembl and vmd_selection only; "
                                 "use vmd_selection, not selection")
            entry = {key: value for key, value in entry.items() if value is not None}
            if not any(key in entry for key in ("pubchem", "drugbank", "chembl")):
                raise ValueError(f"{field} requires a PubChem, DrugBank or ChEMBL identifier")
            if "pubchem" in entry:
                cid = entry["pubchem"]
                if not ((type(cid) is int and cid > 0) or
                        (isinstance(cid, str) and re.fullmatch(r"[0-9]+", cid) and int(cid) > 0)):
                    raise ValueError(f"{field}.pubchem must be a positive integer CID or numeric string")
            for key in ("drugbank", "chembl"):
                if key in entry and not nonempty_string(entry[key]):
                    raise ValueError(f"{field}.{key} must be a nonempty identifier string")
            if "vmd_selection" in entry:
                value = entry["vmd_selection"]
                selections = value if isinstance(value, list) else [value]
                if not selections or not all(nonempty_string(s) for s in selections):
                    raise ValueError(f"{field}.vmd_selection must be a nonempty string or list of nonempty strings")
            normalized.append(entry)
        metadata["ligands"] = normalized
    references = metadata.get("forced_references")
    if references is not None:
        if isinstance(references, dict):
            valid = all(nonempty_string(k) and nonempty_string(v) for k, v in references.items())
        else:
            valid = isinstance(references, list) and all(nonempty_string(v) for v in references)
        if not valid:
            raise ValueError("metadata.forced_references must be a list of UniProt identifiers or a "
                             "chain-to-identifier mapping with nonempty strings; noref marks an unreferenced chain")
    return metadata


@contextlib.contextmanager
def _native_stderr():
    """DCD's C plugin logs to stdout; keep the single-tool CLI's JSON clean."""
    libc = ctypes.CDLL(None)
    libc.fflush(None)
    original = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        libc.fflush(None)
        os.dup2(original, 1)
        os.close(original)


def _hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _artifact(record, key):
    value = record["artifacts"].get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing {key} artifact at {record['node_id']}")
    path = (Path(record["artifact_base_dir"]) / value).resolve()
    if not path.is_file():
        raise ValueError(f"Missing artifact: {path}")
    return path


def _nearest(records, start, predicate):
    """Select the unique nearest parent ancestor, not a dependency or arbitrary branch."""
    level, seen = [start], set()
    while level:
        matches = sorted(nid for nid in level if predicate(records[nid]))
        if len(matches) > 1:
            raise ValueError(f"Ambiguous source nodes {matches}; select the intended trajectory node explicitly")
        if matches:
            return records[matches[0]]
        seen.update(level)
        level = sorted({p for nid in level for p in records[nid]["parent_node_ids"]} - seen)
    raise ValueError(f"No matching artifact in parent lineage of {start}")


def _source(subject):
    records = {r["node_id"]: r for r in subject["history"]}
    record = _nearest(records, subject["node_id"], lambda r: any(
        isinstance(r["artifacts"].get(k), str) for k in ("trajectory", "combined_trajectory")))
    if subject["status"] != "completed" or record["status"] != "completed":
        raise ValueError("Only completed targets and completed trajectory sources can be exported")
    combined = "combined_trajectory" in record["artifacts"]
    if combined and "trajectory" in record["artifacts"]:
        raise ValueError("Target has two trajectories; select an unambiguous source")
    trajectory = _artifact(record, "combined_trajectory" if combined else "trajectory")
    if trajectory.suffix.lower() != ".dcd":
        raise ValueError("MDDB export currently accepts recorded DCD trajectories only")
    if combined:
        topology_record = record
        topology = _artifact(record, "reference_pdb")
    else:
        topology_record = _nearest(records, record["node_id"], lambda r: "topology_pdb" in r["artifacts"])
        topology = _artifact(topology_record, "topology_pdb")
    if topology.suffix.lower() != ".pdb":
        raise ValueError("A recorded PDB topology is required")
    signature = record["recorded_metadata"].get("system_signature", {})
    expected = signature.get("topology_pdb_sha256") if isinstance(signature, dict) else None
    if not combined and expected and expected != _hash(topology):
        raise ValueError("Trajectory's recorded topology hash differs from the source PDB")
    return record, topology_record, topology, trajectory


def _mapping(value, name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Invalid recorded {name}: expected an object")
    return value


def _topology_metadata(record):
    """Use only the selected topology's metadata and its recorded build result."""
    topo = dict(record["recorded_metadata"])
    if record["artifacts"].get("amber_metadata"):
        path = _artifact(record, "amber_metadata")
        raw = path.read_bytes()
        build = _mapping(json.loads(raw), "amber_metadata")
        if build.get("success") is not True:
            raise ValueError(f"Topology build metadata does not record success: {path}")
        parameters = _mapping(build.get("parameters"), "topology parameters")
        fallback = {"effective_forcefield": parameters.get("effective_forcefield"),
                    "water_model": parameters.get("water_model"),
                    "solvent_type": build.get("solvent_type"),
                    "forcefield_provenance": build.get("forcefield_provenance")}
        for key, value in fallback.items():
            if value is None:
                continue
            if topo.get(key) is not None and topo[key] != value:
                raise ValueError(f"Topology {key} conflicts with recorded build metadata: {path}")
            topo[key] = value
        record["runtime_sources"]["amber_metadata"] = {
            "file": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    return topo


def _required_conditions(record, topology_record):
    topo = _topology_metadata(topology_record)
    provenance = _mapping(topo.get("forcefield_provenance"), "forcefield_provenance")
    method = _mapping(provenance.get("method"), "forcefield method")
    signature = _mapping(record["recorded_metadata"].get("system_signature"), "system_signature")
    solvents = {v for v in (signature.get("solvent_type"), topo.get("solvent_type"),
                            method.get("solvent_type")) if v is not None}
    if len(solvents) > 1:
        raise ValueError(f"Conflicting recorded solvent types: {sorted(solvents)}")
    required = {"ff", "temp", "timestep", "ensemble"}
    if not solvents or not solvents <= {"implicit", "vacuum"}:
        required.add("wat")
    return required


def _recorded_conditions(record, topology_record):
    """Scientific settings, without trajectory-specific sampling intervals."""
    recorded = record["recorded_metadata"]
    topo = _topology_metadata(topology_record)
    runtime = record["runtime"]
    integrator = runtime.get("integrator", {})
    system = runtime.get("runtime_system", {})
    params = {}
    version = (system.get("openmm_version") or integrator.get("openmmVersion")
               or runtime.get("openmm_serialization", {}).get("openmm_version"))
    if version:
        params.update(program="OpenMM", version=version)
    integrator_signature = _mapping(recorded.get("integrator_signature"), "integrator_signature")
    for field, key, attribute in (("temp", "temperature_kelvin", "temperature"),
                                  ("timestep", "timestep_fs", "stepSize")):
        value = recorded.get(key)
        signed = integrator_signature.get(key)
        if (attribute not in integrator and value is not None and signed is not None
                and not _same_parameter(field, value, signed)):
            raise ValueError(f"Recorded {field} conflicts with the trajectory's integrator_signature")
        if value is None:
            value = signed
        if value is not None:
            params[field] = value
    if "temperature" in integrator:
        params["temp"] = float(integrator["temperature"])
    if "stepSize" in integrator:
        params["timestep"] = float(integrator["stepSize"]) * 1000  # OpenMM ps -> MDDB fs
    signature = _mapping(recorded.get("system_signature"), "system_signature")
    if signature.get("ensemble"):
        params["ensemble"] = signature["ensemble"]
    elif recorded.get("ensemble"):
        params["ensemble"] = recorded["ensemble"]
    if topo.get("effective_forcefield"):
        params["ff"] = [topo["effective_forcefield"]]
    provenance = _mapping(topo.get("forcefield_provenance"), "forcefield_provenance")
    forcefield_xml = provenance.get("openmm_xml") or provenance.get("forcefield_xml") or topo.get("forcefield_xml")
    if forcefield_xml:
        params["ff"] = forcefield_xml
    if topo.get("water_model"):
        params["wat"] = topo["water_model"]
    return params


def _same_parameter(key, left, right):
    if key in ("framestep", "timestep", "temp") and type(left) in (float, int) and type(right) in (float, int):
        return math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-9)
    return type(left) is type(right) and left == right


def _combined_conditions(subject, record, metadata):
    """Recover only conditions evidenced for every recorded concat input."""
    sources = record["recorded_metadata"].get("source_trajectories")
    if not isinstance(sources, list) or not sources or not all(isinstance(p, str) and p for p in sources):
        raise ValueError(f"Combined trajectory {record['node_id']} lacks source_trajectories provenance; "
                         "export the source nodes separately or recreate the combined artifact with recorded inputs")
    records = {r["node_id"]: r for r in subject["history"]}
    ancestors, pending = set(), list(record["parent_node_ids"])
    while pending:
        nid = pending.pop()
        if nid not in ancestors:
            ancestors.add(nid)
            pending.extend(records[nid]["parent_node_ids"])
    conditions = []
    required = {"ff", "temp", "timestep", "ensemble"}
    for source in sources:
        path = Path(source)
        # concat records absolute paths. Never resolve legacy relative paths
        # against the exporter's current working directory.
        matches = [records[nid] for nid in sorted(ancestors) if path.is_absolute()
                   and isinstance(records[nid]["artifacts"].get("trajectory"), str)
                   and (Path(records[nid]["artifact_base_dir"]) /
                        records[nid]["artifacts"]["trajectory"]).resolve() == path.resolve()]
        if len(matches) != 1 or matches[0]["status"] != "completed":
            raise ValueError(f"Combined trajectory source {source!r} must identify one completed parent-lineage "
                             "trajectory; export the source nodes separately or recreate the combined artifact")
        segment = matches[0]
        topo = _nearest(records, segment["node_id"], lambda r: "topology_pdb" in r["artifacts"])
        values = _recorded_conditions(segment, topo)
        required.update(_required_conditions(segment, topo))
        conditions.append((segment["node_id"], values))
    common, missing = {}, []
    fields = required | {key for _, values in conditions for key in values}
    for key in sorted(fields):
        known = {nid: values[key] for nid, values in conditions if key in values}
        if known:
            value = next(iter(known.values()))
            if any(not _same_parameter(key, value, other) for other in known.values()):
                raise ValueError(f"Combined trajectory has conflicting {key} across source nodes: {known!r}; "
                                 "export segments separately instead of assigning one value")
            if metadata.get(key) is not None and not _same_parameter(key, metadata[key], value):
                raise ValueError(f"Metadata {key} conflicts with recorded source values {known!r}")
        if all(key in values for _, values in conditions):
            common[key] = next(iter(known.values()))
        elif metadata.get(key) is None:
            missing.append(key)
    return common, missing


def _parameters(record, topology_record, metadata, stride, inherited=None):
    recorded = record["recorded_metadata"]
    params = _recorded_conditions(record, topology_record)
    for key, value in (inherited or {}).items():
        if key in params and not _same_parameter(key, params[key], value):
            raise ValueError(f"Combined trajectory {key} conflicts with its source nodes")
        params[key] = value
    if recorded.get("output_frequency_ps") is not None:
        params["framestep"] = float(recorded["output_frequency_ps"]) / 1000
    if "energy" in record["artifacts"]:
        with _artifact(record, "energy").open() as stream:
            rows = csv.DictReader(stream)
            times = [float(row["Time (ps)"]) / 1000 for row in rows]
        if len(times) >= 2:
            import numpy as np
            differences = np.diff(times)
            if not np.isfinite(differences).all() or differences[0] <= 0 or not np.allclose(differences, differences[0], rtol=1e-6, atol=1e-9):
                raise ValueError("Recorded energy/frame times are irregular; split/resample explicitly")
            params["framestep"] = float(differences[0])
    if "frame_times_ns" in record["artifacts"]:
        import numpy as np
        times = np.load(_artifact(record, "frame_times_ns"), allow_pickle=False)
        if times.ndim != 1 or len(times) < 2 or not np.isfinite(times).all():
            raise ValueError("Invalid or insufficient recorded frame times")
        steps = np.diff(times)
        if steps[0] <= 0 or not np.allclose(steps, steps[0], rtol=1e-6, atol=1e-9):
            raise ValueError("Irregular frame times cannot be represented by MDDB framestep; split/resample explicitly")
        params["framestep"] = float(steps[0])
    for key in _MD_FIELDS:
        if metadata.get(key) is None:
            continue
        if key in params and not _same_parameter(key, metadata[key], params[key]):
            raise ValueError(f"Metadata {key} conflicts with recorded source value {params[key]!r}")
        params[key] = metadata[key]
    for key in ("framestep", "timestep", "temp"):
        if key in params and (isinstance(params[key], bool) or not isinstance(params[key], (int, float))
                              or not math.isfinite(params[key]) or params[key] <= 0):
            raise ValueError(f"{key} must be a finite positive number")
    for key in ("ff", "wat", "ensemble", "program", "version"):
        if key in params:
            values = params[key] if key == "ff" and isinstance(params[key], list) else [params[key]]
            if not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError(f"Invalid recorded {key}: expected nonempty strings")
    if "framestep" in params:
        params["framestep"] *= stride
    return params


def _selection(topology, selection):
    import numpy as np
    if selection is not None:
        indices = topology.select(selection)
    else:
        # Match MDDB's standard-name counter-ion convention; do not discard all ions/lipids.
        indices = [a.index for a in topology.atoms if not (
            a.residue.is_water or a.residue.name.upper() in _SOLVENT or
            (a.residue.n_atoms == 1 and ''.join(c for c in a.name.upper() if c.isalpha()) in _COUNTER_IONS))]
    indices = np.asarray(indices, dtype=int)
    if not len(indices):
        raise ValueError("Solvent removal/selection left no atoms")
    if any(topology.atom(int(i)).residue.is_water or topology.atom(int(i)).residue.name.upper() in _SOLVENT for i in indices):
        raise ValueError("Selection retains water; MDDB export requires a solvent-stripped selection")
    return indices


def _write_pdb(path, source_lines, topology, indices, xyz, lengths, angles):
    """Keep source identities/CONECT and mark selection-created residue gaps with TER."""
    atoms = [line for line in source_lines if line.startswith(("ATOM  ", "HETATM"))]
    residue_indices = [atom.residue.index for atom in topology.atoms]
    serials = {atoms[int(i)][6:11].strip() for i in indices}
    if len(serials) != len(indices):
        raise ValueError("PDB atom serials are not unique; cannot safely retain CONECT")
    lines = []
    if lengths is not None and angles is not None:
        lines.append("CRYST1" + ''.join(f"{x:9.3f}" for x in lengths) +
                     ''.join(f"{x:7.2f}" for x in angles) + " P 1           1\n")
    chosen = {int(i): position for i, position in zip(indices, xyz)}
    index, retained = 0, False
    previous_residue = None
    for line in source_lines:
        if line.startswith(("ATOM  ", "HETATM")):
            if index in chosen:
                residue = residue_indices[index]
                # Removing whole residues makes non-neighbors adjacent in the
                # PDB. A TER prevents readers from inventing a polymer bond.
                # Source indices, unlike resSeq labels, include insertion codes
                # and do not mistake a numbering gap for an omitted residue.
                if retained and residue > previous_residue + 1:
                    lines.append("TER\n")
                coords = ''.join(f"{x:8.3f}" for x in chosen[index])
                if len(coords) != 24:
                    raise ValueError("Coordinates exceed PDB field width")
                lines.append(line[:30] + coords + line[54:].rstrip() + "\n")
                retained = True
                previous_residue = residue
            index += 1
        elif line.startswith("TER") and retained:
            lines.append("TER\n")
            retained = False
        elif line.startswith("CONECT"):
            fields = [line[i:i + 5].strip() for i in range(6, len(line.rstrip()), 5)]
            if fields and fields[0] in serials:
                targets = [s for s in fields[1:] if s in serials]
                if targets:
                    lines.append("CONECT" + ''.join(f"{s:>5}" for s in [fields[0], *targets]) + "\n")
    path.write_text(''.join(lines) + "END\n")


def _convert(topology_path, trajectory, out, selection, stride, chunk):
    import mdtraj as md
    import numpy as np
    from mdtraj.formats import DCDTrajectoryFile

    original_hashes = _hash(topology_path), _hash(trajectory)
    source_lines = topology_path.read_text().splitlines(keepends=True)
    source_atoms = [line for line in source_lines if line.startswith(("ATOM  ", "HETATM"))]
    topology = md.load_topology(str(topology_path))
    if len(source_atoms) != topology.n_atoms:
        raise ValueError("Source PDB records/topology mismatch (multiple models or alternate locations)")
    indices = _selection(topology, selection)
    out.mkdir()
    first, frame_count = None, 0
    with DCDTrajectoryFile(str(trajectory)) as infile:
        probe, _, _ = infile.read(n_frames=1)
        if probe.size == 0 or probe.shape[1] != topology.n_atoms:
            raise ValueError("Source DCD is empty or its atom count differs from the topology")
        source_frames = len(infile)
        infile.seek(0)
        with DCDTrajectoryFile(str(out / "trajectory.dcd"), "w", force_overwrite=False) as outfile:
            while True:
                xyz, lengths, angles = infile.read(n_frames=chunk, stride=stride, atom_indices=indices)
                if xyz.size == 0:
                    break
                if not np.isfinite(xyz).all():
                    raise ValueError("Trajectory contains non-finite coordinates")
                if lengths is not None and (not np.isfinite(lengths).all() or (lengths <= 0).any()):
                    raise ValueError("Invalid periodic box lengths")
                if angles is not None and not np.isfinite(angles).all():
                    raise ValueError("Invalid periodic box angles")
                if first is None:
                    first = xyz[0], lengths[0] if lengths is not None else None, angles[0] if angles is not None else None
                outfile.write(xyz, cell_lengths=lengths, cell_angles=angles)
                frame_count += len(xyz)
    if frame_count != (source_frames + stride - 1) // stride:
        raise ValueError("Exported frame count does not match source/stride")
    _write_pdb(out / "system.pdb", source_lines, topology, indices, *first)
    # Round-trip paired atom count, coordinates, and selected bond connectivity.
    # PDB precision is 0.001 A.
    pdb = md.load(str(out / "system.pdb"))
    with DCDTrajectoryFile(str(out / "trajectory.dcd")) as check:
        xyz, _, _ = check.read(n_frames=1)
        if len(check) != frame_count or pdb.n_atoms != xyz.shape[1] or not np.allclose(pdb.xyz[0] * 10, xyz[0], atol=0.001, rtol=0):
            raise ValueError("Exported PDB/DCD round-trip validation failed")
    identity = [source_atoms[int(i)][12:27] + source_atoms[int(i)][76:78] for i in indices]
    atom_map = {int(old): new for new, old in enumerate(indices)}
    bonds = sorted(sorted([atom_map[a.index], atom_map[b.index]]) for a, b in topology.bonds
                   if a.index in atom_map and b.index in atom_map)
    exported_bonds = sorted(sorted([a.index, b.index]) for a, b in pdb.topology.bonds)
    if exported_bonds != bonds:
        raise ValueError("Exported PDB bond round-trip validation failed: selected source bonds differ")
    if original_hashes != (_hash(topology_path), _hash(trajectory)):
        raise ValueError("Source artifacts changed during export; retry from immutable inputs")
    return {"n_atoms_original": topology.n_atoms, "n_atoms": len(indices), "n_frames": frame_count,
            "source_frames": source_frames, "atom_indices": indices.tolist(),
            "topology_identity_sha256": hashlib.sha256(json.dumps([identity, bonds]).encode()).hexdigest(),
            "source_topology": {"file": str(topology_path), "sha256": original_hashes[0]},
            "source_trajectory": {"file": str(trajectory), "sha256": original_hashes[1]},
            "outputs": {f: _hash(out / f) for f in ("system.pdb", "trajectory.dcd")}}


def export_mddb(
    output_dir: str,
    job_dir: Optional[str] = None,
    study_dir: Optional[str] = None,
    targets: Optional[list[dict]] = None,
    grouping: Optional[str] = None,
    plan_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    selection: Optional[str] = None,
    stride: int = 1,
    chunk: int = 100,
) -> dict:
    """Create an offline MDDB YAML/PDB/DCD bundle in a new directory; never upload.

    Same target/grouping contract as generate_md_report. Metadata requires name,
    authors, contact, license, linkcense and method (exporter safeguards, not an
    assertion about mandatory website fields). Missing source frame spacing can
    be supplied as metadata.framestep in ns before stride. Defaults remove water
    and standard Na/K/Cl counter ions; selection is an optional MDTraj expression.
    Export the nearest unique recorded DCD, not a silently concatenated prod chain.
    Optional metadata.ligands accepts PubChem/DrugBank/ChEMBL identifiers and
    vmd_selection; metadata.forced_references accepts UniProt IDs as a list or
    chain-to-ID mapping (including noref). Identifier existence and molecular
    mapping are checked by the downstream MDDB workflow, not this offline export.
    """
    import yaml

    try:
        if any(type(x) is not int or x < 1 for x in (stride, chunk)):
            raise ValueError("stride and chunk must be positive integers")
        result = generate_md_report(job_dir=job_dir, study_dir=study_dir, targets=targets,
                                    grouping=grouping, plan_id=plan_id)
        if not result["success"]:
            return result
        report = result["report"]
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict) or set(metadata) - (_HUMAN_FIELDS | _MD_FIELDS):
            raise ValueError("Unsupported metadata fields; file paths and MD entries are generated by the exporter")
        missing = sorted(k for k in _REQUIRED if not metadata.get(k))
        if missing:
            return {"success": False, "code": "mddb_metadata_required", "missing_fields": missing,
                    "message": "Ask the user; do not invent authors, license or MD method. No files written."}
        if not all(isinstance(metadata[k], str) and metadata[k].strip() for k in _REQUIRED - {"authors"}):
            raise ValueError("Project name, contact, license, linkcense and method must be nonempty strings")
        if not isinstance(metadata["authors"], (str, list)) or (isinstance(metadata["authors"], list)
                and not all(isinstance(a, str) and a.strip() for a in metadata["authors"])):
            raise ValueError("authors must be a name or list of names")
        metadata = _reference_metadata(metadata)
        for key, value in metadata.items():
            if value is None or key in ("framestep", "timestep", "temp", "ligands", "forced_references"):
                continue
            values = value if key in {"authors", "groups", "pdb_ids", "collections", "ff"} and isinstance(value, list) else [value]
            if not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError(f"Invalid metadata type or empty value: {key}")
            if key == "pdb_ids" and any(not re.fullmatch(r"[1-9][a-zA-Z0-9]{3}", v) for v in values):
                raise ValueError("Invalid metadata.pdb_ids: expected four-character PDB identifiers")
        out = Path(output_dir).expanduser().resolve()
        if out.exists() or any(out.is_relative_to(Path(s["job_dir"]) / "nodes") for s in report["subjects"]):
            raise ValueError("output_dir must be new and outside immutable node directories")
        sources = [_source(s) for s in report["subjects"]]
        identities = [str(src[3]) for src in sources]
        if grouping == "replicas" and len(set(identities)) != len(identities):
            raise ValueError("Replica targets resolve to the same source trajectory")
        params = []
        for subject, src in zip(report["subjects"], sources):
            inherited = None
            missing = []
            required = {"framestep"}
            if "combined_trajectory" in src[0]["artifacts"]:
                inherited, missing = _combined_conditions(subject, src[0], metadata)
            else:
                required.update(_required_conditions(src[0], src[1]))
            values = _parameters(src[0], src[1], metadata, stride, inherited)
            missing = sorted(set(missing) | (required - values.keys()))
            if missing:
                return {"success": False, "code": "mddb_metadata_required", "missing_fields": missing,
                        "node_id": src[0]["node_id"], "job_dir": subject["job_dir"],
                        "message": "Trajectory conditions are not fully recorded for the selected source(s). "
                                   "Supply the missing metadata only if it applies to all included frames; "
                                   "otherwise export segments separately. No files written."}
            params.append(values)
        template = yaml.safe_load(Path(__file__).with_name("mddb_template.yaml").read_text())
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mdclaw-mddb-", dir=out.parent) as staging:
            stage = Path(staging)
            projects, runs = {}, []
            for i, (subject, src, md_params) in enumerate(zip(report["subjects"], sources, params)):
                project_key = f"project_{i + 1:03d}" if grouping == "separate" else "project"
                project = stage / project_key
                project.mkdir(exist_ok=True)
                md_dir = f"md_{i + 1:03d}"
                with _native_stderr():
                    converted = _convert(src[2], src[3], project / md_dir, selection, stride, chunk)
                if "energy" in src[0]["artifacts"]:
                    with _artifact(src[0], "energy").open() as stream:
                        if sum(1 for _ in csv.DictReader(stream)) != converted["source_frames"]:
                            raise ValueError("Energy/frame-time count differs from the source DCD")
                if "frame_times_ns" in src[0]["artifacts"]:
                    import numpy as np
                    if len(np.load(_artifact(src[0], "frame_times_ns"), allow_pickle=False)) != converted["source_frames"]:
                        raise ValueError("Frame-time count differs from the source DCD")
                data = {**converted, "label": subject["label"], "requested_node_id": subject["node_id"],
                        "trajectory_node_id": src[0]["node_id"], "job_dir": subject["job_dir"],
                        "selection": selection or "MDDB standard water/counter-ion removal", "stride": stride,
                        "coordinate_processing": "No imaging, fitting, concatenation or pooling"}
                if project_key not in projects:
                    projects[project_key] = {**template, **{k: v for k, v in metadata.items() if k in _HUMAN_FIELDS},
                                             "input_structure_filepath": f"{md_dir}/system.pdb", "mds": [],
                                             "metadditions": {"mdclaw": {"template_source": _UPSTREAM + "mddb_workflow/resources/inputs_file_template.yml",
                                                                          "report": "../report.json"}}}
                entries = projects[project_key]["mds"]
                if entries and entries[0]["metadditions"]["mdclaw"]["topology_identity_sha256"] != converted["topology_identity_sha256"]:
                    raise ValueError("MDDB project topology differs between replicas; use grouping='separate'")
                entries.append({"name": subject["label"], "mdir": md_dir,
                                "input_structure_filepath": f"{md_dir}/system.pdb",
                                "input_trajectory_filepaths": [f"{md_dir}/trajectory.dcd"], **md_params,
                                "metadditions": {"mdclaw": {**data, "history": subject["history"]}}})
                runs.append({"project": project_key, "directory": md_dir, **data})
            for key, inputs in projects.items():
                text = yaml.safe_dump(inputs, sort_keys=False, allow_unicode=True)
                if yaml.safe_load(text) != inputs:
                    raise ValueError("YAML round-trip validation failed")
                (stage / key / "inputs.yaml").write_text(text)
            manifest = {"schema_version": 1, "runs": runs, "projects": list(projects),
                        "validation": "PDB/DCD counts, atom ordering, first coordinates, YAML round-trip",
                        "uploaded": False, "mddb_server_validated": False}
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (stage / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            (stage / "references.bib").write_text(report["citations"]["bibtex"])
            # Reserve a fresh final directory before moving any files; never overwrite a prior bundle.
            out.mkdir(exist_ok=False)
            for child in sorted(stage.iterdir(), key=lambda p: p.name == "manifest.json"):
                shutil.move(str(child), out / child.name)
        return {"success": True, "code": "ok", "output_dir": str(out),
                "inputs_files": [str(out / k / "inputs.yaml") for k in projects], "manifest": manifest,
                "warnings": ["PDB is a structure fallback, not a full force-field topology.",
                             "Bundle prepared offline; MDDB ingestion/QA has not been run."]}
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        return {"success": False, "code": "mddb_export_failed", "errors": [str(exc)]}
