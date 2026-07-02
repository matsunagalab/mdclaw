#!/usr/bin/env bash
set -euo pipefail

# Lightweight example: create a study_dir that groups planned WT and mutant
# MDClaw job_dirs. This does not run MD; it demonstrates the study tool API.

STUDY_DIR="${1:-study_mutation_screen}"

mdclaw init_study \
  --study-dir "$STUDY_DIR" \
  --title "WT vs V148A stability screen" \
  --objective "Compare whether V148A changes active-conformation stability relative to WT." \
  --description "Example study_dir that groups two ordinary MDClaw job_dirs." \
  --metadata '{"example":"study/mutation_campaign","domain":"protein_md"}'

mdclaw add_study_job \
  --study-dir "$STUDY_DIR" \
  --job-id wt \
  --job-dir jobs/wt \
  --role reference \
  --label "Wild type" \
  --description "Reference WT MDClaw job." \
  --create-job-dir \
  --metadata '{"system":"WT","planned_replicates":3}'

mdclaw add_study_job \
  --study-dir "$STUDY_DIR" \
  --job-id v148a \
  --job-dir jobs/v148a \
  --role candidate \
  --label "V148A mutant" \
  --description "Mutant MDClaw job for comparison against WT." \
  --create-job-dir \
  --metadata '{"system":"V148A","planned_replicates":3}'

mdclaw record_study_log \
  --study-dir "$STUDY_DIR" \
  --record-type question \
  --question "Does V148A stabilize the active conformation relative to WT?" \
  --status active \
  --rationale "The study has two planned systems and should compare replicate-level MD evidence."

mdclaw record_study_log \
  --study-dir "$STUDY_DIR" \
  --record-type decision \
  --phase plan \
  --decision "Prepare WT and V148A as separate job_dirs under one study_dir." \
  --reason "Each job_dir keeps a single source root while the study_dir captures the cross-system comparison." \
  --inputs study.json \
  --outputs jobs/wt jobs/v148a

mdclaw record_study_log \
  --study-dir "$STUDY_DIR" \
  --record-type token_usage \
  --phase plan \
  --purpose "Draft initial WT/mutant campaign plan." \
  --tokens 12000 \
  --result "Created two planned jobs and one active study question."

mdclaw summarize_study --study-dir "$STUDY_DIR"
