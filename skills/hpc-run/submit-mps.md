# MPS-Packed Submission (several simulations sharing one GPU)

`submit_mps_job` runs several nodes at the same stage (replicates from one
`eq`, seeds, several small systems) in **one** SLURM job that holds one whole
GPU, starts an NVIDIA Multi-Process Service (MPS) daemon, and runs every task
at once. The point: an OpenMM simulation of a small system cannot fill a
large GPU by itself (too few atoms for the number of streaming
multiprocessors), so several of them together deliver more total ns/day than
one after another, for the GPU-hours of a single GPU. Each simulation runs
slower than alone; only the aggregate is faster.

## Decide: pack or not

Two quantities decide it: how far one simulation is from saturating *this*
GPU, and whether the user wants total sampling (pack) or one trajectory as
early as possible (do not pack).

1. **Is MPS usable here?** All of these must hold; otherwise use `submit_job`
   or `submit_array_job`:
   - NVIDIA GPU of Volta generation or newer (V100, T4, A-series, L-series,
     H-series, B-series, GB-series; also GeForce RTX 20 and later).
   - `nvidia-cuda-mps-control` exists on the compute nodes (check the login
     node with `command -v nvidia-cuda-mps-control`; it ships with the driver,
     so it is normally present wherever `nvidia-smi` is). Compute mode
     `Default` or `Exclusive_Process` both work (`nvidia-smi -q | grep
     "Compute Mode"`); `Prohibited` does not.
   - The job gets whole GPUs (`--gpus`/`--gres=gpu`). No site configuration is
     needed: the daemon runs inside the job. MIG slices are not supported by
     this tool.
   - Task commands use `--platform CUDA`; MPS does nothing for OpenCL or CPU.

2. **Pick the tasks per GPU from the GPU class and the system size.** The gain
   grows with GPU size and shrinks with atom count. Reference points:

   | GPU class | atoms per task | tasks per GPU | measured aggregate gain |
   |---|---|---|---|
   | data-centre Blackwell (GB200, B200) | ~50k | 8 | 2.65x (4 tasks: 2.24x), GB200, 52k-atom lysozyme/OPC |
   | data-centre Hopper (H100, H200) or L40S | ~24k (DHFR) | 8 | >2x (NVIDIA) |
   | same | ~92k (ApoA1) | 4 | clear gain (NVIDIA) |
   | same | ~400k (cellulose) | 2 | ~1.2x (NVIDIA) |
   | mid-range data-centre (A10, A30, L4) or workstation/consumer (RTX 4090, 6000 Ada) | ~24k | 2-4 | small gain; A10 gains little (NVIDIA) |
   | any GPU | >400k, or membrane systems of that size | 1 | none: one task already fills the GPU |

   Extrapolate between rows: halve the task count each time the atom count
   doubles, and drop one row for a smaller GPU. Explicit-water atoms are what
   count; implicit-solvent systems are tiny and take the top row for their
   GPU. Above 8 tasks the tool warns, above 16 it refuses
   (`mps_tasks_per_gpu_exceeded`).

3. **Unknown GPU or system size near a boundary: calibrate, do not guess.**
   Two short jobs cost minutes: one task alone and 4 packed, each a few
   hundred picoseconds (`--simulation-time-ns 0.5`) on branch nodes made for
   the purpose. Aggregate ns/day = N x ns / job wall time. Pack when the
   packed aggregate beats the single job by 1.3x or more; try 8 when 4 gave
   2x or more. Record the numbers in the study notes so later jobs reuse them.

4. **Never share a GPU between simulations without MPS.** Plain concurrent
   processes time-slice: 4 of them measured 0.84x of running one at a time
   (GB200, 52k atoms).

Do not pack one system alone (`submit_job`), tasks with very different wall
times (the job ends when the slowest task ends), or when the user needs the
first trajectory as early as possible rather than the most sampling per
GPU-hour.

## Rules

