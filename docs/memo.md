# Working Memo

Running record of benchmark work: what was run, what the numbers were, what was
decided, and why. Newest entries go at the top. Append to this file as work
continues; do not rewrite past entries when a later finding contradicts them —
add the correction and say what it overturns.

---

## 2026-08-13 — Independent review of the simplification, and what it found in the tests

A codex advisor (gpt-5.6-sol, xhigh) was stood up in a Herdr pane and asked to
review commit a9c6255 without being told what to conclude. It found real
defects the author's own tests had not, and its second pass — an audit of the
test suite itself — found more. Both passes verified every claim by mutation:
break the implementation, check whether the test notices.

**Defects the review found in a9c6255** (all fixed):

- Seven of the twelve removed tools fell through to an argparse dump on stderr
  (exit 2, empty stdout), breaking the "every failure is JSON on stdout with a
  stable code" contract. The advisor framed this as an incomplete compatibility
  layer and recommended restoring aliases; the maintainer's question — "why
  care, we deleted them?" — produced the better diagnosis. Measurement showed a
  never-existed name behaves identically, so this was a pre-existing hole in the
  CLI that the deletions merely joined, and the fix is one generic
  unknown-subcommand handler, not a seven-entry tombstone table (which would
  have re-created exactly the hand-maintained name list this refactor deleted).
  `--list-json` already answered such names correctly, so both paths now share
  one resolver.
- Three migration hints silently dropped the old tool's defaults
  (`setup_model_backend` requires `--model`; `fetch_structure` defaults to CIF
  where `get_alphafold_structure` defaulted to PDB), and the comment above
  `_RENAMED_TOOLS` still claimed the Python functions survived for direct
  importers — false since a9c6255 deleted them.
- `search_structures` still advertised the deleted 0–120 MD-suitability rubric
  (`ranking_method: "md_suitability"`, `md_score_info` with interpretation
  bands) while computing a plain method-then-resolution sort, and a skill page
  claimed chain composition entered the ranking.
- **A regression this refactor introduced**: routing `setup_logger` through the
  root logger made merely importing mdclaw attach a root handler, so a host
  application's own records started printing. Fixed with a package-level
  NullHandler; `literature/_base.py` turned out to have been doing the same
  thing via `logging.basicConfig` since well before this work.
- budget validation, loosened to a shape check because "no Python code reads
  these numbers", had the wrong test: the reader is a later *agent*
  (`md-production` takes production length from `derived.target_*`). Restored
  as enums/types/signs only — and the first restoration was itself buggy
  (`headroom_hours` unchecked rather than sign-unconstrained, explicit nulls
  passing, enum checks raising TypeError on list input, NaN/Infinity accepted).

**What the test audit found.** The suite was green throughout, which turned out
to mean less than it looks:

