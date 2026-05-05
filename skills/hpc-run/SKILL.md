---
name: HPC Run
description: "SLURM-based HPC submission for MDClaw workflow nodes. Handles cluster inspection, single-node and job-array submission, status sync to the DAG, and production restart extensions. Use whenever an equilibration or production node (or a batch of them across multiple systems) should run on a compute cluster instead of the login node."
---

# HPC Run Skill

You are an HPC workflow specialist managing SLURM batch jobs for the MDClaw
node-based workflow. A SLURM submission in this skill always maps 1:1 to a
DAG node (`eq_XXX`, `prod_XXX`, …) and is tracked through `node.json`. When
multiple nodes need to run in parallel — for example `prod_001..prod_N` from
the same `eq_001`, or one `prod_001` per system across a set of job
directories — pick the shape that matches the campaign: `submit_array_job`
for small batches of homogeneous, low-failure tasks (Step 3), or per-entry
`submit_job` + `afterok` chains for larger campaigns where some upstream
failures are expected (Step 3.5). The two patterns are not interchangeable
at scale — see the "failure isolation" note in Step 3.5.

Respond in the user's language. Use English for tool parameter values. All
MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON
on stdout.

---

## When to Use This Skill

Trigger this skill when the user:
- Wants to run equilibration or production on SLURM instead of interactively.
- Asks for multiple replicates / multiple systems to run in parallel via
  SLURM.
- Needs to monitor, restart, or extend an MD SLURM job.
- Asks about cluster discovery, resource policy, or Singularity container
  setup for SLURM.

Structure preparation (`prepare_complex`, `solvate_structure`,
`build_amber_system`) remains a **login-node / interactive** step. It is
fast, does not need GPUs, and creates the `topo` node that the SLURM jobs
downstream depend on.

---

## Prerequisites

1. SLURM commands available (`sinfo`, `sbatch`, `squeue`, `sacct`).
2. A schema-v3 job directory with a completed `topo` node (from
   `/md-prepare`). SLURM jobs for `eq` / `prod` auto-resolve their input
   files (parm7, rst7, restart state) from DAG ancestors — do not pass
   them manually.
3. (Optional) Singularity SIF configured via `configure_container`.

---

## Step 1: Cluster Discovery

```bash
mdclaw inspect_cluster
```

Creates / updates `.mdclaw_cluster.json` with partitions, GPU types, time
limits, and memory per node. If the file already exists, read it instead
of re-running.

Optionally, lock in a resource policy — `submit_job` / `submit_array_job`
validate against it before sbatch-ing:

```bash
mdclaw set_policy \
  --allowed-partitions gpu \
  --max-gpus-per-job 1 \
  --max-time-limit "24:00:00" \
  --max-memory "128G" \
  --default-partition gpu
```

For Singularity-based clusters (recommended, matches the local MDClaw
container), configure the SIF once:

```bash
mdclaw configure_container \
  --image /abs/path/to/mdclaw.sif \
  --extra-flags "--nv"
```

`submit_job` and `submit_array_job` then wrap each task's command with
`singularity exec --nv --bind <job_dir> <image> <cmd>` automatically. The
per-task `--job-dir` is auto-extracted as a bind path, so nodes/
subdirectories are writable from the compute node.

---

## Step 2: Single-Node Submission (`submit_job`)

Use this when one DAG node runs as one SLURM job. The canonical pattern:

```bash
JD=$(realpath job_4m3j_B)

# 1. Create the eq node on the login node (cheap)
mdclaw create_node --job-dir $JD --node-type eq --parent-node-ids topo_001 \
  --label 300K --conditions '{"temperature_kelvin":300,"pressure_bar":1.0}'

# 2. Submit it to SLURM, linking the SLURM job to the node
mdclaw submit_job \
  --job-dir $JD --node-id eq_001 \
  --script "mdclaw --job-dir $JD --node-id eq_001 run_equilibration \
    --temperature-kelvin 300 --pressure-bar 1.0 --platform CUDA" \
  --job-name eq_4m3j_B \
  --partition gpu --gpus 1 --cpus-per-task 4 \
  --time-limit "00:30:00" --memory "32G"
```

On successful `sbatch`, the tool:
- Stamps `metadata.slurm_job_id` (plus script/log paths) onto
  `nodes/eq_001/node.json`.
