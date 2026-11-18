# Weighted Ensemble Phase 3 — test request (GPU measurements and literature comparison)

Written: 2026-09-21. Covers the round-driven sampling (`mdclaw/rounds/`) and
weighted ensemble (`mdclaw/we/`) implemented on 2026-09-21. The design is in `docs/research/weighted-ensemble-plan.md`, the implementation record in
the 2026-09-21 entry of `docs/memo.md`.

## 0. Purpose of the request

1. Apply the new implementation to real systems on GPU and produce **rate constants comparable to literature values** (publication quality: agreement with
   brute force in the same force field, and differences from literature MD and experiment explainable, including force-field differences).
2. Measure the implementation side: the overhead of one segment, DAG-operation time per round, and
   `progress.json` / `events/` / disk at ~10³ nodes.
3. **Feed back everything you noticed while running** (§8): skill text, CLI output, waiting times, places where you hesitated,
   workarounds. Even if things work, please write down "points that took time to find".

## 1. Read first

- `skills/md-we/SKILL.md` → `pcoord-and-bins.md` → `kinetics.md` (the WE procedure, how to choose a pcoord, how to read results)
- `skills/md-production/rounds.md` (round-driven replicas; the same loop as WE)
- `skills/hpc-run/SKILL.md`, `submit-single.md` (the form for running `run_rounds` in a 1-GPU job)
- The `rounds/` and `we/` sections of `docs/developer/tool-reference.md` (arguments and codes)

## 2. Environment and ground rules (RIKYU)

- The checkout `/home/$USER/mdclaw` **contains uncommitted work. Read-only.** git operations, editing, `pi update`,
  and swapping the SIF are forbidden. Do not fix bugs; report them in the §8 format (if you took a workaround, write that too).
- Run with the launcher + shared SIF (overlay of the checkout code onto the image):

  ```bash
  export MDCLAW_RUNTIME=apptainer
  export MDCLAW_SIF=/data1/$PROJECT/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif
  export MDCLAW_OUTPUT=full
  M=/home/$USER/mdclaw/bin/mdclaw
  "$M" --list-json run_rounds        # if the rounds / we tools are visible, the environment is correct
  ```

- The working directory is `/data1/$PROJECT/$USER/we-trials/<system name>/` (the study is `study/` under it).
  As models for setting up a trial, use the existing `/data1/$PROJECT/$USER/seus-trials/ala3-e2e/launch.sh` and
  `cln025-e2e/launch.sh` (the sequence `register_local_structure` → prep → solv → topo → min → eq,
  the form of `submit_job`, `--output-dir slurm`).
- Slurm: copy `/data1/$PROJECT/$USER/runs/mps-bench-20260916/.mdclaw_cluster.json` into each working directory
  and use it (image, `--nv`, `source_mode: overlay`, partition gpu, account $PROJECT). GPU jobs
  always go through `submit_job`. Submit `run_rounds` as a 1-GPU job with `--max-wall-hours (time-limit − 1)`, and
  when it stops, resubmit the same command (the state is in the DAG):

  ```bash
  "$M" submit_job --job-name we1 --gpus 1 --cpus-per-task 2 --time-limit 24:00:00 --output-dir "$W/slurm" \
    --script "mdclaw run_rounds --job-dir $JD --scheme-id we1 --max-wall-hours 23 --platform CUDA"
  ```

- **Budget: 350 GPU hours total, as a hard cap** (roughly A ≈ 45, B ≈ 200, C ≤ 100). Before submitting a long job,
  run just 1 round, measure ns/day and the segment wall time, write the estimate in the report, then submit.
  If it looks like the cap will be exceeded, stop and consult. Include the GPU-hour ledger (`sacct -X --format=JobID,JobName,Elapsed,AllocTRES,State`)
  in the report.
- Always report rate constants together with the `verdict` of `analyze_we`. `flux_transient` is a lower bound; `no_target_events` means
  no estimate. Do not move the target closer, shorten segments, or change the bootstrap width to make the verdict pass.

## 3. Implementation-side numbers to record for every task

| Number | How to obtain |
|---|---|
| Wall time of one segment and breakdown of MD time | Wall time = difference between `tool_started` → `tool_completed` in `events/`; MD time from the segment's `energy.dat` / the result's `ns_per_day`. The difference is overhead (Simulation construction + state read/write) |
| Round wall time and DAG-operation time | `elapsed_seconds` and `segments_run` in the `run_rounds` result. Round wall time − Σ segment wall time = time for node creation, policy, and plan |
| Size of `progress.json`, number of files in `events/` | `ls -la progress.json; ls events | wc -l` at 100 / 1,000 nodes |
| Disk per segment | `du -sh nodes/prod_<scheme>_r0010_w0001` |
| Response time of `inspect_job` / `inspect_rounds` / `explain_node` | `time` at around 1,000 nodes |
| Re-running a failed segment | If it happens: node id, code, result of the re-run |

