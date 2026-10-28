"""Turn a rejected ``--conditions`` key into the key that was meant.

A declared condition is a contract: the stage tool must report the same key
back in ``actual_conditions`` or the node fails, and a failed node is terminal.
The accepted vocabulary is therefore something the caller has to know in
advance, and it is not discoverable from the tool signature: not every
parameter is reported as a condition. Measured over a 300-attempt campaign, 16
attempts lost a node this way; the keys were semantically right and lexically
wrong (``chains`` for ``select_chains``, ``ligands`` for
``include_ligand_ids``), and the agent that recovered did so by declaring no
conditions at all, which throws the DAG's record of intent away.

The vocabulary cannot usefully be tabulated ahead of time. Several tools serve
one node type -- ``prep`` is prepare_complex, mutation, phosphorylation and
modxna; ``topo`` is the Amber and OpenMM builders -- and they accept different
keys, so any per-node-type table is either too strict for one tool or too loose
for another. A table built by hand also drifts: an early version of this module
was one, and it silently omitted the Amber builder's ``forcefield`` and
``water_model``, which would have rejected the most ordinary ``topo``
declaration there is.

At the point of failure none of that is a problem, because the executor is
known exactly: its vocabulary is the keys of the ``actual_conditions`` it just
reported. These helpers take that set and say what the caller probably meant.
"""

from __future__ import annotations

import difflib
import re
from typing import Optional

# A trailing ``_<unit>`` names the unit a condition value is in. Two keys
# whose units differ are never the same key: ``total_simulation_time_ps``
# once resolved to ``simulation_time_ns`` and 2500 was compared with 2.5
# (MDDataBench v5). Factors are to the dimension's base unit (ns, nm, K,
# bar, mol/L). ``a`` and ``c`` are not units here (``salt_a``, ``salt_c``).
_UNITS: dict[str, tuple[str, float]] = {
    "fs": ("time", 1e-6), "ps": ("time", 1e-3), "ns": ("time", 1.0), "us": ("time", 1e3),
    "ms": ("time", 1e6), "s": ("time", 1e9), "sec": ("time", 1e9), "seconds": ("time", 1e9),
    "angstrom": ("length", 0.1), "ang": ("length", 0.1), "nm": ("length", 1.0), "pm": ("length", 1e-3),
    "kelvin": ("temperature", 1.0), "k": ("temperature", 1.0),
    "bar": ("pressure", 1.0), "atm": ("pressure", 1.01325),
    "molar": ("concentration", 1.0), "m": ("concentration", 1.0), "mm": ("concentration", 1e-3),
}
# Reported keys whose unit is not in their name.
_IMPLICIT_UNITS = {"dist": "angstrom", "dist_wat": "angstrom", "saltcon": "molar"}


def split_unit(key: str) -> tuple[str, Optional[str]]:
    """``('simulation_time', 'ns')`` for ``simulation_time_ns``; no unit -> None."""
    head, sep, tail = str(key).rpartition("_")
    if sep and head and tail in _UNITS:
        return head, tail
    return str(key), None


def unit_of(key: str) -> Optional[str]:
    return split_unit(key)[1] or _IMPLICIT_UNITS.get(str(key))


