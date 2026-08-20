# Preparation Defaults And Guardrails

`skills/common/solvent-regimes.md` is the detailed source for regime mapping
and explicit-water constants (`ff19SB + opc`, 15 Å buffer, 0.15 M NaCl,
300 K / 1 bar, HMR 4 fs). Open it when those details are needed.

Preparation-specific defaults:

- Solvent regime is study-level intent. Use the `solvent_regime` recorded by
  `bootstrap_md_workflow` or richer `md-study` planning. For minimal direct
  runs, default to explicit solvent unless the user explicitly asks for
  implicit/vacuum/no-solvent or membrane handling; the bootstrap records the
  chosen regime before `prepare_complex`.
- pH-aware protein protonation through `clean_protein`.
- User-specified residue protonation states use `protonation_states`, e.g.
  `{"A:57": "HIP", "A:25": "ASH"}` or a list of `{chain, resnum, state}`
  records. Supported Amber variants include ASP/ASH, GLU/GLH, HID/HIE/HIP,
  LYS/LYN, and CYS/CYX/CYM.
- Standard DNA/RNA are preserved as nucleic polymers, not treated as ligands.
- Glycan residues are preserved and passed to GLYCAM-aware topology generation.
- Ligands are cleaned into `ligand_chemistry`; charge comes from charged
  SMILES/SDF, not a detached integer.
- Ion retention/exclusion by regime, water-model ion coverage, and the
  metal-site XML policy: `skills/common/solvent-regimes.md`.

Guardrail handling:

- Branch on structured `code` values.
- If `pdbfixer_missing_residues_out_of_scope` appears, the failed prep node is
  sealed. Create a sibling with the same completed parent and repair there:
  ```bash
  mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids <completed_parent_node_id>
  mdclaw --job-dir <job_dir> --node-id <new_prep_node_id> prepare_complex --missing-residue-method modeller
  ```
  Export `KEY_MODELLER*`. Restart from `source` only when the source itself is
  wrong; then use `modeller-predict` or `boltz-predict`.
- If `forcefield_water_blocked` appears, change the incompatible pairing rather
  than retrying.
- If ligand preparation returns `workflow_recommendation.options`, present only
  those valid options to the user.
- If `recommended_next_action = hard_fail`, stop.
- If topology returns `explicit_ions_in_implicit_solvent`, rebuild the prep
  branch with `--solvent-type implicit`, switch to the explicit path with
  `solvate_structure`, or make a deliberate vacuum choice if that is the
  scientific request.