- `test_direct_args_win_over_structure_analysis` asserted nothing at all. A
  first fix made it assert — and mutation testing showed it *still* passed with
  the precedence inverted, because the rule applies to what reaches
  `clean_protein`, and that block never runs when the fixture returns no
  proteins. The original author knew ("full precedence is exercised in the
  end-to-end test") but no such test exists. Now stubs a protein through and
  inspects `clean_protein`'s actual kwargs; mutation fails it.
- `test_removed_tools_are_deliberate` did not implement its own docstring: it
  claimed to fail when a server still imports, but used a hardcoded core-server
  list — which still named the deleted `benchmark` server, and let any
  non-core tool vanish silently.
- Tests pinned prose where the contract is a code: rewriting a failure's `code`
  to `unhandled_error` left them green. `test_representative_tool_failures`
  hid this structurally by passing raw results straight through
  `finalize_error`, which defaults a missing code to `unhandled_error`.
- `_run_cli` discarded the exit status; the guardrail-registry tests skipped
  (rather than failed) if the registry went missing; two live-API tests were
  unconditionally skipped placeholders behind a `--runslow` flag that does not
  exist; stage mappings survived for two tools deleted in a9c6255.

The lesson worth keeping: a green suite is evidence that the tests pass, not
that they guard anything. Every fix in this entry was checked by breaking the
implementation first. Also, a reviewer can identify a real defect and still
recommend the wrong remedy — the unknown-tool finding was correct, its proposed
fix would have partly undone the simplification.

Still open: old job dirs carry `claim` metadata that the agent-facing index no
longer surfaces (a migration warning was proposed, not written), and
`test_registry` still skips on any ImportError, so an accidental import typo in
a server can hide its tools from discovery without failing anything.

---

## 2026-08-13 — MDPrepBench pi+deepseek revalidation after the simplification: 40/40 at 1.0

The de-over-engineered tree (previous entry) was revalidated with the same
solver as the 2026-07-20 sweep: pi (`pi-user` profile) +
`spark1-vllm/deepseek-v4-flash`, skills+cli, 30-min/task cap, deterministic
scoring — now through the standalone MDPrepBench repo (`~/tmp/MDPrepBench`,
runs `refactor_verify_*`). **Every one of the 40 tasks scored 1.0**, one task
better than July's 39×1.0 + P28 0.9639 (P28 scored 1.0 this time). The
consolidated skills and the 77-tool CLI carried the whole suite, including the
new `--lipids` list contract (P18/P34/P37/P39 membranes all 1.0).

Caveats worth the record:

- **Not one-shot.** The first pass ran as two concurrent shards to halve
  wall-clock; that self-inflicted vLLM congestion produced 6 walltime timeouts
  (P03, P21, P24, P26, P28, P29 — 5 of 6 in the same shard). Sequential
  retries passed 5 of them at 1.0 immediately. July's 40/40 was sequential;
  concurrency, not the refactor, was the variable — P37–P40 sped up the moment
  shard A finished.
- **P26 (zinc, 2CBA) needed 4 attempts.** Attempts 1–3 timed out the same way:
  with a byte-identical prompt and identical skills, the agent ignored the CLI
  and spelunked openmm data dirs for ion XMLs, ending in `find /` scans. The
  first assistant sentence already diverges from July's run ("inspect the local
  OpenMM environment" vs July's "read the relevant skills"), and the spark1
  serving config changed since July (220K → 1M context on the same model
  name) — model-side drift/variance, not a skills regression: P27 (Mn) and
  P30 (Zn) passed 1.0 first try, and attempt 4 passed 1.0 in 20 min via the
  normal CLI path.
- **The harness now really tests the checkout.** `bin/mdclaw` previously ran
  the SIF's baked-in mdclaw package while host-side native tools ran the
  checkout — a silent version skew. It now binds PKG_ROOT and sets PYTHONPATH
  into the container, so these runs exercised the refactored source, verified
  by tool count (77) from inside the solver workspace.

---

## 2026-08-12 — De-over-engineering executed: −6,433 net lines, 89 → 77 tools

The audit below was executed the same day: 148 files changed, +1,274 / −7,707
(net −6,433). Suite green afterwards (1,252 passed to the old stop point plus
the tail files; ruff clean on `mdclaw/`). Skills: 4,332 → ~3,290 lines,
61 → ~46 files. `_cli.py` 1,409 → ~1,113; `evidence/reporting.py` 1,683 → 503.

**Deleted outright:** claim/lease machinery + its guardrail codes; `update_node`;
`find_nodes`/`get_children`; `mdclaw/metal/`; `research/structure_analysis.py`
(its two disulfide helpers moved to `structure/disulfide.py` — the audit missed
that `prepare_complex` imports them); `research/scoring.py` (the 0–120 rubric;
`--rank-for-md` now sorts X-ray→cryo-EM→NMR, best resolution first); the
evidence Methods half + `citation_inventory.md` + `evidence_schema.py` (folded);
alias tools (`download_structure`, `get_alphafold_structure`,
`setup/check_surrogate_backend`, `explain_failure`) — all now `tool_renamed`
redirects; PLIP; write-only `artifact_sha256` (existence check kept — no more
hashing multi-GB trajectories inside node.lock); the `_tool_meta` shims; false
MCP docstrings (`test_mcp_server.py` → `test_registry.py`); stale
`mdclaw/benchmark/` and `tests/test_benchmark/` pycache ghosts.

**Refactored:** one `_tool_param_specs` pass now feeds argparse, `--list-json`,
and kwargs assembly (the triple type-dispatch ladder is gone);
`embed_in_membrane.lipids` is `list[str]` (the 60-line repeated-string CLI
special case died); `fetch_structure` defaults `source="auto"` (CLI convenience
layer died); benchmark JSONL hook moved to `_benchmark_log.py`; `setup_logger`
propagates to one root handler (stream-swap surgery collapsed); TOOLS/`__all__`
derived from function objects in all 16 package `__init__`s; glycan helpers and
`CANONICAL_WATER_MODELS` moved to `chemistry_constants`; study log
triple-wrapper inlined; budget validation reduced to shape-only (field-level
tests replaced accordingly); prod-chain walkers unified; node.json readers
collapsed onto `_read_node_json`; sealed-node handling uses a typed
`NodeSealedError` instead of exception-message string matching.

**Bugs fixed:** `--json-input` skipped required-argument validation (regression
test added); `atomic_write_text_group`'s except-path deleted backups that had
just failed to restore (now `else`-scoped); broken `mdclaw.__all__`;
ineffective `_NODE_REQUIRED_TOOLS` monkeypatch in test_cli; the false
"boolean flags reject true/false" skill sentence; `bin/mdclaw` now binds
PKG_ROOT + PYTHONPATH into the SIF so container tools run the same source as
host-side native tools (previously the SIF's baked package — a version skew).

**Deliberately NOT done, with reasons:** guardrail registry kept at 257 codes
(the hint text is weak-agent scaffolding; measure before pruning);
`read_ancestor_final_step`'s three-state sentinel kept (tests use the omitted
form as real API — audit overcounted); `validate_node_execution_context`'s
`validate_conditions` param kept (None-collapse would change strictness for
callers passing `actual_conditions=None`); progress.json entry shape kept
(agent-facing via inspect_job; thinning it is a contract change — decide
separately); the three failure entry points kept (thin adapters, distinct call
shapes); `literature/` kept (skill-referenced and working); visualization
constants kept (audit wrongly called them dead — they are module-local, used).

---

## 2026-08-12 — Over-engineering audit: ~7.5–8k lines removable, 89 → ~78 tools

Four parallel audits (DAG/node core, CLI/dispatch, peripheral subsystems,
skills) over ~59k lines of Python + 4.3k lines of skills, looking only at
harness/plumbing complexity, not MD physics. Findings are an assessment;
nothing has been changed yet.

**Headline ratios.** 263 guardrail codes, 2 code branches anywhere that test a
code value; 89 tools dispatched by `fn(**kwargs)` behind ~2,244 lines of
CLI/registry/meta machinery; all 89 `TOOLS` entries are identity mappings;
`from mdclaw import *` raises (all 17 `__all__` names unbound).

**Dead or orphaned, highest confidence.** claim/release node-lease machinery
(~170 lines, zero production callers); `mdclaw/metal/` (whole package, zero
callers, consumer removed in 8a39b78); `research/structure_analysis.py` (694
lines, docstring cites a workflow phase that no longer exists);
the Methods-report half of `evidence/reporting.py` (~1,208 of 1,683 lines +
534-line citation inventory — zero output files across ~50 recorded benchmark
runs); alias tools (`download_structure`, `get_alphafold_structure`,
`setup/check_surrogate_backend`, `explain_failure`); write-only
`artifact_sha256` that hashes multi-GB trajectories inside node.lock with no
reader.

**Same-fact-N-times.** Tool names stated 3–4x per tool across
import/TOOLS/`__all__`; parent-type contract implemented twice (create_node
branches vs `_ALLOWED_PARENT_TYPES` table); progress.json has grown from
"thin index" into a node.json mirror with its own repair tool; in skills/,
ion policy ×6, platform preflight ×6, `guardrail-codes.md` (276 lines) is a
byte-level duplicate of what `hints[0]` already delivers at runtime.

**MCP ghost.** No MCP plumbing remains, but 11 files carry a false "integrates
with external MCP servers" docstring and `test_mcp_server.py` tests the
registry — misnaming propagated into CLAUDE.md/testing docs.

**Bugs found incidentally.** `--json-input` path skips required-argument
validation entirely; `mdclaw/__init__.py.__all__` fully broken;
skills/md-prepare/explicit-water.md:86 states boolean flags reject
`true`/`false` values (false — `_parse_cli_bool` accepts them, and
bioemu-sample instructs `--reconstruct-sidechains false`); inconsistent
node.lock/progress.lock ordering (latent deadlock shape, masked by
single-writer usage).

**Deliberately deferred.** The 263-code guardrail registry is the one place
where over-engineering may be load-bearing weak-agent scaffolding (the payload
is LLM-facing hint text). Decision: measure against benchmarks before pruning
to the ~40 referenced codes; don't drift.

Totals: CLI/dispatch ~1,000–1,700; node/DAG ~1,150; peripherals ~4,040 py +
534 md; skills ~1,320–1,420 (61 files → ~35). Full per-finding detail with
line numbers lives in the session transcript of this date.

---

## 2026-08-12 — GPU verification: the CPU hour was an invocation defect

Follow-up to the fourteen-failure entry: the hour-long membrane equilibration
was not a property of the tests but of how I launched them. The SIF was
invoked without `--nv`, so the container had no CUDA platform and
`platform="auto"` silently fell back to CPU.

Verified three ways. Platform probes: without `--nv` the usable set is
`[Reference, CPU]`; with it, `[Reference, CPU, CUDA]`, and a 20k-particle
auto-selected Context lands on CUDA. Timing: the same membrane+metal chains
that took 1 h 08 m on CPU completed in **1 m 58 s** with `--nv` — about 35x —
with identical results (7 passed both ways). The production paths
(`bin/mdclaw`, the benchmark task wrappers) were never affected; they already
add `--nv` when `nvidia-smi` is present. Only hand-typed SIF commands
following the guide missed it, and the guide is fixed (`18ca28a`).

Corrected estimate: a full suite with the revived pipeline chains is ~45 min
with `--nv`, not the 1.5 h previously reported. The 3PWB chain deliberately
pins `platform="CPU"` for determinism and is unaffected.

Noted, not done: the executed platform lives only in tool results, not in
node.json metadata, so post-hoc provenance cannot say which platform produced
an artifact. Worth considering if platform ever becomes scientifically
relevant (e.g. mixed-precision differences).

---

## 2026-08-11 — The fourteen failures: one real bug, thirteen stale fixtures

All fourteen pre-existing failures are fixed (`5eb0486`, `5363222`); the full
suite is green for the first time on record (1381 passed, 0 failed). None of
the tests were unnecessary — the question that prompted the investigation.

**The real bug** hid in plain sight for three weeks. The node-sealing change
(2026-07-16, c532626) made terminal node.json immutable but missed
`_register_preview_on_node`, which re-called `complete_node` on completed
nodes. Every post-hoc preview/review attachment on a finished node failed —
and the tests that would have caught it were failing for unrelated fixture
reasons, so the signal read as noise. That is the cost of tolerating a red
suite: real regressions become indistinguishable from stale tests. Attachments
now go through append-only `preview_registered` events, the resolvers read
them back, and a regression test pins the sealed-node render-then-review flow.

**The thirteen others** were fixtures asserting contracts the code had
deliberately outgrown: the parentless-node ban in study jobs, the candidates
layout, mandatory prep-time candidate selection, prep-owned hydrogen
completeness, a package-attr shadowing, an unverified writeFile-inventory pin
(the new membrane call site does restore long residue names — verified before
pinning), and a chemically impossible synthetic nucleic fixture, now generated
from pdbfixer template geometry against the force fields' terminal templates.

Two side finds from review: `test_split_molecules` was writing `split_N/`
directories into the repository checkout (two were committed; removed, output
now under tmp_path), and the first version of the event fix wrote events
nothing read — the reviewer's "writing is useless without a reader" catch led
to the resolver change that makes the flow actually work.

With the membrane/metal prepare steps unblocked, those chains run their full
MD legs (packmol packing through CPU equilibration) in-suite again, adding
roughly 1.5 h to a full run. That is the price of the coverage being real.

---

## 2026-08-09 — MDStudyBench extracted; the benchmark harness leaves mdclaw

MDStudyBench is now its own public repository,
<https://github.com/matsunagalab/MDStudyBench>, extracted with the same
copy-and-trim pattern as MDPrepBench and reviewed the same way before the first
commit (the review caught a missed `parents[2]`, an unconditional host
`import mdclaw`, a README claim that `MDCLAW_PYTHON` drives the scoring
delegate, a self-contradictory default CLI policy, and the spark1 profile
defaults surviving the copy — all fixed pre-publish; see that repo's memo).

Unlike the prep suite, `mdclaw` is a deliberate runtime dependency of its
confirmatory path: the runner executes MDClaw production nodes, snapshots the
installed `mdclaw` package as the attested adapter source, and resolves node
inputs through `mdclaw.node`. Scoring an existing submission needs only
openmm/mdtraj/numpy.

With both suites gone, this repository dropped `mdclaw/benchmark/` (~17k
lines), `tests/test_benchmark/`, `benchmarks/`, `docs/benchmark/`, and the
registry entry — 74 files, −36.8k lines. What deliberately stays is the
stage-record hook in `mdclaw/_cli.py` (`MDCLAW_BENCHMARK_HARNESS_LOG`): it is
now a cross-repository protocol both benchmark harnesses rely on, and its
stage vocabulary must not change silently. The layout the maintainer asked for
is three sibling checkouts: `mdclaw`, `MDPrepBench`, `MDStudyBench`.

**Pre-existing test failures catalogued during the removal** (fail identically
with the removal stashed; none are benchmark-related): three
`test_evidence_server` study-evidence report tests (missing prod `node.json`
in the fixture), three `test_visualization_server` node-registration tests,
two implicit-solvent `test_md_helpers` builds, `test_modxna_support` residue
mapping, `test_pdb_export_resname_guard` inventory pin, one prepare step in
each of the 3PWB/membrane/metal pipeline DAG tests, and one structure smoke
test. 14 in total against 1357 passing; they need their own investigation.

This memo stays as the historical record of the benchmark work done while the
suites lived here; new benchmark entries belong in the respective repos'
docs/memo.md.

---

## 2026-08-09 — MDPrepBench extracted to matsunagalab/MDPrepBench

MDPrepBench is now its own public repository,
<https://github.com/matsunagalab/MDPrepBench>, laid out as a sibling checkout
(`/home/yasu/tmp/MDPrepBench`). Fresh history, MIT, everything public — the
task contracts and truth references were already world-readable in this repo,
so openness was made deliberate rather than accidental. The extraction is
copy-and-trim: package `mdprepbench` is the harness minus the four study-only
modules, with `grounded_correct_v2` entry points raising NotImplementedError
pointing back here. All 337 tests pass there; CI runs lint, dataset
consistency, and a no-OpenMM test subset (verified in a bare venv).

A pre-publish external review caught five release blockers before the first
public commit, the worst being container-delegated scoring still invoking
`python -m mdclaw._cli` — it would have scored with whatever MDClaw the image
carried instead of the published code. Details in the new repo's docs/memo.md.

On this side, mdclaw dropped the prep dataset, prep-only tests/tools/docs, and
the prep-fixture-dependent tests whose coverage now lives in the new repo
(336 still pass). Kept: `mdclaw/benchmark` (MDStudyBench needs it),
`run_mdprepbench_all_agents.py` + `audit_mdprepbench_run.py` (the study batch
wrapper builds on them; canonical copies are in MDPrepBench), and
`validate_submission.py` / `package_submission.py`.

**Accepted risk, recorded deliberately:** the removal deletes tests for code
mdclaw still ships — the shared batch runner's execution/pass^k tests, the
public-export overwrite guards, the fabrication-policy scorer tests, and the
P18/P24 scorer regressions. Their coverage lives on, green, in the MDPrepBench
repository, and the harness here is feature-frozen until MDStudyBench leaves the
same way; restoring transitional copies was judged not worth the drift. The
review that flagged this (rightly calling the hybrid unsound as a permanent
state) also caught that `datasets.py` still defaulted to the deleted
`benchmarks/mdprepbench` — fixed to `benchmarks/mdstudybench` before commit —
and that a first trim pass had deleted the *study* tests too, because
`DATASET_DIR` substring-matched `STUDY_DATASET_DIR`; restored from HEAD and
re-trimmed with a lookbehind. Suite after all fixes: 441 passed.

MDStudyBench is planned to leave the same way. When it does, `mdclaw/benchmark`
and the remaining shared tools go with it, and the copy-and-trim pattern plus
the blocker list from this extraction are the template.

---

## 2026-08-05 — MDPrepBench reference bundles, and what 40/40 does not mean

codex (gpt-5.6-sol, xhigh) was run as the solver over all 40 tasks through
`run_benchmark_agent`, so it saw only the public export — never `task.json`, never
the deterministic checks. **All 40 scored 1.0**, no failures, ~4 h 15 m across
three shards, ~11 min per task, essentially no GPU.

Bundles total 1.78 GB and live outside git at `$MDPREPBENCH_WITNESS_DIR`:

```
<task_id>/submission/prepared_structure.pdb
<task_id>/submission/topology/{system.xml,topology.pdb,state.xml}
<task_id>/harness_execution.json
```

`benchmarks/tools/witness.py` records them into
`benchmarks/mdprepbench/witnesses/manifest.json` (per task: run id, provenance,
repository head, a hash over everything the scorer reads for that task, and a
hash per bundle file) and re-scores them on demand.

**What 40/40 establishes, and what it does not.** It establishes that every task
has at least one bundle this model, scaffold, and runtime can produce inside the
budget and that the current scorer accepts. It does *not* establish scientific
correctness beyond what the scorer checks, resistance to scorer-targeted
shortcuts, task difficulty, or pass@1 reliability — there is one observation per
task. The historical per-task means of 0.28–0.66 are not a comparison: they mix
models, scaffolds, code versions, and known instrumentation failures.

**A rule I had stated and have withdrawn.** I proposed treating a codex failure
as evidence to suspect the scorer. That is unsound: a failure warrants diagnosis,
not a presumption against the scorer. And the converse matters more here —
40/40 does not vindicate the scorer either, because an overly permissive scorer
produces 40/40 too. Positive fixtures cannot detect a weakened scorer; deleting a
check leaves every witness at 1.0. The negative fixtures remain the other half.

**Defects caught in review before commit**, all in the first draft of the tool:
scoring writes `normalized_submission/` and `score.json` *into* the bundle, and
hashing those would have produced a delayed false "drift" the artifacts never
caused; acceptance checked only `preparation == 1.0`, ignoring `status` and
`weighted_total`; `record` and `verify` returned 0 on skipped bundles, an unknown
`--task`, or an empty manifest; drift detection missed added files; a bare
`--task` meant "everything"; the contract hash covered only `task.json`, so
swapping one of the five private `truth/*.pdb` references would have gone
unnoticed; and `_scorer_revision()` shelled out to git, which the container does
not have, silently recording "unknown".

---

## 2026-08-05 — Artifacts versus harness evidence: the declaration was wrong

`dataset.json` declared `evaluation_unit: "submission_artifacts"`, and the
maintainer states an agent need not use MDClaw's DAG. But the prep tasks carry a
reject-level integrity check, `workflow_execution_recorded`, requiring a harness
execution record. Demonstrated on codex's P01 bundle, with the artifacts
unchanged between the two runs:

| submitted | preparation |
|---|---|
| artifacts alone | **0.0** (`harness execution record required but missing or empty`) |
| artifacts + `harness_execution.json` | 1.0 |

So a third party preparing a perfect system elsewhere and submitting the files
scores zero, which is not what "artifact-based" promises.

Resolved by **fixing the declaration, not the check**, after the maintainer
confirmed that requiring the harness is acceptable: a foreign agent can be
plugged in with `--agent-command` and still not touch MDClaw's MD tools, and
`mdclaw/benchmark/*.py` imports nothing from the MD side, so the harness is
separable in practice. `evaluation_unit` became
`harness_executed_preparation_bundle`, following MDStudyBench's existing
`runner_certified_study_bundle`; `agent_independent: true` stays, being accurate.
`environment_type: "artifact_only"` in `task_specs/defaults.json` — which is
exported into the *public* contract agents read — became
`harness_executed_artifacts`.

Scoring behaviour is unchanged, so historical scores stay comparable. The known
weakness is recorded in the dataset notes: harness evidence establishes
runner-executed provenance, not that the preparation was genuinely performed. The
check asks for one successful `min`-stage command with a measured walltime, which
a wrapper around a trivial command satisfies.

---

## 2026-08-05 — Correction: the `mdclaw-free` arm is not structurally blocked

I claimed that all 120 free-condition task instances scored exactly 0.00 and
suggested the integrity requirement blocked the arm by construction. Wrong on
both counts.

The 0.00 figure came from globbing `benchmark_runs/cond_*` and deciding the
condition from `_free_` appearing in the run name. Those runs all record
`tooling_condition: "unknown"`. The runs actually labelled `mdclaw-free` are four
others, and they score normally:

```
20260704_mdprepbench_pi_v2_pi          overall 0.5136   40 tasks
20260706_mdprepbench_pi_pi             overall 0.5470   40 tasks
haiku_sif_free_20260616_125805         overall 0.2585   25 tasks
pi_deepseek_sif_free_20260616_171959   overall 0.5714   25 tasks
```

The uniform zeros in the `cond_20260705_*` haiku runs are recorded as
`missing_raw_artifacts` — those agents produced nothing — not as an integrity
failure. This overturns the suggestion in the 2026-08-04 measurement entry that
the ablation's free baseline could not score.

---

## 2026-08-04 — Correction: five MDPrepBench tasks do ship reference data

The entry below claims "No task ships one; `tasks/<id>/` holds only `prompt.md`
and `task.json`". That was checked against `P01` alone and is wrong. Five tasks
carry a `truth/` directory:

```
P03_prep_ligand_pose_t4l_benzene    ligand_reference.pdb        105 KB
P18_prep_membrane_mixed_lipids      model_1_reference.pdb       124 KB
P19_prep_nmr_model_selection        model_5_reference.pdb        97 KB
P24_prep_biological_assembly        assembly_1_reference.pdb    317 KB
P28_prep_kinase_inhibitor_gaff_1iep ligand_pose_reference.pdb   184 KB
```

The conclusion still holds, because these are a different kind of artifact. They
are *input-side* references: coordinates used to check that the agent started
from the right thing — the fifth NMR model rather than the first, the biological
assembly rather than the asymmetric unit, the ligand in the deposited pose. They
say nothing about whether a finished, force-field-applied system is correct.

What is still missing is the *output* side: a stored `system.xml` /
`topology.pdb` / `state.xml` bundle for a task, whose purpose is to detect the
scorer breaking rather than to grade an agent. Zero tasks have one, and no task's
`scoring` references a stored bundle (`ground_truth_checks` is `[]` for P01, and
no task.json mentions a reference or golden file).

| | existing `truth/*.pdb` | the reference bundle still wanted |
|---|---|---|
| stores | starting coordinates | the finished, parameterised system |
| detects | agent picked the wrong input | **the scorer itself regressed** |
| size | 100–300 KB | ~35 MB (P01, measured) |
| coverage | 5 tasks | none |

---

## 2026-08-04 — Retiring MDStudyBench S02-S04, and what the review changed

**Commit:** `8399dc6` (32 files, +55 / −2159)

Deleted `S01_stability_t4l_l99a` (referenced from nowhere in `dataset.json`, yet
still holding its prompt, task spec, and held-out truth on disk while sharing the
`S01_` prefix with the live task), the `S02`–`S04` extended tier, and the
fixtures for the v0.3 comparative-study construct they were the only users of:
`test_study_scoring_fabrication.py` (162 lines), `_fake_study_submissions.py`
(591 lines), and a scoring test asserting the agent must submit its own
comparative trajectories — the v2 contract has the runner own those.

**Reversed mid-change.** The first draft also deleted the `execution` and
`evidence_communication` score axes, which are used by no live task
(`execution` was non-null in 0 of 87 historical runs). A codex review pointed out
that those axes live in **MDPrepBench's** schemas and in the shape of every run
summary, so removing them would change the target suite's artifacts — and make
new summaries structurally incomparable to the 83 historical runs — purely to
finish MDStudyBench housekeeping. Reverted. The axes stay.

The LLM judge was also left alone. No shipped task declares `llm_judge_rubrics`
any more, so it has no scoring consumer, but the legacy study-scoring path is
interleaved with generic path validation, OpenMM rescans, and status handling.
Cutting it belongs in its own change, end to end, if it happens at all. The judge
tests now build a synthetic rubric task rather than referencing a deleted one.

---

## 2026-08-04 — MDPrepBench: measuring before proposing

Aggregated 83 historical runs from `benchmark_runs/*/summary.json`.

**Task quality is fine.** Every one of the 40 tasks has scored
`weighted_total = 1.00` at least once. Per-task mean ranges 0.28
(`P18_prep_membrane_mixed_lipids`) to 0.66 (`P17_prep_dna_duplex_neutralization`);
the fraction of runs at ≥ 0.8 ranges 26% to 69%. No unsolvable or broken task.
This overturns an earlier note claiming P18 fails for all models — true of the
model set at the time, not of the 54 runs now on record.

**Failure attribution — first answer was wrong.** 426 recorded task failures:
392 `missing_raw_artifacts`, 22 `invalid_openmm_bundle` (a known operator
environment misconfiguration), 10 `incomplete_running_work`, 2
`background_processes`. Inspecting 311 of the `missing_raw_artifacts` cases for
whether the agent had produced `topology.pdb` / `system.xml` / `state.xml` /
`minimized.pdb` anywhere under `work/` gave 310 "produced nothing", which was
reported as "essentially all failures are genuine capability failures".

That was wrong. It checked only for artifacts, never whether the agent process
ran at all. Adding exit code and tool-call records:

| classification | count | |
|---|---|---|
| zero tool calls recorded (start-up / infra suspect) | 253 | 81% |
| timed out (exit 124) | 48 | 15% |
| ran tools, produced nothing (genuine capability failure) | 9 | 3% |
| produced artifacts, failed to submit | 1 | 0% |

Those 253 concentrate in **10 runs**; one run has all 40 tasks failing that way.

**But do not over-correct either.** The harness log records only MDClaw CLI
calls, and in the `mdclaw-free` condition the agent is instructed not to use the
CLI, so zero tool calls is expected there and is not evidence of a start-up
failure. Seven of those ten runs are `cond_20260705_*_claude_code_*` ablation
runs. The honest reading is: the earlier "100% capability failure" claim is
definitely wrong; failures concentrate at the run level, which is a poor signal
for per-task capability; and only 9 cases are demonstrated capability failures.

**Consequence for the ablation.** MDPrepBench's distinguishing purpose is the
`mdclaw-free` / `mdclaw-cli-only` / `mdclaw-skills+cli` ablation. Zero-call does
not mean the same thing across those conditions, and the CLI-usage log was
separately shown to be silently discarded under the SIF runtime (see below). The
recorded conclusion — "the skill is the active ingredient, CLI alone ≈ free" —
should be treated as an observation under nominal conditions, not a causal
result, until treatment fidelity is verified per episode.

**Reference bundles.** No task ships one; `tasks/<id>/` holds only `prompt.md`
and `task.json`. Rather than promote a historical 1.00 submission (which shares
assumptions with the scorer that produced the score), witnesses are being
generated by running codex as a solver through the normal harness, which exposes
only the public export. First result: `P01_prep_simple_monomer_t4l`,
`overall_score = 1.0`.

---

## 2026-08-03 — Singularity inside a user namespace

**Commit:** `2699d45`

An agent working in another checkout wrapped `singularity` in `unshare -Ur`
after hitting the `unknown userid` warning, and every SIF invocation became a
full 5.1 GB extraction. Reproduced on this host, so it is not account-specific:

| invocation | elapsed |
|---|---|
| `singularity exec mdclaw.sif …` | 0.80 s |
| `singularity exec --no-home --bind "$PWD:/work" --pwd /work …` | 0.36 s |
| `unshare -Ur singularity exec …` | 65.7 s + 5.1 GB scratch churn |

A user namespace makes the kernel ignore the setuid bit on `starter-suid` and on
`fusermount3`, because the files' owner is unmapped there (`unshare -Ur` maps
only the caller: `uid_map = 0 37014 1`). Singularity falls back to FUSE, that
fails with `Operation not permitted`, and it extracts the image instead.

Floyd's accounts come from NIS (`nsswitch.conf: passwd: compat nis`, server
`crab`), which is why the lookup warning appears at all — but it is a warning,
not a failure. The guide's old wording, "avoid host account lookup by binding
the checkout at a neutral path", was read as "use a neutral UID". Reworded, and
`bin/mdclaw` now warns on stderr when it is about to launch Singularity from
inside a user namespace.

