"""surrogate.setup submodule (behavior-preserving split)."""

from __future__ import annotations

from mdclaw.surrogate._base import (
    _get_backend,
)


def setup_model_backend(
    model: str,
    device: str = "cpu",
    prefix: str | None = None,
    reinstall: bool = False,
) -> dict:
    """Create or update an isolated venv for a heavy model backend.

    Supported models: ``bioemu`` (MD surrogate ensembles) and ``boltz``
    (structure prediction). The venv is created under
    ``$MDCLAW_SURROGATE_DIR/<model>/venv`` and never touches the conda
    ``mdclaw`` environment.
    """
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


def check_model_backend(
    model: str,
    prefix: str | None = None,
) -> dict:
    """Check whether a model backend venv is installed and importable."""
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


def setup_surrogate_backend(
    model: str = "bioemu",
    device: str = "cpu",
    prefix: str | None = None,
    reinstall: bool = False,
) -> dict:
    """Backward-compatible alias for :func:`setup_model_backend`."""
    return setup_model_backend(model, device=device, prefix=prefix, reinstall=reinstall)


def check_surrogate_backend(
    model: str = "bioemu",
    prefix: str | None = None,
) -> dict:
    """Backward-compatible alias for :func:`check_model_backend`."""
    return check_model_backend(model, prefix=prefix)

