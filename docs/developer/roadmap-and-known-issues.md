# Roadmap And Known Issues

## Known Issues

### Established Metal Site Is Not Held Through Minimisation (fix candidate)

Observed 2026-09-16 in kimi-k3 v4, 062_metal_6w9c r3 (6W9C PLpro, ZN C402
bound by Cys189/192/224). prep records the site as `metal_sites[0].established`
with motif `ZN-Cys3` and the thiolates are built as CYM, but the topology is a
plain non-bonded zinc and `run_minimization --restraint-atoms solute_heavy`
does not cover the ion. In one replicate of three the zinc left two of its
thiolates during minimisation (SG-Zn 1.9 A -> more than 6 A); the other two
replicates, flag-identical, kept all three. Downstream, MDDataBench's
metal-ligand exemption (3.5 A on `minimized_structure.pdb`) no longer covered
CYM224 and the attempt failed the residue-atom-count check. Proposed fix:
when prep establishes a metal site, `build_amber_system` should either add
harmonic Zn-ligand distance restraints that `run_minimization` and
`run_equilibration` keep by default (a `metal_site_restraints` parameter with
an explicit off switch), or offer a bonded model; the topo receipt should state
the metal model in `applied.facts` (today it has no metal entry). On the
benchmark side, deriving the exemption from the declared site instead of a
coordinate frame is recorded in the MDDataBench memo.

### Skill Text Fixes from the glm-5.3-flash Campaign (fix candidates)

From the transcripts of the five cli_skill_sif failures (memo 2026-09-16):

1. `skills/md-prepare/membrane.md`: next to the embed example, say which
   parameters select the cached patch (lipids, ratio, water model, salt,
   `dist_wat`, `leaflet`, `patch_side`) and that changing any of them rebuilds
   the patch on the current host (CPU on a login node: 25+ minutes); list the
   bundled compositions and their geometry. Today the only mention of
   `dist_wat` is in the Packmol-race retry paragraph, and an agent optimising
   box size for speed chose `--dist-wat 10.0`.
2. Same page: mark `--membrane-backend packmol-memgen` as a debugging path
   that packs the whole box with packmol on the CPU and never as a way to
   avoid a patch cold build. An agent chose it to avoid a cold build that
   would not have happened.
3. Expose the patch-cache probe as a tool (`probe_patch_cache` already exists
   in `mdclaw/solvation/patch_membrane.py`) or report `hit`/`miss` and the
   fingerprint parameters in `explain_node` for a solv node, so an agent can
   check before embedding.
4. `skills/md-prepare/SKILL.md` step 4 (or `defaults-and-guardrails.md`, which
   every agent reads): state in one line that a request for standard ionisable
   states means `--protonation-method no-prediction` (renamed from `standard` on 2026-09-17), not `--ph 7.0`; the rule now
   lives only in `prep-chemistry.md`, which the failing agent never opened.
5. `mdclaw/_receipt.py` prep facts: add the protonation method actually used
   and a tally of non-default states (HIP, HID/HIE counts, ASH, GLH, LYN, CYM)
   so a propka outcome that contradicts the request is visible in the receipt.

### Prep Receipt Hides Auto-Included Glycan Chains (fix candidate)

Observed 2026-09-14 in the glm-5.3-flash pilot, task 069_soluble_1aol (1AOL:
protein label chain A, N-linked NAG on label chains B/C). The agent was told to
leave the glycan out, ran `prepare_complex --select-chains A
--protonation-method standard --solvent-type explicit`, and `split` applied
`covalently_linked_glycan_chains_auto_included` (14 NAG on two chains). The
result the agent saw carried no trace of it:

- `message` / `applied.summary`: `prepared: 1 protein chain(s) A (228
  residues), 0 ligands, 6 disulfide(s), 3,484 atoms`
- `warnings_count`: 1 (only the ASN A168 HD21 - NAG B237 C1 close contact)
- `applied.options.select_chains`: `not_reported`
- the adjustment lives only under `split.selection_adjustments` and
  `applied.facts.glycans` in `result.json`

The agent reported "glycan excluded" from the summary, built and ran the
glycosylated system, and the scorer saw seven backbone fragments (12/20). The
same signature appeared in kimi-k3 campaign v2 on 069. Attempts that fail this
way are rerun candidates after the fix, not evidence about the model.