---

## 2026-08-03 — Conditions the certified adapter cannot honour

**Commit:** `9cdf91e`

A GPU run of MDStudyBench S01 in another checkout failed with
`condition_unverifiable` on every node, after spending 1 h 55 m on topology,
minimisation, and equilibration.

A declared node condition is a contract `run_production` must cross-check
(`mdclaw/node/lifecycle.py`), but the certified confirmatory adapter passes only
`--job-dir`, `--node-id`, `--simulation-time-ns`, `--temperature-kelvin`,
`--pressure-bar`, and (since `3420bc7`) `--random-seed`. `run_production` reports
13 conditions. Anything it reports as `None` that the node declared fails closed.

The immediate cause was `random_seed`, fixed in `3420bc7` — physics-neutral, and
the S01 prompt explicitly allows seeds to differ, so it should always have been
forwarded. The other checkout simply had not pulled.

The structural fix in `9cdf91e` rejects `platform`, `device_index`, and
`custom_force` at **plan freeze**, where the agent can still repair the node,
instead of at node execution after the GPU budget is gone. Deliberately still
declarable: `hmr`, `timestep_fs`, `implicit_solvent`, `is_membrane` —
`production.py` resolves these from the topology *before* building
`actual_conditions`, so they do verify. An earlier claim that `hmr` was dangerous
to declare was wrong; it was inferred from function signature defaults without
reading the resolution order.

