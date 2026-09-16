"""Single-walker solute tempering (SST2) as a production node.

``run_sst2`` runs one Simulated Solute Tempering 2 walker on the
``system.xml`` / ``topology.pdb`` / ``state.xml`` triple of the topo
ancestor.  The tempering itself is done by the SST2 package (GPL-2.0,
matsunagalab/SST2 fork, branch ``mdclaw``), which MDClaw calls as a
separate process through its ``SST2.driver`` entry point; MDClaw never
imports SST2.  Every walker is an independent ``prod`` node, so N
walkers are N nodes, and a walker is continued with ``--continue-from``
exactly like a plain production run: the sidecar written by the driver
(current rung, running averages, effective weights) is handed back to
the next node.

Locate SST2 with ``sst2_home`` or ``MDCLAW_SST2_HOME`` (a checkout: its
``src`` and optional ``.pylib`` are put on ``PYTHONPATH``); when neither
is set the current interpreter must be able to ``import SST2``.
"""
# Configure logging early to suppress noisy third-party logs
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402
logger = setup_logger(__name__)

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import create_unique_subdir  # noqa: E402
from mdclaw._tool_meta import node_tool  # noqa: E402
from mdclaw.simulation._base import (  # noqa: E402
    _node_artifact_path,
    _resolve_topology_run_settings,
)
from mdclaw.simulation.xml_contract import WORKING_DIR  # noqa: E402

SAMPLING_METHOD = "sst2"
# Residue names that are never part of a tempered solute (water models, ions).
_SOLVENT_RESIDUES = frozenset({
    "HOH", "WAT", "SOL", "TIP3", "TIP4", "TP3", "OPC", "SPC", "H2O",
    "NA", "NA+", "CL", "CL-", "K", "K+", "MG", "MG2", "CA", "CA2", "ZN", "ZN2",
    "LI", "RB", "CS", "F", "BR", "I", "SOD", "CLA", "POT", "CAL", "MG2+", "CA2+",
})
DRIVER_MODULE = "SST2.driver"