Proposed fix (`mdclaw/_receipt.py` and the prep envelope):

1. Any selection adjustment that changes composition must appear in `message`
   and `applied.summary`, e.g. `2 glycan chain(s) auto-included (NAG x14);
   pass --include-types protein to leave them out`, and be echoed as a
   structured warning with its code.
2. `applied.options.select_chains` (and `include_types`) must report the
   effective value rather than `not_reported`, so the requested-vs-applied
   difference is visible in the receipt.
3. Consider a hint on the prep result when a glycan was auto-included and the
   study plan or `--conditions` names no glycan.

Evidence: `/data1/rkp00079/rku00161/runs/glm-5.3-flash-skill-pilot/attempts/069_soluble_1aol/`
(`agent.stdout.jsonl`, `workspace/study/jobs/main/nodes/prep_001/result.json`);
`docs/memo.md` entry of 2026-09-14 on the glm-5.3-flash pilot.

### Agent-Detached Processes Outlive a Timed-out Attempt (fix candidate, MDDataBench harness)

Observed 2026-09-15, glm-5.3-flash campaign, 010_membrane_6kux r3. After
`embed_in_membrane --membrane-backend packmol-memgen` hit pi's 1500 s command
timeout, the agent wrote `continue_attempt.py` under `$TMPDIR` and started it
detached; the attempt was sealed `agent_no_submission` at 1786 s, but 26
minutes later the login node still ran its tree (python3 -> apptainer starter
-> mdclaw embed -> packmol-memgen x2 -> packmol x2 at 100 % CPU). Killed by
PID. Proposed fix: run each agent in its own session/cgroup (`setsid`, or a
systemd scope where available) and kill the whole group at timeout, then
verify with `pgrep -f <attempt dir>` and record `orphans_killed` in the
result; `sweep_dead_jobs.py` could add the same scan for sealed attempts.

### `--membrane-backend packmol-memgen` on a CPU-only Host (fix candidate)

Same attempt: the legacy full-box backend packs the whole protein box with
packmol-memgen (two `packmol` runs at 100 % CPU for over 25 minutes), while
the default patch-tile backend embedded the same protein in about 70 s in r1
and r2. The skill never suggests the flag. Proposed fix: refuse or warn before
`begin_node` when `membrane_backend != "patch-tile"` and no GPU platform is
available, with a hint naming the default; or drop the legacy backend from the
CLI surface and keep it as an internal fallback.

### Uncached Membrane Patch Build Runs on a CPU-only Login Node (fix candidate)

Observed 2026-09-15 in the glm-5.3-flash campaign, 002_membrane_5zk3 r1. The
agent passed `--dist-wat 10.0` (default and cached value 17.5); `dist_wat` is in
the patch-cache fingerprint, so `embed_in_membrane` logged "no cached membrane
patch ... building it once now" and ran the packmol + OpenMM patch equilibration
on the login node's CPU for the remaining 25 minutes of the attempt's budget
(8 threads on a shared node; the attempt timed out with `solv_001` still
`running`). On a GPU the same build takes minutes.

Proposed fix (`mdclaw/solvation/patch_membrane.py::ensure_membrane_patch` and
the `embed_in_membrane` preflight):

1. When the cache probe misses and no CUDA/OpenCL platform is available (or
   `platform` resolves to CPU), refuse before `begin_node` with a stable code
   (e.g. `membrane_patch_cache_miss_on_cpu`) whose hints name the cached
   parameter set (`--dist-wat 17.5`, the bundled compositions) and an explicit
   opt-in flag (`--allow-patch-build`) for hosts where the build is intended.
2. Report the probe result (`hit` / `miss`, fingerprint, the differing
   parameters) in the result envelope so the agent sees why a build started.
3. Optionally exclude `dist_wat` from the fingerprint when it only changes the
   water slab, if the patch geometry allows re-solvating a cached patch.

Evidence: `/data1/rkp00079/rku00161/runs/glm-5.3-flash-skill-full/attempts/002_membrane_5zk3/cli_skill_sif__pi__rikyu-glm-5.3-flash__r1/workspace/.mddatabench/tmp/solv_err.log`;
`docs/memo.md` entry of 2026-09-15.

