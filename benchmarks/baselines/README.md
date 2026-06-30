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

## `json_only_no_run.py`

Writes a confident `manifest.json` + `metrics.json` declaring a completed,
minimized, force-field-applied system, but ships no real artifacts.

Expected behavior:

- Rejected / scored zero. Because the artifact is the source of truth, a
  `completed` submission with no loadable OpenMM bundle and no minimized
  structure fails the hard physical-validity gate, and the empty `command_log`
  fails the execution-evidence integrity check. A correct benchmark must not
  credit this; a non-zero score here would indicate the scorer trusts JSON over
  artifacts.

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
| `json_only_no_run` | `mdclaw-free` | fabrication, must be rejected |
| MDCrow (when run) | `mdclaw-free` | external entrant |

All are scored by the same neutral MDClaw scorer. The spread between the MDClaw
reference and the naive floor on discriminating tasks — and the rejection of the
fabrication baseline — is the evidence that the benchmark is rigorous and
discriminative. See `docs/benchmark/fairness-protocol.md` and
`docs/benchmark/capability-coverage.md`.