- Advances `node.status = "queued"` (progress.json index kept in sync).
- Appends a row to `.mdclaw_jobs.jsonl` that links `slurm_job_id ↔ job_dir
  ↔ node_id`.

**Do not** pass `--prmtop-file` / `--inpcrd-file` / `--restart-from` in the
inner `mdclaw` command — DAG auto-resolution handles all three (topology
from the `topo` ancestor, restart state from the `eq` or named `prod`
ancestor). The compute node runs the same CLI the login node would;
`--job-dir` is what makes the resolution work.

### eq → prod dependency chain (single system)

```bash
JD=$(realpath job_4m3j_B)

mdclaw create_node --job-dir $JD --node-type eq --parent-node-ids topo_001 \
  --label 300K --conditions '{"temperature_kelvin":300,"pressure_bar":1.0}'
mdclaw create_node --job-dir $JD --node-type prod --parent-node-ids eq_001 \
  --label 100ns --conditions '{"simulation_time_ns":100}'

EQ_ID=$(mdclaw submit_job \
  --job-dir $JD --node-id eq_001 \
  --script "mdclaw --job-dir $JD --node-id eq_001 run_equilibration \
    --temperature-kelvin 300 --pressure-bar 1.0 --platform CUDA" \
  --job-name eq_4m3j_B --partition gpu --gpus 1 \
  --time-limit "00:30:00" --memory "32G" 2>/dev/null | jq -r .slurm_job_id)

mdclaw submit_job \
  --job-dir $JD --node-id prod_001 \
  --script "mdclaw --job-dir $JD --node-id prod_001 run_production \
    --simulation-time-ns 100 --temperature-kelvin 300 --pressure-bar 1.0 \
    --output-frequency-ps 100 --platform CUDA" \
  --dependency "afterok:$EQ_ID" \
  --job-name prod_4m3j_B --partition gpu --gpus 1 \
  --time-limit "24:00:00" --memory "32G"
```

`--dependency "afterok:<eq_id>"` tells SLURM the prod job only starts after
the eq job exits 0. Inside the prod script, `run_production` walks the DAG
upward, finds `eq_001`'s `equilibrated.chk`, and uses it as
`--restart-from` automatically.

---

## Step 3: Multi-Node Parallel (`submit_array_job`)

When you have N independent DAG nodes to run in parallel — for example, one
`prod_001` per system across 3 job directories, or N replicate prods from
the same `eq_001` — use `submit_array_job`. It emits a single sbatch script
with `#SBATCH --array=0-(N-1)` and a case-statement dispatcher keyed on
`$SLURM_ARRAY_TASK_ID`. Each array task is 1:1 with a DAG node.

### Fan out across systems (3 antibodies, 1 prod each)

```bash
JDS=(job_4m3j_B job_4m3j_A job_4b50_A)

# Pre-create all prod nodes on the login node
for JD in "${JDS[@]}"; do
  mdclaw create_node --job-dir $(realpath $JD) --node-type prod \
    --parent-node-ids eq_001 --label "0.1ns" \
    --conditions '{"simulation_time_ns":0.1}'
done

# Build the tasks JSON (one entry per system)
TASKS=$(python3 -c '
import json, os, sys
jds = [os.path.realpath(p) for p in sys.argv[1:]]
print(json.dumps([
    {
        "job_dir": jd,
        "node_id": "prod_001",
        "command": (
            f"mdclaw --job-dir {jd} --node-id prod_001 run_production "
            "--simulation-time-ns 0.1 --temperature-kelvin 300 "
            "--pressure-bar 1.0 --output-frequency-ps 10 --platform CUDA"
        ),
    }
    for jd in jds
]))
' "${JDS[@]}")

mdclaw submit_array_job \
  --json-input "$(jq -n --argjson tasks "$TASKS" '{
    tasks: $tasks,
    job_name: "prod_batch",
    partition: "gpu",
    gpus: 1,
    cpus_per_task: 4,
    time_limit: "00:30:00",
    memory: "32G"
  }')"
```

Or equivalently with explicit flags (short runs):

```bash
mdclaw submit_array_job \
  --tasks "$TASKS" \
  --job-name prod_batch \
  --partition gpu --gpus 1 --cpus-per-task 4 \
  --time-limit "00:30:00" --memory "32G"
```

