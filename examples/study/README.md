# Study Tool Examples

The study layer is an optional wrapper for campaigns that span multiple
ordinary MDClaw `job_dir`s. Each `job_dir` still represents one physical system
with one `source` root; the `study_dir` only indexes jobs and records
cross-job decisions, question revisions, token usage, and evidence reports.

## Example 1: WT vs Mutant Campaign Skeleton

Run the lightweight setup script:

```bash
bash examples/study/mutation_campaign.sh
```

It creates this campaign scaffold:

```text
study_mutation_screen/
  study.json
  decisions.jsonl
  question_history.jsonl
  token_ledger.jsonl
  jobs/
    wt/
    v148a/
```

The script intentionally does not run molecular dynamics. It shows the
study-level contract that can wrap either manual MDClaw CLI runs or an external
agent harness.

## Extending The Skeleton Into Real MD Jobs

After registering a job, create and run normal MDClaw nodes inside that job:

```bash
mdclaw create_node \
  --job-dir study_mutation_screen/jobs/wt \
  --node-type source \
  --label "WT PDB source"

mdclaw --job-dir study_mutation_screen/jobs/wt \
  --node-id source_001 \
  fetch_structure \
  --source pdb \
  --pdb-id 1AKE \
  --format pdb
```

Then continue with the usual per-job DAG:

```bash
mdclaw create_node --job-dir study_mutation_screen/jobs/wt \
  --node-type prep --parent-node-ids source_001

mdclaw --job-dir study_mutation_screen/jobs/wt \
  --node-id prep_001 \
  prepare_complex
```

For a mutant branch, keep it in its own `job_dir` if it represents a separate
physical system:

```bash
mdclaw create_node \
  --job-dir study_mutation_screen/jobs/v148a \
  --node-type source \
  --label "V148A source"
```

If the mutant is derived from the same prepared source within one physical
system exploration, use a normal `prep` branch inside a single `job_dir`
instead. The `study_dir` is for grouping jobs, not replacing the DAG.

## Recording Agent Or Human Decisions

Use study logs for cross-job reasoning that should survive across sessions:

```bash
mdclaw record_study_question \
  --study-dir study_mutation_screen \
  --question "Does V148A stabilize the active conformation relative to WT?" \
  --status active

mdclaw record_study_decision \
  --study-dir study_mutation_screen \
  --phase plan \
  --decision "Run WT and V148A with three production replicates each." \
  --reason "Replicates are needed before comparing stability metrics." \
  --inputs study.json \
  --outputs jobs/wt/progress.json jobs/v148a/progress.json

mdclaw record_token_usage \
  --study-dir study_mutation_screen \
  --phase critique \
  --purpose "Review replicate convergence and choose whether to extend runs." \
  --tokens 32000 \
  --result "Extend only the V148A replicate with unstable RMSD."
```

## Evidence Reports

Once individual jobs contain completed `prod` or `analyze` nodes, generate
reports for downstream agents, collaborators, or notebooks:

```bash
mdclaw generate_md_evidence_report \
  --job-dir study_mutation_screen/jobs/wt \
  --question "What MD evidence exists for WT stability?" \
  --target '{"system":"WT","protein":"1AKE"}'

mdclaw generate_study_evidence_report \
  --study-dir study_mutation_screen \
  --question "How do WT and V148A compare across completed MD jobs?"
```

These reports summarize node status, completed production/analyze nodes,
available analysis metrics, artifacts, limitations, and provenance. They do not
interpret trajectories or call an LLM.

## SLURM Campaign Pattern

For homogeneous production batches, build one `submit_array_job` task per
ready node across registered jobs:

```json
[
  {
    "job_dir": "study_mutation_screen/jobs/wt",
    "node_id": "prod_001",
    "command": "mdclaw --job-dir study_mutation_screen/jobs/wt --node-id prod_001 run_production --simulation-time-ns 20"
  },
  {
    "job_dir": "study_mutation_screen/jobs/v148a",
    "node_id": "prod_001",
    "command": "mdclaw --job-dir study_mutation_screen/jobs/v148a --node-id prod_001 run_production --simulation-time-ns 20"
  }
]
```

Use the existing SLURM tools for execution and monitoring. The study layer is
only the campaign index and audit trail.
