# Register The Study And Jobs

Create or reuse a `study_dir` and record the plan. For one-job workflows, prefer
`bootstrap_md_workflow`; for richer multi-job plans, use the lower-level commands
so every job is registered explicitly.

```bash
mdclaw bootstrap_md_workflow \
  --study-dir <study_dir> \
  --question "<question>" \
  --md-goal "<md goal>" \
  --solvent-regime explicit \
  --execution-mode autonomous

# For richer multi-job plans:
mdclaw init_study --study-dir <study_dir> --title "<short title>" \
  --objective "<one sentence objective>"   # only if the study does not exist

mdclaw record_study_plan --study-dir <study_dir> --plan '<plan-json>'
```

Register planned jobs and propagate `execution_mode` and `solvent_regime` so
downstream skills inherit them:

```bash
mdclaw add_study_job --study-dir <study_dir> \
  --job-id <id> --job-dir <study_dir>/jobs/<id> \
  --role <baseline|test|control|...> \
  --label "<short label>" --description "<one-line purpose>" \
  --create-job-dir

mdclaw update_workflow_state --job-dir <study_dir>/jobs/<id> \
  --params '{"execution_mode":"autonomous","solvent_regime":"explicit"}'
```

Register jobs only when the job IDs are clear. Otherwise leave job creation to
the downstream prepare step. Keep execution state in each job DAG; the study
plan is intent and design, not a replacement for node artifacts or
`progress.json`.
