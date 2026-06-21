# Run Loop

This is the loop every stage skill follows to advance a job. The source of
truth is the study plan plus the per-job DAG evidence, not a separate next-step
planner. Use tool JSON to inspect current state, create nodes, and validate
candidate nodes before running them.

## The loop

1. **Inspect the job DAG.**

   ```bash
   mdclaw inspect_job --job-dir <job_dir>
   ```

   Read `params.solvent_regime`, `nodes`, `leaf_nodes`, `pending_nodes`,
   `running_nodes`, `failed_nodes`, `claims`, and `open_needs`. The current stage
   skill plus study plan decide which node type/tool to create or run.

2. **Create the node (parents resolve themselves).**

   ```bash
   mdclaw create_node --job-dir <job_dir> --node-type <next_node_type>
   ```

   When you omit `--parent-node-ids`, `create_node` auto-attaches the single
   completed frontier node of the correct parent type and reports it as
   `auto_resolved_parent`. Only pass `--parent-node-ids` explicitly when you are
   branching (replicates, mutations, multi-parent analyze) or when `inspect_job`
   shows an ambiguous frontier. Never copy a literal example node ID into a real
   command — use the IDs from `inspect_job`, `explain_node`, and `create_node`
   output.

   `create_node` returns the new `node_id`. Use that exact value next.

3. **Validate an existing or newly created node.**

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
   parse stderr or human messages. Do not rerun a completed or partially run
   node with different settings — create a new node/branch so stale artifacts
   cannot mix with the new result.

## Re-entry

Coming back to an existing job is the same loop: start with `inspect_job`, then
use `explain_node` on any candidate node before running it.

## Working a shared job (multiple agents)

A job DAG is collaborative: another agent may have advanced it earlier, may be
running a node right now, or may resume it later. `inspect_job` gives the shared
state snapshot but does not take or check a lease, so it does not replace the
coordination tools.

- Before working a node in a shared job, take a lease with `claim_node`, and
  `release_node_claim` when done. Sealed (completed) nodes are immutable: branch
  a new node rather than mutating one.
- For the full collaboration picture (claims, open needs, attempted nodes), use
  `mdclaw inspect_job --job-dir <job_dir>`.
