#!/usr/bin/env bash
# Parallel MDPrepBench runner: shards the 25 tasks across N processes.
# The runner itself is sequential, so we fan out run_benchmark_agent instead.
#
# Usage:
#   ./run-bench-parallel.sh [MODEL] [NSHARDS] [TIMEOUT_MIN] [RUN_PREFIX] [REPEATS]
# Defaults: claude-haiku-4-5  5  60  haiku  1
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${1:-claude-haiku-4-5}"
NSHARDS="${2:-5}"
TIMEOUT="${3:-60}"
PREFIX="${4:-haiku}"
REPEATS="${5:-1}"

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

echo "Model=$MODEL  shards=$NSHARDS  timeout=${TIMEOUT}m  repeats=$REPEATS  GPUs=$NGPU"
pids=()
for rep in $(seq 1 "$REPEATS"); do
  for s in $(seq 0 $((NSHARDS-1))); do
    gpu=$(( s % NGPU ))
    rid="${PREFIX}_s${s}"
    [ "$REPEATS" -gt 1 ] && rid="${rid}_rep${rep}"
    echo "[shard $s rep $rep] GPU $gpu  run-id $rid  tasks:${SHARD[$s]}"
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
done

echo "Launched ${#pids[@]} shard-runs; waiting..."
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done
echo "All shard-runs done (fail=$fail). Per-shard summaries:"
for rep in $(seq 1 "$REPEATS"); do
  for s in $(seq 0 $((NSHARDS-1))); do
    rid="${PREFIX}_s${s}"
    [ "$REPEATS" -gt 1 ] && rid="${rid}_rep${rep}"
    echo "  benchmark_runs/${rid}/summary.json"
  done
done
