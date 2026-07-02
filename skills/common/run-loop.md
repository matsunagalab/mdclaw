# Run Loop

This is the single canonical loop every stage skill follows to advance a job.
It merges the per-step loop, the node CLI invariants, and the
explicit-water/resume checklists that used to live in separate pages. The
source of truth is the study plan plus the per-job DAG evidence, not a separate
next-step planner. Use tool JSON to inspect state, create nodes, and validate
candidate nodes before running them.

## The Loop

1. **Inspect the job DAG.**

   ```bash
   mdclaw inspect_job --job-dir <job_dir>
   ```

   Read `params.solvent_regime`, `nodes`, `leaf_nodes`, `pending_nodes`,
   `running_nodes`, `failed_nodes`, `claims`, and `open_needs`. The current stage
   skill plus study plan decide which node type/tool to create or run. If a
   relevant node is already `running`, keep monitoring or explain that node; do
   not create a sibling retry unless the running node fails, the user asks for an
   explicit branch, or a tool result recommends superseding it.

2. **Create the node (parents resolve themselves).**

   ```bash
   mdclaw create_node --job-dir <job_dir> --node-type <next_node_type>
   ```

   When you omit `--parent-node-ids`, `create_node` auto-attaches the single
   completed frontier node of the correct parent type and reports it as
   `auto_resolved_parent`. Only pass `--parent-node-ids` explicitly when you are
   branching (replicates, mutations, multi-parent analyze) or when `inspect_job`
   shows an ambiguous frontier. `create_node` returns the new `node_id`; use that
   exact value next. Never copy a literal example node ID into a real command.

3. **Validate the node before running it.**

   ```bash
   mdclaw explain_node --job-dir <job_dir> --node-id <node_id>
   ```

   Run the stage tool only when `ready_to_run=true` and there are no
   `validation.blocking_codes` or `missing_inputs`. If parents are running,
   failed, or pending, wait, repair, or branch from a valid ancestor instead of
   re-running the blocked child.

4. **Run the stage tool with node context.**

   ```bash
   mdclaw --job-dir <job_dir> --node-id <new_node_id> <suggested_tool> ...
   ```

   Workflow tools require both `--job-dir` and `--node-id`. Running them without
   node context returns `code=node_context_required` (structured JSON, not a
   shell error) — create the node first, then run the tool. Let the tool
   auto-resolve ancestor artifacts (topology XML triple, restart state,
   trajectories); do not wire those paths by hand.

5. **On failure, branch on `code`.**

   Use the stable `code` field (see `skills/common/guardrail-codes.md`). Never
   parse stderr or human messages. Do not rerun a completed or partially run node
   with different settings — create a new node/branch so stale artifacts cannot
   mix with the new result. Preserve scientific invariants from the user request
   or study plan when branching: target molecules, chain/ligand selections,
   stoichiometry or ratios, solvent regime, and force-field intent. Retry
   branches may change search or packing controls such as random seed, packing
   budget, or recommended buffer/box expansion; do not silently simplify the
   scientific target. If the tool output does not make the next action clear:

   ```bash
   mdclaw trace_failure --job-dir <job_dir> --node-id <failed_node_id>
   ```

   Follow `recovery_options` / `next_commands` from that read-only trace; it
   explains which completed ancestor should be used for an explicit branch.

## Node CLI Invariants

- Create the workflow node first, then run the mutating tool with both
  `--job-dir` and `--node-id`. Do not try a bare workflow command and add node
  context after it fails; a bare call returns `code=node_context_required`.
- Never pass `--node-id` without `--job-dir`.
- Let workflow tools auto-resolve ancestor artifacts. For topology tools this is
  mandatory in normal workflows: build from the completed `solv` parent for
  explicit/membrane systems or the completed `prep` parent for implicit/vacuum
  systems. Do not pass a raw/manual PDB into topology generation, and do not hand
  wire restart state or trajectory paths.
- Start new scientific work from a `study_dir`; a simple run can use one job such
  as `jobs/main`. One job DAG has exactly one `source` node, but that node may
  hold a bundle with multiple candidate structures under `artifacts/candidates/`.
  Use `list_source_candidates` before asking the user to choose, and pass an
  explicit `prepare_complex` selector when the bundle has more than one candidate.
- Treat completed node artifacts as immutable evidence for one attempted
  parameter set. If a chain/ligand/solvent choice was wrong, create a new
  node/branch instead of rerunning the same node with changed inputs.
- Never remove node directories with `rm -rf` as normal recovery. Preserve
  `node.json`, artifacts, and events; use `inspect_job` / `explain_node` to pick
  the next valid branch.
- Use `mdclaw --list-json` when unsure about flags, defaults, or whether a tool
  requires node context. Do not scrape `--help` text for automation.

## Normal Explicit-Water Flow

Use this compact checklist for the default explicit-water path. If any step
returns `success: false`, stop and branch on the structured `code` before
retrying.

1. Confirm the target exactly as written by the user, and choose
   `execution_mode=autonomous` unless the user asked for checkpoint-by-checkpoint
   confirmation.
2. Create and run a `source` node.
3. Inspect molecules and decide chains / ligands from tool JSON.
4. Create and run a `prep` node with `prepare_complex`. Verify the completed prep
   output matches the request before solvation (check the prepared `merged_pdb`;
   if the user requested no ligand, confirm no `ligand_chemistry` artifact was
   registered).
5. Create and run a `solv` node with `solvate_structure`.
6. Run `inspect_openmm_platforms --atom-count <total_atoms> --solvent-type explicit`
   before local topology/min/eq/prod.
7. Create and run a `topo` node with `build_amber_system`; let it auto-resolve the
   completed `solv` parent's artifact.
8. Hand off to `skills/md-equilibration/SKILL.md` (harness shortcut:
   `/md-equilibration`). Do not auto-chain stages.

## Re-entry And Resume

Coming back to an existing job is the same loop: start with `inspect_job`, then
`explain_node` on any candidate node before running it. Continue only when
`ready_to_run=true` or the reported `validation.blocking_codes` have been
resolved. Do not rerun a completed/partial node with different settings and do
not delete node directories; branch from a valid ancestor instead.

## Working A Shared Job (Multiple Agents)

A job DAG is collaborative: another agent may have advanced it earlier, may be
running a node now, or may resume it later. `inspect_job` gives the shared state
snapshot but does not take or check a lease.

- Before working a node in a shared job, take a lease with `claim_node`, and
  `release_node_claim` when done. Sealed (completed) nodes are immutable: branch a
  new node rather than mutating one.
- For the full collaboration picture (claims, open needs, attempted nodes), use
  `mdclaw inspect_job --job-dir <job_dir>`.

## Substitution Rule

Never emit angle-bracket placeholders literally. Resolve `<job_dir>`,
`<node_id>`, parent IDs, atom counts, and numeric values from the latest tool
JSON or from `inspect_job` / `explain_node` before running a command.
