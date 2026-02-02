"""MCP Toolset configuration for MDZen.

This module configures McpToolset instances for all 7 MCP servers
using ADK's native MCP integration.

Supports two transport modes:
- stdio: Default for CLI and Colab (subprocess-based, more reliable)
- http: Alternative for Colab/Jupyter (HTTP-based, requires servers running with --http flag)
  - Streamable HTTP (/mcp endpoint) - recommended, current MCP standard
  - SSE (/sse endpoint) - deprecated, for backwards compatibility
"""

import os
import sys
from pathlib import Path

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    SseConnectionParams,
    StreamableHTTPConnectionParams,
)
from mcp import StdioServerParameters

from mdzen.config import get_server_path, get_timeout
from mdzen.workflow import STEP_CONFIG

# Detect Colab environment
IN_COLAB = "google.colab" in sys.modules

# Colab-specific Python path (conda Python with scientific packages)
COLAB_PYTHON = "/usr/local/bin/python"
COLAB_PYTHONPATH = "/content/mdzen/src"

# SSE port mapping for each server
SSE_PORT_MAP = {
    "research": 8001,
    "structure": 8002,
    "genesis": 8003,
    "solvation": 8004,
    "amber": 8005,
    "md_simulation": 8006,
    "metal": 8007,
    "literature": 8008,
}

# Cache for project root
_project_root: Path | None = None

# Track active toolsets so we can close sessions between workflow steps
_active_toolsets: list[McpToolset] = []


def get_project_root() -> Path:
    """Get the project root directory by looking for pyproject.toml.

    Returns:
        Path to project root (where pyproject.toml and servers/ are located)

    Raises:
        RuntimeError: If project root cannot be found
    """
    global _project_root
    if _project_root is not None:
        return _project_root

    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            _project_root = parent
            return parent
    raise RuntimeError(
        "Could not find project root (no pyproject.toml found in parent directories)"
    )


def _create_toolset(server_name: str, tool_filter: list[str] | None = None) -> McpToolset:
    """Create a McpToolset for a server with optional tool filtering.

    Args:
        server_name: Name of the server (structure, genesis, etc.)
        tool_filter: List of tool names to include (None = all tools)

    Returns:
        Configured McpToolset instance
    """
    timeout = get_timeout(server_name)

    if IN_COLAB:
        # Colab: Use conda Python with PYTHONPATH for scientific packages
        server_path = f"/content/mdzen/servers/{server_name}_server.py"
        python_cmd = COLAB_PYTHON

        # Set environment for subprocess
        env = os.environ.copy()
        env["PYTHONPATH"] = COLAB_PYTHONPATH

        return McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=python_cmd,
                    args=[server_path],
                    env=env,
                ),
                timeout=timeout,
            ),
            tool_filter=tool_filter,
        )
    else:
        # Local: Use current Python
        server_path = str(get_project_root() / get_server_path(server_name))

        return McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=sys.executable,
                    args=[server_path],
                ),
                timeout=timeout,
            ),
            tool_filter=tool_filter,
        )


def create_mcp_toolsets() -> dict[str, McpToolset]:
    """Create McpToolset instances for all 7 MCP servers.

    Each server is configured with stdio transport for local execution.

    Returns:
        Dictionary mapping server names to McpToolset instances
    """
    server_names = [
        "research", "structure", "genesis", "solvation",
        "amber", "md_simulation", "literature"
    ]
    return {name: _create_toolset(name) for name in server_names}


def create_filtered_toolset(
    server_name: str,
    tool_filter: list[str] | None = None,
) -> McpToolset:
    """Create a McpToolset with optional tool filtering.

    Args:
        server_name: Name of the server (structure, genesis, etc.)
        tool_filter: List of tool names to include (None = all tools)

    Returns:
        Configured McpToolset instance
    """
    return _create_toolset(server_name, tool_filter)


