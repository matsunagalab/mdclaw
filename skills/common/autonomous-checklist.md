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

1. Create and run a `source` node.
2. Inspect molecules and decide chains / ligands from tool JSON.
3. Create and run a `prep` node with `prepare_complex`.
4. Create and run a `solv` node with `solvate_structure`.
5. Create and run a `topo` node with `build_amber_system`.
6. Hand off to `skills/md-equilibration/SKILL.md`; use `/md-equilibration`
   only as a harness shortcut. Do not auto-chain stages.

## Resume Flow

1. Run `mdclaw inspect_job --job-dir <job_dir>`.
2. For any candidate node, run `mdclaw explain_node --job-dir <job_dir> --node-id <node_id>`.
3. Continue only when `ready_to_run=true` or the reported blockers have been
   resolved.
4. If `validation.blocking_codes` is non-empty, use those codes rather than
   parsing human-readable error strings.

## Substitution Rule

Never emit angle-bracket placeholders literally. Replace `<job_dir>`,
`<node_id>`, atom counts, and parent node IDs from the latest JSON output or
from `inspect_job` / `explain_node`.
