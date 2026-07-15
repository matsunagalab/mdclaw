"""Regenerate the CLI contract golden used by tests/test_cli_contract.py.

Run with: conda run -n mdclaw python scripts/gen_cli_contract.py

The golden pins the agent-facing CLI surface (tool -> server, node-context
requirement and type, job_dir-is-data flag, and required parameters) so
refactors that would silently change the contract fail loudly in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from mdclaw._cli import _discover_tools, _tool_list_json  # noqa: E402


def build_contract() -> dict:
    tools = _discover_tools()
    payload = _tool_list_json(tools)
    contract: dict[str, dict] = {}
    for tool in payload["tools"]:
        required = sorted(p["name"] for p in tool["parameters"] if p.get("required"))
        contract[tool["name"]] = {
            "server": tool["server"],
            "requires_node": tool["requires_node"],
            "node_type": tool["node_type"],
            "job_dir_is_data": tool["job_dir_is_data"],
            "required_params": required,
        }
    return contract


def main() -> None:
    contract = build_contract()
    out = _REPO_ROOT / "tests" / "data" / "cli_contract.json"
    out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(contract)} tools to {out}")


if __name__ == "__main__":
    main()
