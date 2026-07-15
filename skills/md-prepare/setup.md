# MD Prepare Setup Router

`SKILL.md` contains the complete normal-path spine and pre-command gate. Do not
load a fixed baseline set or depend on a particular read order. Open only the
pages whose conditions apply:

- `[if:runtime_or_shell_setup]` `skills/common/preamble.md`
- `[if:tool_output_is_unclear]` `skills/common/tool-output.md`
- `[if:resume_shared_branch_or_failure]` `skills/common/run-loop.md`
- `[if:solvent_override_or_nondefault_regime]`
  `skills/common/solvent-regimes.md`
- `[if:structured_failure]` `skills/common/guardrail-codes.md`
- `[if:hitl]` `skills/md-prepare/checkpoints.md`
- `[if:forcefield_override_or_ligand_failure]`
  `skills/md-prepare/defaults-and-guardrails.md`
- `[if:compact_explicit_checklist]` `skills/md-prepare/happy-path.md`

- `[if:remote_generated_assembly_or_multi_candidate_source]` Source acquisition:
  `skills/md-prepare/acquisition.md`
- `[if:nontrivial_chains_or_ligands]` Inspection and chain selection:
  `skills/md-prepare/inspection-and-chains.md`
- `[if:nontrivial_prep_or_ligand]` Cleaning and merge details:
  `skills/md-prepare/prepare-complex.md`
- `[if:branch]` Mutation or supported PTMs:
  `skills/md-prepare/branches.md`
- `[if:ions]` Ion retention/exclusion by regime: `skills/md-prepare/ion-policy.md`
- `[if:caps_isotopes_nucleic_glycan]` Terminal caps, isotope exclusion,
  DNA/RNA hydrogen rebuild, glycoproteins:
  `skills/md-prepare/prep-chemistry.md`
- `[if:explicit]` Explicit water: `skills/md-prepare/explicit-water.md`
- `[if:membrane]` Membrane embedding: `skills/md-prepare/membrane.md`
- `[if:implicit]` Implicit solvent: `skills/md-prepare/implicit-water.md`
- `[if:resume]` Resume/re-entry: `skills/md-prepare/session-resume.md`

Start new work from a `study_dir`, even when the study has only one job. Each
job has one `source` node whose source bundle may contain multiple structures;
declare a `prep` source selection when needed, then branch after `prep` for
variants of that prepared system.