---

## 2026-07-28 — S01 blind run: the answer was wrong, the harness was worse

**Run:** `studyv04_opus_s01_7h` — claude-code / opus, skills+cli, 7 h budget,
GPUs 1 and 5, dataset copied to scratch with `time_limit_minutes: 420` and the
prompt's "24 hours" reworded to match.

Final gates:

```
valid_execution   = true
claim_supported   = true
truth_agreement   = false
grounded_correct  = false      result_class = "grounded_wrong"
```

The solver claimed `decreased_hydration`; the evaluator's own replay agreed with
the claim; held-out truth is `increased_hydration`.

**The failure is the agent's, and it diagnosed it itself.** All four replicas
started from one `start_state.xml` (identical sha256) in which four bulk waters
had been relocated into the cavity. So the runs measured mild expulsion from a
pre-wet pocket rather than equilibrium filling of a dry one, and the
replica-agreement check passed vacuously. The solver said so in its own report,
considered claiming `unresolved`, and decided that substituting its judgement for
the published adequacy rules would be redefining the contract. That reasoning is
sound, and `claim_supported = true` backs it.

**Two harness defects surfaced first, both fixed.**

`03e7383` — the task-local `mdclaw` wrapper mounts `source_root` read-only, but
the harness execution log lives under it (`benchmark_runs/<run>/tasks/<task>/`).
`_write_benchmark_harness_record` swallows write failures by design, so every CLI
execution record was silently dropped, which to the scorer is indistinguishable
from an agent that ran nothing. This was a same-day regression: until `6f01e45`
the bind was read-write. For MDPrepBench, whose integrity checks set
`require_harness_record`, that would turn an environment detail into a hard
scoring failure.

