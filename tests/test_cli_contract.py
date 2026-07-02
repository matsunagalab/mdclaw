"""Golden snapshot of the agent-facing CLI contract.

This is a refactor safety net (plan phase 0). It pins, for every discovered
tool, the fields a weak agent branches on:

- which server owns it,
- whether it requires node context (``--job-dir`` + ``--node-id``),
- whether ``job_dir`` is data rather than execution context,
- the set of required parameters.

If a change intentionally alters this surface, regenerate the golden with
``conda run -n mdclaw python scripts/gen_cli_contract.py`` and review the diff.
An accidental change (e.g. a refactor that drops a required flag or flips a
node-context requirement) fails here loudly instead of silently shipping.
"""

import json
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).parent / "data" / "cli_contract.json"


def _build_current_contract() -> dict:
    from mdclaw._cli import _discover_tools, _tool_list_json

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


def _load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def test_cli_contract_matches_golden():
    """All tools present in the golden must keep their contract.

    Only compares tools present in *both* sets so that optional
    dependency-gated servers (which may be missing in a lean env) do not
    cause spurious failures. New tools are covered by
    ``test_no_unexpected_new_tools``.
    """
    golden = _load_golden()
    current = _build_current_contract()

    shared = sorted(set(golden) & set(current))
    assert shared, "No overlapping tools between golden and current discovery."

    mismatches = {
        name: {"golden": golden[name], "current": current[name]}
        for name in shared
        if golden[name] != current[name]
    }
    assert not mismatches, (
        "CLI contract drift detected. If intentional, regenerate with "
        "`python scripts/gen_cli_contract.py`. Mismatches:\n"
        + json.dumps(mismatches, indent=2, sort_keys=True)
    )


def test_no_unexpected_new_tools():
    """Newly discovered tools must be added to the golden deliberately."""
    golden = _load_golden()
    current = _build_current_contract()
    new_tools = sorted(set(current) - set(golden))
    assert not new_tools, (
        "New CLI tools are not in the contract golden. If intentional, run "
        "`python scripts/gen_cli_contract.py`. New tools: " + ", ".join(new_tools)
    )


def test_removed_tools_are_deliberate():
    """Tools removed from discovery must be removed from the golden too.

    Dependency-gated servers are tolerated: only fail when a *core* server's
    tool disappears while the server itself still imports.
    """
    from mdclaw._cli import _discover_tools

    golden = _load_golden()
    current = set(_discover_tools())
    missing = sorted(set(golden) - current)

    # Servers that are always importable without heavy scientific deps.
    core_servers = {"node", "study", "benchmark", "evidence", "throughput"}
    unexpected = [name for name in missing if golden[name]["server"] in core_servers]
    if unexpected:
        pytest.fail(
            "Core tools vanished from discovery without a golden update: "
            + ", ".join(unexpected)
        )
