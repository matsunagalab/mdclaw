# MD Prepare Happy Path (Explicit Water)

The compact complete checklist for the default explicit-water preparation.
It is the prepare-stage specialization of `skills/common/run-loop.md`; each
numbered item below is one turn of that loop (inspect -> create -> explain ->
run). If any step returns `success: false`, stop and branch on the structured
`code` before retrying.

1. Confirm the target exactly as written by the user, and choose
   `execution_mode=autonomous` unless the user asked for
   checkpoint-by-checkpoint confirmation. (See `## Step 0` in `SKILL.md`.)
2. Create and run a `source` node.
3. Inspect molecules and decide chains / ligands from tool JSON (Step 0b).
4. Create and run a `prep` node with `prepare_complex`. Verify the completed
   prep output matches the request before solvation: check the prepared
   `merged_pdb`; if the user requested no ligand, confirm no `ligand_chemistry`
   artifact was registered.
5. Create and run a `solv` node with `solvate_structure` (skip for
   implicit/vacuum; use `embed_in_membrane` for membrane).
6. Run the platform preflight before local topology/min/eq/prod:
   `inspect_openmm_platforms --atom-count <total_atoms> --solvent-type explicit`
   (see `skills/common/solvent-regimes.md`).
7. Create and run a `topo` node with `build_amber_system`; let it auto-resolve
   the completed `solv` parent's artifact.
8. Treat topology-time minimization as initial relaxation only. If the request
   requires a minimized/relaxed state or post-minimization artifact, create a
   `min` node, run `run_minimization`, and stop before equilibration unless it
   was requested. Otherwise report the `min` handoff and stop.

For implicit or vacuum regimes, skip steps 5-6 and follow
`skills/md-prepare/implicit-water.md`.
