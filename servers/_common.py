"""Shared utilities for MCP servers.

Provides: logging, directory management, external tool wrappers, error helpers.
"""

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


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
        "mcp", "mcp.server", "mcp.server.lowlevel", "mcp.server.stdio",
        "httpx", "httpcore", "urllib3", "asyncio", "openai", "anthropic",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logger(name: str, level: int | None = None) -> logging.Logger:
    """Setup logger with environment-based configuration."""
    _quiet_noisy_loggers()

    if level is None:
        env_level = os.getenv("MDZEN_LOG_LEVEL", "").upper()
        if env_level:
            level = getattr(logging, env_level, logging.INFO)
        elif name.startswith((
            "servers.", "__main__", "structure_server", "amber_server",
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
# Session / brief (read-only helpers for MCP servers)
# ---------------------------------------------------------------------------


def get_current_session() -> Optional[Path]:
    """Get current session directory from .mdzen_session file."""
    session_file = Path.cwd() / ".mdzen_session"
    if session_file.exists():
        session_dir = session_file.read_text().strip()
        if session_dir and Path(session_dir).exists():
            return Path(session_dir)
    return None


def get_simulation_brief() -> Optional[dict]:
    """Get simulation brief from current session directory."""
    session_dir = get_current_session()
    if session_dir:
        brief_path = session_dir / "simulation_brief.json"
        if brief_path.exists():
            try:
                return json.loads(brief_path.read_text())
            except Exception:
                pass
    return None


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


def run_command(
    cmd: list[str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    """Run external command with error handling."""
    logger.debug(f"Running command: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=capture_output,
            text=True, timeout=timeout, check=True,
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
            except subprocess.CalledProcessError:
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
        return run_command(cmd, cwd=cwd, timeout=timeout)


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
    "amber": 900,
    "md_simulation": 3600,
}


def get_timeout(timeout_type: str) -> int:
    """Get timeout value. Override via MDZEN_<TYPE>_TIMEOUT env var."""
    env_key = f"MDZEN_{timeout_type.upper()}_TIMEOUT"
    env_val = os.getenv(env_key)
    if env_val is not None:
        return int(env_val)
    return _TIMEOUT_DEFAULTS.get(timeout_type, _TIMEOUT_DEFAULTS["default"])


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def create_validation_error(
    field: str, message: str,
    expected: Optional[str] = None, actual: Optional[str] = None,
) -> dict:
    """Standardized validation error dict."""
    hints = [f"Check the '{field}' parameter"]
    if expected:
        hints.append(f"Expected: {expected}")
    if actual:
        hints.append(f"Received: {actual}")
    return {
        "success": False, "error_type": "ValidationError",
        "message": f"Validation failed for '{field}': {message}",
        "hints": hints,
        "context": {"field": field, "expected": expected, "actual": actual},
        "recoverable": True, "errors": [f"{field}: {message}"], "warnings": [],
    }


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