### packmol-memgen NumPy Compatibility

Some packmol-memgen versions still reference removed NumPy aliases.

```bash
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" \
  "$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
```

### Protein Protonation

`clean_protein` uses a two-tier strategy:

1. Primary: `pdb2pqr` + propka for pH-aware protonation.
2. Fallback: `pdb4amber` + reduce for geometry-based protonation.

### Anionic Lipid Patch Equilibration Segfault

Anionic mixtures such as `DOPE:DOPG 3:1` now pack (via the charged-lipid
`--saltcon` neutralization retry in `ensure_membrane_patch`) and build a valid
Lipid21 topology, but the subsequent OpenMM patch equilibration segfaults
(SIGSEGV) during NVT heating. They are excluded from the default warm-up set in
`scripts/warmup_membrane_cache.py` so container builds stay green. PC/PE/CHL1
compositions are unaffected. Root-causing the crash (likely a bad packed
contact or PGR-specific topology issue feeding NaN forces) is deferred.

### Large DAGs: every status change rewrites the whole progress.json

`progress.json` is one JSON index rewritten (under `progress.lock`) by every
node operation: creation, Slurm stamping, `begin_node`, `complete_node`. On
a 26k-node weighted-ensemble job (13.7 MB indented) the driver's own work
became ~90 % of a round's wall time (WE-26). Done so far: the index is
written compact (8.7 MB, serialization 0.20 s -> 0.04 s), a round's
segments are created in one write with no per-node preflight
(`lifecycle._create_nodes_bulk`: 0.22 s -> 0.003 s per node at 26k nodes),
and `inspect_rounds` no longer reads every segment (WE-25). Still per
segment: the Slurm stamp at submission and the tool's own `begin` /
`complete` (2-3 index rewrites per segment, lock-serialized across the
packed tasks). Beyond ~10^5 nodes the index needs a different shape (a
per-scheme sub-index, or a journal folded into the index periodically).

## Resolved

### Shared file systems: a record replaced by another host can be missed for a moment

