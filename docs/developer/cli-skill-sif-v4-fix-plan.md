# The 4 cli_skill_sif failures in MDDataBench v4: causes and the fix plan for MDClaw (CLI and skills)

Written: 2026-09-28. A pre-implementation plan (for handoff). Checkout `53f2595`. The files referenced below,
`mdclaw/node/lifecycle.py`, `mdclaw/node/constants.py`, `mdclaw/slurm/submit.py`,
`mdclaw/slurm/preflight.py`, `mdclaw/simulation/production.py`, `mdclaw/_envelope.py`,
`skills/hpc-run/submit-single.md`, and `skills/md-prepare/SKILL.md`, are unchanged from `0528df5`, the image used
in the campaign (line numbers are the same in either).

Evidence: the MDDataBench campaign `runs/glm-5.3-flash-3cond-full-v4` (pi + rikyu/glm-5.3-flash,
98 tasks × 3 conditions × 3 repeats = 882 attempts, with agent isolation, image membranecache-3dd0abe30156 =
mdclaw `0528df5`, pi skills at `87050da`, 2026-09-26 21:30 to 09-28 01:43 JST).
cli_skill_sif passed 290/294. All 4 failures were traced back through the transcript, the jobs, and the code.

Scope: MDClaw's CLI and skills only. Harness-side (MDDataBench) work (retrying scorer-job submission, rescoring
010 r3, etc.) is not included.

## Summary

| Attempt | Failure code | Direct cause | Main fix location |
|---|---|---|---|
| 004_membrane_5zkb r3 | `node_execution_context_invalid` | prod could be created as a child of topo | Parent-type check in `create_node`, preflight in `submit_job` |
| 010_membrane_6kux r3 | `scorer_submit_failed` (harness side) | After a Slurm timeout, a second job on the same prod node | Handling of an unknown result in `submit_job`, double-run prevention in `begin_node` |
| 015_antibody_1ahw r1 | `agent_no_submission` | A self-written python script to get per-chain residue ranges looped forever | Default output of `inspect_molecules`, `next` on source completion, `prepare_complex` |
| 015_antibody_1ahw r2 | `production_incomplete` | 3 ns put into a single 20-minute job, cut off by the time limit | Time-limit handling in `run_production`, estimate in `submit_job` |

In all 4 cases, the procedure written in the skills was itself correct. When the agent strayed from the skill, the CLI
accepted it, or did not output the needed information by default. Following the design principle (skills state intent, tools refuse),
the fix goes into the CLI, and the skills are only shortened.

Common structure (a session on the MDDataBench side independently reached the same conclusion): **checks that only happen at run time
are invisible to the agent.** By the time a refusal or cut-off happens inside a Slurm job, the agent has already
ended its session (004 r3 ended at 462 of 1800 seconds, 015 r2 at 669 seconds). Had the refusal come in a synchronous call whose result the agent receives,
such as `create_node` or `submit_job`, it could have been fixed in the remaining time. So rather than removing the run-time checks, the same checks are also done at creation and submission time.

## 1. `create_node` does not check the parent type (004_membrane_5zkb r3)

### Sequence of events

- 37 LLM calls, 462 seconds. It read 16 skill pages (including `hpc-run/submit-single.md`).
- It created min, eq, and prod at once, all as children of `topo_001` (verbatim from the transcript):

  ```bash
  ./mdc.sh --job-dir $JD create_node --node-type min --parent-node-ids topo_001 \
    --conditions '{"max_iterations": 5000, "restraint_atoms": "solute_heavy", "restraint_force_constant": 100.0}'
  ./mdc.sh --job-dir $JD create_node --node-type eq --parent-node-ids topo_001 \
    --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0, "nvt_time_ns": 0.5, "npt_time_ns": 1.0}'
  ./mdc.sh --job-dir $JD create_node --node-type prod --parent-node-ids topo_001 \
    --conditions '{"simulation_time_ns": 1.0}'
  ```

