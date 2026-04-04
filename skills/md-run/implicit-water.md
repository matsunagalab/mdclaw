# Production MD: Implicit Solvent

## Equilibration Protocol

### Stage 1: Energy Minimization
Already handled by `run_md_simulation` internally (1000 steps steepest descent).

### Stage 2: NVT Heating
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --simulation-time-ns 0.1 \
  --temperature-kelvin 300.0 \
  --pressure-bar 0 \
  --timestep-fs 1.0 \
  --output-frequency-ps 10.0
```

### Stage 3: NVT Production
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7_from_prev> \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin 300.0 \
  --pressure-bar 0 \
  --timestep-fs 2.0 \
  --output-frequency-ps 10.0
```

> **No barostat** (`--pressure-bar 0`): implicit solvent has no periodic box, so NPT is not applicable. All production runs use NVT.

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Already done in md-prepare |
| Conformational sampling | 10-100 ns | Faster than explicit, good for screening |
| Folding study | 100 ns - 1 us | GB allows longer effective sampling |
| Mutant screening | 10 ns x N | Quick comparative runs |

---

## HPC / GPU Usage

### GPU Selection
```bash
mdclaw run_md_simulation --platform CUDA --device-index "0" \
  --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --pressure-bar 0
```

### HMR — ~2x Throughput
```bash
mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --hmr --timestep-fs 4.0 --simulation-time-ns 100.0 --pressure-bar 0
```

### Checkpoint / Restart
Same as explicit water. Use `--restart-from /path/to/checkpoint.chk`.

---

## Implicit Solvent Considerations

- **No density or box volume to monitor** — focus on RMSD and energy convergence
- **Faster per-step** than explicit water (~5-10x) due to fewer atoms
- **Salt bridges may be overstabilized** — compare with explicit water for key interactions
- **GB model choice matters**: GBn2 (igb=8) is most accurate, set during md-prepare

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry | Reduce timestep to 1 fs |
| Unrealistic compaction | GB artifacts | Consider explicit water for this system |
| Salt bridges too stable | GB dielectric overestimation | Validate with explicit water run |
| Slow performance | GPU not detected | Check `--platform CUDA` and `nvidia-smi` |
