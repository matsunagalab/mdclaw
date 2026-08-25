# Run Loop

MDClaw CLI manages each job as a DAG: it records node state and resolves parent
artifacts automatically. The agent does not move files between workflow nodes.
Inspect once on entry, then use `create -> explain -> run` for each stage. Copy
files only when the user or an external submission contract requires an export.

## Decide How Far To Go

Decide where the current work stops from the latest explicit user or harness
request before creating a node. Do not store that stopping point in
`study_plan.json` or `progress.json`.

| Current request | Stop after |
|---|---|
| Plan, review, or inspect only | Record the plan or report DAG state; do not run a stage tool |
| Names preparation, equilibration, production, or analysis as the last stage | Complete that stage for the required job(s) |
| Asks to run or simulate MD without requesting analysis or a conclusion | Complete the requested production run |
| Asks to answer the scientific question, compare, conclude, or provide evidence | Complete the required planned jobs and analyses, package evidence, and return an evidence-backed answer |
| Resumes existing work for a stated purpose | Inspect the DAG, reuse completed artifacts, and stop when the current purpose is met |

When several stages are named, the last named stage is the stopping point. If
the request is unclear, plan or inspect and report the current state; do not
start a new compute stage. At every stage boundary, continue to the next skill
only when the current request requires it.

The study plan describes scientific intent, and its `workflow_steps` do not
authorize execution. `execution_mode` controls confirmation pauses only:
`autonomous` skips routine confirmations within the requested work, while
`human_in_the_loop` pauses at major checkpoints without changing the stopping
point. An explicit mode in the current request overrides a stored mode;
otherwise inherit the stored mode. Neither mode authorizes HPC/SLURM
submission; the current request or harness must do that explicitly.

For a scientific-answer request, verify the required `prod` and `analyze`
nodes with `inspect_job`; do not treat an evidence report's status alone as
proof that the study is complete. Keep monitoring required local work. If
required work remains queued or running externally, preserve and report the
DAG handoff instead of claiming a scientific answer.

## The Loop

1. **Inspect when entering or disambiguating a job.**

   ```bash
   mdclaw inspect_job --job-dir <job_dir>
   ```

   Run `inspect_job` immediately after bootstrap, when re-entering an existing
   job, before working a shared job, or before choosing among ambiguous branch
   parents. Read `params.solvent_regime`, `nodes`, `leaf_nodes`, `pending_nodes`,
   `running_nodes`, `failed_nodes`, and `open_needs`. If a relevant
   node is already `running`, keep monitoring or explain that node; do not create
   a sibling retry unless it fails, the user requests a branch, or a tool result
   recommends superseding it. During one fresh, unambiguous serial run, do not
   repeat `inspect_job` before every node; the state-changing core is
   `create_node` -> `explain_node` -> stage tool.

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

   For `prep`, `solv`, `topo`, `min`, `eq` and `prod`, `--conditions` is
   optional and each key is a contract: the stage tool must report that key
   back with a concrete value or the node fails and is spent. Which keys a tool
   reports is narrower than its parameter list and is not visible in
   `--list-json`, so declare sparingly and keep other intent in `--label` or
   the study plan; when a node does fail this way the error lists the keys that
   invocation did cross-check. `analyze` is the exception — it requires
   `analysis_data_scope` at creation and has no runtime cross-check, so follow
   `skills/md-analyze/SKILL.md`.

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

   On a batch cluster, `min`, `eq` and `prod` are *submitted*, not run here:
   follow `skills/hpc-run/SKILL.md` and use `submit_job`, which owns the sbatch
   script and the `--dependency` flag. Do not hand-write sbatch files.

5. **Report what ran, before moving on.**

   `autonomous` skips confirmations, not reporting. After each stage tool
   returns, give the user one short block — do not wait for the end of the job:

   - the node ID, the stage, and whether it completed;
   - the conditions the tool actually used, read from its `parameters` (not from
     what you asked for): for `min` the minimizer and step/force tolerance; for
     `eq` and `prod` the ensemble, temperature, pressure, timestep, total time,
     thermostat/barostat, constraints, cutoff, and restraints; for `solv` the
     box dimensions, water model, ion species and concentration, and lipid
     composition;
   - the system size (atom count, box, net charge) once it exists;
   - any `warnings` the result carries.

   Render a preview only if the user asked for one, per
   `skills/common/visual-qa.md`. Reporting the numbers above is the
   report; a picture is not part of it unless it was requested.

6. **On failure, follow the structured result.**

   Follow `code` and `next_action` as described in
   `skills/common/tool-output.md`. If a failed node has no clear recovery action,
   run:

   ```bash
   mdclaw trace_failure --job-dir <job_dir> --node-id <failed_node_id>
   ```

   Follow its `next_commands`. Never rerun a terminal node or replace an MDClaw
   stage with a hand-written workflow.

## Node CLI Invariants

- Never pass `--node-id` without `--job-dir`.
- Do not pass artifact paths between nodes. Workflow tools resolve inputs from
  completed DAG ancestors.
- Start new scientific work from a `study_dir`; a simple run can use one job such
  as `jobs/main`. One job DAG has exactly one `source` node, but that node may
  hold a bundle with multiple candidate structures under `artifacts/candidates/`.
  Use `list_source_candidates` before asking the user to choose, and pass an
  explicit `prepare_complex` selector when the bundle has more than one candidate.
- Treat terminal node artifacts as immutable evidence for one attempted
  parameter set. Continue from completed nodes; branch from the same completed
  parent after failed nodes.
- Never remove node directories with `rm -rf` as normal recovery. Preserve
  `node.json`, artifacts, and events; use `inspect_job` / `explain_node` to pick
  the next valid branch.
- Do not scan the global registry with bare `mdclaw --list`; the active skill
  names the normal-path tools. Check a tool's signature with targeted
  `mdclaw --list-json <tool>`. Only if it is insufficient, read the full
  `mdclaw <tool> --help` before running the node. Previewing output is fine;
  before concluding that a tool or parameter is unavailable, confirm with
  `mdclaw --list-json <tool>`.

The prepare-stage specialization of this loop is the `skills/md-prepare/SKILL.md`
workflow; equilibration and production apply the loop above directly.

## Re-entry And Resume

On re-entry, start with `inspect_job`, then resume the loop from the relevant
node. Run a candidate only when `explain_node` reports `ready_to_run=true`.

## Working A Shared Job (Multiple Agents)

A job DAG is collaborative: another agent may have advanced it earlier, may be
running a node now, or may resume it later. `inspect_job` gives the shared state
snapshot but does not take or check a lease.

- Terminal nodes are immutable; create a new node instead of reworking one.
- For the full collaboration picture (open needs, attempted nodes), use
  `mdclaw inspect_job --job-dir <job_dir>`.

## Substitution Rule

Never emit angle-bracket placeholders literally. Resolve `<job_dir>`,
`<node_id>`, parent IDs, atom counts, and numeric values from the latest tool
JSON or from `inspect_job` / `explain_node` before running a command.
