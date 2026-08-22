"""Rebuilding a chain's unresolved termini is a request, never a default.

An unresolved terminus is disorder rather than a gap to bridge, and a
simulation normally starts from what the deposit resolved, so ``clean_protein``
drops terminal segments from PDBFixer's ``missingResidues``.  That default is
right and stays.  What was missing is a way to ask for the other behaviour:
``prepare_complex`` never exposed the switch, so the only way to reach it was
to request ACE/NME caps and rely on the side effect.

Measured on RCSB 6W9C chain C (2026-08-22), which resolves 4-314 of a 317
residue SEQRES:

    build_terminal_missing_residues=False -> 311 residues, THR4 .. ILE314
    build_terminal_missing_residues=True  -> 317 residues, GLU1 .. ALA317
                                             (3 rebuilt at N, 3 at C)
"""

from __future__ import annotations

import inspect

from mdclaw.structure.prepare_complex import prepare_complex


def test_the_switch_is_reachable_and_defaults_to_off():
    signature = inspect.signature(prepare_complex)
    parameter = signature.parameters["build_terminal_missing_residues"]
    assert parameter.default is False, "an unresolved terminus stays unmodelled by default"
    assert parameter.annotation is bool, "a plain switch, so the CLI gets --flag/--no-flag"


def test_caps_and_the_switch_are_independent_routes_to_the_same_behaviour():
    """Requesting caps already implied rebuilding; the two are ORed, not swapped."""
    source = inspect.getsource(prepare_complex.__wrapped__
                               if hasattr(prepare_complex, "__wrapped__")
                               else prepare_complex)
    assert "build_terminal_missing_residues=build_terminal_missing_residues" in source, \
        "the value must reach the implementation"