Result (abbreviated):

```json
{
  "success": true,
  "parent_job_id": "99999",
  "array_spec": "0-2",
  "tasks": [
    {"array_task_id": 0, "slurm_job_id": "99999_0",
     "job_dir": "/abs/job_4m3j_B", "node_id": "prod_001", ...},
    {"array_task_id": 1, "slurm_job_id": "99999_1", ...},
    {"array_task_id": 2, "slurm_job_id": "99999_2", ...}
  ],
  "script_file": "/cwd/prod_batch.sbatch"
}
```

Each child `slurm_job_id` has the SLURM `<parent>_<task>` form (matches
`squeue` / `sacct`). Every target `node.json` is stamped with its own
child id and `slurm_array_task_id`.

### Chaining an array stage after another array (eq → prod across N systems)

When you want to run N `(eq, prod)` pipelines in parallel and **N is
small (roughly ≤ a few dozen) and failures are not expected**, the
compact SLURM pattern is **array → array with `aftercorr`**. In the
documented per-task semantics, each prod array task waits for its own
eq array task (task 0 ↔ task 0, task 1 ↔ task 1, …), not the entire eq
array — keeping the systems independent even when one eq runs longer
than another.

**Caveat observed in practice.** Some SLURM installs hold
`aftercorr:<parent>_*(unfulfilled)` until the *entire* parent array
reaches a terminal state. Any task in the parent that ends up in
`DependencyNeverSatisfied` (typical when an upstream prep array had
failures) then permanently stalls every dependent child — we have seen
1147 eq-completed children release only ~10 prod tasks this way. If the
campaign is large enough that some upstream failures are likely, prefer
the per-entry pattern in **Step 3.5**; reserve this array-chain only
for small, clean N. Remediation when stuck:
`squeue -u $USER -r -h -o '%i %T %R' | awk '$3=="(DependencyNeverSatisfied)"{print $1}' | xargs -r scancel`
then let SLURM re-evaluate the parent array's terminal state.

**Always set the dependency, regardless of whether the eq array has
already finished.** You do not need to inspect eq state first — SLURM
resolves the dependency either way:

- eq still running → prod tasks stay PENDING until their matching eq
  child completes successfully.
- eq already completed → dependency is already satisfied; prod tasks
  dispatch immediately.

Skipping the dependency "because eq just finished" is a race waiting to
happen: if the two submissions overlap with eq's last task, prod can
start before that system's equilibration finishes. Always chain.

The job-id plumbing: `submit_array_job` returns `parent_job_id` (the SLURM
array parent id, e.g. `117135`). It is also stamped onto every target
node's `node.json.metadata.slurm_parent_job_id` — so you can either thread
it directly from the first call's JSON output, or read it back off any of
the eq nodes later.