def get_step_tools(step: str) -> list[McpToolset]:
    """Get MCP toolsets for a specific workflow step.

    Creates only the toolsets needed for the given step, reducing
    token consumption and preventing tool selection errors.

    Args:
        step: Step name ("prepare_complex", "solvate", "build_topology", "run_simulation")

    Returns:
        List of McpToolset instances for the step

    Raises:
        ValueError: If step name is not recognized
    """
    if step not in STEP_CONFIG:
        valid_steps = list(STEP_CONFIG.keys())
        raise ValueError(f"Unknown step '{step}'. Valid steps: {valid_steps}")

    server_names = STEP_CONFIG[step]["servers"]
    toolsets = []
    for name in server_names:
        toolsets.append(create_filtered_toolset(name))
    return toolsets


# =============================================================================
# Workflow v2: stepwise toolsets ((1)→(2)→(3)→(4)→(quick_md)→(validation))
# =============================================================================

# Tool filters per step. Keep these minimal for small models.
WORKFLOW_V2_TOOL_FILTERS: dict[str, dict[str, list[str]]] = {
    "acquire_structure": {
        "research": [
            "get_structure_info",
            "download_structure",
            "get_alphafold_structure",
            "search_proteins",
            "get_protein_info",
        ],
        "genesis": [
            "boltz2_protein_from_seq",
            "rdkit_validate_smiles",
            "pubchem_get_smiles_from_name",
        ],
    },
    "select_prepare": {
        "research": [
            "inspect_molecules",
        ],
        "structure": [
            "split_molecules",
            "merge_structures",
        ],
    },
    "structure_decisions": {
        "research": [
            "analyze_structure_details",
        ],
        "structure": [
            "prepare_complex",
        ],
    },
    "solvate_or_membrane": {
        "solvation": [
            "solvate_structure",
            "embed_in_membrane",
            "list_available_lipids",
        ],
    },
    "quick_md": {
        "amber": [
            "build_amber_system",
        ],
        "md_simulation": [
            "run_md_simulation",
        ],
    },
    # validation step uses FunctionTool only (no MCP servers)
    "validation": {},
}


async def close_active_toolsets() -> None:
    """Evict stale MCP sessions from all active toolsets.

    Between workflow steps, old SessionContext background tasks may linger in a
    different async-task context.  Calling ``McpToolset.close()`` or
    ``MCPSessionManager.close()`` triggers ``exit_stack.aclose()`` which invokes
    ``SessionContext.__aexit__`` — this in turn exits the ``stdio_client``'s
    anyio CancelScope from a *different* task than it was entered in, raising
    ``RuntimeError("Attempted to exit cancel scope …")``.

    The safe workaround is to **not** call close at all.  Instead we:

    1. Signal each ``SessionContext._run()`` task to stop via its
       ``_close_event`` (cheap, no cross-task scope exit).
    2. Forcibly clear the ``_sessions`` dict so the next ``create_session()``
       starts fresh.
    3. Drop all references so the OS can reap subprocesses on exit.
    """
    for ts in _active_toolsets:
        try:
            mgr = ts._mcp_session_manager
            for _key, (session, exit_stack) in list(mgr._sessions.items()):
                # Best-effort: signal the SessionContext._run task to stop.
                # SessionContext stores its events on the *session* wrapper kept
                # by the exit_stack.  We cannot safely call exit_stack.aclose()
                # (that triggers the cancel-scope error), but we CAN look for
                # the SessionContext's _close_event on the exit_stack callbacks.
                for cb in getattr(exit_stack, "_exit_callbacks", []):
                    ctx = getattr(cb, "__self__", None) if callable(cb) else None
                    if ctx is None:
                        # exit_stack stores (is_sync, callback) tuples
                        if isinstance(cb, tuple) and len(cb) >= 2:
                            ctx = getattr(cb[1], "__self__", None)
                    if hasattr(ctx, "_close_event"):
                        ctx._close_event.set()
            mgr._sessions.clear()
        except Exception:
            pass
    _active_toolsets.clear()


def clear_toolset_cache() -> None:
    """Synchronous cleanup — clears tracking list without closing sessions.

    Prefer close_active_toolsets() when an event loop is available.
    """
    _active_toolsets.clear()


