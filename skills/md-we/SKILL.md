---
name: md-we
description: "Weighted ensemble (WE) kinetics with MDClaw CLI tools and OpenMM: rate constants and mean first-passage times of folding, conformational change, ligand unbinding or binding, from unbiased dynamics. A rounds scheme runs walker segments as prod nodes, we_resample splits, merges and recycles walkers by weight on a per-round analyze node, and analyze_we turns the recycled flux into a rate. Before any state-changing command, follow the pre-command gate in this skill."
---

# MD Weighted Ensemble (kinetics)

You are a computational biophysics expert estimating a rate constant with a
weighted ensemble (Huber & Kim 1996): many short, unbiased trajectory
segments (walkers) carry statistical weights, walkers that reach the target
are recycled to the initial state, and the weight they carry per unit time is
the rate (Hill relation, `k = 1 / MFPT`).

Follow `skills/common/preamble.md`, `skills/common/run-loop.md` (the canonical
node loop), `skills/common/solvent-regimes.md`, and
`skills/common/tool-output.md` for error handling.

## Pre-command gate

Before every state-changing command in this skill:

1. The question is a **rate** (k, MFPT, k_off, k_on) or a **transition
   between two named states**. A free-energy profile along a coordinate is
   umbrella sampling (`skills/md-production/distance-restraints.md`); an ensemble without kinetics is SST2 (`sst2.md`).
2. The two states are written as **ranges of a progress coordinate**
   (`pcoord`, 1 or 2 CVs: `distance`, `rmsd`, `dihedral`, `q`), the initial
   state is a **completed `eq` node** (or a completed `prod` node) and the
   target is `target.pcoord_ranges`. Choose them with
   `skills/md-we/pcoord-and-bins.md`. `setup_rounds` evaluates the start
   structure's pcoord (`scheme.start_pcoords` in its result) and refuses a
   start inside the target (`we_start_in_target`), a selection that does not
   match (`cv_selection_invalid`) or an intermolecular target beyond half the
   box — read that result before running.
3. Walkers are never created, run or continued one by one. The scheme is
   recorded once (`setup_rounds`) and advanced only by `run_rounds`; the
   `next` of any scheme node says so.
4. Segments run on a GPU (`--platform CUDA`) for anything larger than a
   peptide. One round is `n_walkers x segment length` of MD.