`4abffc3` — confirmatory production runs in the SIF, but the runner inspected the
resulting artifacts in its own interpreter. The runner venv has `openmm` but not
`mdtraj`, so `_inspect_openmm_artifacts` raised on import and the fail-closed
catch recorded `openmm_artifact_inspection_failed` for four runs whose MD was
clean (adapter exit 0, no timeout, 1,250,000 steps and 206 MB trajectory each).
That zeroed `valid_execution` for a property of the operator's environment.
Inspection now delegates to the same container as the adapter, and a missing
container runtime yields `openmm_artifact_inspection_unavailable` rather than the
artifact-trust code.

**Salvage.** Re-inspecting the four completed nodes with the fixed code returned
`valid=True`, empty reason codes, and full runtime facts in ~17 s per node. The
episode was amended by merging only the inspection-derived fields — `runtime`,
`reason_codes`, `diagnostic_reason_codes`, `valid`, `attestation_scope` — while
keeping the runner's timings, adapter results, frozen plan, and artifact
snapshots, with a guard that aborts if live artifact hashes no longer match the
custodied snapshots. `attestation_scope` was missed in the first attempt, which a
codex review caught: `grounded_v2` requires
`production_runtime_matches_frozen_base_system` to be `true`, and the un-merged
event still carried `false`, so the amendment would have failed
`event_runtime_scope_unattested`. An audit receipt records the original and
corrected episode hashes, the SIF hash, and the full fresh inspection output.

