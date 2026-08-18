# Working Memo

Running record of benchmark work: what was run, what the numbers were, what was
decided, and why. Newest entries go at the top. Append to this file as work
continues; do not rewrite past entries when a later finding contradicts them —
add the correction and say what it overturns.

---

## 2026-08-18 — `eq` could silently skip `min`; found by running the onboarding guide

Wrote a RIKYU onboarding guide for the hackathon and ran it end to end as a new
member would — fresh clone under `/data1/rkp00048/$USER`, shared arm64 SIF, 4AKE
chain A, apo, 100 ps NVT + 100 ps NPT + 100 ps production on SLURM. It works:
`min` 18 s, `eq` 45 s, `prod` 38 s, **1 min 41 s of GPU time**, 300.34 +/- 0.90 K
and 1.018 +/- 0.001 g/mL. 4AKE is the open form, so at a 15 A buffer it solvates
to ~90,000 atoms — nearly twice 1AKE's 49,671.

**The run exposed a real bug.** Submitting `min` -> `eq` -> `prod` as
dependency-chained SLURM jobs means creating all three nodes before any of them
runs. `_auto_resolve_parent` walks `_AUTO_PARENT_PREFERENCE["eq"] = ("min",
"topo")` and falls through to the next entry whenever the preferred one has no
*completed* node. With `min_001` still pending, `eq` silently attached to
`topo_001` — equilibrating from the topology-time state and skipping
minimization entirely. `explain_node` reported `ready_to_run: true` with no
warnings, because `topo` is a legitimate `eq` parent for legacy DAGs.

The fix distinguishes *absent* from *not yet complete*: a less-preferred parent
type is now only reached when the preferred type has no nodes in the job at all.
Present-but-incomplete (or failed) returns `None`, so `create_node` demands an
explicit `--parent-node-ids` — the same structured `node_context_required` that
`prod` already gave in this situation. `_auto_parent_candidates` stops at the
same place so the error never advertises a `topo` candidate while a `min`
exists. Legacy `topo -> eq` DAGs with no `min` node are untouched.

This also covers `topo`, whose preference is `("solv", "prep")`: a pending
`solv` no longer falls through to `prep` and builds an unsolvated topology.

Verified in the live workflow, not just unit tests — re-running the guide, the
bare `create_node --node-type eq` now fails with `node_context_required` instead
of quietly mis-parenting, and the corrected chain gives a DAG with 7 completed
nodes, 0 failed, 0 orphaned.

---

## 2026-08-18 — Merged arm64 image verified on Rikyu; the shim contract holds

Pulled the merge onto Rikyu (`c000`, GB200, driver 580.173.02) and checked the
parts the merging host could not. The entry below flagged the
`MDCLAW_FUSEFIX_LIB` indirection as unverified because that host had no arm64
builder; it verifies clean.

**The build-time assertions pass.** The published SIF predates
`MDCLAW_FUSEFIX_LIB`, so it was injected to reproduce what a rebuilt image will
see. Both `RUN` assertions in `Dockerfile.rikyu-arm64` — the devel-stage
`LD_PRELOAD` check and the final-stage one that reads the variable — succeed.
The sanitized `container/mdclaw_fusefix.c` also compiles under the stricter
flags the Dockerfile now uses (`-Wall -Wextra -Werror -Wl,-z,relro,-z,now`,
gcc 13.3 aarch64), and the freshly compiled shim still fixes `torch.fft` on the
GPU. The rewrite that dropped the site-specific comments changed no behavior.

**The `check_declared` gate is presence-based in all three states**, tested on
the same SIF: undeclared → `SKIP` (20 passed), declared and correct → `PASS`
(21 passed), declared with a bad path → `FAIL` (20 passed / 1 failed). It cannot
silently pass.

`test-rikyu-gpu.sh` from the SIF: `ARM64_CUDA13_GPU_SMOKE=PASS`, including
`openmm_pme_cufft=PASS` and `pytorch_cufft=PASS` — the two checks the old script
lacked, which is why two broken images passed it 5/5 before the merge.
`test-container.sh`, 304 unit tests, and `ruff check mdclaw/ tests/` are clean.