`progress.json` and every `node.json` are written as tmp + `os.replace`, so a
reader on the same host always sees a complete version. On Lustre (RIKYU,
`flock` mount) a reader on *another* client found `progress.json` absent for
a moment while a rename from a different host landed: the index was intact
throughout (node directories == index entries), but lock-free readers on
compute nodes — the CLI preflight, input resolution, the execution-context
check — reported "progress.json is missing", refused the run and spent the
segment (`parent_not_completed`, `input_resolution_blocked`,
`node_execution_context_invalid`; 6 of ~10,000 WE segments on 2026-09-21).
Readers now go through `mdclaw/node/io.py::_load_json_settled` (5 reads,
0.1 s apart, before a miss is believed; `_load_progress_v3`,
`_read_node_json_path`, `read_node`, the rounds driver's `_node_exists`),
and `_load_progress_v3(create_if_missing=True)` never initializes a fresh
index on such a miss. The CLI preflight additionally answers
`progress_unreadable` (node not spent) when the index stays unreadable.
Writers were never the problem: all of them hold `progress.lock` (cluster-wide
flock) and write atomically.

### Membrane Building: patch-tile Backend

`embed_in_membrane` no longer packs the full protein box with packmol-memgen on
every run. The default `membrane_backend="patch-tile"` builds a small
composition-keyed lipid patch once, equilibrates it under PBC, caches it under a
protein-size-independent fingerprint, then tiles the equilibrated patch around
the MEMEMBED-oriented protein, carves overlaps, and neutralizes by swapping bulk
waters for ions. This resolves the slow / non-converging full-box packing for
cholesterol mixtures (e.g. `POPC:POPE:CHL1 2:1:1`, MDPrepBench P18).

- Cold build cost (packmol pack + OpenMM min/eq) is paid once per composition;
  `scripts/warmup_membrane_cache.py` pre-builds representative compositions into
  a read-only bundled cache (`MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR`, populated in
  the container build) so runtime hits without equilibration.
- Bundled cache hits require fingerprint agreement on composition + defaults
  (patch size, salt, water model, equilibration params, force field). The
  fingerprint deliberately excludes the packmol-memgen version (schema v2), so a
  patch built in one environment (local conda) still hits in another (the
  source-built container) even when their AmberTools builds differ. On a miss
  the runtime cold-builds into the writable cache (`MDCLAW_MEMBRANE_CACHE_DIR` /
  `MDCLAW_CACHE_DIR/membrane_patches`). The packer version is kept in the patch
  manifest as provenance only.
- The removed `slab-cache` backend's low-level PDB/geometry/carve helpers were
  moved to `mdclaw/solvation/patch_membrane.py`; `mdclaw/membrane_cache.py` was
  deleted.

### Benchmark Integrity Rollout

Moved with the benchmark suites to
[matsunagalab/MDPrepBench](https://github.com/matsunagalab/MDPrepBench) and
[matsunagalab/MDStudyBench](https://github.com/matsunagalab/MDStudyBench).

### Ligand Chemistry Handoff

The public ligand contract is `ligand_chemistry`: prep records
SDF/SMILES/charge/provenance, and `build_amber_system` validates OpenFF
Molecule formal charge, assigns ligand partial charges with OpenFF NAGL first,
and uses `GAFFTemplateGenerator` AM1-BCC only as fallback.

## Source-Bundle DAG Principle

Each `job_dir` should contain one structural source bundle with one `source`
node. That bundle may contain multiple candidate structures normalized under
`artifacts/candidates/`; optional raw inputs are provenance only. A `prep` node
selects one concrete candidate before creating an MD-ready physical system, and
variant exploration then happens by branching from `prep`, `solv`, `topo`,
`eq`, or `prod` nodes inside the same DAG.

Supporting multiple independent source roots in one job remains out of scope
because it makes input resolution and system identity ambiguous.

## PTM Coverage

Current support covers SEP, TPO, and PTR:

- `prepare_complex` detects them through `detect_ptm_sites`.
- PDBFixer strips them as nonstandard replacements.
- `phosphorylate_residues` reapplies them on a branched prep node, either from
  detected metadata or explicit sites.
- `build_amber_system` auto-loads `phosaa19SB` for ff19SB or `phosaa14SB` for
  ff14SB.

Deferred PTM work:

- Phospho-histidine (`H1D`, `H2D`, `HEP`; Amber naming varies).
- O-GlcNAc, acetylation, methylation, ubiquitination, lipidation, and other PTMs.
- User-selectable phosphate protonation states.
- Optional preservation of crystallographic phosphate coordinates.
- Per-chain PTM summaries and PTM-aware roundtrip validation in
  `inspect_molecules`.

## MMDB Integration

Future MMDB support should cover:

1. Reading forcefield recommendations, known issues, and reference parameters
   into node metadata.
2. Writing completed job results back for cataloging.
3. Letting agents query MMDB to choose parameters and compare systems.

Likely schema additions include a `node.json` `mmdb` section and a top-level
`progress.json` `mmdb_id`.

## HPC Follow-ups

The node-aware SLURM integration has landed. Remaining nice-to-haves:

- Propagate SLURM state into `progress.json` summaries so skills can surface
  active jobs without iterating tracker rows.
- Add an optional `check_job --poll` command that blocks until terminal state.
- `submit_mps_job` (2026-09-16) manages the NVIDIA MPS daemon inside the job
  because RIKYU configures `GresTypes=gpu` only. Follow-ups: (a) a
  `gres/mps` route for sites that expose it (one Slurm job per simulation
  with `--gres=mps:<pct>`, no daemon of our own); (b) multi-GPU packing
  (`--gpus 2` and up) is implemented by per-slot `CUDA_VISIBLE_DEVICES`
  under one daemon but has only been unit-tested, not run; (c) failure
  isolation: one failed slot fails the whole job and cancels the `afterok`
  chain behind it, so the skill resubmits the survivors by hand -- a
  `submit_mps_job --continue-from-job <id>` that lists the completed
  parents' children would remove that step; (d) `estimate_md_throughput` does
  not know about packing; the md-study budget page uses a fixed 1.5x planning
  gain for 4 packed replicates until GB200 numbers per size class exist.
