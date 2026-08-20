"""Render a tool's ``confirmation_needed`` block as text for the user.

Tools put the chemistry they decided -- protonation states, histidine
tautomers, disulfide bonds -- into ``confirmation_needed`` so a caller can
check it before building a system on top of it. Deciding *what* belongs there
is the tool's job, and it builds it by reading the structure it produced.
Making it *visible* is this module's job, and the CLI calls it for any result
that carries the block.

The split matters. A tool that prints its own summary has to remember to, and
every new tool that assigns chemistry is a new place to forget; rendering once
at the boundary covers all of them. And an agent is free to skip relaying a
JSON field, while a batch job has no agent at all -- so this goes to stderr,
where stdout stays the JSON contract.
"""

from mdclaw._common import setup_logger

logger = setup_logger(__name__)


def _format_missing_residue_repair(entry: dict) -> str:
    """One line per chain: how much was built, in how many stretches, how long."""
    chain = entry.get("chain_id") or "?"
    total = entry.get("total_residues")
    segments = entry.get("segment_count")
    longest = entry.get("max_segment_length")
    text = f"chain {chain}: {total} residue(s) in {segments} segment(s)"
    if longest:
        text += f", longest {longest}"
    seed = entry.get("random_seed")
    if seed is not None:
        text += f" (seed {seed})"
    if entry.get("interface_context") == "chain_isolated":
        text += " [built without partner chains]"
    return text


def report_confirmation_items(confirmation_items: dict) -> None:
    """Write the block as human-readable lines, or nothing if it is empty."""
    disulfides = confirmation_items.get("disulfide_bonds", {}) or {}
    pairs = disulfides.get("pairs") or []
    his = confirmation_items.get("histidine_states", {}) or {}
    his_states = his.get("states") or {}
    prot = confirmation_items.get("protonation_states", {}) or {}
    prot_states = prot.get("states") or []
    missing = confirmation_items.get("missing_residues", {}) or {}
    repairs = missing.get("repairs") or []
    undetectable = [
        entry for entry in (missing.get("detection") or [])
        if not entry.get("reference_sequence_available")
    ]
    if not (pairs or his_states or prot_states or repairs or undetectable):
        return

    lines = ["Chemistry assigned — check this before building on it:"]
    if pairs:
        lines.append(f"  disulfide bonds ({disulfides.get('source', 'auto_detected')}): {len(pairs)}")
        for pair in pairs:
            lines.append(f"    {_format_disulfide_pair(pair)}")
    if his_states:
        lines.append(f"  histidine states ({his.get('source', 'auto_detected')}): {len(his_states)}")
        for residue, state in sorted(his_states.items(), key=_residue_sort_key):
            lines.append(f"    {residue} {state}")
    if prot_states:
        lines.append(
            f"  protonation states ({prot.get('source', 'auto_detected')}): {len(prot_states)}"
        )
        for entry in prot_states:
            lines.append(f"    {_format_protonation_state(entry)}")
    if repairs:
        total = sum(int(entry.get("total_residues") or 0) for entry in repairs)
        lines.append(
            f"  missing residues rebuilt ({missing.get('method', 'pdbfixer')}): "
            f"{total} residue(s) in {len(repairs)} chain(s) — predicted, not measured"
        )
        for entry in repairs:
            lines.append(f"    {_format_missing_residue_repair(entry)}")
    for entry in undetectable:
        lines.append(
            f"  chain {entry.get('chain_id', '?')}: no reference sequence, so missing "
            "residues were NOT checked (absence of gaps here proves nothing)"
        )
    lines.append(
        "  To change any of these, re-run prep with --disulfide-pairs / "
        "--histidine-states / --protonation-states. A state marked "
        "from_input_structure came in with the structure and will not move "
        "when you change --ph."
    )
    # INFO, not WARNING: nothing went wrong. These are ordinary decisions a
    # successful preparation makes, and flagging them as warnings trains the
    # reader to expect a failure that is not there.
    logger.info("\n".join(lines))


def _residue_sort_key(item: tuple) -> tuple:
    """Sort ``chain:resnum`` keys by chain then number, not lexically.

    Without this ``A:112`` sorts before ``A:77``.
    """
    residue = item[0]
    chain, _, resnum = str(residue).rpartition(":")
    digits = "".join(ch for ch in resnum if ch.isdigit())
    return (chain, int(digits) if digits else 0, resnum)


def _format_disulfide_pair(pair: object) -> str:
    """One disulfide as ``A:124 - A:203  2.02 A (high)``.

    Falls back to the raw value for shapes this does not recognise, so an
    unexpected record is still shown rather than swallowed.
    """
    if not isinstance(pair, dict):
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            return f"{pair[0]} - {pair[1]}"
        return str(pair)

    def _end(key: str) -> str | None:
        end = pair.get(key)
        if isinstance(end, dict):
            chain = end.get("chain")
            resnum = end.get("resnum")
            if resnum is None:
                return None
            return f"{chain}:{resnum}" if chain else str(resnum)
        return None

    first, second = _end("cys1"), _end("cys2")
    if not (first and second):
        return str(pair)

    text = f"{first} - {second}"
    distance = pair.get("distance_angstrom")
    if distance is not None:
        text += f"  {distance} A"
    confidence = pair.get("confidence")
    if confidence:
        text += f" ({confidence})"
    return text


def _format_protonation_state(entry: object) -> str:
    """One protonation state as ``A:97 ASP -> ASH (auto_detected)``."""
    if not isinstance(entry, dict):
        return str(entry)
    chain = entry.get("chain")
    resnum = entry.get("resnum")
    icode = entry.get("icode") or ""
    residue = f"{chain}:{resnum}{icode}" if chain else str(resnum)
    state = entry.get("state", "?")
    default = entry.get("default_state")
    source = entry.get("source", "auto_detected")
    if default:
        return f"{residue} {default} -> {state} ({source})"
    return f"{residue} {state} ({source})"