| Rule | Value |
|---|---|
| Task command | Must contain `--platform CUDA` (refused otherwise: `mps_task_requires_cuda_platform`). Same `mdclaw --job-dir ... --node-id ... <stage tool>` payload as `submit_job`. |
| `--time-limit` | `N x T_single / G`, where `T_single` is the wall time of one task alone (from `estimate_md_throughput` or a previous run) plus 10 minutes of startup, and `G` is the expected aggregate gain from the table (use 2 for the top rows, 1 when unsure). A too-short limit kills every task at once; an over-long limit costs nothing where billing is by elapsed time. |
| `--cpus-per-task` | Left at 0: the tool reserves `--cpus-per-sim` (default 2) cores per task. |
| `--gpus` | 1. Pass 2 or more only when one MPS job is to hold two whole GPUs; tasks are spread round-robin. |
| `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` | Set by the tool to `200 / tasks_per_gpu` (NVIDIA's rule for OpenMM; 15-25 % better than unrestricted). Override with `--active-thread-percentage` only for a measured reason. |

Every task gets its own `slurm_job_id` stamp (the same id) plus
`slurm_mps_slot` and its own logs `<job_name>_<id>.task<slot>.out/.err`; the
job's own `.out/.err` hold the wrapper and daemon messages. `check_job` and
`list_tracked_jobs --sync` reflect the job state onto every packed node.

## Chain: min -> eq -> prod for N replicates

Prepare one system through `topo` on the login node, then create N `min`
nodes from that `topo` (each branch is a replicate), N `eq` and N `prod`
nodes behind them, and submit three MPS jobs chained with `afterok`. Create
all nodes first so the ids are known.

```bash
JD=$(realpath job_1aki_A)
TOPO=<topo_node_id>
for i in 1 2 3 4; do
  MIN[$i]=$(mdclaw create_node --job-dir "$JD" --node-type min --parent-node-ids $TOPO --label rep$i --output id)
  EQ[$i]=$(mdclaw create_node --job-dir "$JD" --node-type eq --parent-node-ids ${MIN[$i]} --label rep$i \
    --conditions '{"temperature_kelvin":300,"pressure_bar":1.0}' --output id)
  PROD[$i]=$(mdclaw create_node --job-dir "$JD" --node-type prod --parent-node-ids ${EQ[$i]} --label rep$i \
    --conditions "{\"simulation_time_ns\":100,\"random_seed\":$i}" --output id)
done

tasks() {  # <stage tool + options> <node ids...>
  local cmd=$1; shift
  python3 -c 'import json,sys; jd,cmd=sys.argv[1:3]; print(json.dumps([{"job_dir":jd,"node_id":n,"command":f"mdclaw --job-dir {jd} --node-id {n} {cmd} --platform CUDA"} for n in sys.argv[3:]]))' "$JD" "$cmd" "$@"
}

mdclaw submit_mps_job --job-name min_1aki --time-limit 00:20:00 \
  --tasks "$(tasks 'run_minimization --max-iterations 5000' "${MIN[@]}")"
# -> slurm_job_id MIN_ID
mdclaw submit_mps_job --job-name eq_1aki --time-limit 00:40:00 --dependency afterok:MIN_ID \
  --tasks "$(tasks 'run_equilibration --temperature-kelvin 300 --pressure-bar 1.0' "${EQ[@]}")"
# -> EQ_ID
mdclaw submit_mps_job --job-name prod_1aki --time-limit 08:00:00 --dependency afterok:EQ_ID \
  --tasks "$(tasks 'run_production --simulation-time-ns 100 --random-seed 0' "${PROD[@]}")"
```

Give each production task its own `--random-seed` (edit the JSON, or build
the list per node) so replicates differ; the `random_seed` condition on the
node and the command must agree.

If one task fails, the job reports FAILED: nodes whose tool already recorded
completion stay `completed`, the failed node is `failed` with its slot's
stderr as evidence, and the `afterok` job behind it is cancelled. Resubmit the
next stage as a new MPS job listing only the completed parents' children.

Report the three job ids and the per-task log paths, then stop, as in
`skills/hpc-run/SKILL.md` "Critical Rules".
