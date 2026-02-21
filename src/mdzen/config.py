"""Configuration settings for MDZen.

Centralizes timeout and server path configuration.

Usage:
    from mdzen.config import settings, get_timeout, get_server_path
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All settings use the MDZEN_ prefix. For example:
        MDZEN_OUTPUT_DIR=/path/to/output
        MDZEN_DEFAULT_TIMEOUT=300
    """

    # Output directory (defaults to current working directory)
    output_dir: str = "."

    # Timeout settings (seconds)
    default_timeout: int = 300
    structure_timeout: int = 600
    solvation_timeout: int = 600
    amber_timeout: int = 900
    membrane_timeout: int = 7200
    md_simulation_timeout: int = 3600

    # Logging settings
    log_level: str = "WARNING"

    # Server paths (relative to project root)
    research_server_path: str = "servers/research_server.py"
    literature_server_path: str = "servers/literature_server.py"
    structure_server_path: str = "servers/structure_server.py"
    genesis_server_path: str = "servers/genesis_server.py"
    solvation_server_path: str = "servers/solvation_server.py"
    amber_server_path: str = "servers/amber_server.py"
    md_simulation_server_path: str = "servers/md_simulation_server.py"
    metal_server_path: str = "servers/metal_server.py"

    class Config:
        env_prefix = "MDZEN_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()


def get_output_dir() -> Path:
    """Get the output directory, creating it if needed."""
    output_dir = Path(settings.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_server_path(server_name: str) -> str:
    """Get the path to a server script."""
    server_map = {
        "research": settings.research_server_path,
        "literature": settings.literature_server_path,
        "structure": settings.structure_server_path,
        "genesis": settings.genesis_server_path,
        "solvation": settings.solvation_server_path,
        "amber": settings.amber_server_path,
        "md_simulation": settings.md_simulation_server_path,
        "metal": settings.metal_server_path,
    }
    return server_map.get(server_name, f"servers/{server_name}_server.py")


def get_timeout(timeout_type: str) -> int:
    """Get timeout value by type."""
    timeout_map = {
        "default": settings.default_timeout,
        "research": settings.default_timeout,
        "structure": settings.structure_timeout,
        "genesis": settings.default_timeout,
        "solvation": settings.membrane_timeout,
        "membrane": settings.membrane_timeout,
        "amber": settings.amber_timeout,
        "md_simulation": settings.md_simulation_timeout,
    }
    return timeout_map.get(timeout_type, settings.default_timeout)
