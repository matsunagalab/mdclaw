#!/usr/bin/env bash
# Parallel MDPrepBench runner: shards the 25 tasks across N processes.
# The runner itself is sequential, so we fan out run_benchmark_agent instead.
#
# Usage:
#   ./run-bench-parallel.sh [MODEL] [NSHARDS] [TIMEOUT_MIN] [RUN_PREFIX]
# Defaults: claude-haiku-4-5  5  60  haiku
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${1:-claude-haiku-4-5}"
NSHARDS="${2:-5}"
TIMEOUT="${3:-60}"
PREFIX="${4:-haiku}"

export PATH="$PWD/bin:$PATH"
export PYTHONPATH="$PWD"
MD="$HOME/.venvs/mdclaw-runner/bin/python -m mdclaw._cli"
NGPU=$(nvidia-smi -L 2>/dev/null | wc -l); [ "$NGPU" -lt 1 ] && NGPU=1

# All task IDs, round-robin into NSHARDS buckets so heavy tasks spread out.
mapfile -t TASKS < <(ls benchmarks/mdprepbench/tasks/ | sort)
declare -a SHARD
for i in "${!TASKS[@]}"; do
  s=$(( i % NSHARDS ))
  SHARD[$s]="${SHARD[$s]:-} ${TASKS[$i]}"
done

echo "Model=$MODEL  shards=$NSHARDS  timeout=${TIMEOUT}m  GPUs=$NGPU"
pids=()
for s in $(seq 0 $((NSHARDS-1))); do
  gpu=$(( s % NGPU ))
  rid="${PREFIX}_s${s}"
  echo "[shard $s] GPU $gpu  run-id $rid  tasks:${SHARD[$s]}"
  CUDA_VISIBLE_DEVICES=$gpu $MD run_benchmark_agent \
    --output-dir benchmark_runs --run-id "$rid" \
    --dataset-dir benchmarks/mdprepbench --agent-name pi \
    --execution-mode lite --judge-mode deterministic \
    --max-walltime-minutes-per-task "$TIMEOUT" \
    --mdclaw-cli-policy forbid-without-skill \
    --agent-model "pi=$MODEL" \
    --task-ids ${SHARD[$s]} \
    > "benchmark_runs/${rid}.log" 2>&1 &
  pids+=($!)
done

echo "Launched ${#pids[@]} shards; waiting..."
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done
echo "All shards done (fail=$fail). Per-shard summaries:"
for s in $(seq 0 $((NSHARDS-1))); do
  echo "  benchmark_runs/${PREFIX}_s${s}/summary.json"
done
