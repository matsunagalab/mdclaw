"""Shared utilities for tool modules.

Provides: logging, directory management, external tool wrappers, error helpers.
"""

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)


CANONICAL_WATER_MODELS = {
    "tip3p": "tip3p",
    "opc": "opc",
    "opc3": "opc3",
    "tip4pew": "tip4pew",
    "spce": "spce",
    "spc/e": "spce",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_noisy_loggers_quieted = False


def _quiet_noisy_loggers():
    """Suppress noisy third-party loggers."""
    global _noisy_loggers_quieted
    if _noisy_loggers_quieted:
        return
    _noisy_loggers_quieted = True

    for name in (
        "httpx", "httpcore", "urllib3", "asyncio", "openai", "anthropic",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logger(name: str, level: int | None = None) -> logging.Logger:
    """Setup logger with environment-based configuration."""
    _quiet_noisy_loggers()

    if level is None:
        env_level = os.getenv("MDCLAW_LOG_LEVEL", "").upper()
        if env_level:
            level = getattr(logging, env_level, logging.INFO)
        elif name.startswith((
            "mdclaw.", "__main__", "structure_server", "amber_server",
            "solvation_server", "genesis_server", "md_simulation_server",
        )):
            level = logging.INFO
        else:
            level = logging.WARNING

    log = logging.getLogger(name)
    log.setLevel(level)
    log.propagate = False

    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s - %(levelname)s - %(message)s"))
        log.addHandler(handler)

    return log


# ---------------------------------------------------------------------------
# File / directory utilities
# ---------------------------------------------------------------------------


def ensure_directory(path: Union[str, Path]) -> Path:
    """Ensure directory exists, create if not."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_unique_subdir(base_dir: Union[str, Path], name: str) -> Path:
    """Create a uniquely-named subdirectory (appends _2, _3, ... if exists)."""
    base_path = Path(base_dir).resolve()
    ensure_directory(base_path)

    target_dir = base_path / name
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    suffix = 2
    while suffix <= 1000:
        target_dir = base_path / f"{name}_{suffix}"
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir
        suffix += 1

    fallback_name = f"{name}_{generate_job_id(4)}"
    target_dir = base_path / fallback_name
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def generate_job_id(length: int = 8, prefix: str = "") -> str:
    """Generate unique job identifier using UUID."""
    return f"{prefix}{uuid.uuid4().hex[:length]}"


def count_atoms_in_pdb(pdb_path: Union[str, Path]) -> int:
    """Count ATOM/HETATM lines in a PDB file."""
    count = 0
    with open(pdb_path) as f:
        for line in f:
            if line.startswith(("ATOM", "HETATM")):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Session / brief (read-only helpers for tool modules)
# ---------------------------------------------------------------------------



# get_current_session() and get_simulation_brief() removed.
# All tools now use --output-dir parameter directly. No session state.


# ---------------------------------------------------------------------------
# External tool execution
# ---------------------------------------------------------------------------


def check_external_tool(tool_name: str) -> bool:
    """Check if external tool is available in PATH."""
    try:
        result = subprocess.run(["which", tool_name], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def get_module_loads() -> list[str]:
    """Get module names from MDCLAW_MODULE_LOADS environment variable."""
    s = os.getenv("MDCLAW_MODULE_LOADS", "").strip()
    return s.split() if s else []


def is_containerized() -> bool:
    """Detect if running inside a Singularity or Docker container."""
    return Path("/.singularity.d").exists() or Path("/.dockerenv").exists()


def run_command(
    cmd: list[str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    env: Optional[dict] = None,
    use_modules: bool = False,
) -> subprocess.CompletedProcess:
    """Run external command with error handling.

    Args:
        cmd: Command and arguments.
        cwd: Working directory.
        timeout: Timeout in seconds.
        capture_output: Capture stdout/stderr.
        env: Extra environment variables merged into os.environ.
        use_modules: If True, prepend ``module load`` commands from
            MDCLAW_MODULE_LOADS before running *cmd* (requires shell=True).
    """
    run_env = None
    if env:
        run_env = {**os.environ, **env}

    if use_modules:
        modules = get_module_loads()
        if modules:
            module_init = os.getenv("MDCLAW_MODULE_INIT", "/etc/profile.d/modules.sh")
            load_cmds = " && ".join(f"module load {m}" for m in modules)
            shell_cmd = f"source {module_init} && {load_cmds} && {' '.join(cmd)}"
            logger.debug(f"Running with modules: {shell_cmd}")
            try:
                result = subprocess.run(
                    shell_cmd, shell=True, cwd=cwd,
                    capture_output=capture_output, text=True,
                    timeout=timeout, check=True,
                    env=run_env or None,
                )
                return result
            except subprocess.CalledProcessError as e:
                logger.error(f"Command failed: {e.stderr}")
                raise
            except subprocess.TimeoutExpired:
                logger.error(f"Command timed out after {timeout}s")
                raise

    logger.debug(f"Running command: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=capture_output,
            text=True, timeout=timeout, check=True,
            env=run_env or None,
        )
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {e.stderr}")
        raise
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s")
        raise


class BaseToolWrapper:
    """Wrapper for external CLI tools (tleap, antechamber, etc.)."""

    def __init__(self, tool_name: str, conda_env: Optional[str] = None):
        self.tool_name = tool_name
        self.conda_env = conda_env
        self.executable = self._find_executable()
        if not self.executable:
            logger.warning(f"{tool_name} not found in PATH")

    def _find_executable(self) -> Optional[str]:
        if check_external_tool(self.tool_name):
            return self.tool_name
        if self.conda_env:
            try:
                result = subprocess.run(
                    ["conda", "run", "-n", self.conda_env, "which", self.tool_name],
                    capture_output=True, text=True, check=True,
                )
                exe_path = result.stdout.strip()
                if exe_path:
                    return exe_path
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
        return None

    def is_available(self) -> bool:
        return self.executable is not None

    def run(
        self, args: list[str],
        cwd: Optional[Union[str, Path]] = None,
        timeout: Optional[int] = None,
        env_vars: Optional[dict] = None,
    ) -> subprocess.CompletedProcess:
        if not self.is_available():
            raise RuntimeError(f"{self.tool_name} is not available")
        if self.conda_env:
            cmd = ["conda", "run", "-n", self.conda_env, self.executable] + args
        else:
            cmd = [self.executable] + args
        logger.debug(f"Running: {' '.join(cmd)}")
        return run_command(cmd, cwd=cwd, timeout=timeout, env=env_vars)


# ---------------------------------------------------------------------------
# Timeout configuration
# ---------------------------------------------------------------------------

_TIMEOUT_DEFAULTS = {
    "default": 300,
    "research": 300,
    "structure": 600,
    "genesis": 300,
    "solvation": 7200,
    "membrane": 7200,
    # ``amber`` is the tleap wall-time budget for build_amber_system.
    # 3600 s (60 min) covers solvated nanobody–scaffold fusions
    # (megabody-style, ~450 residues / 400k atoms) observed in the
    # 2422-row SabDab batch where the earlier 900 s default timed out
    # on 48 entries. For even larger systems, override via
    # MDCLAW_AMBER_TIMEOUT=<seconds>.
    "amber": 3600,
    "md_simulation": 3600,
    "slurm": 120,
}


def get_timeout(timeout_type: str) -> int:
    """Get timeout value. Override via MDCLAW_<TYPE>_TIMEOUT env var."""
    env_key = f"MDCLAW_{timeout_type.upper()}_TIMEOUT"
    env_val = os.getenv(env_key)
    if env_val is not None:
        return int(env_val)
    return _TIMEOUT_DEFAULTS.get(timeout_type, _TIMEOUT_DEFAULTS["default"])


# ---------------------------------------------------------------------------
# Guardrail helpers
# ---------------------------------------------------------------------------


def normalize_choice(value: Optional[str], aliases: dict[str, str]) -> Optional[str]:
    """Normalize a user-provided string through a case-insensitive alias map."""
    if value is None:
        return None
    return aliases.get(str(value).strip().lower())


def create_guardrail_result(
    field: str,
    message: str,
    severity: str = "error",
    *,
    actual: Optional[str] = None,
    expected: Optional[str] = None,
    suggested_fix: Optional[str] = None,
    code: Optional[str] = None,
) -> dict[str, Any]:
    """Create a normalized rule-evaluation result for validation guardrails."""
    return {
        "field": field,
        "message": message,
        "severity": severity,
        "actual": actual,
        "expected": expected,
        "suggested_fix": suggested_fix,
        "code": code,
    }


def split_guardrail_results(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split guardrail results into blocking errors and non-blocking warnings."""
    blocking = [result for result in results if result.get("severity") == "error"]
    warnings = [result for result in results if result.get("severity") == "warning"]
    return blocking, warnings


def guardrail_messages(results: list[dict[str, Any]]) -> list[str]:
    """Extract human-readable messages from guardrail results."""
    return [result["message"] for result in results if result.get("message")]


def _dedupe_strings(items: list[str]) -> list[str]:
    seen = set()
    unique_items = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            unique_items.append(item)
    return unique_items


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def create_validation_error(
    field: str, message: str,
    expected: Optional[str] = None, actual: Optional[str] = None,
    hints: Optional[list[str]] = None,
    context_extra: Optional[dict[str, Any]] = None,
    warnings: Optional[list[str]] = None,
) -> dict:
    """Standardized validation error dict."""
    error_hints = [f"Check the '{field}' parameter"]
    if expected:
        error_hints.append(f"Expected: {expected}")
    if actual:
        error_hints.append(f"Received: {actual}")
    if hints:
        error_hints.extend(hints)
    context = {"field": field, "expected": expected, "actual": actual}
    if context_extra:
        context.update(context_extra)
    return {
        "success": False, "error_type": "ValidationError",
        "message": f"Validation failed for '{field}': {message}",
        "hints": _dedupe_strings(error_hints),
        "context": context,
        "recoverable": True,
        "errors": [f"{field}: {message}"],
        "warnings": warnings or [],
    }


def create_validation_error_from_guardrails(
    field: str,
    results: list[dict[str, Any]],
    *,
    summary: Optional[str] = None,
    expected: Optional[str] = None,
    actual: Optional[str] = None,
) -> dict:
    """Convert structured guardrail results into a standardized validation error."""
    blocking, warnings = split_guardrail_results(results)
    if not blocking:
        raise ValueError("create_validation_error_from_guardrails requires at least one blocking result")

    error_messages = guardrail_messages(blocking)
    hint_items = []
    for result in results:
        suggested_fix = result.get("suggested_fix")
        if suggested_fix:
            hint_items.append(suggested_fix)
        result_expected = result.get("expected")
        if result_expected:
            hint_items.append(f"Expected: {result_expected}")
        if result.get("severity") == "error" and result.get("actual"):
            hint_items.append(f"Received: {result['actual']}")

    return create_validation_error(
        field,
        summary or "; ".join(error_messages),
        expected=expected,
        actual=actual,
        hints=_dedupe_strings(hint_items),
        context_extra={"guardrail_results": results},
        warnings=guardrail_messages(warnings),
    )


def create_file_not_found_error(file_path: str, file_type: str = "file") -> dict:
    """Standardized file-not-found error dict."""
    error_msg = f"{file_type} not found: {file_path}"
    return {
        "success": False, "error_type": "FileNotFoundError",
        "message": error_msg,
        "hints": [f"Verify the {file_type} path is correct", "Check that the file exists"],
        "context": {"file_path": file_path, "file_type": file_type},
        "recoverable": True, "errors": [error_msg], "warnings": [],
    }


def create_tool_not_available_error(
    tool_name: str, install_hint: Optional[str] = None,
) -> dict:
    """Standardized tool-not-available error dict."""
    hints = [f"Tool '{tool_name}' is not available in PATH"]
    hints.append(install_hint or "Ensure AmberTools is installed and conda environment is activated")
    return {
        "success": False, "error_type": "ToolNotAvailableError",
        "message": f"Required tool '{tool_name}' not found",
        "hints": hints, "context": {"tool_name": tool_name},
        "recoverable": False, "errors": [f"Tool not found: {tool_name}"], "warnings": [],
    }