- All 3 returned `success: true`. `submit_job` for prod_001 also succeeded, and `condition_preflight` was `code: ok`.
- Jobs: 141574 (min) COMPLETED, 141575 (eq) COMPLETED, 141576 (prod) FAILED.
  - eq ran from topo_001 via the legacy-compatibility path, and the output of min_001 was not used.
  - prod was refused at run time with `Node 'prod_001' cannot run with parent 'topo_001' of type 'topo'; expected one of ['eq', 'prod']`.
- The skill examples (`skills/hpc-run/submit-single.md:8`, `:19`) correctly have min ← topo and eq ← min. The agent strayed.

### Cause (code)

- `create_node` only checks whether the parent node exists (`mdclaw/node/lifecycle.py:544-551`).
- The parent-type table `_ALLOWED_PARENT_TYPES` (`mdclaw/node/constants.py:168`) is used only by the run-time check
  (`mdclaw/node/lifecycle.py:1453-1469`). In this table, the parents of prod are `{"eq", "prod"}`.
  The parents of eq also include `topo` for compatibility.
- The `submit_job` preflight (`mdclaw/slurm/preflight.py:60-108`) only compares the condition values declared by run_production with
  the CLI arguments; it does not look at the node's parent type.
- The only refusal came from `run_production` after the job had started on a compute node. By then
  the agent had already finished (ended at 462 seconds, with 1800 seconds allotted). Had it been refused at creation or submission time,
  it could have been fixed in time.

### Fix

1. **Check the parent type in `create_node`.**
   - For each explicitly given parent, verify `nodes_index[parent]["type"] in _ALLOWED_PARENT_TYPES[node_type]`.
   - If it does not match, refuse with `success: false`, `code: parent_type_invalid` (the code the run-time check already uses).
   - Put the correct parent found by `_auto_resolve_parent` in `next_action`
     (e.g. `mdclaw create_node --job-dir J --node-type prod --parent-node-ids eq_001`).
     Also add "omitting `--parent-node-ids` resolves it automatically".
   - Special combinations written in the table, such as topo ← eq for fep and ABFE, pass as the table says.
   - Check whether `parent_type_invalid` is registered in `mdclaw/guardrail_codes.py` and `tests/data/guardrail_codes.json`,
     and register it if not (`tests/test_guardrail_code_registry.py`).
2. **In canonical study jobs, do not allow eq to be created under topo.**
   - If topo is given as the parent of eq while a min exists under the same topo (or the job has a study context),
     refuse or warn (a new code, e.g. `eq_parent_should_be_min`).
   - Keep topo → eq only for old DAGs without min.
3. **Extend the structural checks in the `submit_job` preflight to every run_*.**
   - Of the run-time checks (the context check in `lifecycle.py`), perform only the node type vs. tool correspondence, the parent type, and whether the node is terminal
     before submission as well. A pending parent is allowed, for chained submission.
   - If any fails, refuse with `node_execution_context_invalid` before calling sbatch.

### Tests and acceptance criteria

- `create_node` refuses prod ← topo, min ← eq, etc. with `parent_type_invalid` and gives the correct parent in `next_action`.
- prod ← eq, prod ← prod, eq ← min, eq ← eq, and ABFE's topo ← eq pass as before.
- The existing tests for eq ← topo in old DAGs without min pass.
- `submit_job` for a prod node whose parent is topo is refused before sbatch is called.

### Other failures the same fix prevents

- 039_ligand_3ikd cli_sif r3, 027_complex_1b6c cli_sif r1 (same pattern).

### skill

No change needed.

## 2. "Unknown-result submission" in `submit_job` and double execution in `begin_node` (010_membrane_6kux r3)

The final failure of the attempt was the harness's scorer-job submission failing with the same Slurm timeout, which is
outside this scope. The problem on the MDClaw side is that, before that, two jobs ran on the same prod node.

### Sequence of events (times in UTC)

- 17:03:11 min (141977) and 17:03:17 eq (141978) were submitted.
- 17:03:33, when submitting prod, sbatch returned
  `sbatch: error: Batch job submission failed: Socket timed out on send/recv operation`.
  In fact, Slurm had created job 141979 (prod_6kux).
