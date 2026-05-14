"""MD surrogate source generation tools.

BioEmu is the first backend. It is intentionally executed from an isolated
venv so its JAX/Torch stack does not modify the main mdclaw environment.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mdclaw._common import create_unique_subdir, ensure_directory, generate_job_id, setup_logger

logger = setup_logger(__name__)

WORKING_DIR = Path(os.getenv("MDCLAW_OUTPUT_DIR", "outputs")).resolve()
ensure_directory(WORKING_DIR)

_AA_ALPHABET = set("ACDEFGHIKLMNPQRSTVWY")


def _default_surrogate_root() -> Path:
    override = os.getenv("MDCLAW_SURROGATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    container_root = Path("/opt/mdclaw/surrogates")
    if container_root.exists():
        return container_root
    return Path.home() / ".cache" / "mdclaw" / "surrogates"


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _model_root(model: str, prefix: str | None = None) -> Path:
    if prefix:
        return Path(prefix).expanduser().resolve()
    return (_default_surrogate_root() / model).resolve()


def _run_command(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    logger.debug("Running surrogate command: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass
class BioEmuBackend:
    name: str = "bioemu"

    def root(self, prefix: str | None = None) -> Path:
        return _model_root(self.name, prefix)

    def venv_dir(self, prefix: str | None = None) -> Path:
        return self.root(prefix) / "venv"

    def python(self, prefix: str | None = None) -> Path:
        return _venv_python(self.venv_dir(prefix))

    def install_package(self, device: str) -> str:
        if device == "cpu":
            return "bioemu"
        if device == "cuda":
            return "bioemu[cuda]"
        raise ValueError("device must be one of: cpu, cuda")

    def setup(self, *, device: str = "cpu", prefix: str | None = None, reinstall: bool = False) -> dict:
        root = self.root(prefix)
        venv_dir = self.venv_dir(prefix)
        python = self.python(prefix)
        package = self.install_package(device)
        commands: list[list[str]] = []

        if reinstall and venv_dir.exists():
            shutil.rmtree(venv_dir)
        root.mkdir(parents=True, exist_ok=True)

        uv = shutil.which("uv")
        if uv:
            if not python.exists():
                commands.append([uv, "venv", str(venv_dir)])
            commands.append([uv, "pip", "install", "--python", str(python), package])
        else:
            if not python.exists():
                commands.append([sys.executable, "-m", "venv", str(venv_dir)])
            commands.append([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            commands.append([str(python), "-m", "pip", "install", package])

        executed = []
        for cmd in commands:
            proc = _run_command(cmd)
            executed.append({"cmd": cmd, "returncode": proc.returncode})
            if proc.returncode != 0:
                return {
                    "success": False,
                    "model": self.name,
                    "venv": str(venv_dir),
                    "python": str(python),
                    "device": device,
                    "commands": executed,
                    "errors": [proc.stderr.strip() or proc.stdout.strip() or "backend setup failed"],
                    "warnings": [],
                }

        check = self.check(prefix=prefix)
        check["commands"] = executed
        check["device"] = device
        return check

    def check(self, *, prefix: str | None = None) -> dict:
        venv_dir = self.venv_dir(prefix)
        python = self.python(prefix)
        if not python.exists():
            return {
                "success": False,
                "model": self.name,
                "venv": str(venv_dir),
                "python": str(python),
                "installed": False,
                "errors": [
                    "BioEmu backend venv is not installed. Run: "
                    "mdclaw setup_surrogate_backend --model bioemu --device cuda"
                ],
                "warnings": [],
            }

        code = (
            "import json, pathlib\n"
            "import bioemu\n"
            "print(json.dumps({"
            "'version': getattr(bioemu, '__version__', 'unknown'), "
            "'cache_home': str(pathlib.Path.home() / '.cache')"
            "}))\n"
        )
        proc = _run_command([str(python), "-c", code])
        if proc.returncode != 0:
            return {
                "success": False,
                "model": self.name,
                "venv": str(venv_dir),
                "python": str(python),
                "installed": True,
                "errors": [proc.stderr.strip() or proc.stdout.strip() or "BioEmu import failed"],
                "warnings": [],
            }

        info: dict[str, Any] = {}
        try:
            info = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            info = {"version": "unknown"}
        return {
            "success": True,
            "model": self.name,
            "venv": str(venv_dir),
            "python": str(python),
            "installed": True,
            "version": info.get("version", "unknown"),
            "cache_home": info.get("cache_home"),
            "errors": [],
            "warnings": [],
        }

    def sample(
        self,
        *,
        sequence: str,
        num_samples: int,
        output_dir: Path,
        prefix: str | None = None,
        msa_path: str | None = None,
        model_name: str | None = None,
        filter_samples: bool = True,
        batch_size_100: int | None = None,
        denoiser_config: str | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        python = self.python(prefix)
        sample_input = str(Path(msa_path).expanduser().resolve()) if msa_path else sequence
        cmd = [
            str(python),
            "-m",
            "bioemu.sample",
            "--sequence",
            sample_input,
            "--num_samples",
            str(num_samples),
            "--output_dir",
            str(output_dir),
            f"--filter_samples={filter_samples}",
        ]
        if model_name:
            cmd.extend(["--model_name", model_name])
        if batch_size_100 is not None:
            cmd.extend(["--batch_size_100", str(batch_size_100)])
        if denoiser_config:
            cmd.extend(["--denoiser_config", denoiser_config])
        return _run_command(cmd, timeout=timeout)


SURROGATE_BACKENDS = {
    "bioemu": BioEmuBackend(),
}


def _get_backend(model: str):
    backend = SURROGATE_BACKENDS.get(model)
    if backend is None:
        raise ValueError(
            f"Unsupported surrogate model {model!r}. Available models: {sorted(SURROGATE_BACKENDS)}"
        )
    return backend


def _validate_bioemu_sequence(sequence: str) -> str | None:
    if not sequence:
        return "amino_acid_sequence is required"
    cleaned = sequence.strip().upper()
    if ":" in cleaned or "/" in cleaned or "," in cleaned:
        return "BioEmu backend supports monomer sequences only; use Boltz-2 for complexes"
    invalid = sorted(set(cleaned) - _AA_ALPHABET)
    if invalid:
        return f"sequence contains unsupported residue codes: {''.join(invalid)}"
    if len(cleaned) < 5:
        return "sequence is too short for BioEmu sampling (minimum length: 5)"
    return None


def _resolve_source_artifacts_dir(job_dir: str, node_id: str) -> Path:
    out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _validate_source_node(job_dir: str, node_id: str) -> str | None:
    from mdclaw._node import read_node

    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return (
            f"Node '{node_id}' does not exist under {job_dir}. "
            "Create it first with: "
            f"`mdclaw create_node --job-dir {job_dir} --node-type source`"
        )
    node = read_node(job_dir, node_id)
    node_type = node.get("node_type")
    if node_type != "source":
        return f"Node '{node_id}' has type '{node_type}', expected 'source'."
    return None


def _complete_surrogate_source_node(
    job_dir: str,
    node_id: str,
    *,
    source_id: str,
    candidate_paths: list[Path],
    metadata: dict[str, Any],
    candidate_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    from mdclaw._node import complete_node
    from mdclaw.source_bundle import build_source_bundle, write_source_bundle

    source_node_dir = (Path(job_dir) / "nodes" / node_id).resolve()
    bundle = build_source_bundle(
        source_type="surrogate",
        source_id=source_id,
        structure_paths=candidate_paths,
        source_node_dir=source_node_dir,
        metadata=metadata,
        candidate_metadata=candidate_metadata,
    )
    rel_bundle = write_source_bundle(source_node_dir, bundle)
    primary_candidate = bundle["structures"][0]["candidate_file"]
    complete_node(
        job_dir,
        node_id,
        artifacts={
            "structure_file": primary_candidate,
            "source_bundle": rel_bundle,
        },
        metadata=metadata,
    )
    return {
        "primary_candidate": primary_candidate,
        "source_bundle": rel_bundle,
        "metadata": metadata,
    }


def _repack_sidechains_with_faspr(
    candidate_paths: list[Path],
    backbone_archive_dir: Path,
) -> tuple[list[Path], list[str]]:
    """Repack side-chains on each candidate PDB in place via FASPR.

    The original backbone-only PDB is archived under ``backbone_archive_dir``
    so the raw BioEmu output remains available for provenance.
    """
    from py_FASPR import faspr

    backbone_archive_dir.mkdir(parents=True, exist_ok=True)
    repacked: list[Path] = []
    warnings: list[str] = []
    for path in candidate_paths:
        archived = backbone_archive_dir / path.name
        shutil.copy2(path, archived)
        try:
            faspr(input_pdb=str(path), output_pdb=str(path))
        except Exception as exc:
            warnings.append(
                f"FASPR repack failed for {path.name}: {type(exc).__name__}: {exc}; "
                "keeping backbone-only frame"
            )
            shutil.copy2(archived, path)
        repacked.append(path)
    return repacked, warnings


def _find_bioemu_outputs(output_dir: Path) -> tuple[Path | None, Path | None, list[Path]]:
    xtc_files = sorted(output_dir.rglob("*.xtc"))
    pdb_files = sorted(output_dir.rglob("*.pdb"))
    cif_files = sorted(output_dir.rglob("*.cif"))

    topology = None
    for path in pdb_files:
        if "topolog" in path.name.lower():
            topology = path
            break
    if topology is None and pdb_files:
        topology = pdb_files[0]

    trajectory = xtc_files[0] if xtc_files else None
    structures = [p for p in [*pdb_files, *cif_files] if p != topology]
    if not structures and topology and trajectory is None:
        structures = [topology]
    return topology, trajectory, structures


def setup_surrogate_backend(
    model: str,
    device: str = "cpu",
    prefix: str | None = None,
    reinstall: bool = False,
) -> dict:
    """Create or update an isolated venv for a surrogate backend."""
    try:
        backend = _get_backend(model)
        return backend.setup(device=device, prefix=prefix, reinstall=reinstall)
    except Exception as exc:
        return {
            "success": False,
            "model": model,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "warnings": [],
        }


def check_surrogate_backend(
    model: str,
    prefix: str | None = None,
) -> dict:
    """Check whether a surrogate backend is installed and importable."""
    try:
        backend = _get_backend(model)
        return backend.check(prefix=prefix)
    except Exception as exc:
        return {
            "success": False,
            "model": model,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "warnings": [],
        }


def generate_surrogate_candidates(
    amino_acid_sequence: str,
    model: str = "bioemu",
    num_samples: int = 100,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
    output_dir: str | None = None,
    job_dir: str | None = None,
    node_id: str | None = None,
    prefix: str | None = None,
    msa_path: str | None = None,
    model_name: str | None = None,
    filter_samples: bool = True,
    batch_size_100: int | None = None,
    denoiser_config: str | None = None,
    timeout: int | None = None,
    reconstruct_sidechains: bool = True,
) -> dict:
    """Generate source candidates from an MD surrogate backend."""
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "model": model,
        "output_dir": None,
        "topology_file": None,
        "trajectory_file": None,
        "candidate_files": [],
        "sidechain_method": "none",
        "source_bundle": None,
        "file_path": None,
        "errors": [],
        "warnings": [],
    }

    try:
        backend = _get_backend(model)
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result

    sequence = amino_acid_sequence.strip().upper()
    if model == "bioemu":
        seq_error = _validate_bioemu_sequence(sequence)
        if seq_error:
            result["errors"].append(seq_error)
            return result
    if num_samples <= 0:
        result["errors"].append("num_samples must be positive")
        return result

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node, fail_node

        node_error = _validate_source_node(job_dir, node_id)
        if node_error:
            result["errors"].append(node_error)
            return result
        base_dir = _resolve_source_artifacts_dir(job_dir, node_id)
    else:
        base_dir = Path(output_dir).expanduser().resolve() if output_dir else WORKING_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

    check = backend.check(prefix=prefix)
    if not check.get("success"):
        result["errors"].extend(check.get("errors", []))
        result["warnings"].extend(check.get("warnings", []))
        return result

    run_dir = create_unique_subdir(base_dir, f"{model}_surrogate")
    result["output_dir"] = str(run_dir)

    if _node_mode:
        begin_node(job_dir, node_id)

    proc = backend.sample(
        sequence=sequence,
        num_samples=num_samples,
        output_dir=run_dir,
        prefix=prefix,
        msa_path=msa_path,
        model_name=model_name,
        filter_samples=filter_samples,
        batch_size_100=batch_size_100,
        denoiser_config=denoiser_config,
        timeout=timeout,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "surrogate backend sampling failed"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    topology, trajectory, structure_files = _find_bioemu_outputs(run_dir)
    result["topology_file"] = str(topology) if topology else None
    result["trajectory_file"] = str(trajectory) if trajectory else None

    candidates_parent = base_dir if _node_mode else run_dir
    candidates_dir = candidates_parent / "candidates"
    backbone_archive_dir = candidates_parent / "candidates_backbone"

    try:
        if trajectory and topology:
            from mdclaw.source_bundle import candidate_paths_from_trajectory

            candidate_paths, frame_indices = candidate_paths_from_trajectory(
                topology,
                trajectory,
                candidates_dir,
                max_candidates=max_candidates,
                subsample_strategy=subsample_strategy,
            )
        else:
            candidate_paths = structure_files
            if max_candidates and max_candidates > 0:
                candidate_paths = candidate_paths[:max_candidates]
            frame_indices = list(range(len(candidate_paths)))
        if not candidate_paths:
            result["errors"].append("surrogate backend produced no candidate structures")
            if _node_mode:
                fail_node(job_dir, node_id, errors=result["errors"])
            return result
    except Exception as exc:
        result["errors"].append(f"Failed to normalize surrogate candidates: {type(exc).__name__}: {exc}")
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    sidechain_method = "none"
    if reconstruct_sidechains and candidate_paths:
        try:
            repacked_paths, repack_warnings = _repack_sidechains_with_faspr(
                candidate_paths, backbone_archive_dir
            )
            result["warnings"].extend(repack_warnings)
            candidate_paths = repacked_paths
            sidechain_method = "faspr"
        except ImportError as exc:
            result["warnings"].append(
                f"Side-chain reconstruction skipped: py_FASPR is not installed ({exc}). "
                "Candidates remain backbone-only."
            )
        except Exception as exc:
            result["warnings"].append(
                f"Side-chain reconstruction failed: {type(exc).__name__}: {exc}. "
                "Candidates remain backbone-only."
            )

    candidate_tag = "faspr_repacked" if sidechain_method == "faspr" else "backbone_only"
    result["candidate_files"] = [str(p) for p in candidate_paths]
    result["sidechain_method"] = sidechain_method

    if _node_mode:
        try:
            digest = hashlib.sha256(
                f"{model}:{sequence}:{datetime.datetime.now(datetime.UTC).isoformat()}".encode()
            ).hexdigest()[:12]
            metadata = {
                "source_type": "surrogate",
                "surrogate_model": model,
                "sequence": sequence,
                "num_samples_requested": num_samples,
                "num_candidates": len(candidate_paths),
                "subsample_strategy": subsample_strategy,
                "backend_output_dir": str(run_dir),
                "topology_file": str(topology) if topology else None,
                "trajectory_file": str(trajectory) if trajectory else None,
                "filter_samples": filter_samples,
                "sidechain_method": sidechain_method,
            }
            if msa_path:
                metadata["msa_path"] = str(Path(msa_path).expanduser().resolve())
            if model_name:
                metadata["bioemu_model_name"] = model_name

            candidate_metadata = []
            for idx, (path, frame_idx) in enumerate(zip(candidate_paths, frame_indices), start=1):
                candidate_metadata.append({
                    "label": f"{model} candidate {idx}",
                    "origin": {
                        "kind": model,
                        "surrogate_model": model,
                        "surrogate_version": check.get("version"),
                        "bioemu_frame_index": frame_idx,
                        "bioemu_output_file": str(path),
                        "bioemu_num_samples_requested": num_samples,
                        "bioemu_filter_samples": filter_samples,
                    },
                    "metrics": {},
                    "tags": [candidate_tag],
                })

            completed = _complete_surrogate_source_node(
                job_dir,
                node_id,
                source_id=f"{model}_{digest}",
                candidate_paths=candidate_paths,
                metadata=metadata,
                candidate_metadata=candidate_metadata,
            )
            result["source_bundle"] = completed.get("source_bundle")
            result["file_path"] = str(candidate_paths[0])
        except Exception as exc:
            msg = f"Failed to attach surrogate candidates to source node: {type(exc).__name__}: {exc}"
            result["errors"].append(msg)
            fail_node(job_dir, node_id, errors=[msg])
            return result

    result["success"] = True
    return result


TOOLS = {
    "setup_surrogate_backend": setup_surrogate_backend,
    "check_surrogate_backend": check_surrogate_backend,
    "generate_surrogate_candidates": generate_surrogate_candidates,
}
