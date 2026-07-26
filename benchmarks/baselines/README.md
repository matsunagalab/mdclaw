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

For the standard v0.4 S01 task, writes a confident grounded-v2 submission that
reports a supplied literature outcome, a prospective-looking intent, and the
correct public construct, but ships fake trajectories and no harness-attested
production. Legacy task IDs retain the older v0.3 shape.

Expected behavior:

- Classified as `invalid_execution` with zero scientific-answer credit. The v2
  scorer reloads raw trajectories and requires prospective harness lineage, so
  matching the held-out outcome cannot compensate for missing MD evidence.

The active S01 task requires a real prospective pressure comparison; the
`tests/test_benchmark/_fake_study_submissions.py` builder shows the v2
`analysis_intent` / `study_index` / `evidence_report` shape.

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