def get_workflow_step_tools(step: str) -> list[McpToolset]:
    """Get MCP toolsets for a v2 workflow step.

    This is designed for small models: each step exposes only the minimum tools.
    Creates fresh toolsets each time. Call close_active_toolsets() between steps
    to cleanly terminate previous sessions.
    """
    if step not in WORKFLOW_V2_TOOL_FILTERS:
        valid = list(WORKFLOW_V2_TOOL_FILTERS.keys())
        raise ValueError(f"Unknown workflow step '{step}'. Valid steps: {valid}")

    toolsets: list[McpToolset] = []
    for server_name, tool_filter in WORKFLOW_V2_TOOL_FILTERS[step].items():
        toolsets.append(create_filtered_toolset(server_name, tool_filter=tool_filter))
    _active_toolsets.extend(toolsets)
    return toolsets


def get_workflow_step_tools_sse(host: str, step: str) -> list[McpToolset]:
    """Get MCP toolsets for a v2 workflow step using HTTP transport."""
    if step not in WORKFLOW_V2_TOOL_FILTERS:
        valid = list(WORKFLOW_V2_TOOL_FILTERS.keys())
        raise ValueError(f"Unknown workflow step '{step}'. Valid steps: {valid}")

    toolsets: list[McpToolset] = []
    for server_name, tool_filter in WORKFLOW_V2_TOOL_FILTERS[step].items():
        toolsets.append(
            create_http_toolset(
                server_name,
                tool_filter=tool_filter,
                host=host,
                use_streamable_http=True,
            )
        )
    return toolsets


def get_clarification_tools() -> list[McpToolset]:
    """Get tools for clarification phase.

    Returns literature and research tools for Phase 1.
    Literature tools are listed first to encourage literature-first workflow
    for ambiguous queries (search papers before jumping to structure databases).

    Returns:
        List of McpToolset instances with filtered tools
    """
    return [
        # Literature search first - use for ambiguous queries
        create_filtered_toolset(
            "literature",
            tool_filter=[
                "pubmed_search",  # Search PubMed for relevant papers
                "pubmed_fetch",  # Get detailed article info with abstracts
            ],
        ),
        # Structure database tools
        create_filtered_toolset(
            "research",
            tool_filter=[
                "search_structures",  # Search PDB with detailed info
                "get_structure_info",  # PDB metadata with UniProt cross-refs
                "get_protein_info",  # UniProt biological info (subunit, function)
                "download_structure",
                "get_alphafold_structure",
                "inspect_molecules",
                "search_proteins",
                "analyze_structure_details",  # Disulfide, HIS pKa, missing residues
            ],
        ),
    ]


def get_setup_tools() -> list[McpToolset]:
    """Get all tools for setup phase.

    Returns all MCP toolsets for Phase 2 workflow.

    Returns:
        List of all McpToolset instances
    """
    toolsets = create_mcp_toolsets()
    return list(toolsets.values())


async def close_toolsets(toolsets: list[McpToolset], timeout: float = 5.0) -> None:
    """Close all MCP toolsets to release resources.

    Note: Due to anyio task context issues with MCP's stdio_client,
    explicit close() calls often fail with "Attempted to exit cancel scope
    in a different task". Since we're exiting the process anyway, it's
    safer to let the OS clean up the resources.

    Args:
        toolsets: List of McpToolset instances to close
        timeout: Maximum time to wait for each toolset to close (seconds)
    """
    # Skip explicit cleanup - the OS will clean up when the process exits.
    # Calling toolset.close() causes anyio task context errors that spam
    # the console and can hang the process.
    #
    # See: https://github.com/modelcontextprotocol/python-sdk/issues/XXX
    pass


# =============================================================================
# HTTP Transport Functions (for Colab/Jupyter)
# =============================================================================

# Port mapping for each server (used by both SSE and Streamable HTTP)
HTTP_PORT_MAP = SSE_PORT_MAP  # Alias for clarity