class SST2ToolError(RuntimeError):
    """Structured failure with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sst2_environment(sst2_home: Optional[str]) -> tuple[dict, Optional[str]]:
    """Environment for the driver process and where SST2 comes from."""
    home = sst2_home or os.environ.get("MDCLAW_SST2_HOME")
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    if home:
        home_path = Path(home).expanduser().resolve()
        src = home_path / "src"
        if not (src / "SST2" / "driver.py").is_file():
            raise SST2ToolError(
                code="sst2_not_installed",
                message=f"MDCLAW_SST2_HOME={home_path} has no src/SST2/driver.py; point it at a "
                "checkout of matsunagalab/SST2 (branch mdclaw).",
            )
        extra = [str(src)]
        pylib = home_path / ".pylib"
        if pylib.is_dir():
            extra.append(str(pylib))
        env["PYTHONPATH"] = os.pathsep.join(extra + [p for p in [env.get("PYTHONPATH")] if p])
        return env, str(home_path)
    probe = subprocess.run(
        [sys.executable, "-c", "import SST2.driver"],
        capture_output=True, text=True, env=env,
    )
    if probe.returncode != 0:
        raise SST2ToolError(
            code="sst2_not_installed",
            message="SST2 is not importable and MDCLAW_SST2_HOME is not set; install the "
            "matsunagalab/SST2 fork (branch mdclaw) or set MDCLAW_SST2_HOME to its checkout.",
        )
    return env, None


def _resolve_solute_indices(
    topology_pdb_file: str,
    *,
    solute_selection: Optional[str],
    solute_indices_file: Optional[str],
) -> tuple[list[int], dict]:
    """Solute atom indices from an mdtraj selection or a JSON list."""
    if bool(solute_selection) == bool(solute_indices_file):
        raise SST2ToolError(
            code="sst2_solute_required",
            message="Give exactly one of solute_selection (mdtraj DSL) or solute_indices_file "
            "(JSON list of 0-based atom indices).",
        )
    if solute_indices_file:
        with open(solute_indices_file) as fh:
            indices = sorted({int(i) for i in json.load(fh)})
        provenance = {"solute_indices_file": str(Path(solute_indices_file).resolve())}
    else:
        import mdtraj as md
        from openmm.app import PDBFile

        topology = PDBFile(topology_pdb_file).topology
        mdtop = md.Topology.from_openmm(topology)
        try:
            indices = sorted({int(i) for i in mdtop.select(solute_selection)})
        except Exception as exc:  # noqa: BLE001
            raise SST2ToolError(
                code="sst2_solute_selection_invalid",
                message=f"solute_selection {solute_selection!r} is not a valid mdtraj selection: {exc}",
            ) from exc
        atoms = list(topology.atoms())
        residues = sorted({atoms[i].residue.index for i in indices})
        provenance = {
            "solute_selection": solute_selection,
            "solute_residues": [
                f"{atoms[i].residue.chain.id}:{atoms[i].residue.name}{atoms[i].residue.id}"
                for i in indices if atoms[i].name in ("CA", "C1'", "P")
            ][:200],
            "solute_residue_count": len(residues),
        }
        n_atoms = topology.getNumAtoms()
        if len(indices) == n_atoms:
            raise SST2ToolError(
                code="sst2_solute_selection_invalid",
                message="solute_selection selects every atom; solute tempering needs a solvent part.",
            )
    if not indices:
        raise SST2ToolError(
            code="sst2_solute_selection_empty",
            message="The solute selection matched zero atoms.",
        )
    # A solute must be a part of the solute molecules. mdtraj selections such
    # as "resid 96 to 108" address global residue indices, and index lists can
    # be built against the wrong topology, so water, ions and virtual sites
    # sneak in silently; that would temper the solvent instead of the loop.
    from openmm.app import PDBFile

    topology = PDBFile(topology_pdb_file).topology
    atoms = list(topology.atoms())
    if indices[-1] >= len(atoms):
        raise SST2ToolError(
            code="sst2_solute_selection_invalid",
            message=f"solute index {indices[-1]} is beyond the {len(atoms)} atoms of topology.pdb",
        )
    offenders: dict[str, int] = {}
    for i in indices:
        atom = atoms[i]
        rname = atom.residue.name.upper()
        if atom.element is None:
            offenders["virtual sites"] = offenders.get("virtual sites", 0) + 1
        elif rname in _SOLVENT_RESIDUES:
            offenders[rname] = offenders.get(rname, 0) + 1
    provenance["solute_residue_names"] = sorted({atoms[i].residue.name for i in indices})
    if offenders:
        raise SST2ToolError(
            code="sst2_solute_includes_solvent",
            message=(
                "The solute selection contains solvent, ions or virtual sites: "
                + ", ".join(f"{k} x{v}" for k, v in sorted(offenders.items()))
                + ". Restrict it to solute molecules, e.g. add 'and protein' or 'chainid 0', "
                "and check that index files were built against this topology.pdb."
            ),
        )
    return indices, provenance


def _validate_ladder(temperatures_kelvin, reference_temperature_kelvin) -> tuple[list[float], float]:
    if not temperatures_kelvin or len(temperatures_kelvin) < 2:
        raise SST2ToolError(
            code="sst2_ladder_invalid",
            message="temperatures_kelvin needs at least two rung temperatures, increasing, "
            "e.g. 300 357 424 505 600.",
        )
    try:
        ladder = [float(t) for t in temperatures_kelvin]
    except (TypeError, ValueError) as exc:
        raise SST2ToolError(
            code="sst2_ladder_invalid",
            message=f"temperatures_kelvin must be numbers: {temperatures_kelvin!r} ({exc})",
        ) from exc
    if any(b <= a for a, b in zip(ladder, ladder[1:])) or ladder[0] <= 0:
        raise SST2ToolError(
            code="sst2_ladder_invalid",
            message=f"temperatures_kelvin must be strictly increasing and positive: {ladder}",
        )
    ref = float(reference_temperature_kelvin) if reference_temperature_kelvin is not None else ladder[0]
    if ref not in ladder:
        raise SST2ToolError(
            code="sst2_ladder_invalid",
            message=f"reference_temperature_kelvin {ref} must be one of the rungs {ladder}.",
        )
    return ladder, ref


def _summarize_report(report_csv: Path, ladder: list[float]) -> dict:
    """Rung occupancy, accepted moves and round trips from the SST2 report."""
    import csv

    temps: list[float] = []
    with open(report_csv) as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                temps.append(float(row["Aim Temp (K)"]))
            except (KeyError, ValueError):
                continue
    if not temps:
        return {"report_rows": 0}
    index = {t: i for i, t in enumerate(ladder)}
    rungs = [index.get(t, -1) for t in temps]
    occupancy = [rungs.count(i) for i in range(len(ladder))]
    changes = sum(1 for a, b in zip(rungs, rungs[1:]) if a != b)
    # round trips: bottom -> top -> bottom
    trips, seen_top = 0, False
    for r in rungs:
        if r == len(ladder) - 1:
            seen_top = True
        elif r == 0 and seen_top:
            trips += 1
            seen_top = False
    return {
        "report_rows": len(rungs),
        "rung_occupancy": occupancy,
        "rung_occupancy_fraction": [c / len(rungs) for c in occupancy],
        "rung_changes": changes,
        "rung_change_fraction": changes / max(1, len(rungs) - 1),
        "round_trips": trips,
        "final_rung": rungs[-1],
    }


@node_tool(node_type="prod")
def run_sst2(
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    solute_selection: Optional[str] = None,
    solute_indices_file: Optional[str] = None,
    temperatures_kelvin: Optional[list[str]] = None,
    reference_temperature_kelvin: Optional[float] = None,
    simulation_time_ns: float = 1.0,
    exchange_interval_ps: float = 2.0,
    output_frequency_ps: float = 10.0,
    pressure_bar: Optional[float] = None,
    timestep_fs: Optional[float] = None,
    scale_nonbonded: bool = True,
    exclude_proline_omega: bool = False,
    move: str = "gibbs",
    weights_file: Optional[str] = None,
    restart_state_file: Optional[str] = None,
    random_seed: Optional[int] = None,
    platform: str = "auto",
    device_index: Optional[str] = None,
    hmr: Optional[bool] = None,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    sst2_home: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Run one Simulated Solute Tempering 2 (SST2) walker.

    A walker samples the solute (a part of the system, e.g. a CDR-H3
    loop) over a ladder of effective temperatures while the rest of the
    system stays at the reference temperature; it is a single trajectory
    that moves between rungs (REST2 scaling, simulated-tempering moves),
    so no communication between replicas is needed.  Use several nodes
    with different ``random_seed`` values for several walkers.

    Args:
        system_xml_file: ``system.xml`` of the topo ancestor (auto-resolved
            in node mode).
        topology_pdb_file: ``topology.pdb`` of the same topo ancestor.
        state_xml_file: State to start from (auto-resolved: the eq or
            parent prod ``state``).
        solute_selection: mdtraj selection of the solute atoms, e.g.
            ``"chainid 0 and resid 97 to 109"``.  Cut at residue
            boundaries.  Alternative: ``solute_indices_file``.
        solute_indices_file: JSON list of 0-based solute atom indices.
        temperatures_kelvin: Rung temperatures in K, increasing (CLI:
            ``--temperatures-kelvin 300 357 424 505 600``); the solute
            scaling is ``lambda = T_ref / T``.
        reference_temperature_kelvin: Physical temperature (default: the
            first rung).
        simulation_time_ns: Time to run in this call.
        exchange_interval_ps: Interval between rung moves (default 2 ps).
        output_frequency_ps: Trajectory frame interval.
        pressure_bar: NPT pressure; None or 0 runs NVT.
        timestep_fs: Default 4 fs with HMR, 2 fs otherwise.
        scale_nonbonded: False scales torsions only (gREST dihedral mode).
        exclude_proline_omega: Keep proline omega torsions unscaled.
        move: ``gibbs`` (all rungs, default) or ``neighbor``.
        weights_file: JSON list of fixed rung free energies (kJ/mol) for a
            fixed-weight production stage; omitted = adaptive weights.
        restart_state_file: Sidecar JSON of a previous walker run (auto-
            resolved from a ``--continue-from`` parent's ``tempering_state``).
        random_seed: Seed for the integrator and the rung moves.
        platform: OpenMM platform (``auto``, ``CUDA``, ``CPU``...).
        device_index: CUDA/OpenCL device index.
        hmr: Inherited from the topo ancestor when omitted.
        name: Output prefix (default ``sst2``).
        output_dir: Output location outside node mode.
        sst2_home: Checkout of the SST2 fork (default ``MDCLAW_SST2_HOME``).
        job_dir: Study job directory (node mode).
        node_id: Node id (node mode).

    Returns:
        dict with ``success``, ``output_dir``, artifact paths,
        ``tempering`` (rung occupancy, moves, weights) and errors.
    """
    result: dict = {
        "success": False,
        "sampling_method": SAMPLING_METHOD,
        "errors": [],
        "warnings": [],
    }
    _node_mode = bool(job_dir and node_id)
    restart_from_node_id = None
    if _node_mode:
        from mdclaw._node import resolve_node_inputs, validate_node_execution_context

        _inputs = resolve_node_inputs(job_dir, node_id, "prod")
        system_xml_file = system_xml_file or _inputs.get("system_xml_file")
        topology_pdb_file = topology_pdb_file or _inputs.get("topology_pdb_file")
        if not state_xml_file:
            state_xml_file = _inputs.get("restart_from") or _inputs.get("state_xml_file")
        restart_from_node_id = _inputs.get("restart_from_node_id")
        hmr, _implicit, timestep_fs = _resolve_topology_run_settings(
            hmr=hmr,
            implicit_solvent=None,
            topology_hmr=_inputs.get("topology_hmr"),
            topology_implicit_solvent=_inputs.get("topology_implicit_solvent"),
            timestep_fs=timestep_fs,
        )
        if _implicit:
            result["errors"].append("SST2 needs explicit solvent with PME; this topology is implicit-solvent.")
            result["code"] = "sst2_requires_pme"
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(job_dir, node_id, result)
        # A continued walker carries its rung / averages in the parent's sidecar.
        if restart_state_file is None and restart_from_node_id:
            from mdclaw._node import read_node

            parent = read_node(job_dir, restart_from_node_id)
            side = (parent.get("artifacts") or {}).get("tempering_state")
            if side:
                restart_state_file = str(Path(job_dir) / "nodes" / restart_from_node_id / side)
        if _inputs.get("input_resolution_error") or _inputs.get("input_resolution_errors"):
            result["errors"].append(_inputs.get("input_resolution_error") or "; ".join(_inputs.get("input_resolution_errors")))
            result["code"] = "input_resolution_blocked"
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(job_dir, node_id, result)
        _ctx = validate_node_execution_context(
            job_dir, node_id, "prod",
            actual_conditions={
                "sampling_method": SAMPLING_METHOD,
                "simulation_time_ns": simulation_time_ns,
                "temperature_kelvin": float(reference_temperature_kelvin)
                if reference_temperature_kelvin is not None
                else (float(temperatures_kelvin[0]) if temperatures_kelvin else None),
                "temperatures_kelvin": [float(t) for t in temperatures_kelvin]
                if temperatures_kelvin else None,
                "pressure_bar": pressure_bar,
                "ensemble": "NPT" if (pressure_bar is not None and pressure_bar > 0) else "NVT",
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "exchange_interval_ps": exchange_interval_ps,
                "solute_selection": solute_selection,
                "scale_nonbonded": scale_nonbonded,
                "move": move,
                "platform": platform,
                "device_index": device_index,
                "hmr": hmr,
                "random_seed": random_seed,
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}
        result["warnings"].extend(_ctx.get("warnings") or [])
    else:
        if hmr is None or timestep_fs is None:
            hmr_eff, _implicit, timestep_fs = _resolve_topology_run_settings(
                hmr=hmr, implicit_solvent=None, timestep_fs=timestep_fs,
            )
            hmr = hmr_eff

    for label, path in (("system_xml_file", system_xml_file), ("topology_pdb_file", topology_pdb_file)):
        if not path or not Path(path).is_file():
            result["errors"].append(f"{label} is missing: {path!r}")
            result["code"] = "file_not_found"
            if _node_mode:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(job_dir, node_id, result)
            return result

    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "sst2")
    result["output_dir"] = str(out_dir)
    prefix = name or "sst2"

    try:
        ladder, ref_temp = _validate_ladder(temperatures_kelvin, reference_temperature_kelvin)
        if move not in ("gibbs", "neighbor"):
            raise SST2ToolError(code="invalid_parameter_value", message="move must be 'gibbs' or 'neighbor'")
        solute, provenance = _resolve_solute_indices(
            topology_pdb_file,
            solute_selection=solute_selection,
            solute_indices_file=solute_indices_file,
        )
        solute_file = out_dir / "solute_indices.json"
        with open(solute_file, "w") as fh:
            json.dump(solute, fh)
        if restart_state_file and not Path(restart_state_file).is_file():
            raise SST2ToolError(
                code="sst2_restart_missing",
                message=f"restart_state_file {restart_state_file!r} does not exist.",
            )
        env, home = _sst2_environment(sst2_home)
        if device_index is not None and platform.lower() in ("cuda", "auto"):
            env["CUDA_VISIBLE_DEVICES"] = str(device_index)
        driver_platform = {"auto": "auto", "cuda": "CUDA", "opencl": "OpenCL",
                           "cpu": "CPU", "reference": "Reference"}.get(platform.lower(), platform)
        cmd = [
            sys.executable, "-m", DRIVER_MODULE,
            "--system", str(Path(system_xml_file).resolve()),
            "--topology", str(Path(topology_pdb_file).resolve()),
            "--solute-indices", str(solute_file.resolve()),
            "--temperatures", ",".join(f"{t:g}" for t in ladder),
            "--ref-temp", f"{ref_temp:g}",
            "--out-dir", str(out_dir.resolve()),
            "--name", prefix,
            "--time-ns", f"{simulation_time_ns:g}",
            "--dt-fs", f"{timestep_fs:g}",
            "--exchange-ps", f"{exchange_interval_ps:g}",
            "--dcd-ps", f"{output_frequency_ps:g}",
            "--platform", driver_platform,
            "--pressure-bar", f"{(pressure_bar or 0.0):g}",
            "--move", move,
            "-v",
        ]
        if state_xml_file and Path(state_xml_file).is_file():
            cmd += ["--state", str(Path(state_xml_file).resolve())]
        if random_seed is not None:
            cmd += ["--seed", str(int(random_seed))]
        if not scale_nonbonded:
            cmd.append("--only-dihed")
        if exclude_proline_omega:
            cmd.append("--exclude-pro-omega")
        if weights_file:
            cmd += ["--weights-json", str(Path(weights_file).resolve())]
        if restart_state_file:
            cmd += ["--restart-json", str(Path(restart_state_file).resolve())]

        log_path = out_dir / "sst2_driver.log"
        logger.info("Running SST2 driver: %s", " ".join(cmd))
        with open(log_path, "w") as log_fh:
            log_fh.write("# " + " ".join(cmd) + "\n")
            log_fh.flush()
            proc = subprocess.run(cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env, cwd=str(out_dir))
        if proc.returncode != 0:
            tail = ""
            try:
                tail = "".join(open(log_path).readlines()[-15:])
            except OSError:
                pass
            raise SST2ToolError(
                code="sst2_driver_failed",
                message=f"SST2 driver exited with {proc.returncode}; see {log_path}\n{tail}",
            )

        # Map the driver outputs onto the production artifact names.
        renames = {
            f"{prefix}_sst2.dcd": "trajectory.dcd",
            f"{prefix}_sst2.csv": "energy.dat",
            f"{prefix}_sst2_final.xml": "state.xml",
            f"{prefix}_sst2_full.csv": "tempering.csv",
            f"{prefix}_sst2_state.json": "tempering.json",
        }
        for src, dst in renames.items():
            s, d = out_dir / src, out_dir / dst
            if not s.is_file():
                raise SST2ToolError(code="sst2_driver_failed", message=f"driver did not write {src}")
            if d.exists():
                d.unlink()
            shutil.move(str(s), str(d))
        for leftover in out_dir.glob(f"{prefix}_sst2*.xml"):
            leftover.unlink()  # periodic checkpoint copies of the state
        with open(out_dir / "tempering.json") as fh:
            sidecar = json.load(fh)
        sidecar["artifact_paths"] = {k: str(out_dir / v) for k, v in {
            "trajectory": "trajectory.dcd", "energy": "energy.dat", "state": "state.xml",
            "tempering_report": "tempering.csv"}.items()}
        sidecar["solute"] = provenance
        sidecar["sst2_home"] = home
        with open(out_dir / "tempering.json", "w") as fh:
            json.dump(sidecar, fh, indent=2)

        # Final structure (PDB) for the analysis stage.
        from openmm import XmlSerializer
        from openmm.app import PDBFile
        with open(out_dir / "state.xml") as fh:
            final_state = XmlSerializer.deserialize(fh.read())
        topology = PDBFile(topology_pdb_file).topology
        with open(out_dir / "final_structure.pdb", "w") as fh:
            PDBFile.writeFile(topology, final_state.getPositions(), fh, keepIds=True)

        summary = _summarize_report(out_dir / "tempering.csv", ladder)
        steps = int(sidecar.get("step", 0))
        result.update({
            "success": True,
            "trajectory_file": str(out_dir / "trajectory.dcd"),
            "energy_file": str(out_dir / "energy.dat"),
            "state_file": str(out_dir / "state.xml"),
            "final_structure": str(out_dir / "final_structure.pdb"),
            "tempering_report_file": str(out_dir / "tempering.csv"),
            "tempering_state_file": str(out_dir / "tempering.json"),
            "solute_indices_file": str(solute_file),
            "driver_log": str(log_path),
            "steps_completed": steps,
            "tempering": {
                "temperatures_kelvin": ladder,
                "reference_temperature_kelvin": ref_temp,
                "lambdas": sidecar.get("lambdas"),
                "solute_atoms": sidecar.get("solute_atoms", len(solute)),
                "boundary_exceptions": sidecar.get("boundary_exceptions"),
                "torsion_buckets": sidecar.get("torsion_buckets"),
                "weights_kJ_per_mol": sidecar.get("weights_kJ_per_mol"),
                "weights_fixed": sidecar.get("weights_fixed"),
                "rung_visits": sidecar.get("e_num"),
                "final_rung": sidecar.get("rung"),
                **summary,
            },
        })
    except SST2ToolError as exc:
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["code"] = "unhandled_exception"

    if _node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            start_step = 0
            if restart_from_node_id:
                from mdclaw._node import read_ancestor_final_step
                try:
                    start_step = int(read_ancestor_final_step(job_dir, node_id) or 0)
                except Exception:  # noqa: BLE001
                    start_step = 0
            artifacts = {
                "trajectory": _node_artifact_path(result["trajectory_file"]),
                "energy": _node_artifact_path(result["energy_file"]),
                "state": _node_artifact_path(result["state_file"]),
                "final_structure": _node_artifact_path(result["final_structure"]),
                "tempering_report": _node_artifact_path(result["tempering_report_file"]),
                "tempering_state": _node_artifact_path(result["tempering_state_file"]),
                "solute_indices": _node_artifact_path(result["solute_indices_file"]),
                "driver_log": _node_artifact_path(result["driver_log"]),
            }
            metadata = {
                "sampling_method": SAMPLING_METHOD,
                "sampling_role": "tempering",
                "simulation_time_ns": simulation_time_ns,
                "temperature_kelvin": result["tempering"]["reference_temperature_kelvin"],
                "temperatures_kelvin": result["tempering"]["temperatures_kelvin"],
                "pressure_bar": pressure_bar,
                "platform": platform,
                "hmr": hmr,
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "exchange_interval_ps": exchange_interval_ps,
                "random_seed": random_seed,
                "num_steps": result["steps_completed"],
                "start_step": start_step,
                "final_step": start_step + result["steps_completed"],
                "tempering": result["tempering"],
            }
            complete_node(job_dir, node_id, artifacts=artifacts, metadata=metadata,
                          warnings=result.get("warnings") or None)
        else:
            fail_node(job_dir, node_id, errors=result["errors"], code=result.get("code"))
    return result
