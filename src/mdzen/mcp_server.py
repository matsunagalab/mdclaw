"""MDZen unified MCP server.

Combines all individual MCP servers into a single entry point.

Usage:
    # Start with all servers (default)
    mdzen-mcp

    # Select specific servers
    mdzen-mcp --servers research,structure

    # HTTP transport (for Jupyter/Colab)
    mdzen-mcp --http --port 8080

    # Test with MCP Inspector
    mcp dev src/mdzen/mcp_server.py
"""

import argparse
import sys

from fastmcp import FastMCP

mcp = FastMCP("mdzen")

# Server registry: name -> module path and FastMCP instance attribute
SERVER_REGISTRY = {
    "research": "servers.research_server",
    "structure": "servers.structure_server",
    "solvation": "servers.solvation_server",
    "amber": "servers.amber_server",
    "md_simulation": "servers.md_simulation_server",
    "genesis": "servers.genesis_server",
    "literature": "servers.literature_server",
    "metal": "servers.metal_server",
}


def _import_servers(selected: list[str] | None = None) -> None:
    """Import and mount selected MCP servers.

    Args:
        selected: List of server names to import. None means all servers.
    """
    import importlib

    targets = selected or list(SERVER_REGISTRY.keys())

    for name in targets:
        if name not in SERVER_REGISTRY:
            print(f"Warning: Unknown server '{name}', skipping.", file=sys.stderr)
            continue
        module_path = SERVER_REGISTRY[name]
        try:
            mod = importlib.import_module(module_path)
            server_mcp = getattr(mod, "mcp", None)
            if server_mcp is None:
                print(f"Warning: {module_path} has no 'mcp' attribute, skipping.", file=sys.stderr)
                continue
            mcp.import_server(name, server_mcp)
        except ImportError as e:
            print(f"Warning: Could not import {module_path}: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="MDZen unified MCP server")
    parser.add_argument(
        "--servers",
        type=str,
        default=None,
        help="Comma-separated list of servers to load (default: all)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    # Aliases for convenience
    parser.add_argument("--http", action="store_true", help="Use HTTP transport")

    args = parser.parse_args()

    selected = args.servers.split(",") if args.servers else None
    _import_servers(selected)

    transport = "http" if args.http else args.transport
    if transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