- `submit_job` returned `success: false`, `code: unhandled_error`,
  `next_action: "Read the message and errors, fix the reported cause, then retry."`.
- 17:03:46 the agent followed that and resubmitted, creating 141980 (prod_6kux).
- 141979 and 141980 both started at 02:05:09 JST and each computed the same prod_001 on 2 GPUs for about 3 minutes.
  - 141980 became COMPLETED at 02:08:05 and finalized the node.
  - 141979 became FAILED at 02:08:06 with `node_terminal` (`terminal node.json record is sealed`).

### Cause (code)

- sbatch is called at `mdclaw/slurm/submit.py:444`.
  - Every `CalledProcessError` becomes `errors.append("sbatch failed: …")` with no `code` attached
    (`submit.py:528-532`). So the CLI's outer wrapper attaches `unhandled_error` (`mdclaw/_common.py:565`).
  - The `finally` calls `_clear_slurm_submission_intent`, which clears the node's submission reservation (`submit.py:536-541`).
    Even if a job was created, the node looks unsubmitted.
- `begin_node` (`mdclaw/node/lifecycle.py:1187`) only calls `_apply_status(..., "running")`.
  `_apply_status` refuses writes to terminal nodes but allows running → running.
  The second job also computes to the end, and only fails at the completion write.

### Fix

1. **Mark the submission, and query Slurm when the result is unknown.**
   - Put `#SBATCH --comment=mdclaw:<submission_intent_id>` in the sbatch script
     (`submission_intent_id` is already created at `submit.py:431`).
   - For failures whose result is unknown (`Socket timed out on send/recv operation`, `Unable to contact slurm controller`,
     `Zero Bytes were transmitted or received`, `subprocess.TimeoutExpired`, etc.),
     search for the mark with `squeue --me -h -o "%i|%k"` (sacct if unavailable) for 30 to 60 seconds.
     - If found, treat it as submitted (`_stamp_slurm_on_node`, tracking record, `success: true` with a warning).
     - If not found, return `code: slurm_submit_uncertain` and do not clear the submission reservation.
       `next_action` is `mdclaw check_job --job-dir J --node-id N` (searches by the mark; records the job if found,
       otherwise releases the reservation).
   - `_validate_node_ready_for_slurm_submit` refuses resubmission to a node that still holds an unknown-result reservation.
2. **Give other sbatch failures a stable code.**
   - Return `code: slurm_submit_failed` and sbatch's stderr, not `unhandled_error`.
3. **Prevent double execution in `begin_node`.**
   - Inside `node.lock`, record the executor (`SLURM_JOB_ID`, host, pid, start time) in metadata.
   - If the node is already running and the recorded other executor is alive (the Slurm job is RUNNING, or the pid on the same host
     is alive), exit with `node_already_running` before starting the computation.
   - If the recorded executor no longer exists, take over (cleanup after an abnormally terminated job).
   - The `submit_array_job` and `submit_mps_job` paths also go through the same `begin_node`, so they are protected as well.

### Tests and acceptance criteria

- With a fake sbatch that prints a timeout and exits with code 1 but has created the job (a fake squeue returns the mark),
  `submit_job` adopts that job and records it on the node.
- With a fake sbatch that has not created a job, it returns `slurm_submit_uncertain`, the reservation remains, and a second submission is refused.
  `check_job` releases the reservation.
- `begin_node` refuses with `node_already_running` if another live executor exists, and takes over otherwise.

### skill

If needed, add one line to hpc-run: "for `slurm_submit_uncertain`, follow `next_action`; do not resubmit to the same node".
Not needed if `next_action` is sufficient.

### Note

141979, detected by the campaign monitoring as an "isolation escape", is the job that left no record through this sequence.
The job itself was submitted through the isolation wrapper.

## 3. Per-chain residue ranges do not appear in the default output (015_antibody_1ahw r1)

### Sequence of events

- The task is "chain A 1–214, chain B 1–214, chain C 4–211". 1AHW has 6 protein chains, A to F.
- The agent read 16 skill pages, created the study, and fetched the source (source_001 completed;
  `next` was "create prep").
- `skills/md-prepare/SKILL.md:144` instructs running `inspect_molecules`, but the agent skipped it.
  Instead, it wrote its own python to output per-chain author-numbering ranges and gaps, and ran it over chains A to F.
- In that python, the `i += 1` of the inner `while` was only inside `if p[idx['label_asym_id']] == ch:`.
  At the first atom of a different chain, `i` stopped advancing, causing an infinite loop.
- This tool call never returned; the agent used up its 1800-second allotment, and no job was submitted.

### Cause (output)

- In the default output of `inspect_molecules`, `chains` is omitted entirely
  (`{"_omitted": true, "chars": 104090, "see": "rerun with --output full"}`, `mdclaw/_envelope.py:284`).
  `summary` and `action_contract` contain only chain IDs.
- `chains[].residue_numbering` in `--output full` has `count`, `first`, `last`, and a list of all residues.
  Chain C of 1AHW has first 4 THR, last 211 GLY, count 200, with an 8-residue gap within 4–211.
- In other words, even using the recommended tool, the information the agent wanted is not visible by default.
- The `next` on source completion skips `inspect_molecules` and points to creating prep.

### Fix

1. **Add `chain_ranges` to the default output of `inspect_molecules` (not subject to omission).**
   - One row per chain: `chain_id`, `author_chain`, type, first and last residue (author number, insertion code, residue name), residue count,
     gaps (a list of intervals in author numbering), and residues with insertion codes (e.g. `1A`).
   - Even 6 chains fit in a few hundred characters.
2. **Put the same `chain_ranges` in the result on source completion, and have `next` point to `inspect_molecules` before prep.**
   - Targets are `fetch_structure` and `register_local_structure`.
3. **Return `kept_residue_ranges`, the range actually kept per chain, in the result of `prepare_complex --residue-ranges`.**
   - Warn in these cases: the specified range spans a gap (show the missing residues), an insertion-code residue with the same number as a boundary
     is dropped (e.g. `A:1-79` drops `1A`), or the specified range exceeds the residues present in the structure.

### Tests and acceptance criteria

- For a structure with gaps and insertion codes, `chain_ranges` is correct and is included in the default output.
- `prepare_complex` returns `kept_residue_ranges` and warns about dropped insertion codes and ranges that span gaps.

### Other failures the same fix prevents

- 036_ligand_1ceb cli_sif r2 (`A:1-79` dropped 1A).
- 028_complex_1dfj cli_sif r1 and r2 (no range specified; the N-terminal ACE of chain I remained).
- 024_antibody_5cba cli_sif r2 (no range specified; chain B had one extra residue).
- In every case, had the kept range appeared in the result, the agent could have checked it against the task specification.

### skill

Shorten step 4 of `skills/md-prepare/SKILL.md` to "check residue numbers and gaps with the source result or `inspect_molecules`'s
`chain_ranges`; do not parse structure files yourself".

## 4. `run_production` does not look at the job time limit (015_antibody_1ahw r2)

### Sequence of events

- The harness caps a single MD job at 20 minutes (the agent's CAPABILITIES.md also says
  "Each MD Slurm job wall limit: 00:20:00").
- The agent submitted as follows (with an afterok dependency on eq):

  ```bash
  submit_job --job-dir "$JOB" --node-id prod_001 \
    --script "mdclaw --job-dir $JOB --node-id prod_001 run_production --simulation-time-ns 3.0 --output-frequency-ps 10.0 --platform CUDA" \
    --job-name prod_1ahw --partition gpu --gpus 1 --cpus-per-task 4 --time-limit "00:20:00" --memory 64G \
    --dependency afterok:144251
  ```

- eq_001 had finished 1 ns of NVT and 1 ns of NPT (4 fs, HMR) in 11 minutes 54 seconds. At about 10 ns/hour,
  3 ns takes about 18 minutes, and adding setup time exceeds 20 minutes.
- Job 144252 was cut off at 1221 seconds "DUE TO TIME LIMIT".
  - Remaining artifacts: trajectory.dcd (1.49 GB), state.xml and checkpoint.chk (written at the time of the cut-off),
    energy.dat.
  - prod_001 stayed `running`, and scoring gave `production_incomplete`.
  - The agent had already finished at 669 seconds.
- The passing r3 split the prod into two 1.4 ns runs (`--continue-from`), each finishing in about 10 minutes.

### Cause (code)

- `mdclaw/simulation/production.py` has no handling that looks at the job time limit
  (none of `SLURM_JOB_END_TIME`, walltime, time_limit, or deadline appears).
- The eq metadata has the step count, but not the computation speed (ns/day) or elapsed seconds.

### Fix

1. **Stop `run_production` before the time limit and close the node in a usable state.**
   - The deadline is the job's end time (the environment variable `SLURM_JOB_END_TIME` if present, otherwise
     `squeue -h -j $SLURM_JOB_ID -o %e` or `scontrol show job`).
   - Stop integration at the deadline minus a margin (e.g. the larger of 90 seconds and two output intervals).
     Write a portable state and close the trajectory and energy files.
   - Mark the node completed, record the actual `simulation_time_ns`, `requested_simulation_time_ns`, and
     `stopped_reason: "time_limit"` in metadata, and attach a warning.
   - In `next`, show the remaining length and the extension procedure (`create_node --node-type prod --continue-from <node>`).
   - Completed nodes cannot be changed, so extension is done with the existing prod → prod.
2. **Record the computation speed in the metadata of min, eq, and prod.**
   - `ns_per_day` and elapsed seconds.
3. **Estimate the production run time in the `submit_job` preflight.**
   - Estimate = `simulation_time_ns` ÷ the `ns_per_day` of the nearest completed ancestor + setup time.
   - If it exceeds `--time-limit` minus a margin, refuse with `production_exceeds_time_limit`.
     In `next_action`, show the length per run and how to chain them with `--continue-from` (e.g. "2 runs × 1.5 ns").
   - If the ancestor has not completed yet (chained submission), only warn.
4. (Rare after 1.) In node synchronization, treat Slurm TIMEOUT as a failure with `slurm_time_limit`,
   and point to the last written state, so that `trace_failure` can propose an extension.

### Tests and acceptance criteria

- Running `run_production` with `SLURM_JOB_END_TIME` set to a near time, it stops before the limit, records the actual length,
  becomes completed, and `next` points to an extension.
- If the estimate from `ns_per_day` in metadata exceeds `--time-limit`, `submit_job`
  refuses with `production_exceeds_time_limit`.

### skill

Replace the explanation in `skills/md-production/` and `skills/hpc-run/` with one line: "split a production that exceeds a single job's time limit into prod nodes
chained with `--continue-from`. MDClaw stops before the limit and returns how to extend."

## Implementation order

1. **Parent-type check in `create_node`** (fixes 1 to 3 of section 1). A small change using the existing table that prevents 3 failures
   (including 2 in cli_sif).
2. **Stopping `run_production` at the time limit, recording computation speed, and the pre-submission estimate** (4). Prevents data
   being left in limbo by a time-out.
3. **Handling of unknown results in `submit_job` and prevention of double execution in `begin_node`** (2). Double execution falls under
   "things that must never happen".
4. **`chain_ranges`, the source `next`, and `prepare_complex`'s `kept_residue_ranges`** (3).

After the changes, update `docs/developer/tool-reference.md` and the affected skill examples (`CLAUDE.md` "Adding Or Changing Tools").
As verification, it would be good to rerun, at small scale under cli_skill_sif and cli_sif, these 4 tasks (004, 010, 015) and 027, 028, 036, and 039, which had the same pattern in cli_sif.

## Where the evidence is

