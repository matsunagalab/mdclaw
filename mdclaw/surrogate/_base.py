"""Isolated model-backend management plus MD surrogate source generation.

Heavy AI model backends (BioEmu, Boltz-2) ship their own Torch/CUDA stacks
that conflict with the main mdclaw environment's OpenMM ``cu118`` pin, so each
one runs from its own isolated venv. ``setup_model_backend`` /
``check_model_backend`` create and inspect those venvs; ``setup_surrogate_backend``
/ ``check_surrogate_backend`` remain as ``bioemu``-oriented aliases for
backward compatibility.

BioEmu additionally supports conformational sampling via
``generate_surrogate_candidates``; Boltz-2 prediction is driven from
``mdclaw.genesis_server`` but resolves its venv through the backend registry
here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mdclaw._common import ensure_directory, setup_logger

logger = setup_logger(__name__)

WORKING_DIR = Path(os.getenv("MDCLAW_OUTPUT_DIR", "outputs")).resolve()
ensure_directory(WORKING_DIR)

_AA_ALPHABET = set("ACDEFGHIKLMNPQRSTVWY")

# Pinned Boltz release for the isolated backend venv (reproducibility). Bump
# deliberately; do not float to "latest".
BOLTZ_VERSION = "2.2.1"


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
class VenvBackend:
    """Common isolated-venv lifecycle shared by heavy model backends.

    Subclasses provide ``install_spec`` (pip requirement strings) and
    ``import_check_code`` (a Python snippet that prints a JSON line with at
    least a ``version`` field). ``entry_point`` resolves a console script inside
    the venv (used by callers that shell out to a backend CLI).

    Capabilities are declared as class attributes so callers dispatch on what a
    backend *can do* rather than on its name. This is what makes models
    swappable: a new predictor only needs ``supports_prediction = True`` (plus
    ``entry_script``) to be usable everywhere the boltz predictor is, and a new
    sampler only needs ``supports_sampling = True``. See
    ``docs/developer/model-backends.md``.
    """

    name: str = ""

    # --- capabilities (class-level, not dataclass fields) ---
    # "sampling":   conformational ensemble generation (generate_surrogate_candidates)
    # "prediction": structure prediction from sequence (e.g. boltz2_protein_from_seq)
    supports_sampling = False
    supports_prediction = False
    # Console script inside the venv used by prediction callers (e.g. "boltz").
    entry_script = None

    def capabilities(self) -> list[str]:
        caps: list[str] = []
        if self.supports_sampling:
            caps.append("sampling")
        if self.supports_prediction:
            caps.append("prediction")
        return caps

    def root(self, prefix: str | None = None) -> Path:
        return _model_root(self.name, prefix)

    def venv_dir(self, prefix: str | None = None) -> Path:
        return self.root(prefix) / "venv"

    def python(self, prefix: str | None = None) -> Path:
        return _venv_python(self.venv_dir(prefix))

    def entry_point(self, script: str, prefix: str | None = None) -> Path:
        bin_dir = self.venv_dir(prefix) / ("Scripts" if os.name == "nt" else "bin")
        name = f"{script}.exe" if os.name == "nt" else script
        return bin_dir / name

    def setup_hint(self, device: str = "cuda") -> str:
        return f"mdclaw setup_model_backend --model {self.name} --device {device}"

    # --- backend-specific hooks ---
    def install_spec(self, device: str) -> list[str]:
        raise NotImplementedError

    def import_check_code(self) -> str:
        raise NotImplementedError

    # --- generic lifecycle ---
    def setup(self, *, device: str = "cpu", prefix: str | None = None, reinstall: bool = False) -> dict:
        root = self.root(prefix)
        venv_dir = self.venv_dir(prefix)
        python = self.python(prefix)
        packages = self.install_spec(device)
        commands: list[list[str]] = []

        if reinstall and venv_dir.exists():
            shutil.rmtree(venv_dir)
        root.mkdir(parents=True, exist_ok=True)

        uv = shutil.which("uv")
        if uv:
            if not python.exists():
                commands.append([uv, "venv", str(venv_dir)])
            commands.append([uv, "pip", "install", "--python", str(python), *packages])
        else:
            if not python.exists():
                commands.append([sys.executable, "-m", "venv", str(venv_dir)])
            commands.append([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            commands.append([str(python), "-m", "pip", "install", *packages])

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
                    f"{self.name} backend venv is not installed. Run: {self.setup_hint()}"
                ],
                "warnings": [],
            }

        proc = _run_command([str(python), "-c", self.import_check_code()])
        if proc.returncode != 0:
            return {
                "success": False,
                "model": self.name,
                "venv": str(venv_dir),
                "python": str(python),
                "installed": True,
                "errors": [proc.stderr.strip() or proc.stdout.strip() or f"{self.name} import failed"],
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


@dataclass
class BioEmuBackend(VenvBackend):
    name: str = "bioemu"
    supports_sampling = True

    def install_spec(self, device: str) -> list[str]:
        if device == "cpu":
            return ["bioemu"]
        if device == "cuda":
            return ["bioemu[cuda]"]
        raise ValueError("device must be one of: cpu, cuda")

    def import_check_code(self) -> str:
        return (
            "import json, pathlib\n"
            "import bioemu\n"
            "print(json.dumps({"
            "'version': getattr(bioemu, '__version__', 'unknown'), "
            "'cache_home': str(pathlib.Path.home() / '.cache')"
            "}))\n"
        )

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


@dataclass
class BoltzBackend(VenvBackend):
    """Boltz-2 structure predictor in an isolated venv.

    Boltz manages its own Torch build, so ``device`` is advisory: the pinned
    ``boltz`` wheel pulls a CUDA-capable Torch on Linux by default. Prediction
    itself is invoked from :mod:`mdclaw.genesis_server` via the venv ``boltz``
    console script (see :func:`entry_point`).
    """

    name: str = "boltz"
    supports_prediction = True
    entry_script = "boltz"

    def install_spec(self, device: str) -> list[str]:
        if device not in ("cpu", "cuda"):
            raise ValueError("device must be one of: cpu, cuda")
        return [f"boltz=={BOLTZ_VERSION}"]

    def import_check_code(self) -> str:
        return (
            "import json\n"
            "from importlib import metadata\n"
            "try:\n"
            "    version = metadata.version('boltz')\n"
            "except metadata.PackageNotFoundError:\n"
            "    import boltz\n"
            "    version = getattr(boltz, '__version__', 'unknown')\n"
            "print(json.dumps({'version': version}))\n"
        )


# Registry of isolated model backends. ``MODEL_BACKENDS`` is the source of
# truth; ``SURROGATE_BACKENDS`` is a backward-compatible alias for callers and
# tests that predate the generic naming.
MODEL_BACKENDS = {
    "bioemu": BioEmuBackend(),
    "boltz": BoltzBackend(),
}
SURROGATE_BACKENDS = MODEL_BACKENDS




def _get_backend(model: str):
    backend = MODEL_BACKENDS.get(model)
    if backend is None:
        raise ValueError(
            f"Unsupported model backend {model!r}. Available models: {sorted(MODEL_BACKENDS)}"
        )
    return backend


def models_with_capability(capability: str) -> list[str]:
    """Names of registered backends that declare ``capability``.

    ``capability`` is one of ``"sampling"`` or ``"prediction"``. Callers should
    dispatch on capability, not on backend name, so models stay swappable.
    """
    return sorted(
        name for name, backend in MODEL_BACKENDS.items()
        if capability in backend.capabilities()
    )


def _get_capable_backend(model: str, capability: str, action: str):
    backend = _get_backend(model)
    if capability not in backend.capabilities():
        raise ValueError(
            f"Model backend {model!r} does not support {action}. "
            f"{capability.capitalize()} backends: {models_with_capability(capability)}"
        )
    return backend


def _get_sampling_backend(model: str):
    return _get_capable_backend(model, "sampling", "surrogate sampling")




def resolve_prediction_backend(model: str = "boltz", prefix: str | None = None):
    """Resolve an installed structure-prediction backend for a caller.

    Returns ``(entry_point_path, check)`` where ``entry_point_path`` is the
    venv console script to invoke, or ``None`` when the backend is missing,
    not importable, or lacks a declared ``entry_script``. This is the
    capability-based entry used by ``genesis_server`` so a predictor can be
    swapped (boltz -> alphafold3 -> ...) without touching the caller.
    """
    backend = _get_capable_backend(model, "prediction", "structure prediction")
    check = backend.check(prefix=prefix)
    if not check.get("success"):
        return None, check
    if not backend.entry_script:
        check["success"] = False
        check.setdefault("errors", []).append(
            f"Backend {model!r} declares no entry_script for prediction callers."
        )
        return None, check
    return str(backend.entry_point(backend.entry_script, prefix=prefix)), check
