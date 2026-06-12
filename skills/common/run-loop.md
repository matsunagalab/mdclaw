# Run Loop

This is the single loop every stage skill follows to advance a job. It removes
the three judgement calls that most often go wrong: *what node comes next*,
*which parent to attach it to*, and *is it ready to run*. Let the tools answer
those instead of inferring them from prose or copying example IDs.

## The loop

1. **Ask what to do next.**

   ```bash
   mdclaw plan_next --job-dir <job_dir>
   ```

   Read `next_action.action` and branch on it:

   | `action` | What to do |
   |----------|------------|
   | `create_source` | Create the `source` node and acquire a structure (see md-prepare). |
   | `create_and_run` | Create `next_action.node_type`, then run `next_action.suggested_tool`. |
   | `run_existing` | A node already exists (`existing_node_id`). Run its tool; do not create a duplicate. |
   | `wait_running` | A node is running (`running_node_ids`). Wait / sync HPC before advancing. |
   | `inspect_failure` | A node failed (`failed_node_ids`). Inspect, fix the input, re-run or branch. |
   | `workflow_complete` | Production has been analyzed; stop unless a new question needs more analysis. |

   `plan_next` also returns `solvent_regime` and `next_skill`, so you do not need
   to re-derive the solvent path or guess which skill owns the next stage.

2. **Create the node (parents resolve themselves).**

   ```bash
   mdclaw create_node --job-dir <job_dir> --node-type <next_node_type>
   ```

   When you omit `--parent-node-ids`, `create_node` auto-attaches the single
   completed frontier node of the correct parent type and reports it as
   `auto_resolved_parent`. Only pass `--parent-node-ids` explicitly when you are
   branching (replicates, mutations, multi-parent analyze) or when `plan_next`
   reports an ambiguous frontier. Never copy an example ID such as `topo_001`
   into a real command — use the IDs from `plan_next` / `create_node` output.

   `create_node` returns the new `node_id`. Use that exact value next.

3. **Run the stage tool with node context.**

   ```bash
   mdclaw --job-dir <job_dir> --node-id <new_node_id> <suggested_tool> ...
   ```

   Workflow tools require both `--job-dir` and `--node-id`. Running them without
   node context returns `code=node_context_required` (structured JSON, not a
   shell error) — create the node first, then run the tool. Let the tool
   auto-resolve ancestor artifacts (topology XML triple, restart state,
   trajectories); do not wire those paths by hand.

4. **Read the `workflow_hint`.**

   Every successful workflow tool (and `create_node`) appends a `workflow_hint`
   block — the same recommendation `plan_next` would give next. Use it to chain
   to the following step without a separate `plan_next` call.

5. **On failure, branch on `code`.**

   Use the stable `code` field (see `skills/common/guardrail-codes.md`). Never
   parse stderr or human messages. Do not rerun a completed or partially run
   node with different settings — create a new node/branch so stale artifacts
   cannot mix with the new result.

## Re-entry

Coming back to an existing job is the same loop: start at step 1 with
`plan_next`. For a specific candidate node, `mdclaw explain_node --job-dir
<job_dir> --node-id <node_id>` reports `ready_to_run`, `missing_inputs`, and
`validation.blocking_codes`.

## Working a shared job (multiple agents)

A job DAG is collaborative: another agent may have advanced it earlier, may be
running a node right now, or may resume it later. `plan_next` is *advisory* — it
recommends the next step but does not take or check a lease, so two agents can
receive the same recommendation. It does not replace the coordination tools.

- Before working a node in a shared job, take a lease with `claim_node`, and
  `release_node_claim` when done. Sealed (completed) nodes are immutable: branch
  a new node rather than mutating one.
- `plan_next` now surfaces coordination state so you can act on it:
  - top-level `coordination.claims` / `coordination.open_needs` mirror
    `inspect_job` — a job-wide snapshot of who holds leases and which nodes have
    open needs.
  - `run_existing` includes `next_action.claim`; if it is active, `warnings`
    flags that another agent likely holds the node — coordinate or branch a
    variant instead of running it.
  - `wait_running` includes `next_action.claims` for the running nodes.
- For the full collaboration picture (claims, open needs, attempted nodes), use
  `mdclaw inspect_job --job-dir <job_dir>`.
