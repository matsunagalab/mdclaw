# MDPrepBench Weak Baselines

These baselines are reference runners that establish the benchmark's
**discrimination floor**: deliberately weak or fabricated solvers whose scores
show that MDPrepBench separates real MD-prep capability from shortcuts. They are
all MDClaw-free on the solver side (`tooling_condition="mdclaw-free"`); only the
shared scorer (run separately) uses MDClaw. Running them across the suite is
operator-driven.

## `naive_pdbfixer_prep.py`

pdbfixer + one fixed force field (`amber14-all.xml` + `amber14/tip3pfb.xml`,
tip3p water) + a short minimization, packaged via the standalone no-MDClaw
packager. It has no chain-selection, ligand, ion-concentration, mutation, PTM,
disulfide, terminal-capping, glycan, membrane, or NMR-model logic.

Expected behavior:

- Passes trivial single-chain apo preparation (e.g. `P01`): it builds a
  loadable, force-field-applied, minimized OpenMM system, which clears the
  physical-validity gate and the identity checks for a simple apo protein.
- Loses graded credit on discriminating tasks where the requested capability is
  absent from the baseline, for example: chain/assembly selection, ligand-pose
  preservation, specific ion concentration + neutrality (`P25`), water-model
  fidelity (`P22`, e.g. an OPC request answered with tip3p), point mutations,
  PTM restoration, disulfides, glycans, and mixed-lipid membranes.

This is the "no-MDClaw floor": a credible general-purpose MD-prep workflow must
beat it on the discriminating capabilities, not just on the trivial task.
Because this script performs minimization itself, invoke it through the harness
wrapper so strict v0.3 scoring records that successful stage:

```bash
$MDCLAW_BENCHMARK_STAGE_WRAPPER --stage min -- \
  python benchmarks/baselines/naive_pdbfixer_prep.py \
  --pdb-id 2LZM \
  --submission-dir <task_run_dir>/submission \
  --task-id P01_prep_simple_monomer_t4l
```

## `empty_submission.py`

Creates the requested submission directory but writes no raw artifacts.

Expected behavior:

- Rejected before scoring because the required raw OpenMM triple and
  `prepared_structure.pdb` are missing. A correct benchmark must not infer a
  completed preparation from an empty handoff.

## `study_literature_guess_no_md.py` (MDStudyBench)

Writes a confident comparative-MD submission for a scientific-answer task
(S01/S02/S04/S05) that reports the correct literature direction but ships fake
trajectories (a DCD magic header over junk bytes) and no real paired mutation.

Expected behavior:

- Scored zero on the comparative scientific-answer tasks. StudyBench binds the
  scientific answer to real artifacts: the `trajectory_rescan` and
  `paired_mutation_topology` hard-fail gates are recomputed from the submitted
  trajectories/topologies, so a garbage trajectory clamps `weighted_total` to 0
  even when the declared direction matches the experimental truth. This is the
  StudyBench discrimination floor — a credible solver must run real comparative
  MD, not guess the textbook answer.

All four study tasks require real comparative MD; the
`tests/test_benchmark/_fake_study_submissions.py` builder shows the expected
submission shape (index-aligned `outputs.trajectories` / `outputs.topology`).

## Intended comparison set

Group runs by `tooling_condition` and read the per-capability profile
(`identity`, `physical_validity`, `fidelity`, `provenance`) plus `weighted_total`
from each `summary.json`:

| Run | `tooling_condition` | Role |
| --- | --- | --- |
| MDClaw reference | `mdclaw-skills+cli` | full-capability reference |
| `naive_pdbfixer_prep` | `mdclaw-free` | weak no-MDClaw floor |
| `empty_submission` | `mdclaw-free` | missing-artifact floor, must be rejected |
| MDCrow (when run) | `mdclaw-free` | external entrant |

All are scored by the same neutral MDClaw scorer. The spread between the MDClaw
reference and the naive floor on discriminating tasks — and the rejection of the
fabrication baseline — is the evidence that the benchmark is rigorous and
discriminative. See `docs/benchmark/fairness-protocol.md` and
`docs/benchmark/capability-coverage.md`.
