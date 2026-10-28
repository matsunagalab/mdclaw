"""What an agent meant when the command line does not name a tool the CLI has.

MDDataBench v5 (three models, 2026-10) answered 152 calls in 113 attempts with
``tool_not_available``, and 153 of 158 such replies carried no suggestion. The
names were not random:

- a server or stage word in front of the tool (``mdclaw slurm submit_job``,
  ``mdclaw study bootstrap_md_workflow``, ``slurm_submit_job``,
  ``slurm.submit_job``, ``mdclaw source fetch_structure``);
- a verb the agent expected (``fetch_pdb``, ``fetch_source``,
  ``remove_node``, ``abandon_node``, ``inspect_node``);
- a value in the tool's place because the token before it lost its own
  value: ``--node-id $EMPTY embed_in_membrane --lipids DPPC`` (the empty
  variable makes ``--node-id`` swallow the tool name, and ``DPPC`` becomes
  the "tool"), ``--node-type prep create_node`` (a tool option before the
  tool), ``source_001 fetch_structure`` (a node id without ``--node-id``).

Unambiguous spellings (case, hyphens, a server prefix before an exact tool
name, tool options written before the tool) are read as the tool and noted in
the result's ``warnings``. Everything else is refused with the nearest tools
and, where the intent is clear, the corrected command in ``next_action``.
"""

from __future__ import annotations

import difflib
import re
import shlex
from typing import Callable, Optional

from mdclaw._registry import SERVER_REGISTRY
from mdclaw.node.constants import NODE_TYPE_ORDER

# Options of the ``mdclaw`` program itself (before the tool name).
GLOBAL_VALUE_OPTIONS = frozenset({"--job-dir", "--node-id", "--list-json", "--output", "--log-file",
                                  "--heartbeat-seconds"})
GLOBAL_FLAG_OPTIONS = frozenset({"--list", "--workflow", "--version", "--help", "-h"})

# Words agents put in front of a tool name: server names and their package
# names, stage names, and the program name itself.
PREFIX_WORDS = frozenset(
    {*SERVER_REGISTRY, *(path.rsplit(".", 1)[-1] for path in SERVER_REGISTRY.values()),
     *NODE_TYPE_ORDER, "mdclaw", "tool", "tools", "server"}
)

# Names agents guessed for an existing tool (MDDataBench v5 transcripts).
# Each maps to the tool and the arguments that make the guessed verb mean
# what the agent wanted; the reply names the corrected command, it does not
# run it (the arguments of the guessed verb may differ).
TOOL_ALIASES: dict[str, tuple[str, tuple[str, ...], str]] = {
    **{name: ("fetch_structure", (), "the source stage tool fetches PDB, AlphaFold and local files")
       for name in ("fetch_source", "fetch_pdb", "fetch", "get_pdb", "download_pdb", "acquire_source",
                    "acquire_structure", "source_acquisition", "source_pdb", "run_source",
                    "run_fetch_structure", "get_structure", "download")},
    **{name: ("update_workflow_state", ("--abandon",),
              "a pending node that will never run is retired with update_workflow_state --abandon "
              "(nodes are never deleted)")
       for name in ("remove_node", "delete_node", "abandon_node", "cancel_node", "fail_node",
                    "mark_node_failed", "reset_node", "retire_node", "drop_node")},
    **{name: ("update_workflow_state", (), "node status and job params change through update_workflow_state")
       for name in ("update_node", "set_node_status", "update_status", "set_status")},
    "complete_node": ("explain_node", (), "a node completes only through its stage tool; explain_node says "
                                          "what it still needs"),
    "reparent_node": ("create_node", (), "nodes keep their parents; create a new node with --parent-node-ids"),
    **{name: ("explain_node", (), "explain_node shows one node: status, inputs, readiness")
       for name in ("inspect_node", "show_node", "node_info", "describe_node", "get_node", "node_status")},
    **{name: ("inspect_job", (), "inspect_job shows the job DAG")
       for name in ("inspect", "status", "show_job", "job_status", "inspect_dag", "show_dag", "dag",
                    "list_nodes")},
    **{name: ("inspect_molecules", (), "inspect_molecules reads chains, residue ranges, gaps and ligands")
       for name in ("inspect_structure", "inspect_source", "inspect_pdb", "inspect_chains")},
    **{name: ("list_source_candidates", (), "list_source_candidates lists the source bundle's structures")
       for name in ("list_candidates", "source_candidates")},
    **{name: ("prepare_complex", (), "prepare_complex is the prep stage tool")
       for name in ("prepare", "prepare_structure", "prep_structure", "clean_structure")},
    **{name: ("solvate_structure", (), "solvate_structure is the explicit-water solv stage tool")
       for name in ("solvate", "add_water", "add_solvent")},
    **{name: ("embed_in_membrane", (), "embed_in_membrane is the membrane solv stage tool")
       for name in ("embed_membrane", "build_membrane", "membrane")},
    **{name: ("build_amber_system", (), "build_amber_system is the default topo stage tool")
       for name in ("build_topology", "build_system", "topology", "build_amber")},
    **{name: ("run_minimization", (), "run_minimization is the min stage tool")
       for name in ("minimize", "minimization", "run_min")},
    **{name: ("run_equilibration", (), "run_equilibration is the eq stage tool")
       for name in ("equilibrate", "equilibration", "run_eq")},
    **{name: ("run_production", (), "run_production is the prod stage tool")
       for name in ("production", "run_prod", "run_md")},
    **{name: ("submit_job", (), "submit_job submits one node's stage command to Slurm")
       for name in ("submit", "sbatch", "submit_node")},
    **{name: ("wait_node", (), "wait_node waits for a node to finish")
       for name in ("wait", "wait_job")},
    **{name: ("bootstrap_md_workflow", (), "bootstrap_md_workflow creates the study, plan, job and source node")
       for name in ("bootstrap", "init_workflow", "start_study", "create_study", "new_study")},
    "show_config": ("show_policy", (), "show_policy shows the cluster policy in .mdclaw_cluster.json"),
}

