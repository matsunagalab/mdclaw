#!/usr/bin/env bash
# Step 6: MBAR on both umbrella grids, then the apo/holo comparison.
# Safe to run on a partial grid -- windows without a CV log are skipped and
# reported, so this doubles as a progress check.
set -uo pipefail
W=/data1/rkp00048/rku00161/mdclaw
J=$W/studies/tas1r2_tas1r3_crd_coupling
SIF=/data1/rkp00048/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-54798ff98538.sif
BINS=${BINS:-24}
EQ_PS=${EQ_PS:-3000}          # discard the first 3 ns of each window
SPLIT=${SPLIT:-15.4}          # CV2 (A) dividing inserted from withdrawn
run() { singularity exec --bind "$W:$W" --pwd "$W" "$SIF" python "$@"; }

mkdir -p "$J/analysis"
for state in apo holo; do
  n=$(python3 -c "
import json,os
m=json.load(open('$J/umbrella/${state}.manifest.json'))
print(sum(1 for w in m if os.path.exists(w['cv_csv'])))")
  echo "== $state: $n / 63 windows have CV logs =="
  [ "$n" -lt 10 ] && { echo "   too few windows; skipping MBAR"; continue; }
  run "$W/scripts/umbrella_mbar.py" \
      --manifest "$J/umbrella/${state}.manifest.json" \
      --temperature 300 --equilibration-ps "$EQ_PS" --bins "$BINS" --convergence \
      --out-prefix "$J/analysis/${state}_fes" \
      --label "$( [ "$state" = apo ] && echo 'apo (9UT9)' || echo 'sucralose-bound (9UTC)' )" \
      2>&1 | grep -v "JAX\|jaxlib\|^\*\|^$"
done

if [ -f "$J/analysis/apo_fes.fes.npz" ] && [ -f "$J/analysis/holo_fes.fes.npz" ]; then
  echo "== comparison =="
  run "$W/scripts/umbrella_compare.py" \
      --apo "$J/analysis/apo_fes.fes.npz" --holo "$J/analysis/holo_fes.fes.npz" \
      --split-cv2-A "$SPLIT" \
      --out-png "$J/analysis/pmf_comparison.png" \
      --out-json "$J/analysis/pmf_comparison.json" \
      2>&1 | grep -v "JAX\|jaxlib\|^\*\|^$"
fi