**How to report this number.** As a post-hoc infrastructure-corrected
calibration, not as a clean run. `--no-session-persistence` does not give the
resumed claim stage a clean slate: the solver's own analysis files from its
earlier continuations were still on disk. The official record for this run
remains `0.0 / invalid_execution`; the salvaged score lives in
`score.salvage.json`.

---

## Open questions

- Verify treatment fidelity per episode before trusting any ablation number:
  free sees neither skills nor CLI, cli-only sees CLI but not skills,
  skills+cli sees both with a pinned skill-bundle hash.
- Split the `cond_20260705_*` zero-call failures into condition-expected versus
  genuine start-up failure. The recorded ablation conclusion rests on those runs.
- Extend codex-generated witnesses to the suspicious families — membrane, metal,
  protonation. If codex fails one, suspect the scorer, not only the agent.
- pass^k reporting for K = 3. `--repeats` already exists in
  `benchmarks/tools/run_mdprepbench_all_agents.py`; nothing aggregates across
  repeats. Fix the definition of "pass" first — `P01`'s deterministic checks
  contain zero hard gates, so a gate-based definition is vacuous;
  `scores["preparation"] == 1.0` is the candidate.
- Whether to delete the LLM judge end to end, now that no task declares
  `llm_judge_rubrics`.