```bash
# 1. Pre-create eq_001 + prod_001 nodes in every job_dir
for JD in "${JDS[@]}"; do
  ABS=$(realpath $JD)
  mdclaw create_node --job-dir $ABS --node-type eq \
    --parent-node-ids topo_001 --label 300K \
    --conditions '{"temperature_kelvin":300,"pressure_bar":1.0}'
  mdclaw create_node --job-dir $ABS --node-type prod \
    --parent-node-ids eq_001 --label 0.1ns \
    --conditions '{"simulation_time_ns":0.1}'
done

# 2. Submit the eq array — one child per system
EQ_PAYLOAD=$(python3 - <<'PY'
import json, os
jds = ["job_4m3j_B","job_4m3j_A","job_4b50_A"]
tasks = [{
    "job_dir": os.path.realpath(jd),
    "node_id": "eq_001",
    "command": (
        f"mdclaw --job-dir {os.path.realpath(jd)} --node-id eq_001 "
        "run_equilibration --temperature-kelvin 300 --pressure-bar 1.0 "
        "--platform CUDA"
    ),
} for jd in jds]
print(json.dumps({"tasks": tasks, "job_name": "eq_batch",
                  "partition": "gpu", "gpus": 1, "cpus_per_task": 4,
                  "time_limit": "00:30:00", "memory": "32G"}))
PY
)
EQ_OUT=$(mdclaw submit_array_job --json-input "$EQ_PAYLOAD")
EQ_PARENT=$(echo "$EQ_OUT" | jq -r .parent_job_id)   # e.g. "117135"

# 3. Submit the prod array with aftercorr dependency on the eq parent.
#    aftercorr documented semantics: prod_i starts after eq_i completes
#    successfully (task-for-task). See the caveat above — some installs
#    hold on the parent as a whole, so for large/lossy campaigns prefer
#    Step 3.5's per-entry afterok chain.
PROD_PAYLOAD=$(python3 - <<'PY'
import json, os
jds = ["job_4m3j_B","job_4m3j_A","job_4b50_A"]
tasks = [{
    "job_dir": os.path.realpath(jd),
    "node_id": "prod_001",
    "command": (
        f"mdclaw --job-dir {os.path.realpath(jd)} --node-id prod_001 "
        "run_production --simulation-time-ns 0.1 "
        "--temperature-kelvin 300 --pressure-bar 1.0 "
        "--output-frequency-ps 10.0 --platform CUDA"
    ),
} for jd in jds]
print(json.dumps({"tasks": tasks, "job_name": "prod_batch",
                  "partition": "gpu", "gpus": 1, "cpus_per_task": 4,
                  "time_limit": "00:30:00", "memory": "32G"}))
PY
)
mdclaw submit_array_job \
  --json-input "$(jq -n --argjson p "$PROD_PAYLOAD" --arg dep "aftercorr:$EQ_PARENT" \
                   '$p + {dependency:$dep}')"
```

Alternative dependency types (pick one per submission):
- `aftercorr:<parent>` — documented as per-task (child task *i* after
  parent task *i* exits 0). Fine for small, homogeneous N; see the
  caveat above before using it on campaigns with expected failures.
- `afterok:<parent>` — the whole dependent array waits for the
  **entire** parent array to finish successfully (every parent child
  must succeed first).
- `afterany:<parent>` — same as above but any exit code is acceptable.

For per-entry chains (Step 3.5) the same keyword `afterok:<jobid>` is
used, but `<jobid>` is a *single* SLURM job id instead of an array
parent — which is exactly what makes the chain independent.

`submit_array_job --json-input` is the only path that currently accepts
structured `tasks` on the CLI. Pass the same flag names with `--tasks
'<json>'` directly once you have the list built in your shell — CLI-level
JSON-string decoding works for list-of-dict arguments too.

## Step 3.5: Per-entry chain (`submit_job` + `afterok`) for campaigns with expected failures

For a campaign of **N independent 3-stage pipelines** (e.g., 1177
nanobody `prep → eq → prod` entries) where some upstream failures are
realistic, submit one job per (entry, stage) and chain with
`--dependency afterok:<prev_jobid>`. The key property is **failure
isolation**: each entry has its own 3-job chain, so a failed prep only
prevents *its own* eq and prod from running — the other N-1 entries
are untouched by SLURM's dependency manager.

Trade-offs vs. the array approach (Step 3):

| Aspect | Array + `aftercorr` | Per-entry `submit_job` + `afterok` |
|---|---|---|
| SLURM jobs created | 3 parents × (1…chunks) | 3 × N individual jobs |
| Submission wallclock (N=1177) | seconds | ~3–4 min per stage |
| Failure blast radius | whole dep chain can stall (caveat) | strictly per-entry |
| `MaxArraySize` chunking | required at N > 1000 | not required |
| `squeue` lines | tidy array parents | N × 3 — use tracker for summary |
| When to prefer | small, clean N | N > 100, or failures expected |

Pattern (shell for-loop; `mdclaw submit_job` returns `slurm_job_id`):

