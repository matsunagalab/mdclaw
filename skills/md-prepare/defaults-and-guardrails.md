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
- Supported crystallographic ions are retained by default for explicit-solvent
  systems. For implicit solvent, pass `--solvent-type implicit` to
  `prepare_complex` so explicit ions are removed during prep. Deliberate
  vacuum/no-solvent topologies may keep explicit ions, but they are not the
  default MD workflow.
- Do not create extra parameter artifacts for standard bare monatomic ions
  already supported by the active water XML. Default OPC covers common ions
  such as NA, CL, K, MG, CA, MN, ZN, FE/FE2, CU, CO, NI, CD, and HG. Keep them
  as ions on explicit-solvent paths and let `build_amber_system` handle them.
- Ion residue-name coverage differs by water model. If topology returns
  `unsupported_ion_for_water_model`, use a water model whose active XML
  supports the retained ion, or rename the bare ion residue to the selected
  model's supported template name when chemically equivalent (for example
  OPC `I` versus TIP3P-like `IOD`).
- If a metal is not a standard bare ion, or needs bonded/coordination-specific
  parameters, require a pre-converted OpenMM ForceField XML and route it through
  `build_openmm_system(forcefield_xml=...)`.

Guardrail handling:

- Branch on structured `code` values.
- If `pdbfixer_missing_residues_out_of_scope` appears, do not retry
  `prepare_complex` with the same source. Restart from `source` and use
  `skills/modeller-predict/SKILL.md` when a template/alignment exists, or
  `skills/boltz-predict/SKILL.md` when the sequence should be predicted
  directly.
- If `forcefield_water_blocked` appears, change the incompatible pairing rather
  than retrying.
- If ligand preparation returns `workflow_recommendation.options`, present only
  those valid options to the user.
- If `recommended_next_action = hard_fail`, stop.
- If implicit solvent is planned, pass `--solvent-type implicit` to
  `prepare_complex` so explicit ions are excluded and recorded during prep.
- If topology still returns `explicit_ions_in_implicit_solvent`, rebuild the
  prep branch with that solvent intent, use the explicit-solvent path with
  `solvate_structure`, or make a deliberate vacuum/no-solvent choice if that
  is the scientific request.
