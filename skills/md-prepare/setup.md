# MD Prepare Setup Router

Read the focused guidance pages needed for the user's request instead of
loading one large setup document.

Required baseline:

- `[required:always]` `skills/common/preamble.md`
- `[required:always]` `skills/common/tool-output.md`
- `[required:always]` `skills/common/run-loop.md`
- `[required:always]` `skills/common/solvent-regimes.md`
- `[required:always]` `skills/common/guardrail-codes.md`
- `[required:hitl]` `skills/md-prepare/checkpoints.md`
- `[required:always]` `skills/md-prepare/defaults-and-guardrails.md`

Then read by task:

- `[if:source]` Source acquisition: `skills/md-prepare/acquisition.md`
- `[if:chains_or_ligands]` Inspection and chain selection:
  `skills/md-prepare/inspection-and-chains.md`
- `[if:prep]` Initial cleaning and merge: `skills/md-prepare/prepare-complex.md`
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

## Ordered read sequence (default explicit-water run)

1. Required baseline above (preamble, tool-output, run-loop, solvent-regimes,
   guardrail-codes, defaults-and-guardrails).
2. `skills/md-prepare/explicit-water.md` for the regime defaults and the
   solvation/topology steps.
3. `skills/md-prepare/happy-path.md` as the compact source -> prep -> solv ->
   topo execution checklist.
4. Task pages above only when the request needs them (assembly, ligands, ions,
   caps/isotopes/nucleic/glycan, membrane, resume).

Start new work from a `study_dir`, even when the study has only one job. Each
job has one `source` node whose source bundle may contain multiple structures;
declare a `prep` source selection when needed, then branch after `prep` for
variants of that prepared system.