```bash
# Each manifest entry has a job_dir and a tag. Prep/eq/prod nodes are
# pre-created as usual; submit_job stamps each SLURM id into node.json.
python3 - <<'PY' > /tmp/chain_commands.sh
import json
manifest = json.load(open("nano_manifest.json"))
for e in manifest:
    jd, tag = e["job_dir"], e["tag"]
    print(f'PREP_ID=$(mdclaw submit_job '
          f'--job-dir {jd} --node-id prep_001 '
          f'--script "bash scripts/run_prep_one_imgt.sh {tag}" '
          f'--job-name prep_{tag} --partition cpu --cpus-per-task 2 '
          f'--time-limit "01:00:00" --memory "8G" '
          f'2>/dev/null | jq -r .slurm_job_id)')
    print(f'EQ_ID=$(mdclaw submit_job '
          f'--job-dir {jd} --node-id eq_001 '
          f'--script "mdclaw --job-dir {jd} --node-id eq_001 run_equilibration '
          f'--temperature-kelvin 300 --pressure-bar 1.0 --platform CUDA" '
          f'--dependency afterok:$PREP_ID '
          f'--job-name eq_{tag} --partition gpu --gpus 1 '
          f'--time-limit "02:00:00" --memory "6G" '
          f'2>/dev/null | jq -r .slurm_job_id)')
    print(f'mdclaw submit_job '
          f'--job-dir {jd} --node-id prod_001 '
          f'--script "mdclaw --job-dir {jd} --node-id prod_001 run_production '
          f'--simulation-time-ns 100 --temperature-kelvin 300 --pressure-bar 1.0 '
          f'--output-frequency-ps 100 --platform CUDA" '
          f'--dependency afterok:$EQ_ID '
          f'--job-name prod_{tag} --partition gpu --gpus 1 '
          f'--time-limit "3-00:00:00" --memory "6G"')
PY
bash /tmp/chain_commands.sh
```

What you get:
- 3 × N rows in `.mdclaw_jobs.jsonl`, each with `job_dir`, `node_id`,
  and the SLURM job id. `mdclaw list_tracked_jobs --sync` reflects
  terminal states back into `node.json.status` the same way it does
  for array tasks.
- If prep for entry *k* fails, SLURM marks the eq and prod for *k*
  as `DependencyNeverSatisfied` — only those two. Drop them with
  `squeue | awk '$NF=="(DependencyNeverSatisfied)"{print $1}' | xargs -r scancel`
  if you want them off the queue; other entries keep flowing.
- The "What not to do" rule against submit_job-in-a-loop applies only
  to **small** fan-outs that fit a single array cleanly. For large
  campaigns the per-entry chain is the robust default.

### Concurrency: default is no throttle

**Default:** do **not** pass `--max-concurrent`. Submit all N tasks as a
single array (`#SBATCH --array=0-N-1`) and let the SLURM scheduler decide
how many run concurrently based on live partition availability. With K
free GPUs on the partition, SLURM will dispatch min(N, K) tasks
immediately and queue the rest — that is strictly the fastest way to
drain a batch. Capping concurrency below what the cluster can actually
absorb only *slows* the batch down and leaves GPUs idle.

This is especially important when running many short jobs (e.g., a
fan-out of 100+ 1 ns sanity runs across the full GPU pool). Let SLURM
spread them across every node with a free GPU; don't hand-pick a
concurrency number.

```bash
# Preferred: no --max-concurrent. Submit all N tasks, let SLURM schedule.
mdclaw submit_array_job --tasks "$TASKS" \
  --partition gpu --gpus 1 --time-limit "24:00:00"
```

**When to throttle with `--max-concurrent K`** (rare, opt-in):

```bash
mdclaw submit_array_job --tasks "$TASKS" --max-concurrent 8 \
  --partition gpu --gpus 1 --time-limit "24:00:00"
```

Use this only for concrete, named reasons — **not** as a generic safety
habit. Legitimate reasons include:

- **Fair-share / quota**: you're sharing the partition and cluster
  policy tells you to cap yourself.
- **Shared I/O pressure**: all N tasks hammer the same filesystem
  (large DCD writes to a single scratch mount) and empirically thrash.
- **License limits**: a paid MD engine with a concurrent-seat license.
- **Debugging**: running just the first few to validate the sbatch
  script before unleashing the full batch.

If none of those apply, leave `--max-concurrent` off.

### Replicates from a single `eq` node

Same pattern: pre-create `prod_001..prod_N` all with `--parent-node-ids
eq_001` (optionally varying `random_seed` or `label`), then submit them
as one array. Each task's command references its own `prod_00k`:

```bash
JD=$(realpath job_target)
for i in $(seq -w 1 5); do
  mdclaw create_node --job-dir $JD --node-type prod \
    --parent-node-ids eq_001 --label "100ns_seed${i}" \
    --conditions "{\"simulation_time_ns\":100,\"random_seed\":${i}0}"
done

# tasks[k].node_id = prod_00k, tasks[k].command pins the corresponding seed
```