**Fixed here: the SLURM GPU directive.** `_generate_sbatch_script` emitted
`#SBATCH --gpus-per-node=N`, which Rikyu's job-submit plugin rejects outright
(`[AI4S] Specify GPUs with --gpus=N (-G N). Per-node forms ... are not
supported`), so every `submit_job` with a GPU failed at submission. This is site
policy, not a bug — both spellings are valid Slurm — but the per-node form is
unusable on Rikyu, so both script generators now emit `--gpus=N`. The two forms
are equivalent at the default `--nodes=1` and differ beyond it: `--gpus-per-node`
is per node, `--gpus` is the job total, so `--nodes 4 --gpus 2` meant 8 GPUs
before and 2 now. Nothing in-tree submits multi-node GPU jobs (`nodes` defaults
to 1, and `skills/hpc-run` has no multi-node example), so `--gpus` now means the
job total everywhere. Keeping both spellings was rejected because it would leave
Rikyu with no multi-node GPU path at all. Validated end to end on Rikyu with
the fix in place: 1AKE chain A, apo, ff19SB/OPC, 49,671 atoms, submitted as
`min` -> `eq` -> `prod` with `afterok` dependencies (`--gpus=1`, 1x GB200).
All three `COMPLETED`; 100 ps NPT production held 300.6 +/- 1.2 K and
1.028 +/- 0.002 g/mL.

**Housekeeping:** `.gitignore` no longer excludes `RIKYU.md`, which says on its
first line not to commit it. `RIKYU-SIF-REBUILD.md` is superseded by
`docs/developer/container.md` plus the entry below.

---

## 2026-08-18 — Correction: MODELLER *does* run on arm64; rikyu gets it too

**This overturns the arm64 conclusion in the entry below.** I claimed MODELLER
could not run on arm64 Linux, that building on rikyu would not help, and that
only x86_64 emulation remained. That was wrong. I inspected the *conda package*
— which ships only `lib/x86_64-intel8/`, Intel-Fortran-linked — and generalised
from it to the whole distribution without checking the generic tarball.

`https://salilab.org/modeller/10.8/modeller-10.8.tar.gz` (38 MB) ships five
architectures: `armv6l-gnu`, **`armv8-gnu`**, `i386-absoft`, `i386-intel8`,
`x86_64-intel8`. `libmodeller.so.14` under `armv8-gnu` is `ELF 64-bit LSB shared
object, ARM aarch64`, gfortran-linked (`libgfortran.so.5`, no Intel runtime),
and the `Install` script detects `aarch64:Linux:*` and offers "5) Linux on
64-bit ARM". The conda channel is the limitation, not MODELLER.

The Python side works too: the tarball's `python3.3/_modeller.so` is a
stable-ABI (abi3) build, so one binary covers Python 3.3+. Verified on x86 by
importing the tarball's `python3.3` extension under the SIF's **Python 3.12** —
`import modeller` and `Environ()` both succeed. Same layout exists under
`armv8-gnu`, so rikyu's Python 3.12 is covered.