# Names that ask for the tool list, not for a tool.
LIST_REQUESTS = frozenset({"help", "list", "list_tools", "list_skills", "tools", "ls"})

NODE_ID_SHAPE = re.compile(rf"^(?:{'|'.join(NODE_TYPE_ORDER)})_[0-9][0-9a-z_]*$")


def command_text(argv: list[str]) -> str:
    return "mdclaw " + shlex.join(argv)


def subcommand_index(argv: list[str], global_value_options) -> Optional[int]:
    """Index of the first positional token, skipping global options' values."""
    skip_next = False
    for i, tok in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if tok.split("=", 1)[0] in global_value_options and "=" not in tok:
                skip_next = True
            continue
        return i
    return None


def canonical_tool_name(name: str, tools) -> Optional[str]:
    """The tool an unambiguous spelling variant names, else None.

    Case and hyphens (``Build_Amber-System``), and a server or stage word
    joined to an exact tool name (``slurm_submit_job``, ``slurm.submit_job``,
    ``slurm:submit_job``, ``slurm/submit_job``).
    """
    if name in tools:
        return name
    candidate = name.strip().lower().replace("-", "_")
    if candidate in tools:
        return candidate
    for word in sorted(PREFIX_WORDS, key=len, reverse=True):
        for sep in (".", ":", "/", "_"):
            prefix = word + sep
            if candidate.startswith(prefix) and candidate[len(prefix):] in tools:
                return candidate[len(prefix):]
    return None


def nearest_tools(name: str, tools, n: int = 5) -> list[str]:
    """A few tools ``name`` may have meant, closest first; [] only for noise."""
    names = sorted(tools)
    key = name.strip().lower().replace("-", "_")
    found: list[str] = []

    def add(items):
        for item in items:
            if item not in found:
                found.append(item)

    alias = TOOL_ALIASES.get(key)
    if alias:
        add([alias[0]])
    add(difflib.get_close_matches(key, names, n=n, cutoff=0.6))
    if len(key) >= 4:
        add(t for t in names if key in t or (len(t) >= 6 and t in key))
    words = {w for w in key.split("_") if len(w) >= 4}
    if words:
        add(t for t in names if words & set(t.split("_")))
    add(difflib.get_close_matches(key, names, n=n, cutoff=0.45))
    return found[:n]


def tool_option_rewrite(argv: list[str], sub: int, tools,
                        flags_of: Callable[[str], set]) -> Optional[tuple[list[str], str]]:
    """``--job-dir J --node-type solv create_node`` -> ``--job-dir J create_node --node-type solv``.

    ``argv[sub]`` is the value of a tool option written before the tool name.
    When a later token is a tool that takes every such option, the options
    move behind it. Returns ``(argv, tool)`` or None.
    """
    head_end = first_tool_option(argv, sub)
    if head_end is None:
        return None
    stray = argv[head_end:sub + 1]
    stray_flags = {tok.split("=", 1)[0] for tok in stray if tok.startswith("--")}
    for j in range(sub + 1, len(argv)):
        if argv[j] in tools and stray_flags <= flags_of(argv[j]):
            rewritten = argv[:head_end] + [argv[j]] + stray + argv[sub + 1:j] + argv[j + 1:]
            return rewritten, argv[j]
    return None


def first_tool_option(argv: list[str], sub: int) -> Optional[int]:
    """Index where tool options start before ``argv[sub]``, when ``argv[sub]``
    is the value of such an option (the token before it is a non-global flag)."""
    if sub == 0 or not argv[sub - 1].startswith("--") or "=" in argv[sub - 1]:
        return None
    if argv[sub - 1] in GLOBAL_VALUE_OPTIONS | GLOBAL_FLAG_OPTIONS:
        return None
    i = 0
    while i < sub:
        tok = argv[i]
        option = tok.split("=", 1)[0]
        if option in GLOBAL_VALUE_OPTIONS:
            i += 1 if "=" in tok else 2
            continue
        if option in GLOBAL_FLAG_OPTIONS:
            i += 1
            continue
        return i
    return None


def tools_taking(flags: set, tools, flags_of: Callable[[str], set]) -> list[str]:
    return sorted(t for t in tools if flags <= flags_of(t))


def node_id_value_problem(argv: list[str], tools) -> Optional[dict]:
    """``--node-id`` without a usable value: last token, empty, an option, or
    a tool name. An unset shell variable (``--node-id $NODE``) produces all
    four, depending on what follows it."""
    for i, tok in enumerate(argv):
        if tok == "--":
            break
        if tok == "--node-id=" or (tok.startswith("--node-id=") and not tok.split("=", 1)[1].strip()):
            return {"index": i, "value": "", "reason": "its value is empty", "joined": True}
        if tok != "--node-id":
            continue
        value = argv[i + 1] if i + 1 < len(argv) else None
        if value is None:
            reason = "it is the last argument"
        elif not value.strip():
            reason = "its value is an empty string"
        elif value.startswith("-"):
            reason = f"the next token {value!r} is an option"
        elif value in tools:
            reason = f"the next token {value!r} is a tool name"
        else:
            continue
        return {"index": i, "value": value, "reason": reason, "joined": False}
    return None


def option_value(argv: list[str], option: str) -> Optional[str]:
    for i, tok in enumerate(argv):
        if tok == option and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return argv[i + 1]
        if tok.startswith(option + "="):
            return tok.split("=", 1)[1]
    return None