---

## Step 4: Monitoring

Per-job state (also updates the local tracker AND syncs node state):

```bash
mdclaw check_job --job-id <slurm_job_id>
```

`check_job` reflects SLURM state back onto the linked node:
- `RUNNING` → `node.status = "running"` (only if the node was still
  `queued`/`pending`; never demotes a `completed` node).
- `FAILED` / `TIMEOUT` / `OUT_OF_MEMORY` / `CANCELLED` → `node.status
  = "failed"`, with the SLURM state and stderr tail captured in
  `metadata.slurm_state` / `metadata.slurm_stderr_tail`.
- `COMPLETED` → node status is **not** touched. The tool running inside
  the job is the source of truth for "completed" (via its own
  `complete_node` hook). SLURM exit 0 only means the wrapper exited
  cleanly, which is a weaker signal.

Batch view:

```bash
# All tracked jobs (full history)
mdclaw list_tracked_jobs

# Sync all non-terminal rows with SLURM first, then print
mdclaw list_tracked_jobs --sync

# Filter to a single DAG / node
mdclaw list_tracked_jobs --job-dir $(realpath job_4m3j_B)
mdclaw list_tracked_jobs --job-dir $(realpath job_4m3j_B) --node-id prod_001
```

Live queue (SLURM side only):

```bash
mdclaw list_jobs
```

### Polling loop

For anything longer than a few minutes, use `/loop` to poll. Choose an
interval from the *expected runtime*:

| Expected Runtime | Interval |
|---|---|
| < 10 min | 2m |
| 10 min – 1 h | 5m |
| 1 – 6 h | 15m |
| 6 – 24 h | 30m |
| > 24 h | 1h |

```
/loop 15m mdclaw list_tracked_jobs --sync --job-dir /abs/jd
```

Stop the loop when all tracked rows are in a terminal state
(`COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`) or when the matching
DAG nodes' status is `completed` / `failed`.

---

## Step 5: Error Recovery

Diagnose by reading the job's stderr tail (also auto-captured into
`metadata.slurm_stderr_tail` on the DAG node when check_job saw the
failure):

```bash
mdclaw check_job_log --job-id <slurm_job_id> --log-type stderr --tail-lines 100
mdclaw check_job_log --job-id <slurm_job_id> --log-type stdout --tail-lines 50
```

Common failures and the usual fix:

| Error Pattern | Cause | Fix |
|---|---|---|
| `CUDA out of memory` | GPU too small | Switch to a larger-GPU partition, or reduce system size |
| `CUDA_ERROR_SYSTEM_DRIVER_MISMATCH` | Driver below the supported CUDA runtime floor | Pick a node with NVIDIA driver 520+ (`--nodelist` or `--gres`) |
| `FileNotFoundError` | Relative path outside the bind set | Use absolute paths; verify Singularity binds include `job_dir` |
| `ModuleNotFoundError` | Wrong container / missing env | Verify `configure_container` points at the right SIF, or use `--environment "module load ..."` |
| `DUE TO TIME LIMIT` | Wall time exceeded | Increase `--time-limit` and resubmit from the restart state — see Step 6 |
| `slurmstepd: error: Exceeded memory limit` | OOM kill | Increase `--memory` |
| `invalid number of nodes (-N 4-1)` at sbatch | `--nodes=1` combined with a multi-node `--nodelist=n1,n2,n4,n5` | Use `--exclude=<unwanted nodes>` (or `--gres=gpu:<type>:1` for GPU model targeting) — never pair `--nodes=N` with a longer `--nodelist`. |
| Downstream tasks stuck at `(Dependency)` / `(DependencyNeverSatisfied)` | `aftercorr`/`afterok` parent array held incomplete by a few failing tasks | `squeue -u $USER -r -h -o '%i %T %R' \| awk '$3=="(DependencyNeverSatisfied)"{print $1}' \| xargs -r scancel`, or rebuild the chain as Step 3.5 per-entry. |

Resubmit the fixed job against the **same** `node_id` — the node's
status drops back to `queued` on the new sbatch, and
`resolve_node_inputs` still finds the right topology / restart state.

---

## Step 6: Production Extension

