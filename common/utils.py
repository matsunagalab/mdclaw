"""
Common utility functions for MCP-MD.
"""

import os
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


def setup_logger(name: str, level: int | None = None) -> logging.Logger:
    """Setup logger with environment-based configuration.

    Log level is determined by:
    1. Explicit level parameter (if provided)
    2. MDZEN_LOG_LEVEL environment variable
    3. Default: INFO for server loggers, WARNING for others

    Args:
        name: Logger name
        level: Log level (optional)

    Returns:
        Configured logger
    """
    # Quiet noisy third-party loggers first (before any logging happens)
    _quiet_noisy_loggers()

    # Determine log level
    if level is None:
        env_level = os.getenv("MDZEN_LOG_LEVEL", "").upper()
        if env_level:
            level = getattr(logging, env_level, logging.INFO)
        else:
            # Default: INFO for our servers, WARNING for others
            if name.startswith(("servers.", "__main__", "structure_server",
                               "amber_server", "solvation_server",
                               "genesis_server", "md_simulation_server")):
                level = logging.INFO
            else:
                level = logging.WARNING

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Prevent duplicate logs by not propagating to root logger
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        # Simplified format for cleaner output
        formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


_noisy_loggers_quieted = False


def _quiet_noisy_loggers():
    """Reduce noise from third-party libraries.

    Suppresses internal MCP server logs and HTTP client logs
    while keeping our server logs visible.
    """
    global _noisy_loggers_quieted
    if _noisy_loggers_quieted:
        return
    _noisy_loggers_quieted = True

    # Quiet MCP server internal logs (the "Processing request" messages)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("mcp.server").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.lowlevel").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.stdio").setLevel(logging.WARNING)

    # Quiet HTTP client logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Quiet other noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


def ensure_directory(path: Union[str, Path]) -> Path:
    """Ensure directory exists, create if not
    
    Args:
        path: Directory path
    
    Returns:
        Path object
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_command(
    cmd: list[str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True
) -> subprocess.CompletedProcess:
    """Run external command
    
    Args:
        cmd: Command and arguments list
        cwd: Working directory
        timeout: Timeout in seconds
        capture_output: Capture output
    
    Returns:
        CompletedProcess object
    
    Raises:
        subprocess.CalledProcessError: Command failed
        subprocess.TimeoutExpired: Timeout
    """
    logger.debug(f"Running command: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            check=True
        )
        logger.debug("Command completed successfully")
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {e.stderr}")
        raise
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s")
        raise


def generate_job_id(length: int = 8, prefix: str = "") -> str:
    """Generate unique job identifier using UUID.

    More collision-resistant than timestamp-based IDs.
    Suitable for parallel job submissions.

    Args:
        length: Length of ID (default: 8 characters)
        prefix: Optional prefix (e.g., "job_")

    Returns:
        Unique job ID string (UUID-based)

    Example:
        >>> generate_job_id()
        'a1b2c3d4'
        >>> generate_job_id(8, "job_")
        'job_a1b2c3d4'
    """
    return f"{prefix}{uuid.uuid4().hex[:length]}"


def count_atoms_in_pdb(pdb_path: Union[str, Path]) -> int:
    """Count atoms in PDB file
    
    Args:
        pdb_path: PDB file path
    
    Returns:
        Number of atoms
    """
    count = 0
    with open(pdb_path, 'r') as f:
        for line in f:
            if line.startswith(('ATOM', 'HETATM')):
                count += 1
    return count


def check_external_tool(tool_name: str) -> bool:
    """Check if external tool is available
    
    Args:
        tool_name: Tool name (command name)
    
    Returns:
        True if tool is available in PATH
    """
    try:
        result = subprocess.run(
            ['which', tool_name],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except Exception:
        return False


# Session directory sharing for MCP servers
# This allows MCP tools to know the current session directory without
# relying on the LLM to pass output_dir correctly
SESSION_FILE = ".mdzen_session"


def set_current_session(session_dir: Union[str, Path]) -> None:
    """Set the current session directory for MCP servers to use.

    Creates a .mdzen_session file in the current working directory
    that MCP tools can read to determine the default output directory.

    Args:
        session_dir: Absolute path to the session directory
    """
    session_path = Path(session_dir).resolve()
    session_file = Path.cwd() / SESSION_FILE
    session_file.write_text(str(session_path))
    logger.debug(f"Set current session: {session_path}")


def get_current_session() -> Optional[Path]:
    """Get the current session directory from .mdzen_session file.

    MCP servers call this to get the default output directory when
    output_dir is not explicitly provided.

    Returns:
        Path to session directory, or None if not set or file doesn't exist
    """
    session_file = Path.cwd() / SESSION_FILE
    if session_file.exists():
        session_dir = session_file.read_text().strip()
        if session_dir and Path(session_dir).exists():
            return Path(session_dir)
    return None


def clear_current_session() -> None:
    """Clear the current session file."""
    session_file = Path.cwd() / SESSION_FILE
    if session_file.exists():
        session_file.unlink()
        logger.debug("Cleared current session file")


def create_unique_subdir(base_dir: Union[str, Path], name: str) -> Path:
    """Create a uniquely-named subdirectory within base_dir.

    Creates a subdirectory with a human-readable name. If the name already
    exists, appends a numeric suffix (_2, _3, etc.) to ensure uniqueness.

    This is designed for session-based workflows where each MCP tool creates
    a predictably-named subdirectory:
        session_abc123/
        ├── prepare_complex/    # structure_server
        ├── solvate/            # solvation_server
        ├── amber/              # amber_server
        ├── md_simulation/      # md_simulation_server
        └── boltz/              # genesis_server

    Args:
        base_dir: Parent directory (e.g., session directory)
        name: Desired subdirectory name (e.g., "solvate", "amber")

    Returns:
        Path to the created subdirectory (absolute path)

    Example:
        >>> # First call creates "solvate"
        >>> create_unique_subdir("/output/session_abc", "solvate")
        PosixPath('/output/session_abc/solvate')

        >>> # Second call creates "solvate_2"
        >>> create_unique_subdir("/output/session_abc", "solvate")
        PosixPath('/output/session_abc/solvate_2')
    """
    base_path = Path(base_dir).resolve()
    ensure_directory(base_path)

    # Try the base name first
    target_dir = base_path / name
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    # Name exists, find next available suffix
    suffix = 2
    while True:
        target_dir = base_path / f"{name}_{suffix}"
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir
        suffix += 1
        # Safety limit to prevent infinite loop
        if suffix > 1000:
            # Fall back to UUID-based name
            fallback_name = f"{name}_{generate_job_id(4)}"
            target_dir = base_path / fallback_name
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir

