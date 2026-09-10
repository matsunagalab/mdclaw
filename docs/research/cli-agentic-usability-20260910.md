# The CLI's contract and the skill's procedure: root causes from the campaign and a division of responsibility

2026-09-10, revised the same day with a deeper pass. Evidence: the first 33 sealed
`cli_skill_sif` and 33 sealed `cli_sif` attempts (pi + kimi-k3, 20-minute agent
budget, image mode, membrane tasks 001-011) of the three-condition campaign
`/data1/rkp00079/rku00161/runs/kimi-k3-3cond-full`, read through the campaign's
transcript metrics (`metrics.phases`, `mdclaw_error_codes`, `recovery`,
`timeline.jsonl`) and, for every error, the command that caused it, the result
it received, and what the agent did next. The MDClaw code paths behind each
error were read to confirm the mechanism.

The first version of this note treated the no-skill condition's failures as a
missing mental model and proposed messages to supply it. That is still true,
but it is the smaller finding. The larger one is that four of the root causes
below also cost the *skill* condition calls, time and correctness, and all four
are the CLI's to fix: the skill cannot work around a wrong error code, a
2.9 MB result, or a validation gap. The proposals are therefore reorganised
around one question: what the CLI must say about the DAG it manages, and what
is the skill's to say about the science.

## 1. Numbers

| | cli_skill_sif | cli_sif |
|---|---|---|
| attempts / passed (first 30) | 30 / 29 | 30 / 8 |
| agent timeouts | 5 | 23 |
| `--help` reads per attempt | 2.0 | 11.4 |
| share of agent time in discovery (help, probes, polling) | 0.66 | 0.73 |
| MDClaw errors per attempt | 0.7 | 1.6 |
| median call index of the first successful node | 11 | 9 |
| MDClaw invocations (33 attempts) | 347 | 557 |
| invocations run with `2>&1` | 52 % | 91 % |
| invocations whose result the agent's parser could not read | 3 | 14 |
| invocations truncated with `head` | 74 | 176 |
| stage tools run in the background and polled with `sleep` | 68 / 338 polls | 81 / 213 polls |
| agent reads of MDClaw source code from the image | 0.2 per attempt | 5.5 per attempt |

Where the skill condition's discovery time goes (probe-phase calls per attempt,
seconds): `inspect_job`/`explain_node` 5.5 calls / 139 s, reading node and
progress JSON 2.8 / 52 s, reading artifacts and reports 3.2 / 61 s, help and
`--list-json` 2.3 / 57 s, `sleep` polling 1.6 / 84 s, and 8.7 / 161 s of
python one-liners that parse or trim a result. The no-skill condition spends
its time on help (8.2 / 191 s), artifacts (8.6 / 189 s) and source code
(5.5 / 70 s) instead.

Stage tools themselves are fast on these membrane systems: completed-node wall
times (skill condition, medians) are prep 22 s, solv 65 s, topo 80 s, source
25 s. The 20-minute budget is spent around the tools, not in them.

## 2. Root causes

### RC1. `create_node` cannot attach to a pending chain, and misreports it

`_auto_resolve_parent` (`mdclaw/node/lifecycle.py`) resolves an omitted
parent only to a *completed* leaf of the preferred parent type. When the
preferred stage exists but is not completed it returns `None` on purpose (the
docstring: pre-creating `min -> eq -> prod` "cannot silently attach eq to topo
and skip min"). `create_node` then fails with `code: node_context_required`,
`error: "Cannot choose a parent for node type 'eq'. Pass --parent-node-ids
explicitly."`, `candidate_parent_node_ids: []` (candidates are also filtered
to completed nodes), and a message, hints and `next_action` that all say
"Create the node, then run it with both --job-dir and --node-id" -- the
generic action for that code, which is about running a tool, not creating a
node.

On a batch cluster `min`, `eq` and `prod` are created before any of them has
run, because they are submitted as one dependency chain. The skill's
`run-loop.md` says parents "resolve themselves" and to pass them only when
branching; its `submit-single.md` example passes `--parent-node-ids`
explicitly. Agents that followed the general rule hit this eleven times in 33
attempts (`node_context_required` was the skill condition's most frequent
code). In 004 r3 the prod node was never created because the agent's pipeline
extracted `node_id` from the error result and got `None`; it then ran
`submit_job --node-id prod_001` and received `slurm_node_unavailable: Cannot
read DAG node ... No such file or directory`, a second misleading message for
the same cause.

The docstring's concern is real, but the conclusion is wrong: when exactly one
node of the preferred stage exists and it is pending or running, it *is* the
chain being built and the only sensible parent. Ambiguity is two candidates,
not one incomplete one.

### RC2. Results are hard to consume, and misreading them causes real failures

Four mechanisms, all observed.

**(a) Size.** A `prepare_complex` result written to a file by a skill agent was
2,870,329 bytes: `residue_range_coverage` 615 kB, `chain_identity_map` 468 kB
(already an artifact file on disk), `split` 284 kB. Nothing reads it whole. The
skill agents redirected 61 results to files and piped 141 through python
one-liners; the no-skill agents truncated 176 with `head`. Each of those is a
model call, and each re-sends the growing context.

**(b) Merged streams.** The CLI's contract is JSON on stdout, logs on stderr.
Agents run 52 % (skill) and 91 % (no-skill) of invocations with `2>&1`, and
42 / 97 of those feed the merged stream into a parser. In 003 r3 the skill
agent's `fetch_structure ... 2>&1 | python3 -c "json.load(sys.stdin)" || echo
FAILED` printed `FAILED` although the fetch had succeeded and sealed
`source_001`; the agent ran it again and got `node_terminal`. Three parse
failures in the skill condition and fourteen in the no-skill condition were
recorded in tool output.

**(c) Truncation.** In 007 r1 the agent ran `inspect_job | head -40; create_node
--node-type source | tail -20`, saw neither `success` nor `node_id`, created
the source again, got `source_already_exists` ("One source per job; add to the
existing bundle or use another job.", which names no id), guessed
`source_001`, and continued. In 008 r1 a pipeline's `json.load(...)["node_id"]`
raised `KeyError` on an error result, the shell variable stayed empty, and the
next command became `mdclaw --node-id fetch_structure ...` with `pdb` parsed
as the tool name (`tool_not_available: Unknown MDClaw tool 'pdb'`). Five of
the skill condition's `node_terminal` errors are re-runs of `fetch_structure`
on a source node that the previous, unread result had already completed.

**(d) Guidance that exists but is not reachable.** Every workflow result already
carries `dag_handoff.default_forward_branch.create_command` (attached in
`_cli.py::_attach_dag_handoff`), and `create_node` returns `next_command`
(`explain_node ...`) plus an embedded preflight. Both sit at the bottom of the
payloads above. `explain_node` reports readiness, parents and inputs but not
which tool runs the node; the skill's step 4 says `<suggested_tool>`, and a
skill agent printed `suggested: None` from it.

### RC3. The no-skill agent has no source for the DAG model

The contract the agent needs is short: a node is created for a stage and
executed by that stage's tool with `--job-dir/--node-id`; inputs resolve from
the parent; nodes run once; a retry is a new node. Nothing the agent can read
says this in one place. `mdclaw --list` opens with "MD workflow: follow the
matching skill", and its one-line "DAG: create_node -> explain_node -> stage
tool" compresses the model past usefulness. `--help` is the full docstring
(`embed_in_membrane` 16,230 characters, `prepare_complex` 8,601); the agent
needs the six flags it will use. It read 11 help pages per attempt and, in 5.5
calls per attempt, the package's Python source inside the image
(`node/lifecycle.py`, `node/inputs.py`, `solvation/membrane.py`) to find the
rules. Its errors are the model's absence made concrete: `--node-id prep_005`
for `embed_in_membrane` ("has type 'prep', expected 'solv'", 6 of 15
`node_execution_context_invalid`), `--pdb-file` pointing at intermediate files
while naming a node, `topo_001` created under a prep it never ran ("Parent
node 'prep_001' must be completed ... (status='pending')"), stage tools
re-run on terminal nodes (11 `node_terminal`, 0 recovered), `update_workflow_state
--status completed` by hand (`node_terminal_transition_reserved`), invented
node types (`membrane`, `minimize`), and standalone helpers (`clean_protein`,
`split_molecules`) run as if they were stage tools (6
`missing_required_arguments`, all followed by a timeout). Twenty-three of 30
no-skill attempts timed out; nine never completed a node past source.

### RC4. Validation gaps become unhandled exceptions

- `embed_in_membrane` (`solvation/membrane.py:1964`) carries no
  `@node_tool` decorator; `solvate_structure` does. The CLI therefore skips
  its node preflight and the tool opens `nodes/<id>/node.json` itself, so a
  node the agent never created is a `FileNotFoundError` instead of
  `node_missing` (003 r1, 009 r1).
- `--json-input` keeps keys that are not parameters: `_cli.py` coerces known
  keys and passes the rest to the function, so `{"job_dir": ..., "node_id":
  ..., "pdb_file": ...}` to `clean_protein` is `TypeError: clean_protein() got
  an unexpected keyword argument 'job_dir'` (003 r2). The flag route would
  have refused the same input.
- `clean_protein --protonation-method pdbfixer` raises `ValueError:
  Unsupported protonation_method 'pdbfixer'. Supported: propka, standard`
  from inside the tool; other enums go through the guardrail path with a code
  and the allowed values.
- `prepare_complex` on a pending node whose `artifacts/` already holds content
  (the agent had merged and split by hand into it) de-duplicates its working
  directories to `merge_2/`, `split_2/` but registers the artifact under the
  fixed `artifacts/merge/merged.pdb` (`prepare_complex.py:3164`), and
  `complete_node` raises. A node is single-execution; foreign content in its
  artifact directory is an error to report, not a variant to accommodate.

### RC5. Nothing tells the agent that a stage tool is still working

Sixty-eight stage-tool invocations in the skill condition were backgrounded
and followed by 338 `sleep` polls; 81 / 213 in the no-skill condition. The
tools print nothing to stdout until the final JSON, and stderr logs are
sparse during packmol-memgen and topology building. No per-command watchdog
was active in this campaign (pi's `shellPath` is unset), so the caution was
the agent's own, and every poll is a model call with the whole context.

### RC6. Errors say what is wrong, not what to do

The guardrail messages are correct, and the `next_action` mechanism exists
(`_common.py` supplies `trace_failure` for node failures and the code's
generic action otherwise). But `node_terminal` does not say which parent the
new node should hang from or that `create_node` resolves it; `node_missing`
does not list the ids that exist; `source_already_exists` does not name the
existing source; the type-mismatch message does not say what `--node-id`
means; and no error carries the DAG's current state, so the agent's next call
is `inspect_job` if it knows the tool, and a guess if it does not. Recovery
after an error in the no-skill condition: 6 of 46 episodes.

## 3. Who owns what

The line is drawn so that neither side has to describe the other's contract.

**The CLI owns the DAG contract and its state, and must be usable from its
own output alone.**

- Structure: node types and their canonical order, parent rules, statuses and
  transitions, what a node needs before it can run, what it produced, and what
  comes next structurally (`create_node` for the next type, the tool that runs
  it, how inputs will resolve).
- State: the current frontier and every node's status, in every workflow
  result and error, so no extra call is needed to orient.
- Results: one compact, machine-parseable envelope whose first lines carry
  `success`, `code`, `node_id`, `status` and `next_action`; full detail in
  files that the envelope names; nothing but JSON on stdout.
- Validation: every misuse a structured code with the allowed alternatives; no
  raw exception reaches the agent for an input the CLI could have checked.
- Self-description: what each tool is (stage tool or standalone helper), the
  workflow in one screen, short help.
- Execution: visible progress on long steps; re-entry that is safe by
  construction (a completed node reported as completed, with what to do next).

**The skill owns scientific procedure and judgement, and the site's conventions.**

- Which stages a system needs (membrane versus soluble, implicit versus
  explicit), and which choices to make at each: force field and water model,
  protonation policy, restraints, equilibration protocol, production length,
  HMR, how to split production under a wall limit, when a guardrail's verdict
  should change the plan.
- How to read guardrail outcomes scientifically (a blocked water model is a
  chemistry decision, not a syntax error).
- Harness and site discipline: submit as a dependency chain and exit, do not
  poll, where the runtime lives, site scheduler flags.

**Two rules that follow.** The skill never restates a mechanic the CLI reports
at run time; it says "act on `next` and `next_action`" and shows one example.
The CLI never recommends a scientific parameter; it validates and names
alternatives. A sentence like "parents resolve themselves" belongs to the
CLI's result, not to a skill page, because only the CLI knows when it is
true.

| root cause | CLI change | skill change |
|---|---|---|
| RC1 parent resolution | resolve pending chains; `parent_required` with candidates | drop the "resolve themselves" promise; show `next.create_command` |
| RC2 result consumption | brief envelope, result file, stdout hygiene, `next` block, `existing_node_id` | drop the trim-and-parse workarounds |
| RC3 DAG model | `--workflow`, grouped `--list`, short help, `next` on every result | nothing (the skill already carries the model for its readers) |
| RC4 validation | decorator audit, `--json-input` key check, enum helper, populated-node refusal | nothing |
| RC5 long steps | progress heartbeat on stderr | "run stage tools in the foreground; typical durations" |
| RC6 error content | fix-carrying messages with DAG state | reference `next_action` instead of restating rules |

## 4. CLI proposals

### C1. Parent resolution that understands a chain

- `_auto_resolve_parent`: if the preferred parent stage has exactly one node
  and it is pending, running or completed, attach to it; refuse only when it
  has none (fall through to the next preference, as now) or more than one
  (ambiguous). A failed node is never a candidate.
- Replace the misfiled error with `code: parent_required`, `message: "eq
  needs a parent; candidates: min_001 (pending), min_002 (completed)"`,
  `candidate_parent_node_ids` including pending and running nodes with their
  status, `candidate_commands`, and `next_action` set to the first candidate
  command. Keep `node_context_required` for its real meaning.
- `create_node` records `auto_resolved_parent` in its result (the skill
  already expects it).

### C2. A result envelope an agent can read from the top

- Every tool result starts with the same keys in this order: `success`,
  `code`, `message`, `node_id`, `node_status`, `next_action`, `warnings_count`,
  `result_file`. Stage tools write the full result to
  `nodes/<id>/artifacts/result.json` and return a `summary` block (for
  `prepare_complex`: chains, residues, ligands, disulfides, protonation policy,
  artifact keys) instead of embedding coverage maps and identity maps that
  are already artifacts. `--output full` restores today's payload.
- stdout carries only the JSON. Logs stay on stderr, and a
  `MDCLAW_LOG_FILE` / `--log-file` option redirects them so an agent's `2>&1`
  cannot corrupt the stream. As a second line of defence the JSON is preceded
  by a single marker line (`--- mdclaw result ---`) so a parser given a merged
  stream can still find it; the CLI already uses that marker in failure
  artifacts.
- `create_node --print id` (or `--output id`) prints the bare node id, for
  shell composition without a parser. Error results always include the keys
  a caller might index (`node_id: null`), so `KeyError` cannot empty a
  variable.
- `source_already_exists` and every "already exists" code return
  `existing_node_id` and a `next_action` that uses it.

### C3. The DAG in every workflow result

Add two blocks to every result of `create_node`, `explain_node`, `inspect_job`
and the stage tools, success or error:

```
"dag":  {"leaves": ["topo_001"], "pending": ["min_001"], "running": [],
         "failed": ["topo_002"], "completed": 6}
"next": {"node_type": "min",
         "create_command": "mdclaw create_node --job-dir J --node-type min --parent-node-ids topo_001",
         "stage_tools": ["run_minimization"],
         "run_command": "mdclaw --job-dir J --node-id <new> run_minimization ...",
         "inputs": "auto_resolved"}
```

`next` is structural: which node, which tool, that inputs resolve. It carries
no parameter values; those are the skill's. `explain_node` gains
`stage_tools` for the node's type and regime (`solv` in a membrane job lists
`embed_in_membrane`, otherwise `solvate_structure`). This replaces the
existing `dag_handoff.default_forward_branch` and reuses `inspect_job`'s
frontier computation, so it is a refactor of what exists, not new logic.

### C4. Errors that carry the fix and the state

Every code's `message`, `hints` and `next_action` name the command that
resolves it, with ids filled in from the DAG, and the error carries the `dag`
block of C3. Specifically:

| code | proposed message |
|---|---|
| `node_type_mismatch` / type-mismatch inside a tool | "`--node-id` names the node this tool executes; `embed_in_membrane` runs a `solv` node. Create one: `mdclaw create_node ... --node-type solv --parent-node-ids prep_005`, then run it with the returned id; the prepared structure resolves from prep_005 (no `--pdb-file`)." |
| parent pending (`node_execution_context_invalid`) | "Parent `prep_001` is pending: run its stage tool first (`mdclaw --job-dir J --node-id prep_001 prepare_complex ...`). Files produced outside the DAG are not inputs." |
| `node_terminal` | "Nodes run once. Branch: `mdclaw create_node ... --node-type prep --parent-node-ids source_001` and put the corrected flags on the new node. `trace_failure --node-id prep_001` lists recovery options." |
| `node_missing` | "`prep_009` does not exist. Existing nodes: prep_001 (completed), prep_002 (failed) ... `create_node` assigns ids." |
| `node_terminal_transition_reserved` | "Stage tools seal their node on success; `prep_001` is already `completed`. If it failed, branch as above." |
| `missing_required_arguments` on a standalone helper | "`clean_protein` is a standalone helper (file in, file out). Inside a study use the stage tool `prepare_complex` on a prep node; it needs no file arguments." |
| `source_already_exists` | "This job's source is `source_001` (completed). Next: `mdclaw create_node ... --node-type prep --parent-node-ids source_001`." |
| `invalid_node_type` | keep the list, add one clause per type, and "membrane embedding is a `solv` node". |

### C5. Validation completeness

- Audit: every tool that reads `nodes/<id>/node.json` must be declared with
  `@node_tool`; add a test that asserts it (the decorator is the CLI's only
  signal for node preflight). Declare `embed_in_membrane` as `solv`.
- `--json-input`: unknown keys are `unknown_parameter` with the accepted
  names; node context keys on a non-node tool are `node_context_not_applicable`
  with the stage tool named.
- Enumerated parameters validate through one helper that emits `code`,
  `allowed`, `received`; `clean_protein.protonation_method` first.
- A stage tool run on a pending node whose artifact directory is not empty
  returns `node_artifacts_not_empty` listing the content; artifact paths are
  registered from the tool's actual outputs, never from a fixed string.

### C6. Self-description without a skill

- `mdclaw --workflow`: the canonical chain (soluble and membrane), ~25 lines of
  real commands, the four rules of the contract stated once. `--list` prints
  its first ten lines instead of "follow the matching skill" and groups tools
  as *stage tools (DAG)* and *standalone helpers*; `--list-json` already has
  `requires_node` and `node_type` for that.
- `<tool> --help` shows required flags, the five most used optional flags and
  one example; `--help-full` shows the docstring.

### C7. Progress on long steps

Stage tools that run external programs or minimisation print a heartbeat on
stderr (`[embed_in_membrane] packmol-memgen running, 90 s`). An agent that
sees output keeps the command in the foreground; the skill can then say so.

### C8. Bootstrap does the first step

`bootstrap_md_workflow` creates `source_001`, returns C3's `next` for it, and
with `--pdb-id` runs the fetch in the same call. The skill's acquisition page
becomes shorter, not longer.

### C9. A receipt of what the run applied (added 2026-09-10 evening)

Agents do not trust a stage tool's exit status; they verify. In the 64
sealed `cli_skill_sif` attempts of the campaign, the three commands after a
stage tool were, in most attempts, scripts reading the artifacts:

| what the agent checked by hand | attempts (of 64) |
|---|---|
| ligand presence and charges | 46 |
| node status and artifact files | 43 |
| water model, force field, HMR | 38 |
| lipids and membrane orientation | 37 |
| atom and residue counts | 35 |
| ion counts and salt concentration | 33 |
| disulfides | 32 |
| protonation states (HID/HIE) | 27 |
| box dimensions | 26 |
| a bond across a chain gap | 18 |

The answers are in the result already, but scattered across the large
blocks (`residue_range_coverage` 102 KB, `chain_identity_map` 78 KB,
`split` 47 KB in one real prep result), so the agent reads files instead;
013_membrane_6ps2 r1 wrote fourteen such scripts after `build_amber_system`
and ran out of its budget without submitting.

Proposal: every stage tool result carries an `applied` block directly after
`message`, protected from brief stubbing, in two parts.

- **Per option given by the caller: what it did.** `--residue-ranges
  A:28-230,A:263-342 -> 2 components (203 + 80 residues); gap A:231-262
  left open, no bond across; termini NH3+/COO-`. A value the tool changed is
  reported as `requested -> effective (reason)`; an option that has no
  effect in this mode (`output_dir` in node mode, a water model that the solv
  node decides) is listed under `ignored_options` instead of silently
  accepted. This is the generic layer: the CLI compares the kwargs it passed
  with the tool's `parameters` block.
- **Per stage: the facts the table above shows agents verify.** prep:
  chains kept and dropped with residue ranges, ligands kept and dropped with
  names, charges and protonation method, disulfides formed, gaps left open or
  rebuilt, terminus treatment, HIS state counts, net charge. solv / embed:
  water model, box, atom count, ion counts and concentration, neutralization
  result, lipids per leaflet, orientation method. topo: force fields actually
  loaded (protein, lipid, water, ligand), water model and where it came
  from, HMR and timestep, atom count, net charge, ligand charge method,
  disulfides in the topology, "bonds across gaps: none". min / eq / prod:
  integrator, timestep, temperature, pressure, ensemble, restraint count,
  platform actually used, restart source node.

`message` on success becomes the one-line form of the receipt: `prep_001
completed: chain A as 2 pieces (A:28-230, A:263-342; gap left open), 0
ligands, 2 disulfides, net charge -3`. No new computation is needed; each
stage tool already holds these facts (`component_disposition_summary`,
`disulfide_bonds`, merge statistics, ion counts, `amber_metadata`,
`integrator_signature`). Implementation: a generic requested-vs-effective
layer in `_cli.py` / `_envelope.py`, and one `applied` builder per stage
tool.

## 5. Skill proposals

- **S1 run-loop.md.** Replace "parents resolve themselves ... only pass
  `--parent-node-ids` when branching" with: "use `next.create_command` from the
  previous result; the CLI resolves the parent and reports it. Pass
  `--parent-node-ids` yourself only to branch." Remove the `<suggested_tool>`
  placeholder in favour of `next.stage_tools`.
- **S2 prepare-complex.md and run-loop step 4.** After C2, stop teaching
  redirect-and-parse; say "read `summary`, `warnings_count` and `next`; the
  full result is in `result_file`". Until C2 lands, say explicitly "do not
  merge stderr into the JSON stream; parse stdout only".
- **S3 hpc-run and md-prepare.** "Run stage tools in the foreground; preparation
  steps take seconds to a few minutes on a login node (prep ~30 s, membrane
  embedding 1-3 min, topology 1-2 min); do not background or poll." Keep the
  submit-and-exit discipline.
- **S4 acquisition.md.** After C8, "bootstrap creates and fetches the source;
  do not create a second source node". Before C8, the page is right as
  written; the failures came from unread results (RC2), not from the text.
- **S5 skill-conventions.md.** Add the rule from section 3: skills do not
  restate DAG mechanics; they cite the CLI's `next` and `next_action` fields.

## 6. Expected effect, by condition

Skill condition (33 attempts, 24 errors): C1 removes the 11 parent failures
and the cascaded `slurm_node_unavailable`; C2 removes the 5 `node_terminal`
re-fetches, the duplicate `create_node`, the `source_already_exists` without
an id and the `tool_not_available 'pdb'` cascade; C7 with S3 removes most of
the 338 polls. That is roughly 20 of 24 errors and, from the phase data, a
quarter of the discovery time. The one failed skill attempt (004 r1) timed out
with the pipeline complete and nothing submitted after 7 minutes of reading
and 4 minutes of pre-submission checks; C2 and C3 shorten both.

No-skill condition (30 attempts, 8 passed): C3, C4 and C6 address the model
gap (RC3) that produced 23 timeouts; C5 removes the 5 unhandled exceptions.
It will still lag the skill condition on scientific choices, which is the
comparison the benchmark is meant to make.

## 7. Validation and rollout

CLI changes land in the checkout with tests, then in a rebuilt SIF; skill
changes land on `main` and reach agents through `pi install`. Measure on the
same eleven membrane tasks, both CLI conditions, three attempts each: errors
per attempt (0.7 / 1.6), timeouts (5 / 23), first-node call (11 / 9), polls
per attempt (10 / 6), result-parse failures (3 / 14) and passes (29 / 8). The
campaign's `recovery.csv` and `error_codes.csv` give the per-code deltas
directly.

## 8. Implementation status (2026-09-10, same day)

All of C1-C8 and S1-S5 are implemented in the checkout (see the memo entry
"CLI proposals C1-C8 and skill edits S1-S5 implemented"). Two things changed
relative to the proposals above while implementing them:

- **`parent_not_completed` preflight (new).** Once C1 allowed a chain of
  pending nodes, the first smoke test showed the next failure: running
  `prepare_complex` on a prep node whose source was still pending sealed the
  prep node as `failed` (`input_resolution_blocked` inside the tool calls
  `fail_node_from_result`). The CLI now refuses such a run before the tool
  starts, the node stays pending, and the error carries the parent's stage
  command (`next` points at the parent with `blocked_node_id`). This
  replaces the pending-parent variant of C4's
  `node_execution_context_invalid` for the CLI path; the in-tool check keeps
  its fix-carrying result for programmatic callers.
- **`--help-full` was not needed.** `--help` shrank to one screen by hiding
  the subcommand enumeration (`metavar="<tool>"`); `--list` and `--workflow`
  carry the index and the contract, and per-tool `--help` is unchanged.

Also found: the ligand-chemistry fix of the same morning (commit `8aa6be6`)
had moved `@node_tool(node_type="solv")` off `embed_in_membrane` onto the
inserted helper, so RC4's "missing decorator" was a regression of that
commit, present in the SIF the campaign is running (`293fb7d1…`). A
source-scanning audit test now prevents the class.

Validation (section 7) remains to be run: rebuild the SIF, then rerun the
eleven membrane tasks in `cli_skill_sif` and `cli_sif`.

C9 (the `applied` receipt) was added to this note in the evening from the
campaign's verification-script counts and implemented the same night
(`mdclaw/_receipt.py`; memo entry "C9: every completed stage returns an
applied receipt").

Evening, from the campaign's five real `cli_skill_sif` failures (memo entry
"Restart guard relaxed ..."): the eq -> prod restart check treats timestep,
temperature and friction changes as warnings; equilibration retries a NaN
warmup or heating stage at a halved timestep; and the membrane topology
build no longer asks the CCD once per lipid residue (685 HTTP requests per
build, the contention-sensitive 395 s on 014_membrane_6zdv). Later the same
evening, from 036_ligand_1ceb: the topology stage inherits the solv node's
water model and pairs the force field with it, and a contradicting
`--water-model` is refused with both ways out named (the interface had asked
for one physical decision twice).

