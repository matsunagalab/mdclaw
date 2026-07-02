"""Solvation subpackage.

Cohesive helper clusters extracted from the historically monolithic
``mdclaw/solvation_server.py``:

- ``constants``: water-model maps, nucleic resname sets, membrane backend/cache
  option maps, and the water-model normalization/guardrail helpers.
- ``box``: periodic-box dimension extraction (CRYST1 / packmol ``.inp``) and the
  canonical ``box_dimensions.json`` writer.
- ``pdb_identity``: PDB residue iteration, nucleic charge-delta estimation, and
  solute identity restoration after packmol renumbering.

``mdclaw.solvation_server`` re-exports these names so existing
``from mdclaw.solvation_server import ...`` imports keep working.
"""