When production hits wall time or the user wants to extend a completed
prod run, create a new `prod` node with `--continue-from` and submit it.
Do **not** edit the previous prod node or overwrite its DCD.

```bash
JD=$(realpath job_target)
PREV=prod_001  # the completed prod you want to extend

# New node: DAG records metadata.continued_from = prod_001 and
# resolve_node_inputs will restart from EXACTLY that prod's saved state.
mdclaw create_node --job-dir $JD --node-type prod \
  --continue-from $PREV --label "+100ns" \
  --conditions '{"simulation_time_ns":100}'

# Submit the extension
mdclaw submit_job \
  --job-dir $JD --node-id prod_002 \
  --script "mdclaw --job-dir $JD --node-id prod_002 run_production \
    --simulation-time-ns 100 --temperature-kelvin 300 --pressure-bar 1.0 \
    --output-frequency-ps 100 --platform CUDA" \
  --job-name prod_ext --partition gpu --gpus 1 \
  --time-limit "24:00:00" --memory "32G"
```

Each prod node keeps its own `trajectory.dcd` under its `artifacts/`
directory; concatenate with mdtraj / `md-analyze` when a continuous
trajectory is needed. `simulation_time_ns` in the extension is the
additional time to run *in this node* (added on top of `prod_001`'s
`currentStep`). `state.xml` is preferred for cross-node restarts;
`checkpoint.chk` remains a legacy fallback and is platform-specific.

---

## Step 7: Cancel

```bash
mdclaw cancel_job --job-id <slurm_job_id>
# Array parent (cancels all children):
mdclaw cancel_job --job-id 99999
# Single array child:
mdclaw cancel_job --job-id 99999_2
```

Cancelling a job leaves the DAG node in whatever state it was in
(`queued` / `running`). On the next `check_job`, the SLURM state
`CANCELLED` flows through to `node.status = "failed"`.

---

## Domain Knowledge

### MD on HPC
- **OpenMM**: 1 GPU per job. Multi-GPU scaling is poor; array jobs are
  the right answer for throughput.
- **HMR**: `--hmr --timestep-fs 4.0` is already on by default in
  `run_production`; ~2× throughput over 2 fs.
- **Checkpoints**: OpenMM checkpoints are binary and platform-specific.
  CUDA checkpoints cannot load on CPU and vice versa.
- **Resource estimation** (per replicate, 100 ns with HMR+4fs):

  | System atoms | Estimated time | Recommended `--time-limit` |
  |---|---|---|
  | < 50k | ~6 h | 12:00:00 |
  | 50k – 200k | 12–24 h | 36:00:00 |
  | > 200k | 24–48 h | 3-00:00:00 |

### Singularity / driver compatibility
- The stock MDClaw SIF ships with CUDA 11.8 and is actively verified on
  **NVIDIA driver 520+**. Older nodes may fail with
  `CUDA_ERROR_SYSTEM_DRIVER_MISMATCH`.
- Prefer `--gres "gpu:<type>:1"` to target specific GPU models rather
  than `--nodelist`, which is fragile as the cluster evolves.

### Environment variables
- `MDCLAW_MODULE_LOADS="cuda/11.8 amber/24"` — auto-insert module loads
  into every generated sbatch script (overridden when you pass
  `--environment` explicitly).
- `MDCLAW_MODULE_INIT="/etc/profile.d/modules.sh"` — init script sourced
  before the loads.
- `MDCLAW_SIF=/path/to/mdclaw.sif` — override the container the `mdclaw`
  wrapper uses on the login node.

### What not to do
- Don't run `prepare_complex` / `solvate_structure` / `build_amber_system`
  via `submit_job`. They don't need GPUs and starting a SLURM job just to
  write a few files wastes scheduler slots.
- Don't pass `--prmtop-file` / `--inpcrd-file` / `--restart-from` inside
  the inner `mdclaw` command when the node layout is present — DAG
  auto-resolution is correct, and hand-wiring bypasses the
  `--continue-from` audit trail.
- Don't run `submit_job` N times in a shell loop for **small, clean**
  fan-outs that fit a single array — you lose the parent job id, the
  common sbatch script, and per-task log grouping. Large campaigns
  (N > 100, or failures expected) are the opposite case: the per-entry
  chain in Step 3.5 is the right choice.