`Dockerfile.rikyu-arm64` now installs it from the tarball, laid out by hand
(modlib + src + bin/*.top + bin/lib + lib/armv8-gnu, symlinked into
site-packages) rather than via the interactive `Install`, matching the shape the
conda package produces so nothing downstream can tell the images apart. Proven
on x86 first with the equivalent `x86_64-intel8` layout — 57 MB, config.py left
at the `XXXX` placeholder, runtime `KEY_MODELLER*` injection working. Both
images now declare `MDCLAW_MODELLER_VERSION`, so both run the smoke check.

**Not verified:** the arm64 image was not built — this host's buildx offers only
`linux/amd64 (+4), linux/386` and `qemu-aarch64` binfmt is unregistered (needs
root). The tarball path needs a build on rikyu itself to confirm.

Emulation was measured before the tarball came up, and is no longer needed. For
the record, qemu-user does work: same 9UWI comparative model, native **13.5 s**
vs **116.6 s** under `qemu-x86_64-static` — **8.7x**, import 0.22 -> 1.22 s.
Same-arch TCG, so an arm64 host would differ somewhat, but the order stands.

---

## 2026-08-18 — MODELLER now ships in the amd64 image; two defects fixed on the way

`modeller_from_alignment` and the `modeller-predict` skill had **no working
runtime anywhere**. MODELLER was in neither `environment.yml`,
`container/Dockerfile`, `Dockerfile.rikyu-arm64`, nor `pyproject.toml`, so no
image could contain it; the SIF is read-only, so the skill's
`conda install salilab::modeller` advice was unreachable there; and
`check_model_backend --model modeller` answers `Available models:
['bioemu', 'boltz']`. Confirmed absent in all four local SIFs (0.6.5).

**The license was never the blocker.** mdclaw's runner builds a synthetic
`modeller.config` from `KEY_MODELLER*` and seeds it into `sys.modules` before
importing MODELLER, taking only `install_dir` from the installed config.
Verified against 10.8: installing with no key succeeds and leaves
`license = r'XXXX'`; an injected key is what MODELLER validates (a wrong one
fails with `check_lice_E> Invalid license key: FAKEKEY123`, naming the injected
value, not the placeholder). So the image ships the package unlicensed and each
user supplies a key at runtime.

Installed from `container/Dockerfile`, not `environment.yml`, because the
salilab channel is **linux-64 only** and the rikyu arm64 image derives its
environment from that same shared file. New image: 20 smoke checks pass
(`PASS: MODELLER installed`), SIF 5.2 GB at `mdclaw-modeller.sif`.

**arm64 is not portable, and building on rikyu does not change that.** salilab
publishes linux-64 and osx-arm64 but no linux-aarch64 and no noarch; bioconda
and conda-forge have nothing. `conda install` downloads prebuilt binaries, so
the build host is irrelevant. Nor can it be compiled: Salilab's own
`INSTALLATION` says *"The source code is not generally available"*; the shipped
`src/` holds only 45 SWIG `.i` files and headers, there is no build system, and
the one Linux target `lib/x86_64-intel8/` links the Intel Fortran runtime
(`libifcore.so.5`, `libimf.so`), which has no ARM build. Only Salilab can fix
this.

### Two defects found by actually using it on 9UWI

1. **Models came back in MODELLER's own frame, numbered from 1.** Fine for de
   novo homology modeling, wrong for the `loop_refinement` repair case the skill
   advertises. On 9UWI chain A (V1aR; 269 resolved, 40 missing over three gaps
   incl. a 33-residue ICL3) the returned model sat **9.86 A** CA RMSD from its
   own template, numbered 1..309 instead of 43..351 — so the atosiban taken from
   the same cryo-EM entry landed in the wrong place, with nothing in the output
   saying so. New `--template-frame` refits and renumbers via the PIR alignment:
   **9.858 -> 0.484 A** over 269 paired CAs, 309 residues renumbered to 43..351,
   and the receptor/atosiban interface returns at **311 of 324** crystal contacts
   with zero clash under 2.0 A. The in-place deviation is now always reported.

2. **The frame check read the wrong alignment file.** `AutoModel.auto_align()`
   aligns the seed, writes the result beside it as `<alnfile>.ali`, and leaves
   the seed untouched with an empty template entry. The first implementation read
   the seed, found no template residues, and skipped restoration on every
   auto-aligned run — the exact case it was written for. Its warning said the
   alignment "does not contain both 'v1arA' and '9uwiA'" while printing "found
   ['9uwiA', 'v1arA']", because one branch handled missing and empty entries.

Tests: `tests/test_modeller_template_frame.py`, 6 cases, no MODELLER needed.
162 passed across genesis/registry/cli.

**Not done:** 9UWI itself is parked at `source_001` (fetch complete,
`solvent_regime=membrane`). Atosiban's GAFF parameterization — `MPT`,
`A1EQM` (O-ethyl-D-Tyr), `ORN`, `NH2` plus an MPT-CYS thioether macrocycle — is
untried and is the likely next obstacle.

---

## 2026-08-18 — Rikyu arm64 image merged to main; one smoke test now serves both

`container/rikyu-arm64` (13 commits, last touched 2026-08-01) is on `main` as a
merge commit. Both Dockerfiles now live side by side and share
`environment.yml`, `pyproject.toml`, `container/scripts/test-container.sh`, and
`docs/developer/container.md`:

| | `container/Dockerfile` | `container/Dockerfile.rikyu-arm64` |
| --- | --- | --- |
| arch / CUDA | x86_64, 11.8 | arm64, 13.0 (NVRTC) + 13.1 math libs |
| OpenMM | 8.2.0 | 8.5.1, `openmm-torch` at `sm_100` |
| publishes to | `ghcr.io/matsunagalab/mdclaw:latest` | `ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-<rev>` |

**The merge itself was nearly clean.** One conflict: the MDAnalysis floor, main
at `>=2.7` from v0.6.5 and the branch at `>=2.8,<3` because linux-aarch64
conda-forge builds start at 2.8. Took `>=2.8,<3` — satisfies both, matches
`environment.yml`, and the published image already carries 2.10.0.

**Sharing the smoke test was the part that actually broke.** Two of the checks
the branch added assume the arm64 image: the cuFFT contract globs
`libcufft.so.12.*`, and the shim contract requires `libmdclaw_fusefix.so` in
`LD_PRELOAD`. Run against the published amd64 SIF, the merged script gave
**19 passed / 2 failed**. Fixed with `check_declared <VAR> <desc> <cmd>`, which
skips when the image never declared the contract. Same script, same SIF:
**19 passed / 0 failed**, two `SKIP` lines. The gate is presence-based rather
than a permanent no-op — forcing `MDCLAW_CUFFT_MIN_VERSION` and
`MDCLAW_FUSEFIX_LIB` into the amd64 SIF reproduces both failures, so the arm64
image (which sets both) is still held to them.

`MDCLAW_FUSEFIX_LIB` is new and is now the single definition of the shim path;
the Dockerfile's runtime assertion and `test-rikyu-gpu.sh` read it instead of
repeating the literal.

**Not verified here:** the arm64 image was not rebuilt — no arm64 builder on
this host. The `MDCLAW_FUSEFIX_LIB` indirection touches a build-time `RUN`
assertion in `Dockerfile.rikyu-arm64`, so the next Rikyu build is the first real
test of it. Nothing is pushed; `main` is local-only and ahead of `origin/main`.

---

## 2026-08-15 — Baseline 0.875, then three fixes before the real K=3

`passk3_20260814_v2_pi_rep1` finished 40/40: **overall_score 0.875**, five tasks
`failed` with `missing_raw_artifacts` — the four 60-min timeouts (P09, P13, P19,
P24) plus P36. 11.7 h of task time, of which **4.0 h (34 %) went to those four
hung commands**. This run is the pre-fix baseline; it cannot be one of the three
repeats, because the fixes below change the system under test.

**1. `parse_mutation_specs` accepts any separator, and says so when it doesn't.**
`--mutations` is `nargs="+"`, so the CLI wants `--mutations L99A M102Q`. P09's
agent passed `"L99A,M102Q"`, `_MUTATION_RE` rejected the single token, the node
was sealed terminal, and the agent spent its remaining 55 minutes grepping the
repo for why. Now each token is split on `[,\s]+` first, and the error names the
multi-mutation form instead of only the single-mutation notation. Four
separator forms are covered by tests; mutation-tested 3/3 (drop the split, split
on whitespace only, drop the hint from the message).

**2. Per-command watchdog for the agent** —
`MDPrepBench/tools/pi_shell_timeout.sh`. pi resolves its shell through
`settings.json` `shellPath` and invokes it as `<shellPath> -c "<command>"`
(`pi-coding-agent dist/utils/shell.js`), so pointing that at a wrapper caps every
command. 600 s: the longest legitimate command in the July 40-task run was
257.7 s (p99 63 s). The setting is global but the wrapper only wraps when `$PWD`
is inside a MDPrepBench run, so other pi sessions are untouched
(backup: `~/.pi/agent/settings.json.bak-20260815`).

Verified separately, not end to end. Proven: pi does route through the wrapper
(logged a real `-c echo …` invocation, one `-c` per command, so no persistent
session shell to kill); and the wrapper caps correctly (exit 124, process-group
kill confirmed with marker files, partial output still returned). Not proven: a
long command inside pi actually returning 124 — repeated attempts stalled in pi
before it issued any tool call, and **a control run with `shellPath` removed
stalled identically**, so that is pi flakiness, not the wrapper. Check the first
hour of the run for any tool call over ~615 s. Worst case equals rep1's
behaviour, so this cannot make things worse.

**3. `MDCLAW_RUNTIME=singularity` in the sweep env.** `bin/mdclaw` probes for a
conda env before falling through to Singularity, and `conda env list` costs
1.3 s on a host that has no `mdclaw` env. Measured `bin/mdclaw --version`:
**3.8 s → 2.4 s**. Left as an env var rather than a code change — the probe is
correct on hosts that do have the env.

---

## 2026-08-15 — Correction of the correction: there is no pi floor, and the four timeouts are hung commands

The entry below claims a "quantisation floor inside the pi harness" and cites
`cat common/run-loop.md` taking 7.19 s. The cursor advisor refuted it and I
verified both claims against the transcripts myself.

**1. The 7 s "floor" was a batching artifact.** When one assistant message
issues several toolCalls, all their results are recorded at one timestamp, so
every sibling inherits the slowest one's elapsed time. Measured over rep1:

| | n | median | in the 6.4–7.6 s band |
|---|---|---|---|
| plain shell, `batch=1`, no mdclaw | 466 | **0.11 s** | 2 |
| plain shell, batched, no mdclaw | — | — | 25, **all 25 with an mdclaw sibling** |

The `cat` at 7.19 s was batched with an mdclaw call. There is no harness floor.

**2. The transcript timing method is accurate.** The agent itself ran
`time mdclaw …` 17 times. Comparing the shell's own `real` against the
toolCall→toolResult window: **median gap 0.05 s**, across commands from 0.55 s
to 263 s. So toolCall→toolResult *is* the command's wall time — no harness
overhead to subtract. That kills the "~3 s recording overhead" theory too.

**3. The four 60-min timeouts are not model generation.** Each burned 42–55
minutes inside a single environment-probing command:

- P09 — `grep -rln "mutation_spec_invalid\|hpacker" <benchmark_runs tree>`, toolCall never answered
- P24 — a hand-written `min.py` on the host venv, toolCall never answered
- P13 — 42.2 min in one completed call: `which tleap parmchk2 antechamber; ls /opt/anaconda3/...`
- P19 — 51.7 min in one completed call: host-venv python probing `openmm.__file__`

My earlier reading ("P09 spent 3.1 min of 60 in tool calls, so the rest is
generation") was an artifact of my own script: it yielded only calls that had a
matching toolResult, so a hung command was invisible, and batch double-counting
inflated the others. Same failure mode as P26 in the entry two below — the agent
leaves the workflow and scans a huge tree.

**4. The lazy-preload win does not show up in the benchmark.** Solo, read-only
mdclaw calls: July min 5.71 s / median 6.07 s; now min 6.44 s / median 7.00 s.
The floor went **up ~0.9 s** since July. The direct A/B (5.91 → 2.90 s) is real
and reproducible, and the transcript timings are trustworthy per (2), so the two
must be measuring different work — benchmark calls are `create_node` /
`explain_node` / `inspect_job` against a job dir on NFS, not `--list-json`.
**Unresolved.** Do not claim the fix sped up the benchmark.

**5. Verified MDClaw bug behind P09.** `--mutations` is `nargs="+"`
(`mdclaw/_cli.py:450`), so the correct form is `--mutations L99A M102Q`. The
agent passed `"L99A,M102Q"`; `_MUTATION_RE` (`mdclaw/sidechain_packer.py:178`)
anchors a single token, so it cannot match, and the message
(`mdclaw/sidechain_packer.py:196-198`) is *"Invalid mutation spec 'L99A,M102Q'.
Use L99A or A:L99A notation."* — it never says how to pass more than one. The
task asks for two mutations. The agent tried `"L99A M102Q"` quoted, failed
again, and went grepping. The failure also sealed `prep_002` as terminal, so
retrying on that node was refused.

---

## 2026-08-14 (later) — Correction: the "per-mdclaw-invocation latency" numbers in the entry below are pi's floor, not mdclaw's cost

The entry below reports light-call latency of 6.04 s (July) vs 7.19 s (Aug) and
treats it as mdclaw's per-call cost. **It is not.** In the same transcripts:

```
cat common/run-loop.md                        7.19 s
ls -la && grep -c "^ATOM" 2LZM.cif ...        6.92 s
[read] .../solver_workspace/.agents/skills/…  7.43 s
```

`cat` of a local file does not take seven seconds. A histogram of all 1511 tool
round-trips in the running sweep is bimodal — 732 calls under 0.5 s, a near-empty
valley from 3–6 s, then 385 calls piled at 6.5–7.5 s. July shows the same shape
with the pile at 6.0–6.5 s. That is a quantisation floor inside the pi harness,
applied to anything that does not return almost instantly, and it is what those
medians were measuring. The floor rising ~0.7 s between July and August is a pi
change, not ours.

So the lazy-preload win is real but invisible here. Same-moment A/B in the
benchmark's own solver workspace, while the sweep was running:

| | median |
|---|---|
| checkout, lazy preload | **2.90 s** |
| SIF baked package, import-time preload | 5.91 s |
| via `bin/mdclaw` (adds the wrapper) | 3.65 s |

pi reports all three as ~7 s. Benchmark wall time cannot measure mdclaw CLI
latency; only direct timing can.

**Second finding, not yet fixed:** `bin/mdclaw` calls `_conda_env_exists()`
before falling through to Singularity, and `conda env list` costs **1.06 s** of
the 1.30 s wrapper overhead — on a host that has no `mdclaw` conda env at all.
Reading `~/.conda/environments.txt` answers the same question instantly. Holding
the change until the K=3 sweep finishes so the three repeats stay comparable.

**Third:** the 60-min cap is being consumed by model generation, not by tools.
Of the four timeouts in rep1 so far, P09 spent 3.1 min of its 60 inside tool
calls and P24 spent 0.3 min. Making the CLI faster cannot fix those.

---

## 2026-08-14 — The pass^k sweep ran slow: contention, not the refactor — but it exposed a 3.4 s tax on every CLI call

I stopped the K=3 pass^k sweep at rep1 19/40 because tasks were taking ~1.7×
the July wall time, and went looking for a regression in the de-over-engineering
work. **There is none.** What the transcripts actually show:

| metric (21 tasks in common) | July 20 | Aug 14 |
|---|---|---|
| per-`mdclaw`-invocation latency, light calls | 6.04 s median | 7.19 s median |
| `build_amber_system` (CPU, tleap), same tasks | 1026 s total | 938 s (0.91×) |
| `solvate_structure` (CPU), same tasks | 701 s total | 771 s (1.10×) |
| `run_minimization` (GPU), same tasks | 384 s total | 868 s (**2.26×**) |

Only the GPU step inflated, and only for the first ~4 h of the run: hourly
medians went 121.8 -> 99.0 -> 79.7 -> 48.7 -> 14.2 -> 10.5 s, i.e. back to
July's 13–19 s by +5 h. Same task, same system, `platform: CUDA` in both,
identical `max_iterations`, `restraint_count` (1309) and final energies. That is
host contention — this box shares 7× A6000 with other jobs — not code.

**The `bin/mdclaw` PKG_ROOT bind is not the cost either.** A/B, 10 reps each,
`mdclaw --list-json inspect_molecules`: with the bind + NFS `PYTHONPATH`
5.82 s, against the SIF's baked package 6.06 s. Within noise, and the bind side
is if anything faster. My earlier "+1.7 s per call" was a cold-cache artifact.

**What the hunt did find: `import mdclaw` cost 4.6 s, of which 4.5 s was
`import torch`.** `mdclaw/__init__.py` ran `_preload_torch_for_openmm_torch()`
at import time — every CLI call, including `--list-json`, `inspect_job` and
`create_node`, dlopened libtorch's CUDA libraries to keep the openmm-torch
plugin working (the June-29/July-7 fix for PythonTorchForce). Breakdown inside
the SIF: singularity `exec true` 0.38 s, + python start 0.46 s, + `import
mdclaw` 4.51 s, + full `_discover_tools()` 5.81 s.

Two measured facts turned that into a fix:

1. `importlib.util.find_spec("torch")` gives the library path without executing
   torch: 3.48 s -> 1.46 s.
2. The dlopen does **not** have to precede `import openmm`. It must precede the
   *plugin scan*, and the scan can be re-run: dlopen, then
   `Platform.loadPluginsFromDirectory(Platform.getDefaultPluginsDirectory())`,
   and the CUDA kernel registers. Verified three ways on an A6000 — `early`
   (today's order) OK, `late` without a rescan fails with "Platform does not
   support the requested kernel" exactly like no preload at all, `late` **with**
   the rescan OK (71.25 kJ/mol from a real PythonTorchForce Context).

So the preload moved out of `mdclaw/__init__.py` into
`custom_forces._preload_libtorch_cuda()`, called from `_import_openmmtorch()` —
the one code path that needs it. `MDCLAW_PRELOAD_TORCH_FOR_OPENMM` is gone; a
knob for a cost nobody pays any more is just more surface.

Result, 8 reps each: **5.91 s -> 2.62 s per CLI call (2.26×)**. At ~30 mdclaw
invocations per MDPrepBench task that is ~100 s/task, ~11 % of a 15-min task,
and ~3 h off a 120-task K=3 sweep.

`tests/test_torch_preload.py` was rewritten for the new contract and
mutation-tested: dropping the rescan, reversing the c10/torch_cuda order,
dropping RTLD_GLOBAL, rescanning on CPU-only torch, going back to `import
torch`, and re-adding the preload to `mdclaw/__init__.py` are all caught.

**Lesson.** Two of my three suspects (the PKG_ROOT bind, "the model got slower")
were wrong, and the July-vs-today medians said so within minutes — but only
after I stopped comparing *inter-event gaps* and started comparing *the same
step on the same task*. Aggregate latency hides which layer moved; a paired
comparison names it.

---

## 2026-08-14 — Why P26 kept timing out: it never entered the workflow

`P26_prep_zinc_metalloenzyme_2cba` (carbonic anhydrase II, catalytic Zn) was the
only MDPrepBench task pi + deepseek failed repeatedly — 3 timeouts in 4 attempts
against a 30-min cap, while P27 (Mn), P30 (Zn+DNA) and P06 (Ca) passed first try.

**One thing separates the runs.** Both successes ran the canonical workflow
(bootstrap -> inspect_job -> create_node -> explain_node -> inspect_molecules ->
prepare_complex -> minimize). All three failures reached none of it: two called
no workflow tool at all, one only introspected `--list-json prepare_complex`.
And every failure ends on a filesystem-wide `find /` — neither success runs one.
This host mounts ~390 TB of NFS under `/` (117T + 99T + 98T + 73T); measured,
`find / -name ions.xml -path '*amber*'` does not finish in 60 s. One such command
consumes the whole remaining budget, which is why the transcripts stop at 2.2 /
16.6 / 17.9 min but the runs die at 30.

So the chain is: skip `inspect_molecules` -> never learn the default water XML
already covers ZN -> go establish it yourself -> grep force-field XMLs ->
`find /` -> budget gone.

**Both of my hypotheses were wrong, and the evidence says so.**

- "The CLI cannot answer whether Zn is supported." False. On the real 2CBA,
  `inspect_molecules` already returned `metal_parameterization_required: false`
  plus a note that the default OPC water XML provides the templates. My first
  check used a bare-ZN-only stub PDB that never reached the metal-detection path
  — the test was wrong, not the tool.
- "My skills consolidation buried the ion policy by deleting
  `skills/md-prepare/ion-policy.md`." Refuted decisively by the advisor: the
  July P26 success never read that page (only the spine and explicit-water.md).
  Nor did July's P27, which instead parsed `amber19/opc.xml` inside the
  container by hand. The over-verification habit predates a9c6255 entirely.

**Fixed anyway, where the agent actually looked:**

- The verdict was prose in `notes.metal_handling`, far from the ion guidance.
  `preparation_guidance.ions` now carries stable values — `bare_ion_templates`,
  `bare_ion_templates_water_model`, `bare_ion_templates_scope`. The scope name is
  deliberately narrow: templates existing for a bare ion is not a claim that the
  coordination site is scientifically modelled.
- `metal_parameterization_required` was hardcoded `False` regardless of the
  catalog check. Latent today (every multivalent metal in the detector is in the
  OPC catalog) but a lie waiting to happen; now derived.
- `explicit-water.md` told the agent that finding multivalent metals means
  finishing "the matching explicit prep branch". A standard bare ion needs no
  branch, and that sentence invites exactly the investigation that killed these
  runs. It predates a9c6255.
- `--list-json <node tool>` said `job_dir` and `node_id` are required without
  saying where they come from — the agent that introspected before calling got a
  parameter list and no way in. Node-required tools now carry `workflow_entry`.

Skills net -3 lines (the duplicated ion sentence in `prepare-complex.md` and the
page-hunting route in `SKILL.md` are gone); no new tool.

**What this does not establish.** One passing run would not prove anything: the
pre-fix state also passed 1 in 4. Divergence is model-level variance and these
changes do not forbid it — they put the answer and the way back where a
diverging agent was already looking. The reliable guard is at the harness shell
boundary (refuse `find` rooted at `/`, `/home`, `/data*`; cap discovery commands
and kill the process group), which belongs to MDPrepBench, not MDClaw.

**Also learned:** pi's provider config changed. `spark1-vllm` is gone;
`deepseek-cloudflare/deepseek-v4-flash` now points at the same local vLLM
(`http://192.168.1.61:8000/v1`). The first rerun died in 0 min on
`Model "spark1-vllm/deepseek-v4-flash" not found` — an environment change, not a
code one. The memo entry of 2026-08-13 that called the spark1 name the real one
is superseded.

---

## 2026-08-13 — v0.6.5: MDAnalysis in, image rebuilt, and a lint that started screaming

The runtime image was rebuilt so this week's simplification actually ships, and
MDAnalysis was added beside mdtraj. Both went into one build rather than two:
`container/Dockerfile` copies `mdclaw/` before the conda stage, so any source
change forces a full rebuild including the OpenMM source build (~1 h), and
sequencing them would have cost that twice plus a second ~15 GB push for the
same end state. The dependency risk was retired first — `pip install --dry-run
MDAnalysis` inside the published image showed 2.10.0 resolving with no
numpy/scipy movement, adding only GridDataFormats, mmtf-python, mrcfile,
msgpack and threadpoolctl.

MDAnalysis is declared in `pyproject.toml` next to mdtraj, which is the one
place that reaches both targets: the conda env through `environment.yml`'s
`pip: -e .`, and the image through `pip install ".[dev]"` in stage 1.

Version bumped to 0.6.5 because `bin/mdclaw` derives the default Docker tag
from `plugin.json`: leaving it at 0.6.4 would either strand Docker users on the
old image or redefine a published release tag. `:0.6.5` and `:latest` now share
`sha256:6d5ff025…`; `:0.6.4` is untouched.

Verified on the image (19/19 container tests, GPU) and again on the SIF: 77
tools, v0.6.5, MDAnalysis 2.10.0, mdtraj 1.11.1, CUDA present, and — checked
deliberately — the *baked* package answers an unknown tool with
`tool_not_available` JSON and a renamed one with its replacement. That check
matters now that `bin/mdclaw` binds the checkout: ordinary work would no longer
notice a stale baked package, but plugin users run exactly that copy. Full
suite on the new SIF: 1349 passed, 3 skipped.

**What the swap exposed.** ruff went 0.15.21 → 0.16.2, and since
`pyproject.toml` selected no rules, `ruff check mdclaw/ tests/` — the command
CLAUDE.md tells contributors to run — went from clean to **1,492 findings**
overnight. Nothing in the code changed; the defaults widened. A lint that
always screams is a lint everyone learns to ignore, which is the same failure
mode as the flaky test in the previous entry. The rule set the code was written
under (`E4, E7, E9, F`) is now pinned, and both ruff versions agree on the
result.

Pinning then surfaced three unused imports I had introduced and not seen,
because my final lint runs had narrowed to `mdclaw/` and skipped `tests/`. It
also left the 17 pre-existing E702/E741 violations in two test files visible;
those are fixed too, so the documented command is actually green rather than
green-if-you-ignore-the-usual-noise.

**Unresolved host issue:** `/` is at 100% (5.2 G free), which is what made the
first `singularity pull` fail — SIF conversion was redirected to `/home` via
`SINGULARITY_TMPDIR`. Docker holds 221 GB of images and 190 GB of build cache,
371 GB reclaimable. Left alone deliberately: pruning the cache makes the next
image build much slower, and that is the maintainer's call.

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

**A flaky test that predates all of this.** The full suite then failed on
`test_embed_in_membrane_runs_parallel_packmol_race`. It is not a regression:
at HEAD it passed 2 of 5 runs, and at `dce72c6` — before any of today's work —
1 of 5. Every "full suite green" claim in this memo, including today's, was
partly luck. The implementation is right: the race cancels lanes that have not
started once a winner is accepted, and a sibling test exists for exactly that.
The test's premise was wrong — it assumed all four lanes always reach the
runner, so whenever one lane finished before the last was scheduled, the
cancelled lane went unrecorded and the count came up short. Fixed with a
`threading.Barrier(4, timeout=30)` in the stubbed runner: every lane must
arrive before any returns, which is deterministic (10/10) and still fails
loudly if a real regression starts fewer lanes. Worth noting that a test
failing 40–80% of the time was in a position to hide someone's real regression
for as long as it existed — the same failure mode the 2026-08-11 entry
describes.

Full suite after all of the above: 1349 passed, 3 skipped, 0 failed; ruff clean.

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