5. Never quote a rate except from a completed `analyze_we` node whose
   `verdict` is `flux_steady` (the flux is steady *and* the rate stopped
   moving over the second half of the run, `we_convergence.png`);
   `rate_not_converged` is quoted only with its drift, `flux_transient` is a
   lower bound, `flux_undersampled` and `no_target_events` are no estimate
   at all (the result's `rate_note` says which). Report the verdict and its
   reasons verbatim.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target | job directory with a completed `eq` node (`inspect_job`) |
| Question | which transition, which direction (A -> B) |
| Execution mode | `autonomous` / `human_in_the_loop` |
| Progress coordinate | 1-2 CVs, from `pcoord-and-bins.md` |
| Initial state (basis) | the completed `eq` / `prod` node id |
| Target | `pcoord_ranges` of state B |
| Segment length | `stage_args.simulation_time_ns`; 0.05-0.2 ns for peptides and small proteins, 0.2-1 ns for ligand unbinding (see `pcoord-and-bins.md`) |
| Walkers per bin | 4-8 (default 5) |
| Budget | rounds x walkers x segment length; stop rules of `run_rounds` |

Autonomous default: proceed when the two states are unambiguous from the
request; otherwise ask for the coordinate and the two ranges.

## Workflow

1. Inspect the job and pick the basis node:

   ```bash
   mdclaw inspect_job --job-dir "$JOB"
   ```

   The basis is a completed `eq` node (or a completed `prod` node) with a
   `state` artifact. Its structure defines state A; check that its pcoord
   value lies outside the target ranges.

2. Record the scheme (once):

   ```bash
   mdclaw setup_rounds --job-dir "$JOB" --scheme '{
     "scheme_id": "we1",
     "policy": "we_resample",
     "policy_args": {
       "pcoord": [{"type": "rmsd", "name": "r", "selection": "backbone", "reference_pdb": "'"$JOB"'/nodes/eq_001/artifacts/equilibrated.pdb"}],
       "bins": {"edges": [[0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6]]},
       "walkers_per_bin": 5,
       "target": {"pcoord_ranges": [[0.6, null]]}
     },
     "stage_tool": "run_production",
     "stage_args": {"simulation_time_ns": 0.1, "pressure_bar": 1.0, "output_frequency_ps": 20, "platform": "CUDA"},
     "start": {"node_ids": ["eq_001"], "n_replicas": 8},
     "seed": 20260921
   }'
   ```

   `--scheme` is one JSON string. `initial_weights` defaults to `uniform`
   for a weighted policy. Codes: `rounds_scheme_invalid`,
   `rounds_tool_invalid`, `rounds_start_node_invalid` name the field to fix.
   The walkers run at the basis node's temperature
   (`segments_run_at_kelvin` in the result); `stage_args` carries no
   temperature (`skills/md-production/rounds.md`).

3. Run rounds. On a GPU cluster, submit one job that runs the whole scheme
   until its wall time; on a workstation run it in the foreground:

   ```bash
   mdclaw submit_job --job-name we1 --gpus 1 --time-limit 24:00:00 \
     --script "mdclaw run_rounds --job-dir $JOB --scheme-id we1 --max-wall-hours 23 --platform CUDA"
   # or, locally:
   mdclaw run_rounds --job-dir "$JOB" --scheme-id we1 --max-rounds 50 --platform CUDA
   ```

   Small systems (a peptide or a small protein: one walker cannot fill a
   data-centre GPU) pack under MPS instead — `--executor mps`. The driver
   runs on the login node (it only submits, waits and resamples; keep it
   alive with `nohup` or `tmux`), and every round goes out as
   `submit_mps_job` jobs of `--mps-tasks-per-gpu` walkers per GPU:

   ```bash
   nohup mdclaw run_rounds --job-dir "$JOB" --scheme-id we1 --executor mps \
     --mps-tasks-per-gpu 8 --mps-time-limit 01:00:00 --max-rounds 50 > we1.log 2>&1 &
   ```

   `--mps-time-limit` is the wall time of one packed segment with a margin
   (8 walkers on one GPU each run about 3-4x slower than alone;
   `skills/hpc-run/submit-mps.md`); a segment that hits it is failed and
   retried, so a limit too short wastes rounds. Segments left queued or
   running by a killed driver are waited for when the driver is rerun.
   Each MPS task pays about 11 s of start-up; with many walkers per round
   the driver runs several segments per task in one process (default: only
   once the round fills `--mps-max-jobs` jobs, so GPUs are never left idle
   for the sake of packing; `--mps-segments-per-task k` sets it by hand).
   Then `--mps-time-limit` must cover `k` packed segments.

   Each round runs the pending segments (`prod_we1_r<round>_w<replica>`),
   runs `we_resample` on `analyze_we1_r<round>` and creates the next round.
   `run_rounds` returns at a round boundary (`stopped_because`:
   `max_rounds`, `max_aggregate_ns`, `wall_time`, `policy`); run it again to
   continue — the state is read from the DAG, so a killed job resumes (a
   segment the dead driver left `running` is detected by its stale heartbeat
   — 5 min, at once when its process is gone on this host — sealed
   `rounds_owner_lost` and retried) and a failed walker is retried with a new
   seed. Monitor with:

   ```bash
   mdclaw inspect_rounds --job-dir "$JOB" --scheme-id we1
   ```

4. Analyze when the flux has had time to reach steady state (tens of rounds
   after the first recycling events; `inspect_rounds` shows the rounds):

   ```bash
   mdclaw create_node --job-dir "$JOB" --node-type analyze \
     --parent-node-ids analyze_we1_r0050 --label we1_kinetics \
     --conditions '{"analysis_data_scope": "production_chain"}'
   mdclaw --job-dir "$JOB" --node-id analyze_001 analyze_we
   ```

   The latest policy node is enough: the chain of rounds is followed back.
   Read the result with `skills/md-we/kinetics.md`; `we_convergence.png`
   is the figure that says whether the rate has settled (the rate the
   analysis would have reported had the run stopped after each round).

   To stop a scheme for good (budget, a redesign), close it instead of
   leaving its frontier pending: `mdclaw close_rounds --job-dir "$JOB"
   --scheme-id we1 --reason "..."`; every `next` of the scheme then says
   `done` and `run_rounds` refuses (`rounds_scheme_closed`). A replica
   retired with `update_workflow_state --abandon` is never retried, but a
   weighted scheme cannot lose a walker's weight — close the scheme instead.

5. Report the rate with its verdict, interval, the drift over the second
   half (`convergence.drift_factor`) with `we_convergence.png`, the number
   of rounds, the aggregate sampled time and the pcoord / target
   definition, as in `skills/common/run-loop.md` step 5. For an independent error estimate run
   a second scheme (`scheme_id` `we2`, another `seed`) and give `analyze_we`
   both policy nodes as parents.

## Codes

| Code | Fix |
|---|---|
| `rounds_policy_failed` with `we_policy_args_invalid` | the message names the `policy_args` field; fix it with `setup_rounds --overwrite true` before the first round, or pass the argument to `we_resample` by hand on the pending policy node |
| `rounds_start_temperature_mismatch` | the start or `basis_node_ids` nodes (at `analyze_we`: the parent schemes' segments) ran at different temperatures: use basis nodes equilibrated at one temperature (`pcoord-and-bins.md`, "An unfolded (or unbound) basis"); pool only schemes run at one temperature (`--temperature-kelvin` does not lift this) |
| `we_pcoord_out_of_bins` | widen `bins.edges` or keep `extend_bins` true |
| `we_target_exceeds_half_box` | an intermolecular distance is a minimum-image distance: keep the target and edges below half the box, or solvate in a larger box |
| `cv_selection_invalid` | the selection matches no atoms, solvent, or a different atom count in the reference structure |
| `rounds_replica_unstable` | a walker failed three attempts: `trace_failure` on the listed node (timestep, restraints, platform) |
| `rounds_round_in_progress` | another `run_rounds` (or a Slurm job) owns the round: the message names host, pid, Slurm job and heartbeat age; wait, or — if nothing runs it any more — free the node with `update_workflow_state --clear-slurm-metadata` (the driver does it by itself once the heartbeat is 5 min old) |
| `rounds_submit_failed` | `submit_mps_job` refused the round (its code is in the message): the cluster config (`configure_container`, policy) or a stage arg; fix it, rerun `run_rounds --executor mps` |
| `rounds_scheme_closed` | the scheme was ended with `close_rounds` (budget spent, design redone): analyze its last policy node, or continue under a new `scheme_id` |
| `no_target_events` (verdict) | no walker reached the target yet: more rounds, or a target closer to the initial state |
| `rate_not_converged` (verdict) | the window is steady, but the rate the analysis would have reported moved by 1 kT (a factor of e) or more over the second half of the run (`we_convergence.png`, red half): run the `next` (`next_rounds_suggested`, half the run again) and analyze again on a new node; until then quote the rate only with its drift |
