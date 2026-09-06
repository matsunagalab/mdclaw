---
name: md-report
description: "Review existing MDClaw DAG results, explain calculation history, write evidence-backed Methods with BibTeX, or prepare an offline MDDB deposit bundle. Resolve ambiguous targets before combining replicas; do not launch MD or upload data."
---

# MD Reviewer / Reporter

Follow `skills/common/preamble.md`. Use the deterministic reporting CLI, not
custom scripts to reconstruct history, select citations, or convert trajectories.
This skill reviews existing work; it does not create simulation/analysis nodes.

## Step 0: Parse and Confirm

Identify the existing job/study or requested `(job_dir,node_id)` targets and
whether the user wants an explanation, manuscript Methods, or MDDB files.
Reuse recorded solvent/execution settings; do not ask for new MD parameters.
When multiple leaves need a grouping/selection decision, ask the user, including
in autonomous mode. Never infer that all branches are replicas.

## Workflow

1. Run `mdclaw generate_md_report --job-dir <job>` or `--study-dir <study>`; use
   `--plan-id` for a requested non-active plan. For a specified node or multiple
   selected nodes, pass `--targets '[{"job_dir":"/path/job","node_id":"prod_001","label":"r1"}]'`.
2. On `report_selection_required`, ask whether to combine replicas, separate
   campaigns, or omit targets. Resubmit only selected targets with unique labels
   and `--grouping replicas|separate`. Consult
   `mdclaw --list-json generate_md_report` for the signature, not the global tool list.
3. Explain each target's history and results, then common settings and differences.
   Distinguish declared conditions, recorded execution metadata and runtime XML;
   retain replica identities, shared prefixes and unresolved evidence.
4. Cite only the supplied BibTeX entries, with their selection roles. Dedicated
   papers are not required for every feature: official documentation/version
   can be the implementation source. Never infer convergence from completion or
   compute replica statistics by pooling frames during reporting.

For **manuscript Methods**, write connected prose covering preparation, force
fields, equilibration, production, bias and analysis actually evidenced in the
report. Include units, per-stage settings and replica differences; supply the
matching BibTeX. Put missing facts/references in a separate checklist, not invented
values. Do not describe a draft with unresolved essential methods as ready to submit.

For **MDDB files**, read [mddb.md](mddb.md). Otherwise stop after the requested
report/Methods; a reporting request does not authorize deposition or further MD.
