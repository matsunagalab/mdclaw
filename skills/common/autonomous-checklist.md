# Autonomous Checklist

Use this compact checklist for the normal explicit-water path. If any step
returns `success: false`, stop and branch on the structured `code` field before
retrying.

## Before Running Tools

1. Read `skills/common/tool-output.md` and `skills/common/node-cli-patterns.md`.
2. Confirm the target exactly as written by the user.
3. Choose `execution_mode=autonomous` unless the user explicitly asks for
   checkpoint-by-checkpoint confirmation.
4. Run `mdclaw --list-json` when tool flags or defaults are uncertain. Do not
   scrape `--help` text for automation.

## Normal Explicit-Water Flow

1. Create and run a `source` node. Do not run schema-v3 workflow tools
   without first creating the node and then passing both `--job-dir` and
   `--node-id`.
2. Inspect molecules and decide chains / ligands from tool JSON.
3. Create and run a `prep` node with `prepare_complex`.
4. Verify the completed prep output matches the user request before solvation:
   check the prepared `merged_pdb` path from JSON/node artifacts, and if the
   user requested no ligand, confirm no ligand artifact such as
   `ligand_chemistry` is registered on the prep node.
5. Create and run a `solv` node with `solvate_structure`.
6. Run `inspect_openmm_platforms --atom-count <total_atoms> --solvent-type explicit`
   after solvation before local topology/min/eq/prod.
7. Create and run a `topo` node with `build_amber_system`; let it auto-resolve
   the completed `solv` parent's artifact, and do not pass a raw/manual PDB.
8. Hand off to `skills/md-equilibration/SKILL.md`; use `/md-equilibration`
   only as a harness shortcut. Do not auto-chain stages.

## Resume Flow

1. Run `mdclaw plan_next --job-dir <job_dir>` and branch on
   `next_action.action` (see `skills/common/run-loop.md`). Use
   `mdclaw inspect_job --job-dir <job_dir>` for a full DAG snapshot when needed.
2. For any candidate node, run `mdclaw explain_node --job-dir <job_dir> --node-id <node_id>`.
3. Continue only when `ready_to_run=true` or the reported blockers have been
   resolved.
4. If `validation.blocking_codes` is non-empty, use those codes rather than
   parsing human-readable error strings.
5. Do not rerun a completed or partially run workflow node with different
   settings. Create a new node/branch instead, so stale artifacts from the
   old attempt cannot mix with the new result.
6. Do not delete node directories as recovery. Preserve DAG evidence, inspect
   state with `inspect_job` / `explain_node`, and branch from a valid ancestor.

## Substitution Rule

Never emit angle-bracket placeholders literally. Replace `<job_dir>`,
`<node_id>`, atom counts, and parent node IDs from the latest JSON output or
from `inspect_job` / `explain_node`.
