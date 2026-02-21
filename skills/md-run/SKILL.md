---
name: MD Run
description: "Production molecular dynamics simulation execution using MDClaw CLI tools and OpenMM. Handles extended MD runs beyond the initial sanity check, including equilibration protocols, replica runs, and simulation monitoring. Use when the user wants to run, execute, or continue an MD simulation after preparation is complete."
---

# MD Run Skill

You are a computational biophysics expert running molecular dynamics simulations using the MDClaw CLI tools. This skill handles production MD runs beyond the initial sanity check.

Respond in the user's language. Use English for tool parameter values.

All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

---

## Prerequisites

Before running production MD, ensure these files exist (from md-prepare):
- `parm7` - Amber topology file
- `rst7` - Amber coordinate/restart file

Read `progress.json` in the job directory to find file paths.

---

## Equilibration Protocol

For production runs, use a staged equilibration before the production phase:

### Stage 1: Energy Minimization
Already handled by `run_md_simulation` internally (1000 steps steepest descent).

### Stage 2: NVT Heating (optional, for longer runs)
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

### Stage 3: NPT Production
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7_from_prev> \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0 \
  --timestep-fs 2.0 \
  --output-frequency-ps 10.0
```

---

## Tools

- `mdclaw run_md_simulation --prmtop-file <parm7> --inpcrd-file <rst7> --simulation-time-ns <ns> --temperature-kelvin <K> --pressure-bar <bar> --timestep-fs <fs> --output-frequency-ps <ps>`

---

## Membrane Systems

For membrane-embedded systems:
- Use semi-isotropic pressure coupling (handled automatically by OpenMM)
- Keep `is_membrane=True` context from preparation
- Consider longer equilibration (0.5-1 ns NVT + 1 ns NPT)
- Monitor membrane area and lipid order parameters

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Already done in md-prepare |
| Short equilibration | 1-10 ns | Good for initial testing |
| Production | 50-500 ns | Standard for conformational sampling |
| Extended | 1+ us | For slow processes (folding, binding) |

---

## Troubleshooting

- **SHAKE constraint failure**: Reduce timestep to 1 fs, or check for bad geometry in input structure
- **Simulation blows up (NaN energies)**: Usually means clashes in input; go back to md-prepare and re-minimize
- **Slow performance**: Check that GPU is available; OpenMM automatically uses CUDA if present
- **Out of memory**: Reduce system size (smaller buffer) or use implicit solvent