def _same_unit(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return True
    return _UNITS[a] == _UNITS[b]


def _crosses_units(key: str, candidate: str) -> bool:
    """Both keys name a unit and the units differ (other dimension or scale)."""
    a, b = unit_of(key), unit_of(candidate)
    return a is not None and b is not None and not _same_unit(a, b)


def suggest_condition_keys(
    accepted: object, key: str, limit: int = 4
) -> list[str]:
    """Accepted keys a rejected one plausibly meant, closest first.

    Returns several rather than one: the observed mistakes had more than one
    honest reading. ``ligands`` could be ``include_ligand_ids``,
    ``include_ligand_resnames``, ``exclude_ligand_ids`` or ``process_ligands``,
    and naming a single one would send the caller to the wrong flag as often as
    the right one. An empty list means nothing looked close, which is a more
    useful answer than a guess; unsupported keys must not be pointed at one.

    Similarity alone cannot do this. Measured against the real vocabularies,
    ``chains`` scores 0.632 on ``select_chains`` while ``mutations`` scores
    0.696 on the unrelated ``max_iterations``, so any cutoff that keeps the
    good suggestion keeps the bad one first. What separates them is whether the
    noun survives the rename, so a candidate qualifies only when it contains
    the key's stem or is a near-spelling of the whole key. Ordering inside the
    survivors is by similarity, which does not always put the most useful
    reading first -- for ``ligands`` the boolean ``process_ligands`` leads the
    identifier selectors -- which is the other reason to return the set rather
    than a winner.
    """
    candidates = sorted({str(k) for k in (accepted or ())
                         if str(k) != key and not _crosses_units(key, str(k))})
    if not candidates:
        return []

    def score(candidate: str) -> float:
        return difflib.SequenceMatcher(None, key, candidate).ratio()

    stem = key.rstrip("s")
    pool = {
        candidate for candidate in candidates
        if (len(stem) >= 4 and stem in candidate) or score(candidate) >= 0.75
    }
    return sorted(pool, key=lambda c: (-score(c), c))[:limit]


def describe_condition_key(accepted: object, key: str) -> str:
    """``'chains' (did you mean 'select_chains'?)`` for an error message."""
    suggestions = suggest_condition_keys(accepted, key)
    if not suggestions:
        return repr(key)
    if len(suggestions) == 1:
        return f"{key!r} (did you mean {suggestions[0]!r}?)"
    listed = ", ".join(repr(s) for s in suggestions[:-1])
    return f"{key!r} (did you mean {listed} or {suggestions[-1]!r}?)"


# Natural spellings of condition keys, resolved only when the tool actually
# reports the canonical key with a value. The fuzzy suggestion above finds
# ``chains`` -> ``select_chains`` but not ``salt_concentration_molar`` ->
# ``saltcon`` (no shared stem); both lost nodes in campaign v2 (018, 019, 057).
CONDITION_KEY_ALIASES = {
    "chains": "select_chains",
    "chain": "select_chains",
    "chain_ids": "select_chains",
    "chain_id": "select_chains",
    "chain_selection": "select_chains",
    "selected_chains": "select_chains",
    "protein_chain": "select_chains",
    "protein_chains": "select_chains",
    "ranges": "residue_ranges",
    "residue_range": "residue_ranges",
    "chain_ranges": "residue_ranges",
    "select_residue_ranges": "residue_ranges",
    "salt_concentration_molar": "saltcon",
    "salt_concentration": "saltcon",
    "salt_conc": "saltcon",
    "ion_concentration": "saltcon",
    "salt_molar": "saltcon",
    "ionic_strength": "saltcon",
    "temperature": "temperature_kelvin",
    "temperature_k": "temperature_kelvin",
    "pressure": "pressure_bar",
    "water": "water_model",
    "solvent_model": "water_model",
    "force_field": "forcefield",
    "forcefield_name": "forcefield",
    "force_field_name": "forcefield",
    "protein_force_field": "forcefield",
    "protein_forcefield": "forcefield",
    "ff": "forcefield",
    "timestep": "timestep_fs",
    "dt_fs": "timestep_fs",
    "buffer_distance": "dist",
    "box_padding": "dist",
    "box_buffer": "dist",
    "solvent_buffer": "dist",
    "padding": "dist",
    "protonation": "protonation_method",
    "ph_value": "ph",
}


def resolve_condition_key(accepted: object, key: str) -> str | None:
    """The reported key a declared one unambiguously means, or None.

    A table hit wins when the tool reports that key; otherwise a single fuzzy
    suggestion is accepted. Two or more suggestions stay a refusal: the
    caller has to say which one was meant. Keys in different units are never
    resolved to each other (``_crosses_units``).
    """
    reported = {str(k) for k in (accepted or ())}
    alias = CONDITION_KEY_ALIASES.get(key)
    if alias and alias in reported and not _crosses_units(key, alias):
        return alias
    suggestions = suggest_condition_keys(accepted, key)
    return suggestions[0] if len(suggestions) == 1 else None


# ---------------------------------------------------------------------------
# Screening ``create_node --conditions`` before the node exists
# ---------------------------------------------------------------------------

_STEM_PREFIXES = ("total_", "production_", "prod_", "md_", "run_", "requested_")
_STEM_SYNONYMS = {"simulation_time": "time", "sim_time": "time", "run_time": "time"}


def _stem(base: str) -> str:
    """``total_simulation_time`` / ``production_duration`` -> ``time``."""
    changed = True
    while changed:
        changed = False
        for prefix in _STEM_PREFIXES:
            if base.startswith(prefix) and len(base) > len(prefix):
                base, changed = base[len(prefix):], True
    base = re.sub(r"(duration|length)$", "time", base)
    return _STEM_SYNONYMS.get(base, base)


def _convert(value: object, from_unit: Optional[str], to_unit: Optional[str]) -> object:
    if from_unit is None or to_unit is None or _same_unit(from_unit, to_unit):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip())
        except ValueError:
            return value
    converted = float(value) * _UNITS[from_unit][1] / _UNITS[to_unit][1]
    return round(converted, 9)


