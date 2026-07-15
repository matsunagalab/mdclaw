# Running MDCrow (or any MDClaw-free agent) on MDPrepBench

This runner shows how to evaluate an agent that uses **no MDClaw code on the
solver side** — MDCrow is the worked example, but the same recipe applies to a
plain OpenMM/pdbfixer script or an LLM that writes its own OpenMM code. The
shared MDClaw scorer remains the neutral judge; see
`docs/benchmark/fairness-protocol.md` for why scoring an `mdclaw-free` run with
the MDClaw scorer does not make it an MDClaw run.

The benchmark contract is the `submission/` directory, not the agent's internal
file registry. MDCrow tracks generated files in `ckpt/paths_registry.json`; you
only need to surface the final OpenMM `system.xml` + `topology.pdb` +
`state.xml` triple into a `submission/`.

## 1. Initialize an `mdclaw-free` run

Record the tooling condition up front so the run is grouped correctly. Use
`init_benchmark_run` (not `prepare_benchmark_run`, which defaults to the full
MDClaw skill condition):

```bash
mdclaw init_benchmark_run \
  --output-dir benchmark_runs \
  --run-id 20260613_mdcrow_prep \
  --harness-name mdcrow \
  --backend-name mdcrow-openmm \
  --model-name <llm-used-by-mdcrow> \
  --tooling-condition mdclaw-free \
  --task-ids P01_prep_simple_monomer_t4l
```

This writes `run_config.json` and `attestation.json` with
`tooling_condition="mdclaw-free"`.

## 2. Export the public package and hand the agent only the prompt

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_public/mdprepbench
```

Give MDCrow only `benchmark_public/mdprepbench/tasks/<task_id>/prompt.md` (and
`submission_contract.json` for the machine-readable output requirements). Do
**not** add task-specific flags (chains, salt, model index, FF/water) that are
not stated in the prompt — that breaks comparability.

## 3. Let MDCrow build the system, then serialize the OpenMM triple

After MDCrow finishes setup/minimization, serialize the OpenMM `System`,
`topology.pdb`, and minimized `state` it produced. In a small Python snippet
inside the MDCrow environment:

```python
from openmm import XmlSerializer
from openmm.app import PDBFile

# `system`, `simulation` come from MDCrow's own setup.
open("system.xml", "w").write(XmlSerializer.serialize(system))
state = simulation.context.getState(getPositions=True, getVelocities=True)
open("state.xml", "w").write(XmlSerializer.serialize(state))
with open("topology.pdb", "w") as fh:
    PDBFile.writeFile(simulation.topology,
                      state.getPositions(), fh)
```

## 4. Write raw artifacts into `submission/`

For MDPrepBench preparation tasks, the canonical MDClaw-free output is raw
OpenMM artifacts. Copy the final self-consistent bundle into the exact
`submission_dir` from `task_instructions.json`:

```bash
mkdir -p .../submission/topology
cp system.xml .../submission/topology/system.xml
cp topology.pdb .../submission/topology/topology.pdb
cp state.xml .../submission/topology/state.xml
cp prepared_structure.pdb .../submission/prepared_structure.pdb
# copy any task-specific raw artifacts named in submission_contract.json
```

The evaluator normalizes these files into `manifest.json`, `metrics.json`,
`provenance.json`, raw-output md5 hashes, `minimized_structure.pdb`, and
`minimization_report.json` before scoring.

The standalone packager is still available as an optional helper for a fully
MDClaw-free toolchain:

```bash
python benchmarks/tools/package_submission.py \
  --submission-dir .../submission \
  --task-id P01_prep_simple_monomer_t4l \
  --system-xml system.xml \
  --topology-pdb topology.pdb \
  --state-xml state.xml \
  --prepared-structure prepared_structure.pdb
```

When MDClaw is installed, `mdclaw package_openmm_submission` provides the same
raw-only copy operation:

```bash
mdclaw package_openmm_submission \
  --submission-dir benchmark_runs/20260613_mdcrow_prep/tasks/P01_prep_simple_monomer_t4l/submission \
  --task-id P01_prep_simple_monomer_t4l \
  --system-xml-file system.xml \
  --topology-pdb-file topology.pdb \
  --state-xml-file state.xml \
  --prepared-structure-file prepared_structure.pdb
```

Both packagers are convenience tools. They copy raw files and do not invent or
write force-field declarations, metrics, hashes, reports, or timing records.

Do not hand-edit evaluator-generated `manifest.json`, `metrics.json`, or
`provenance.json`. If raw artifacts are wrong, fix the raw artifacts and rescore
so the evaluator can regenerate derived metadata.

Use the runner-provided stage wrapper for substantive commands. The harness
records measured execution outside `submission/`; neither packager accepts a
solver command log or walltime estimate.

## 5. Score with the same neutral scorer

```bash
mdclaw score_benchmark_run \
  --run-dir benchmark_runs/20260613_mdcrow_prep \
  --dataset-dir benchmarks/mdprepbench
```

The resulting `summary.json` records `tooling_condition="mdclaw-free"`, the
`verified` flag, the per-axis scores, and the per-capability profile — directly
comparable to an MDClaw reference run and to the MDClaw-free baseline floor
(`benchmarks/baselines/naive_pdbfixer_prep`).

## Artifact-as-truth contract

MDPrepBench agents submit no metrics declaration. The evaluator deserializes
the raw OpenMM bundle and recomputes force-field application, net charge, the
water-model fingerprint, and ion molarity from the artifacts themselves.