## 4. Task A: alanine dipeptide φ flip (αR / PPII → αL) — strict comparison with brute force in the same force field

**Why**: transitions are on the 10–100 ns scale, so brute force cheaply yields tens of events, and the WE estimator can be validated with strict statistics.
It is a standard benchmark with literature values (implied timescales per force field). As a small system with short segments, it also measures overhead.

- System: ACE-ALA-NME. Build the PDB with tleap `sequence { ACE ALA NME }` (model: `ala3-e2e/build/tleap_dry.in`),
  `register_local_structure` → prep (keep caps) → solv (OPC, 12 Å, 0.15 M NaCl) → topo (ff19SB/OPC/HMR) →
  min → eq (300 K, NVT 0.2 ns + NPT 1 ns, 1 bar).
- pcoord (2D):

  ```json
  [{"type":"dihedral","name":"phi","selections":["resname ACE and name C","resname ALA and name N","resname ALA and name CA","resname ALA and name C"]},
   {"type":"dihedral","name":"psi","selections":["resname ALA and name N","resname ALA and name CA","resname ALA and name C","resname NME and name N"]}]
  ```

  bins: φ in 30° steps (edges −150 … 150), ψ in 60° steps (−120 … 120). target: `{"pcoord_ranges": [[20, 130], null]}`
  (positive φ = αL side). basis: the eq node (φ < 0). `walkers_per_bin` 4, τ = 0.5 ns (`simulation_time_ns` 0.5,
  `output_frequency_ps` 25), 2 schemes with different seeds (`we1`, `we2`). At least 30 rounds after steady state (after the flux
  ramp-up has settled).
- brute force (from the same eq): `run_production` 4 runs × 500 ns (different seeds, output 1 ps). For the first-passage time
  A(φ < −30°) → B(φ > 30°), count as events only those that stay in B for at least 20 ps, and give MFPT ± SE (target ≥ 20 events). A small mdtraj
  script is fine for the analysis (put it in `analysis/` and reference it from the report). The 20 ps dwell condition is mandatory: without a dwell, the
  ±180° wrap of φ counts about 10,000 spurious transitions, and it also disagrees with the WE criterion (inside the target at the segment end point) (note of 2026-09-22).
- Literature: Vitalini, Noé, Keller, *J. Chem. Phys.* 142, 084101 (2015) (implied timescales per force field. ff19SB is not included, so
  put it alongside the ff99SB-ILDN / ff03 values and explain the difference as a force-field difference). Other literature values you find are also fine (always cite the source).
- Pass criterion (guide): the k_WE of both schemes overlaps the brute-force 95 % interval, and the verdict is `flux_steady`.
- Budget ≈ 45 GPU hours (brute force 2 µs + WE 2 schemes × 2 µs aggregate).

## 5. Task B: chignolin CLN025 unfolding and folding (340 K) — comparison with Anton literature values and experiment

**Why**: Lindorff-Larsen et al., *Science* 334, 517 (2011) report µs-scale folding / unfolding times for CLN025 at 340 K
(CHARMM22*/TIP3P). Running at the same temperature allows direct comparison with the literature MD, and experimental values can be taken from
a table in the same paper. Since the force field is ff19SB/OPC, use **brute force in the same force field (340 K) as the primary reference**, with literature MD and experiment as secondary.

- System: 5AWL chain A. To halve the cost, we recommend **rebuilding with a 12 Å buffer** in `we-trials/cln025`
  (the existing `seus-trials/cln025-e2e` is 20 Å. Its 300 K unbiased 4 × 500 ns `prod_024/027/030/033` can be used as a secondary check at
  300 K). topo ff19SB/OPC/HMR → min → eq 340 K (NPT 1 bar, 2 ns) = **the folded-side basis**.
- Unfolded-side basis: build it with an eq chain. min → eq (500 K, NVT, 5 ns) → eq (340 K, NPT, 2 ns). Confirm with `analyze_q_value` / `analyze_rmsd` that
  the `equilibrated.pdb` of the last eq is unfolded (Q < 0.2, RMSD > 0.6 nm) before
  using it in `start.node_ids`. If it stays folded, extend the 500 K stage.
- pcoord: `q` (`native_pdb` = `equilibrated.pdb` of the 340 K folded eq, default selection) as the primary axis, and `rmsd` (same reference,
  backbone) as the second dimension. bins: q in 0.1 steps (edges 0.1 … 0.9), rmsd in 0.1 nm steps (0.1 … 0.8).
  target: for the unfolding scheme `[[null, 0.2], null]` (q ≤ 0.2), for the folding scheme `[[0.8, null], null]` (q ≥ 0.8).
