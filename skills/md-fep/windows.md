# FEP Window Layout and HPC Submission

Read this when the windows of one leg do not fit in a single serial `run_fep`
call (long sampling, many windows, or a SLURM cluster is authorised).

## One node per window subset (default for anything over ~1 ns per window)

Windows are independent; only `analyze_fep` needs all of them. Create sibling
`fep` nodes under the **same** `eq` parent, each with a disjoint
`--lambda-indices` range, then parent one `analyze` node to all of them. A
node that dies (crash, scheduler time limit) then costs one subset, not the
whole leg — see "Recovering a killed node" below for the rest.

```bash
EQ=<eq_id>
for RANGE in 0-6 7-13 14-20; do
  mdclaw create_node --job-dir <job> --node-type fep --parent-node-ids $EQ \
    --label "fep_$RANGE" --conditions "{\"lambda_indices\": \"$RANGE\"}"
done
```

Every task command carries the same `--sampling-time-ns` and its own
`--lambda-indices`:

```bash
mdclaw submit_array_job --job-name fep_L99A --partition gpu --gpus 1 \
  --cpus-per-task 4 --time-limit "12:00:00" --memory "16G" \
  --tasks '[
    {"job_dir": "<job>", "node_id": "<fep_a>",
     "command": "mdclaw --job-dir <job> --node-id <fep_a> run_fep --lambda-indices 0-6 --sampling-time-ns 5 --platform CUDA"},
    {"job_dir": "<job>", "node_id": "<fep_b>",
     "command": "mdclaw --job-dir <job> --node-id <fep_b> run_fep --lambda-indices 7-13 --sampling-time-ns 5 --platform CUDA"},
    {"job_dir": "<job>", "node_id": "<fep_c>",
     "command": "mdclaw --job-dir <job> --node-id <fep_c> run_fep --lambda-indices 14-20 --sampling-time-ns 5 --platform CUDA"}
  ]'
```

Follow `skills/hpc-run/SKILL.md` for cluster inspection, status sync, and the
GPU rules; `submit_array_job` requires explicit current authorization.

## Extension (more sampling, same windows)

Create a child `fep` node under the completed `fep` node (exactly one fep
parent). `run_fep` restarts each window from the parent's per-window state
and chains the new samples after the parent's; `analyze_fep` follows the
chain automatically. Pass `--lambda-indices` to extend only the windows named
in a low-overlap warning (they must be windows the parent sampled). Pass
`--equilibration-time-ns 0`: the window is already at its lambda, and the
default 0.1 ns would be discarded on top of the 10 % `analyze_fep` drops from
every segment.

```bash
mdclaw create_node --job-dir <job> --node-type fep --parent-node-ids <fep_a> --label "fep_0-6_ext"
mdclaw --job-dir <job> --node-id <fep_a_ext> run_fep --sampling-time-ns 5 \
  --equilibration-time-ns 0 --platform CUDA
```

Parent the final `analyze` node to the **leaf** of each chain (the extension
nodes), not to both parent and child: the leaf's index already chains the
parent's segments (windows the child did not re-sample are carried over
unchanged, so the leaf always lists every window sampled so far). If both are
given anyway, `analyze_fep` counts each segment once and warns. Extension
keeps the parent's ensemble and temperature; `run_fep` refuses
(`fep_windows_incompatible`) a `--pressure-bar` or `--temperature-kelvin`
that differs from the parent's.

## Recovering a killed node

`run_fep` rewrites `artifacts/fep_windows.json` after every window, so a node
that failed at window 7 of 21 still indexes windows 0–6 (`"complete": false`).
A failed node cannot be a parent. Instead create a new `fep` node under the
same `eq` parent and hand it the partial index, naming only the windows that
are still missing; the finished ones are copied into the new index as they
are (no re-sampling), so the new node alone covers the whole protocol:

```bash
mdclaw create_node --job-dir <job> --node-type fep --parent-node-ids <eq_id> --label fep_recover
mdclaw --job-dir <job> --node-id <fep_recover> run_fep --lambda-indices 7-20 \
  --restart-windows-file <job>/nodes/<failed_fep>/artifacts/fep_windows.json \
  --sampling-time-ns 5 --equilibration-time-ns 0.1 --platform CUDA
```

(`--lambda-indices all` would instead continue the finished windows for
another `--sampling-time-ns` as well.) Paths in the index are relative, so
the job directory may be moved first. Parent the `analyze` node to the new
node only.

## Equilibration per window

A window that starts from the `eq` state is first minimised at its own λ
(this is what lets small→large mutations start at all), which also removes
the thermal motion of the whole box. Give such windows at least
`--equilibration-time-ns 0.1` to re-heat and re-equilibrate the NPT box
before samples are taken; `run_fep` warns below 0.05 ns. Windows that
continue a fep parent are already at their λ and need `0`.

## Replicas

Independent replicas are sibling `fep` nodes with the same `--lambda-indices`
and different `--random-seed`. `analyze_fep` pools duplicate window indices
across parents; report the pooled estimate and, when the request asks for
reproducibility, one `analyze_fep` node per replica as well.

## Custom lambda schedules

Densify where overlap is weak by passing an explicit schedule to
`build_hybrid_system`; it must start at 0 and end at 1:

```bash
--lambda-schedule "0,0.05,0.1,0.15,0.2,0.25,0.3,0.35,0.4,0.45,0.5,0.55,0.6,0.65,0.7,0.75,0.8,0.85,0.9,0.95,1"
```

Phase boundaries are λ=0.25 (old side chain fully decharged) and λ=0.75
(sterics swapped); place extra points inside the phase that showed low
overlap. A new schedule is a new `topo` node; min/eq/fep must be rebuilt
under it.
