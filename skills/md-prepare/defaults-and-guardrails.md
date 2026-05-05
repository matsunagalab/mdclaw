# Preparation Defaults And Guardrails

Read `skills/common/defaults.md` first.

Preparation-specific defaults:

- pH-aware protein protonation through `clean_protein`.
- Standard DNA/RNA are preserved as nucleic polymers, not treated as ligands.
- Glycan residues are preserved and passed to GLYCAM-aware topology generation.
- Ligands use curated amber_geostd parameters when available, then GAFF2
  fallback when appropriate.
- Metal ions should be detected and parameterized explicitly when needed.

Guardrail handling:

- Branch on structured `code` values.
- If `forcefield_water_blocked` appears, change the incompatible pairing rather
  than retrying.
- If ligand preparation returns `workflow_recommendation.options`, present only
  those valid options to the user.
- If `recommended_next_action = hard_fail`, stop.
