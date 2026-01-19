"""Configuration settings for MDZen.

This module centralizes all configuration settings for the ADK implementation.
Settings are loaded from environment variables with MDZEN_ prefix.

Usage:
    from mdzen.config import settings, get_litellm_model

    # Access settings
    model = get_litellm_model("clarification")
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings


def _detect_default_models() -> tuple[str, str, str]:
    """Detect available API keys and return appropriate default models.

    Priority:
    1. Anthropic (if ANTHROPIC_API_KEY is set)
    2. OpenAI (if OPENAI_API_KEY is set)
    3. Google (if GOOGLE_API_KEY is set)

    Returns:
        Tuple of (clarification_model, setup_model, compress_model)
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "anthropic:claude-haiku-4-5-20251001",
            "anthropic:claude-haiku-4-5-20251001",
            "anthropic:claude-haiku-4-5-20251001",
        )
    elif os.environ.get("OPENAI_API_KEY"):
        return (
            "openai:gpt-4o-mini",
            "openai:gpt-4o-mini",
            "openai:gpt-4o-mini",
        )
    elif os.environ.get("GOOGLE_API_KEY"):
        return (
            "google:gemini-2.0-flash",
            "google:gemini-2.0-flash",
            "google:gemini-2.0-flash",
        )
    else:
        # Default to Anthropic (will fail if no key, but provides clear error message)
        return (
            "anthropic:claude-haiku-4-5-20251001",
            "anthropic:claude-haiku-4-5-20251001",
            "anthropic:claude-haiku-4-5-20251001",
        )


# Detect defaults based on available API keys
_clarification_default, _setup_default, _compress_default = _detect_default_models()


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All settings use the MDZEN_ prefix. For example:
        MDZEN_OUTPUT_DIR=/path/to/output
        MDZEN_SETUP_MODEL=anthropic:claude-sonnet-4-20250514

    Model defaults are auto-detected based on available API keys:
    - If ANTHROPIC_API_KEY is set: uses Claude models
    - If OPENAI_API_KEY is set: uses GPT models
    - If GOOGLE_API_KEY is set: uses Gemini models
    """

    # Output directory (defaults to current working directory)
    output_dir: str = "."

    # Model settings (defaults auto-detected from available API keys)
    clarification_model: str = _clarification_default
    setup_model: str = _setup_default
    compress_model: str = _compress_default

    # Timeout settings (seconds)
    default_timeout: int = 300
    structure_timeout: int = 600  # antechamber can take several minutes for complex ligands
    solvation_timeout: int = 600
    amber_timeout: int = 900  # tleap needs time for large systems (100k+ waters)
    membrane_timeout: int = 7200  # Large membrane systems (e.g., SERCA) need 2+ hours
    md_simulation_timeout: int = 3600

    # Logging settings
    log_level: str = "WARNING"  # DEBUG, INFO, WARNING, ERROR

    # Message history limit
    max_message_history: int = 6

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
        extra = "ignore"  # Ignore non-MDZEN_ prefixed env vars


# Global settings instance
settings = Settings()


def get_output_dir() -> Path:
    """Get the output directory as a Path object.

    Creates the directory if it doesn't exist.

    Returns:
        Path to output directory
    """
    output_dir = Path(settings.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_server_path(server_name: str) -> str:
    """Get the path to a server script.

    Args:
        server_name: Server name ("research", "literature", "structure", "genesis",
                     "solvation", "amber", "md_simulation", "metal")

    Returns:
        Relative path to server script
    """
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


def get_litellm_model(model_type: str) -> str:
    """Convert mdzen model string to LiteLLM format.

    MDZen uses "anthropic:claude-xxx" format
    LiteLLM uses "anthropic/claude-xxx" format

    Args:
        model_type: Type of model ("clarification", "setup", or "compress")

    Returns:
        LiteLLM-compatible model string
    """
    model_map = {
        "clarification": settings.clarification_model,
        "setup": settings.setup_model,
        "compress": settings.compress_model,
    }

    model_str = model_map.get(model_type, settings.setup_model)

    # Convert "anthropic:model" to "anthropic/model" for LiteLLM
    if ":" in model_str:
        provider, model_name = model_str.split(":", 1)
        return f"{provider}/{model_name}"

    return model_str


def get_timeout(timeout_type: str) -> int:
    """Get timeout value by type.

    Single source of truth for all timeout configuration.

    Args:
        timeout_type: One of "default", "research", "structure", "genesis", "solvation",
                     "membrane", "amber", "md_simulation"

    Returns:
        Timeout in seconds
    """
    timeout_map = {
        "default": settings.default_timeout,
        "research": settings.default_timeout,
        "structure": settings.structure_timeout,  # antechamber needs more time
        "genesis": settings.default_timeout,
        # solvation server handles both water box and membrane - use membrane timeout
        # to ensure MCP connection doesn't timeout during long membrane builds
        "solvation": settings.membrane_timeout,
        "membrane": settings.membrane_timeout,
        "amber": settings.amber_timeout,  # tleap needs time for large systems
        "md_simulation": settings.md_simulation_timeout,
    }
    return timeout_map.get(timeout_type, settings.default_timeout)


__all__ = [
    "settings",
    "Settings",
    "get_output_dir",
    "get_server_path",
    "get_litellm_model",
    "get_timeout",
]