- τ = 0.2 ns (output 20 ps), `walkers_per_bin` 5. First, 1 scheme per direction (`unf1`, `fold1`). If budget remains, a second seed.
- brute force (340 K, from the folded eq): 4 runs × 1 µs (output 10 ps). From the q time series, count first passages for unfolding (reaching q < 0.2) and
  refolding (q > 0.8), and give rate ± SE. The existing 2 µs at 300 K serves as a reference for the unfolding frequency at 300 K.
- Literature: Table 1 of the Science 2011 paper above (CLN025, folding / unfolding times at 340 K, and the cited experimental values).
  **Read the numbers from the paper yourself, and put the source, temperature and force field in the table** (this request does not state the numbers).
- Pass criterion (guide): WE k_fold and k_unfold agree with brute force in the same force field within a factor of 2 (intervals overlap),
  and agree with literature MD in order of magnitude (with a discussion of force-field differences).
- Budget ≈ 200 GPU hours (for a 12 Å box; double for a 20 Å box). **Once brute force and the first 30 rounds are done,
  report once with the measured ns/day, overhead and flux ramp-up, and confirm whether to continue.**

## 6. Task C (optional): ligand unbinding — a system with experimental values

First check, within 2 hours, whether the preparation can be done straightforwardly in MDClaw; if not, make it §8 feedback and skip it.

- Candidate 1: β-cyclodextrin + a small-molecule guest (1-butanol, aspirin, naproxen, etc.). There are experimental k_off values (ultrasonic relaxation,
  a series of reports by Fukahori et al.) and MD literature values (Tang & Chang, *J. Chem. Theory Comput.* 14, 303 (2018);
  SEEKR2, Votapka et al. 2022). k_off is 10⁴–10⁷ s⁻¹, which suits WE. However, it is unverified whether prep of a protein-free
  host–guest system (treating β-CD as a ligand) goes through in MDClaw.
- Candidate 2: FKBP12 + fragments (DMSO, DSS, BUT). The comparisons are the Anton brute-force k_off / k_on of Pan, Xu, Shaw, *J. Chem. Theory Comput.* 13, 3372 (2017) and
  Huang & Caflisch, *PLoS Comput. Biol.* 7, e1002002 (2011).
  Protein + ligand, so prep follows the standard route.
- pcoord: `distance` (binding-site residue group ↔ guest center of mass; intermolecular, so minimum image) + `rmsd` (superposed on the protein / host,
  measured on the guest via `align_selection`). target: distance ≥ 1.2 nm (box twice that + margin; watch the half-box-length guard).
  k_off = steady-state flux. The binding direction (k_on) is a separate scheme; report `rate_per_molar_per_s` with a note on the concentration.
- Budget ≤ 100 GPU hours.

## 7. Comparison practice and report table

- The primary reference is **brute force in the same force field at the same temperature**. Literature MD (different force field) and experiment are secondary. Explain differences by force field, box and temperature,
  and make the state definitions (pcoord ranges) exactly the same between WE and brute force.
- Errors: for WE, the bootstrap interval from `analyze_we` and the scatter across independent schemes (different seeds). For brute force, the SE from the event count.
- Table columns: system / transition / T / force field & water / method (WE: rounds, number of walkers, aggregate ns, verdict) / k (s⁻¹) [95 %] / MFPT /
  reference 1: brute force same force field / reference 2: literature MD (force field) / reference 3: experiment / ratio.

## 8. Feedback (important)

Write down **everything** you noticed while running, numbered (`WE-1`, `WE-2`, …). The scope is unrestricted: skill text that is insufficient or
wrong, a `next_action` or `code` that does not tell you the next step, results you could not interpret, long waits or invisible progress,
places where you hesitated, workarounds you took, documentation errors, features that would be useful. Each item in this format:

```
WE-<n>: <one-line summary>
  What happened: ... (command, and the returned code / message verbatim)
  Expected: ...
  Reproduce: <command> (with job_dir / node_id / scheme_id)
  Related files: result.json, node.json, slurm log, the relevant skill lines
  Proposal: ... (optional)
```

Even if things work, write down "points where it took time to find where to read" and "things you got wrong at first".

## 9. Deliverables

- `/data1/$PROJECT/$USER/we-trials/report.md`: the §3 numbers, the §7 table, the GPU-hour ledger, the §8 feedback,
  and paths to each study's `we_kinetics.json` / `we_flux.png` / brute-force analysis scripts.
- In the final message, include a summary in a form that can be pasted into `docs/memo.md` (date, system, numbers, conclusion, unfinished items). Do not write to the checkout.

## 10. Do not

Run segments by hand (only `run_rounds` advances a scheme), edit `node.json` / `progress.json`,
`rm -rf` `nodes/`, re-run the same node, `pi update`, swap the SIF, modify the checkout,
exceed the budget, or adjust target, τ or bootstrap to make the verdict pass.
