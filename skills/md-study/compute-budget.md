# Compute Budget

Record a `budget` block on `study_plan.json` only when the user actually
mentioned compute (GPUs, wall time, queues, or an ns budget) or when a harness
imposes a per-task time limit. When compute is not mentioned, omit the `budget`
key entirely — but MD length must never be left undetermined, so fall back to
the default assumption below.

Default assumption (no budget stated): ~1 day of wall time on 1 local GPU
(`compute_target: "local"`, `gpu_count: 1`, `wall_time_hours: 24`,
`notes: "default assumption; no budget stated"`). Do not auto-detect compute
via `inspect_openmm_platforms` / `inspect_cluster`; those stay out of `md-study`.

## Derivation

1. **Parse the user's request** into a working budget object:
   - `compute_target`: one of `local`, `hpc`, `none`.
   - `gpu_type`: free-text GPU label (`"A100"`, `"RTX 4090"`, `"M2 Max"`, ...);
     may be `null` if CPU only.
   - `gpu_count`: positive integer; default `1` when the user said "an A100"
     without a count.
   - `wall_time_hours`: total wall-clock budget authorized.
   - `notes`: free-text echo of what the user said.

2. **Estimate throughput.** Use a coarse pre-prepare atom-count estimate
   (`atoms ≈ protein_residues * 130` for explicit-water OPC, or a tighter number
   if the user gave one):

   ```bash
   mdclaw estimate_md_throughput \
     --atom-count <est_atoms> \
     --gpu-type "<gpu_type>"
   ```

   Capture `ns_per_day`, `source`, and `confidence`. Carry `confidence` through
   unchanged — do not upgrade it.

3. **Derive a feasible (replicates × length) plan** with 15% headroom:
   - `usable_gpu_hours = wall_time_hours * gpu_count * 0.85`
   - `total_simulation_ns = ns_per_day * usable_gpu_hours / 24`
   - Split across planned jobs, choosing `target_replicates_per_job` and
     `target_ns_per_replicate` that match the design (typically ≥ 2 replicates
     per job; trim replicates before trimming length).
   - `expected_wallclock_hours = (total_simulation_ns / ns_per_day) * 24 / gpu_count`
   - `headroom_hours = wall_time_hours - expected_wallclock_hours`

4. **Tier the plan to the budget.** If `total_simulation_ns >= 50 * len(jobs)`,
   plan research-scale replicates × length. If smaller (a short benchmark-style
   budget), drop to a **consistency-evidence** tier: plan the longest feasible
   run down to ~1 ns per replicate, set `"evidence_tier": "consistency"` in
   `derived`, and note in `budget.notes` that this is local consistency evidence,
   not a converged free energy. Prefix `"INSUFFICIENT_BUDGET: "` only when the
   budget cannot fit even ~1 ns per job, and surface the gap so the user can
   raise the budget or drop a job.

5. **Record** the budget block on `study_plan.json`:

   ```json
   "budget": {
     "compute_target": "hpc",
     "gpu_type": "A100",
     "gpu_count": 1,
     "wall_time_hours": 168.0,
     "notes": "RIKEN GPU partition, 7-day max",
     "throughput": {
       "ns_per_day_per_gpu": 870.0,
       "source": "estimate_md_throughput",
       "confidence": "medium"
     },
     "derived": {
       "target_ns_per_replicate": 500,
       "target_replicates_per_job": 3,
       "total_simulation_ns": 3000,
       "expected_wallclock_hours": 82.8,
       "headroom_hours": 85.2
     }
   }
   ```

The `budget` block is intent + derivation. Downstream skills (`md-prepare`,
`md-equilibration`, `md-production`, `hpc-run`) do **not** read it as a contract
today — they continue to take their own parameters. The block exists so the
planner's reasoning stays attached to the study and is auditable when results
come back.