def _meant_key(vocabulary: frozenset, key: str) -> Optional[tuple[str, bool, str]]:
    """``(reported key, same unit, how)`` a declared key unambiguously means.

    ``how`` is ``alias`` (the curated table or the key itself with a unit
    added) or ``stem`` (``total_time_ps`` for ``simulation_time_ns``: same
    quantity, maybe another unit).
    """
    base, unit = split_unit(key)
    for candidate in (CONDITION_KEY_ALIASES.get(key), CONDITION_KEY_ALIASES.get(base),
                      base if base != key else None):
        if candidate and candidate in vocabulary:
            target_unit = unit_of(candidate)
            if unit is None or target_unit is None or _UNITS[unit][0] == _UNITS[target_unit][0]:
                return candidate, _same_unit(unit, target_unit), "alias"
    if unit is None:
        return None
    tiers: dict[int, list[str]] = {}
    for candidate in vocabulary:
        cbase, cunit = split_unit(candidate)
        cunit = cunit or _IMPLICIT_UNITS.get(candidate)
        if cunit is None or _UNITS[cunit][0] != _UNITS[unit][0]:
            continue
        if cbase == base:
            tiers.setdefault(0, []).append(candidate)
        elif _stem(cbase) == _stem(base):
            tiers.setdefault(1, []).append(candidate)
    for tier in sorted(tiers):
        if len(tiers[tier]) == 1:
            candidate = tiers[tier][0]
            return candidate, _same_unit(unit, unit_of(candidate)), "stem"
        return None  # two readings: the caller has to choose
    return None


def screen_declared_conditions(vocabulary, conditions: dict) -> dict:
    """Check declared conditions against what the stage can cross-check.

    ``vocabulary`` is every key the node type's stage tools report in
    ``actual_conditions``. A declared key outside it can never be verified, so
    the node is born unrunnable (56 attempts in MDDataBench v5). Returns
    ``conditions`` (accepted keys, curated aliases renamed), ``renamed``
    (``{declared: reported}``) and ``unknown``: one entry per refused key with
    the reported key and value it most likely meant (unit-converted), or
    near spellings.
    """
    vocabulary = frozenset(str(k) for k in vocabulary)
    out: dict = {}
    renamed: dict = {}
    unknown: list[dict] = []
    for key, value in conditions.items():
        key = str(key)
        if key in vocabulary:
            out[key] = value
            continue
        meant = _meant_key(vocabulary, key)
        if meant and meant[1] and meant[2] == "alias" and meant[0] not in conditions and meant[0] not in out:
            out[meant[0]] = value
            renamed[key] = meant[0]
            continue
        entry: dict = {"key": key, "value": value}
        if meant and meant[0] not in conditions:
            entry["suggested_key"] = meant[0]
            entry["suggested_value"] = _convert(value, split_unit(key)[1], unit_of(meant[0]))
        else:
            near = suggest_condition_keys(vocabulary, key)
            if near:
                entry["did_you_mean"] = near
        unknown.append(entry)
    return {"conditions": out, "renamed": renamed, "unknown": unknown}


# ---------------------------------------------------------------------------
# Comparing a declared value with the reported one
# ---------------------------------------------------------------------------

_LIST_SPLIT = {"select_chains": r"[\s,;:]+"}
_NAME_KEYS = frozenset({"forcefield", "water_model", "nucleic_forcefield", "glycan_forcefield"})


def comparable_condition_values(key: str, expected: object, actual: object) -> tuple[object, object]:
    """The forms of a declared and a reported value to compare.

    Equivalent spellings agents declared and lost nodes to (MDDataBench v5):
    ``'A B'`` / ``'A,B'`` / ``'A:B'`` for ``['A', 'B']``; ``'45-86'`` for
    ``['A:45-86']`` when the declaration names no chain; ``'ff99SB-ILDN'`` for
    ``'ff99SBildn'``. Anything else is returned unchanged for the strict
    comparison.
    """
    if isinstance(actual, (list, tuple)) and len(actual) > 1 and isinstance(expected, str):
        parts = [p for p in re.split(_LIST_SPLIT.get(key, r"\s*,\s*|\s+"), expected.strip()) if p]
        if len(parts) > 1:
            expected = parts
    if key in {"residue_ranges", "join_range_groups"}:
        def tokens(value):
            if not isinstance(value, (str, list, tuple)):
                raise TypeError("ranges must be strings or lists")
            items = [value] if isinstance(value, str) else value
            return sorted(part.strip() for item in items
                          for part in str(item).replace(" ", ",").split(",") if part.strip())
        try:
            if key == "residue_ranges":
                expected_tokens, actual_tokens = tokens(expected), tokens(actual)
                if (expected_tokens and all(":" not in t for t in expected_tokens)
                        and all(":" in t for t in actual_tokens)):
                    actual_tokens = sorted(t.split(":", 1)[1] for t in actual_tokens)
                return expected_tokens, actual_tokens
            def groups(value):
                return sorted(tokens(item) for item in ([value] if isinstance(value, str) else value))
            return groups(expected), groups(actual)
        except (TypeError, AttributeError):
            return expected, actual  # malformed values still fail the strict comparison
    if key in _NAME_KEYS and isinstance(expected, str) and isinstance(actual, str):
        def name(value):
            return re.sub(r"[^a-z0-9]", "", value.lower())
        return name(expected), name(actual)
    return expected, actual

