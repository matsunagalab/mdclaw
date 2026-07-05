#!/usr/bin/env bash
# Easy MDPrepBench: run the whole suite N times with one agent, print the mean
# score. Thin wrapper around the existing benchmarks/tools/run_mdprepbench_all_agents.py.
#
# MDClaw auto-selects its runtime in conda -> local SIF -> docker order; with no
# `mdclaw` conda env it uses the repo-root ./mdclaw.sif automatically. We only
# point MDCLAW_SIF at the local SIF so resolution is deterministic regardless of
# cwd; conda still wins if a conda env exists. Nothing is hardcoded to singularity.
#
#   ./run_mdprepbench_bench.sh              # pi + deepseek (local LLM), 3x, sequential
#   ./run_mdprepbench_bench.sh claude-code  # cloud haiku,         3x, 5 concurrent
#   ./run_mdprepbench_bench.sh codex        # cloud gpt-5.4-mini,  3x, 5 concurrent
#
# Optional env: REPEATS(=3)  JOBS  MODEL  WALLTIME_MIN(=30)  DRY_RUN=1
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"          # repo root (holds ./mdclaw.sif)

AGENT="${1:-pi}"
case "$AGENT" in
  pi)          : "${MODEL:=spark1-vllm/deepseek-v4-flash}"; : "${JOBS:=1}"; : "${GPUS:=0}";;  # local LLM: 1 session at a time
  claude-code) : "${MODEL:=haiku}";                         : "${JOBS:=5}"; : "${GPUS:=5}";;  # cloud: 5 concurrent
  codex)       : "${MODEL:=gpt-5.4-mini}";                  : "${JOBS:=5}"; : "${GPUS:=5}";;
  *) echo "usage: $0 [pi|claude-code|codex]" >&2; exit 2;;
esac
REPEATS="${REPEATS:-3}"
WALLTIME_MIN="${WALLTIME_MIN:-30}"
PREFIX="$(date +%Y%m%d_%H%M%S)_mdprepbench_${AGENT//[^a-zA-Z0-9]/_}"
SUMMARY="benchmark_runs/${PREFIX}_all_agents_operator_summary.json"

# Tell MDClaw where the local SIF is (conda->sif->docker order is preserved).
export MDCLAW_SIF="$PWD/mdclaw.sif"

echo "[bench] agent=$AGENT model=$MODEL repeats=$REPEATS jobs=$JOBS gpus=$GPUS walltime=${WALLTIME_MIN}m runtime=auto(conda->sif->docker)"
echo "[bench] summary -> $SUMMARY"

# The orchestrator stays on the host venv (pydantic) so it can spawn the agent
# CLI (pi/claude/codex are host binaries, not inside the SIF). The agent's own
# mdclaw + openmm work runs in the SIF via MDClaw's auto runtime resolution.
PYTHONPATH="$PWD" ~/.venvs/mdclaw-runner/bin/python benchmarks/tools/run_mdprepbench_all_agents.py \
  --output-dir benchmark_runs \
  --run-id-prefix "$PREFIX" \
  --agents "$AGENT" \
  --repeats "$REPEATS" \
  --jobs "$JOBS" \
  --gpus "$GPUS" \
  --max-walltime-minutes-per-task "$WALLTIME_MIN" \
  --mdclaw-cmd "$HOME/.venvs/mdclaw-runner/bin/python -m mdclaw._cli" \
  --agent-model "$AGENT=$MODEL" \
  ${DRY_RUN:+--dry-run}

[ -n "${DRY_RUN:-}" ] && exit 0

echo
echo "===== MEAN overall_score over $REPEATS repeats ====="
python3 - "$SUMMARY" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
agg = d.get("aggregates") or {}
if not agg:
    print("(no numeric overall_score; all repeats failed?)")
for a, v in agg.items():
    print(f"{a}: mean={v.get('mean')}  stdev={v.get('stdev')}  n={v.get('n')}  scores={v.get('scores')}")
print("--- per repeat ---")
for r in d.get("runs", []):
    sc = (r.get("runner_payload") or {}).get("score")
    ov = sc.get("overall_score") if isinstance(sc, dict) else None
    print(f"  {r['run_id']}: overall_score={ov}  success={r.get('success')}  exit={r.get('exit_code')}")
PY
echo "summary JSON: $SUMMARY"
