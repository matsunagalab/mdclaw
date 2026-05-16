# Preparation Defaults And Guardrails

Read `skills/common/defaults.md` first.

Preparation-specific defaults:

- pH-aware protein protonation through `clean_protein`.
- User-specified residue protonation states use `protonation_states`, e.g.
  `{"A:57": "HIP", "A:25": "ASH"}` or a list of `{chain, resnum, state}`
  records. Supported Amber variants include ASP/ASH, GLU/GLH, HID/HIE/HIP,
  LYS/LYN, and CYS/CYX/CYM.
- Standard DNA/RNA are preserved as nucleic polymers, not treated as ligands.
- Glycan residues are preserved and passed to GLYCAM-aware topology generation.
- Ligands are cleaned into `ligand_chemistry` records during prep; topology
  generation resolves Amber geostd first and uses `GAFFTemplateGenerator` for
  remaining ligands.
- Metal ions should be detected and parameterized explicitly when needed.

Guardrail handling:

- Branch on structured `code` values.
- If `forcefield_water_blocked` appears, change the incompatible pairing rather
  than retrying.
- If ligand preparation returns `workflow_recommendation.options`, present only
  those valid options to the user.
- If `recommended_next_action = hard_fail`, stop.
