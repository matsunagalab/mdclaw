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
- Ligands are cleaned into `ligand_chemistry`. A charged SMILES/SDF fixes the
  molecular state; an expected `net_charge` selects a matching protonation
  candidate but never changes the molecular graph by itself.
- Ion retention/exclusion by regime, water-model ion coverage, and the
  metal-site XML policy: `skills/common/solvent-regimes.md`.

Guardrail handling:

- Branch on structured `code` values.
- Missing-residue repair defaults to `auto`: PDBFixer handles short gaps and MODELLER handles larger gaps when its package and license key are present.
- For `missing_residues_require_modeller_license`, export the key, then create a
  sibling prep node with the failed node's same completed parent:
  ```bash
  export KEY_MODELLER10v8=<your license key>
  mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids <completed_parent_node_id>
  mdclaw --job-dir <job_dir> --node-id <new_prep_node_id> prepare_complex
  ```
- `pdbfixer_missing_residues_out_of_scope` now means the caller deliberately
  pinned `pdbfixer`; use the same sibling-node recovery with
  `--missing-residue-method modeller`, or keep the strict failure.
- If `forcefield_water_blocked` appears, change the incompatible pairing rather
  than retrying.
- If ligand preparation returns `workflow_recommendation.options`, present only
  those valid options to the user.
- If `recommended_next_action = hard_fail`, stop.
- If topology returns `explicit_ions_in_implicit_solvent`, rebuild the prep
  branch with `--solvent-type implicit`, switch to the explicit path with
  `solvate_structure`, or make a deliberate vacuum choice if that is the
  scientific request.
