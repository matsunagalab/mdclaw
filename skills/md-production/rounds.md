# Replicas as a rounds scheme (`setup_rounds` / `run_rounds`)

Use this page when several production replicas (different seeds) of one
system are to be run and extended together — N replicas x k segments —
without creating and continuing each node by hand. A *scheme* records the
intent once; `run_rounds` creates the segments, runs them, and continues
every replica round after round. The segments are ordinary `prod` nodes
(`prod_<scheme>_r<round>_w<replica>`) run by `run_production`, so
`concat_trajectory` and every metric work on each replica's lineage as on a
hand-made `--continue-from` chain.

The same loop drives a weighted ensemble (`skills/md-we/SKILL.md`), where an
analyze node decides the next batch instead of "continue every replica".

## Preconditions

- A completed `eq` node (or a completed `prod` node) with a `state` artifact.
- The production settings of `skills/md-production/explicit-water.md` /
  `implicit-water.md` decided (ensemble, pressure, output frequency).

## Record the scheme

```bash
mdclaw setup_rounds --job-dir "$JOB" --scheme '{
  "scheme_id": "rep",
  "policy": "replicas",
  "stage_tool": "run_production",
  "stage_args": {"simulation_time_ns": 10, "pressure_bar": 1.0, "output_frequency_ps": 10, "platform": "CUDA"},
  "start": {"node_ids": ["eq_001"], "n_replicas": 4},
  "seed": 20260921
}'
```

- `stage_args` are the `run_production` arguments of every segment; never
  `random_seed`, `restart_from` or file paths — the driver sets those.
- Every segment gets its own `random_seed`, derived from `seed`, the round
  and the replica, so two replicas never integrate the same noise
  (`run_production` refuses that anyway: `production_sibling_seed_collision`).
- `segment_conditions` declares conditions on every segment (keys
  `run_production` reports, e.g. `{"ensemble": "NPT"}`).

## Run and extend

```bash
mdclaw run_rounds --job-dir "$JOB" --scheme-id rep --max-rounds 1 --platform CUDA
```

One round = one segment per replica, run one after another in this process.
Each call continues from the DAG: round 2 continues every replica from its
round-1 segment (`continue_from`), and so on. Stop rules: `--max-rounds`
(per call), `--max-aggregate-ns` (total sampled time), `--max-wall-hours`
(returns before a round that would not fit). A failed segment is retried as
`prod_rep_r0001_w0002_t0001` with a new seed; three failures in a row stop
the driver (`rounds_replica_unstable`).

On a GPU cluster, one job runs the scheme until its wall time:

```bash
mdclaw submit_job --job-name rep --gpus 1 --time-limit 24:00:00 \
  --script "mdclaw run_rounds --job-dir $JOB --scheme-id rep --max-wall-hours 23 --platform CUDA"
```

This runs the replicas one after another on one GPU. Small systems (the
size table of `skills/hpc-run/submit-mps.md`) pack under MPS with
`--executor mps`: the driver runs on the login node (`nohup` / `tmux`; it
only submits, waits and plans) and each round goes out as `submit_mps_job`
jobs of `--mps-tasks-per-gpu` replicas per GPU:

```bash
nohup mdclaw run_rounds --job-dir "$JOB" --scheme-id rep --executor mps \
  --mps-tasks-per-gpu 8 --mps-time-limit 02:00:00 --max-rounds 10 > rep.log 2>&1 &
```

`--mps-time-limit` covers one packed segment (about `tasks_per_gpu / gain`
times the stand-alone time) with a margin; a segment that hits it is failed
and retried. With many replicas the driver runs several segments per MPS
task in one process (`run_segment_batch`; automatic once the round fills
`--mps-max-jobs` jobs, or `--mps-segments-per-task k`): the start-up is paid
once per task, and the time limit must then cover `k` segments.

To end a scheme on purpose, close it: `mdclaw close_rounds --job-dir "$JOB"
--scheme-id rep --reason "enough sampling"`. `run_rounds` then refuses
(`rounds_scheme_closed`) and every `next` of the scheme says `done`; a
replica retired with `update_workflow_state --abandon` is skipped by later
rounds. A scheme with rounds is never replaced — continue under a new id.

Read the state at any time:

```bash
mdclaw inspect_rounds --job-dir "$JOB" --scheme-id rep
```

## Analyze

Each replica's lineage is a production chain: parent an analyze node to the
replica's latest segment (`prod_rep_r0003_w0001`) with
`analysis_data_scope: production_chain` and use `skills/md-analyze/`. Several
replicas: one analyze node with all their latest segments as parents.

## Codes

| Code | Fix |
|---|---|
| `rounds_scheme_invalid` | the message names the field (scheme_id lowercase, stage_args without reserved keys, start.node_ids and n_replicas) |
| `rounds_start_node_invalid` | start nodes must be completed eq / prod nodes with a state |
| `rounds_round_in_progress` | a segment is running or queued elsewhere (the message names host, pid, Slurm job, heartbeat age); wait, or free a node nobody runs any more with `update_workflow_state --clear-slurm-metadata` — a driver that died is detected after 5 min and its nodes retried (`rounds_owner_lost`) |
| `rounds_segment_refused` | `run_production` refused before running (its code is in the message): fix `stage_args` |
| `rounds_replica_unstable` | `trace_failure` on the listed node; fix the cause, rerun `run_rounds` |
| `rounds_scheme_closed` | the scheme was closed with `close_rounds`; analyze it, or continue under a new `scheme_id` |