def create_http_toolset(
    server_name: str,
    tool_filter: list[str] | None = None,
    host: str = "localhost",
    use_streamable_http: bool = True,
    timeout: float = 120.0,
) -> McpToolset:
    """Create a McpToolset using HTTP transport (for Colab/Jupyter).

    Requires the MCP server to be running with --http flag (Streamable HTTP)
    or --sse flag (deprecated SSE transport).

    Args:
        server_name: Name of the server (research, structure, etc.)
        tool_filter: List of tool names to include (None = all tools)
        host: Hostname where HTTP server is running (default: localhost)
        use_streamable_http: If True, use /mcp endpoint with StreamableHTTPConnectionParams.
                            If False, use /sse endpoint with SseConnectionParams (deprecated).
        timeout: Connection timeout in seconds (default: 120.0 for Colab reliability)

    Returns:
        Configured McpToolset instance using HTTP transport
    """
    port = HTTP_PORT_MAP.get(server_name, 8000)

    if use_streamable_http:
        # Streamable HTTP transport (recommended) - /mcp endpoint
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=f"http://{host}:{port}/mcp",
                timeout=timeout,
            ),
            tool_filter=tool_filter,
        )
    else:
        # SSE transport (deprecated) - /sse endpoint
        return McpToolset(
            connection_params=SseConnectionParams(
                url=f"http://{host}:{port}/sse",
            ),
            tool_filter=tool_filter,
        )


# Backwards compatibility alias
def create_sse_toolset(
    server_name: str,
    tool_filter: list[str] | None = None,
    host: str = "localhost",
) -> McpToolset:
    """Create a McpToolset using SSE transport (deprecated).

    Use create_http_toolset() with use_streamable_http=True instead.
    """
    return create_http_toolset(
        server_name, tool_filter, host, use_streamable_http=False
    )


def get_clarification_tools_sse(host: str = "localhost") -> list[McpToolset]:
    """Get clarification tools using HTTP transport (for Colab/Jupyter).

    Returns literature and research tools for Phase 1.
    Literature tools are listed first for literature-first workflow.
    Requires literature_server (--http --port 8008) and research_server (--http --port 8001).

    Note: Function name kept for backwards compatibility. Uses Streamable HTTP
    transport (/mcp endpoint) by default, which is the current MCP standard.

    Args:
        host: Hostname where HTTP server is running (default: localhost)

    Returns:
        List of McpToolset instances with filtered tools
    """
    return [
        # Literature search first
        create_http_toolset(
            "literature",
            tool_filter=[
                "pubmed_search",
                "pubmed_fetch",
            ],
            host=host,
            use_streamable_http=True,
        ),
        # Structure database tools
        create_http_toolset(
            "research",
            tool_filter=[
                "search_structures",  # Search PDB with detailed info
                "get_structure_info",
                "get_protein_info",
                "download_structure",
                "get_alphafold_structure",
                "inspect_molecules",
                "search_proteins",
                "analyze_structure_details",  # Detailed structure analysis
            ],
            host=host,
            use_streamable_http=True,
        ),
    ]


def get_setup_tools_sse(host: str = "localhost") -> list[McpToolset]:
    """Get all tools for setup phase using HTTP transport (for Colab/Jupyter).

    Returns all MCP toolsets for Phase 2 workflow.
    Requires all MCP servers to be running with --http flag.

    Note: Function name kept for backwards compatibility. Uses Streamable HTTP
    transport (/mcp endpoint) by default.

    Args:
        host: Hostname where HTTP servers are running (default: localhost)

    Returns:
        List of all McpToolset instances using HTTP transport
    """
    server_names = [
        "research", "structure", "genesis", "solvation",
        "amber", "md_simulation", "literature"
    ]
    return [create_http_toolset(name, host=host, use_streamable_http=True) for name in server_names]


def get_step_tools_sse(step: str, host: str = "localhost") -> list[McpToolset]:
    """Get MCP toolsets for a specific workflow step using HTTP transport.

    Creates only the toolsets needed for the given step, reducing
    token consumption and preventing tool selection errors.

    Note: Function name kept for backwards compatibility. Uses Streamable HTTP
    transport (/mcp endpoint) by default.

    Args:
        step: Step name ("prepare_complex", "solvate", "build_topology", "run_simulation")
        host: Hostname where HTTP servers are running (default: localhost)

    Returns:
        List of McpToolset instances for the step using HTTP transport

    Raises:
        ValueError: If step name is not recognized
    """
    if step not in STEP_CONFIG:
        valid_steps = list(STEP_CONFIG.keys())
        raise ValueError(f"Unknown step '{step}'. Valid steps: {valid_steps}")

    server_names = STEP_CONFIG[step]["servers"]
    return [create_http_toolset(name, host=host, use_streamable_http=True) for name in server_names]
