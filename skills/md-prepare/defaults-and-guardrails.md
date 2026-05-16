# Preparation Defaults And Guardrails

Read `skills/common/defaults.md` first.

Preparation-specific defaults:

- Solvation mode is explicit solvent unless the user explicitly asks for
  implicit/vacuum/no-solvent or membrane handling.
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
- Supported crystallographic ions are retained by default for explicit-solvent
  systems. For implicit solvent, remove explicit ion residues before topology
  or switch the task back to explicit solvent. Deliberate vacuum/no-solvent
  topologies may keep explicit ions, but they are not the default MD workflow.
- Metal ions should be detected and parameterized explicitly when needed on
  explicit-solvent paths.

Guardrail handling:

- Branch on structured `code` values.
- If `forcefield_water_blocked` appears, change the incompatible pairing rather
  than retrying.
- If ligand preparation returns `workflow_recommendation.options`, present only
  those valid options to the user.
- If `recommended_next_action = hard_fail`, stop.
- If topology returns `explicit_ions_in_implicit_solvent`, either rebuild the
  prep branch without explicit ions for implicit solvent, use the
  explicit-solvent path with `solvate_structure`, or make a deliberate
  vacuum/no-solvent choice if that is the scientific request.
