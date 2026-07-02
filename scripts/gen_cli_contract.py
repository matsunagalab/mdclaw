"""Regenerate the CLI contract golden used by tests/test_cli_contract.py.

Run with: conda run -n mdclaw python scripts/gen_cli_contract.py

The golden pins the agent-facing CLI surface (tool -> server, node-context
requirement, job_dir-is-data flag, and required parameters) so refactors that
would silently change the contract fail loudly in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw._cli import _discover_tools, _tool_list_json


def build_contract() -> dict:
    tools = _discover_tools()
    payload = _tool_list_json(tools)
    contract: dict[str, dict] = {}
    for tool in payload["tools"]:
        required = sorted(p["name"] for p in tool["parameters"] if p.get("required"))
        contract[tool["name"]] = {
            "server": tool["server"],
            "requires_node": tool["requires_node"],
            "job_dir_is_data": tool["job_dir_is_data"],
            "required_params": required,
        }
    return contract


def main() -> None:
    contract = build_contract()
    out = Path(__file__).resolve().parent.parent / "tests" / "data" / "cli_contract.json"
    out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(contract)} tools to {out}")


if __name__ == "__main__":
    main()