- Attempt directories:
  `/data1/$PROJECT/$USER/runs/glm-5.3-flash-3cond-full-v4/attempts/<task>/<condition>__pi__rikyu-glm-5.3-flash__rN/`
  - `agent.stdout.jsonl`: the pi transcript (commands and outputs on the `tool_execution_start` / `tool_execution_end` lines).
  - `result.json`: failure code, metrics, and each scoring item (`artifacts.score`).
  - `events.jsonl`: agent start/end and scorer submission.
  - `workspace/.mddatabench/sbatch-events.jsonl`: sbatch calls and results recorded by the shim.
  - `workspace/study/jobs/main/nodes/<node_id>/node.json`: MDClaw node status and metadata.
- Slurm jobs: 004 r3 = 141574 to 141576, 010 r3 = 141977 to 141980, 015 r2 = 144250 to 144252,
  015 r3 (passed, for comparison) = 144255, 144257, 144259, 144260.
- Example of listing tool calls from a transcript:

  ```python
  import json
  starts = {}
  for line in open("agent.stdout.jsonl", errors="replace"):
      if '"tool_execution_start"' in line:
          r = json.loads(line); starts[r["toolCallId"]] = (r.get("args") or {}).get("command") or ""
      elif '"tool_execution_end"' in line:
          r = json.loads(line)
          out = "".join(c.get("text", "") for c in (r.get("result") or {}).get("content", []) if c.get("type") == "text")
          print("CMD:", " ".join(starts.get(r["toolCallId"], "").split())[:200])
          print("OUT:", " ".join(out.split())[:300])
  ```

## Implementation status (2026-09-29)

All 4 sections were implemented, with unit tests added (details in the same day's entry in `docs/memo.md`). Differences from the plan:

- §1: `parent_type_invalid` and `eq_parent_should_be_min` in `create_node` are as planned. Creating eq under a topo without min
  in a study context is only a warning (`eq_parent_is_topo`), not a refusal. The structural check in `submit_job` /
  `submit_array_job` / `submit_mps_job` is `node_structure_preflight` (parent type, failed parents,
  tool vs. stage correspondence; pending parents pass; a bare directory without progress.json is not judged).
  At the same time, an envelope bug was fixed in which the recommended tool for the prod stage had been `run_metadynamics` since
  `run_metadynamics` was added (`_STAGE_PREFERENCE["prod"]`).
- §2: The mark is `#SBATCH --comment=mdclaw:<intent>`. When the result is unknown, `find_marked_job` searches squeue for up to 30 seconds
  (`MDCLAW_SLURM_CONFIRM_SECONDS`), adopts the job if found, and otherwise keeps the reservation with `slurm_submit_uncertain`.
  Resolution is `check_job --job-dir J --node-id N` (`settle_uncertain_submission`). Double-run prevention has
  `begin_node` use the rounds owner record (moved to `mdclaw/node/owner.py`; the old path is a shim).
  Slurm end-of-job synchronization does not fail a running node held by another live executor.
  Array / MPS submissions are not marked (single submissions only).
- §3: `inspect_molecules.chain_ranges` (`compact_numbering`), `chain_ranges` on source completion
  (metadata and result, `next.inspect_command`), `prepare_complex.kept_residue_ranges`
  (`split_molecules.delivered_chain_ranges`), and the range warnings.
- §4: `DeadlineStepper` (`mdclaw/simulation/deadline.py`) stops `run_production` before the deadline,
  marks it completed with the actual length, and sets `next` to an extension. Rounds segments are excluded. eq records
  `md_seconds` / `wall_seconds` / `ns_per_day`, and min records `wall_seconds`. The `submit_job` estimate is
  `production_time_budget` (only when the parent's `ns_per_day` is from the same platform family). Fix 4's
  reinterpretation as `slurm_time_limit` was deferred, since fix 1 reduced the need for it.

## Out of scope (for reference)

- Harness side: rescoring 010 r3 (the MD is COMPLETED, so it is expected to pass), retrying scorer-job submission,
  canceling old jobs on reset, etc. The campaign-wide results and the sif_only analysis are summarized in a draft memo
  on the MDDataBench side (not yet appended).
