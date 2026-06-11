# MDStudyBench

MDStudyBench is the study-level suite in the MDAgentBench benchmark family.
The current dataset is `MDStudyBench-v0.1` under `benchmarks/mdstudybench/`.

MDPrepBench asks whether an agent can make an MD-ready system. MDStudyBench asks
whether an agent can turn a scientific question into comparative MD evidence,
analysis, calibrated conclusions, and an auditable study bundle.

This suite is intentionally small. Add tasks only when they cover a distinct
scientific-answer pattern with reliable hidden truth and a clear evidence
contract; broad preparation coverage belongs in MDPrepBench.

## Current Scope

| Task | Focus | Required Evidence |
|---|---|---|
| S01_stability_t4l_l99a | T4 lysozyme L99A stability direction | WT/mutant trajectories, `metrics.md_analysis`, cited evidence, limitations, and `effect.direction`. |
| S02_ppi_hotspot_barnase_d39a | Barnase-barstar D39A binding direction | WT/mutant complex trajectories, interface metrics, cited evidence, limitations, and `effect.direction`. |
| S03_t4l_wt_vs_l99a_methods | Auditable WT vs L99A study bundle | Methods draft, provenance study roles, decision log, evidence report, and calibrated direction. |

## Dataset Layout

```text
benchmarks/mdstudybench/
  dataset.json
  schemas/
    task.schema.json
    submission_manifest.schema.json
    score.schema.json
  tasks/<task_id>/
    prompt.md          # public prompt for the agent under test
    task.json          # runner/scorer metadata; not given to agents
    truth/             # scorer-only experimental direction or reference pool
```

Export the agent-visible package before giving tasks to an external agent:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_public/mdstudybench
```

Run setup is the same scorer framework used by MDPrepBench:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --dataset-dir benchmarks/mdstudybench \
  --execution-mode lite
```
