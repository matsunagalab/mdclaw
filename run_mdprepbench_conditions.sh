#!/usr/bin/env bash
# MDPrepBench across the three MDClaw tooling conditions, N repeats each, for one
# harness (agent) + LLM model. Prints mean +/- stdev of overall_score per
# condition. Reference: run_smoke_codex_s01.sh (direct run_benchmark_agent with
# --agent-skills-dir / --mdclaw-cli-policy).
#
#   ./run_mdprepbench_conditions.sh                 # pi + deepseek (local)
#   ./run_mdprepbench_conditions.sh claude-code haiku
#   ./run_mdprepbench_conditions.sh codex gpt-5.4-mini
#
# The three conditions (tooling_condition recorded in each run's summary.json):
#   skills+cli : --agent-skills-dir skills --mdclaw-cli-policy allow  -> mdclaw-skills+cli
#   cli-only   :                           --mdclaw-cli-policy allow  -> mdclaw-cli-only
#   free       :                --mdclaw-cli-policy forbid-without-skill -> mdclaw-free (SIF only, no mdclaw CLI)
#
# WALLTIME_MIN is deliberately the SAME for all three conditions (a fair
# comparison needs an identical per-task budget). Default 30 (MDPrepBench
# standard). Raise it globally for very slow local LLMs, never per-condition.
#
# Optional env: REPEATS(=3)  WALLTIME_MIN(=30)  JOBS  GPUS  MODEL  DATASET  DRY_RUN=1
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"          # repo root (holds ./mdclaw.sif, skills/)

HARNESS="${1:-pi}"
MODEL_ARG="${2:-}"
case "$HARNESS" in
  pi)          : "${MODEL:=${MODEL_ARG:-spark1-vllm/deepseek-v4-flash}}"; : "${JOBS:=1}"; : "${GPUS:=0}";;  # local LLM: one session at a time
  claude-code) : "${MODEL:=${MODEL_ARG:-haiku}}";                         : "${JOBS:=5}"; : "${GPUS:=5}";;  # cloud: 5 concurrent
  codex)       : "${MODEL:=${MODEL_ARG:-gpt-5.4-mini}}";                   : "${JOBS:=5}"; : "${GPUS:=5}";;
  *) echo "usage: $0 [pi|claude-code|codex] [model]" >&2; exit 2;;
esac
REPEATS="${REPEATS:-3}"
WALLTIME_MIN="${WALLTIME_MIN:-30}"
DATASET="${DATASET:-benchmarks/mdprepbench}"

# Local SIF location for MDClaw's auto runtime resolution (conda -> sif -> docker).
export MDCLAW_SIF="$PWD/mdclaw.sif"

STAMP="$(date +%Y%m%d_%H%M%S)"
BASE="cond_${STAMP}_${HARNESS//[^a-zA-Z0-9]/_}"
RUNNER=("$HOME/.venvs/mdclaw-runner/bin/python" -m mdclaw._cli)

# pi's default profile passes --no-skills, so its skills+cli condition needs the
# pi-user profile. codex/claude-code default profiles accept installed skills.
pi_skills_profile=()
[ "$HARNESS" = "pi" ] && pi_skills_profile=(--agent-profile pi-user)

echo "[cond] harness=$HARNESS model=$MODEL repeats=$REPEATS jobs=$JOBS gpus=$GPUS walltime=${WALLTIME_MIN}m (same for all conditions)"
echo "[cond] base run-id prefix: $BASE"

run_one () {   # $1=condition-token  $2=rep  $3..=condition-specific args
  local cond="$1" rep="$2"; shift 2
  local run_id="${BASE}_${cond}_rep${rep}"
  echo "[cond] >>> $cond rep$rep  (run-id=$run_id)"
  if [ -n "${DRY_RUN:-}" ]; then
    echo "      ${RUNNER[*]} run_benchmark_agent --agent-name $HARNESS --agent-model $MODEL --jobs $JOBS --gpus $GPUS --max-walltime-minutes-per-task $WALLTIME_MIN $* --run-id $run_id"
    return 0
  fi
  PYTHONPATH="$PWD" "${RUNNER[@]}" run_benchmark_agent \
    --output-dir benchmark_runs --run-id "$run_id" \
    --dataset-dir "$DATASET" --agent-name "$HARNESS" \
    --execution-mode lite --judge-mode deterministic \
    --max-walltime-minutes-per-task "$WALLTIME_MIN" \
    --jobs "$JOBS" --gpus "$GPUS" --agent-model "$MODEL" \
    "$@" \
    || echo "[cond] $cond rep$rep: runner exit=$? (summary.json still scored)"
}

for rep in $(seq 1 "$REPEATS"); do
  run_one "skillscli" "$rep" --agent-skills-dir skills --mdclaw-cli-policy allow "${pi_skills_profile[@]}"
  run_one "clionly"   "$rep" --mdclaw-cli-policy allow
  run_one "free"      "$rep" --mdclaw-cli-policy forbid-without-skill
done

[ -n "${DRY_RUN:-}" ] && exit 0

echo
echo "================ MEAN overall_score by condition ($REPEATS reps) ================"
python3 - "$BASE" "$REPEATS" <<'PY'
import json, statistics, sys
base, reps = sys.argv[1], int(sys.argv[2])
label = {"skillscli": "skills+cli", "clionly": "cli-only", "free": "free (SIF only)"}
for tok in ("skillscli", "clionly", "free"):
    scores = []
    for rep in range(1, reps + 1):
        try:
            d = json.load(open(f"benchmark_runs/{base}_{tok}_rep{rep}/summary.json"))
            scores.append(d.get("overall_score"))
        except Exception:
            scores.append(None)
    got = [s for s in scores if isinstance(s, (int, float))]
    mean = round(statistics.fmean(got), 4) if got else None
    sd = round(statistics.stdev(got), 4) if len(got) > 1 else 0.0
    print(f"{label[tok]:16s}: mean={mean}  stdev={sd}  n={len(got)}/{reps}  scores={scores}")
PY
echo "================================================================================"
echo "[cond] run dirs: benchmark_runs/${BASE}_{skillscli,clionly,free}_rep{1..$REPEATS}/"
