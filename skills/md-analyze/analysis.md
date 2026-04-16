# Trajectory Analysis

## Prerequisites

Ensure these files exist (from md-production):
- `parm7` — Amber topology file
- `trajectory` — Trajectory file (DCD or similar)

Read `progress.json` in the job directory to find topology paths.
For jobs with `runs/` subdirectories, also read `runs/<run_id>/run.json`
to find the trajectory path (`stages.production.trajectory`).

---

## Available Analyses

```bash
# RMSD — backbone deviation from starting structure
mdclaw analyze_rmsd --trajectory-file <traj> --parm-file <parm7>

# RMSF — per-residue fluctuations
mdclaw analyze_rmsf --trajectory-file <traj> --parm-file <parm7>

# Hydrogen bonds
mdclaw analyze_hydrogen_bonds --trajectory-file <traj> --parm-file <parm7>

# Secondary structure
mdclaw analyze_secondary_structure --trajectory-file <traj> --parm-file <parm7>

# Contact analysis
mdclaw analyze_contacts --trajectory-file <traj> --parm-file <parm7>

# Distance between atoms/residues
mdclaw calculate_distance --trajectory-file <traj> --parm-file <parm7>

# Energy timeseries
mdclaw analyze_energy_timeseries --trajectory-file <traj> --parm-file <parm7>

# Native contact fraction (Q-value)
mdclaw compute_q_value --trajectory-file <traj> --parm-file <parm7>
```

---

## Interpretation Guide

### RMSD
- < 2 A: stable
- 2-4 A: moderate conformational change
- \> 4 A: large-scale rearrangement or instability

### RMSF
- Loops: typically 2-5 A
- Core: typically < 1 A

### Energy
- Potential energy should plateau after equilibration
- Kinetic energy reflects temperature control
- No sudden energy jumps = good sign

---

## Quality Indicators

### Good Simulation
- RMSD plateaus within a few ns
- Temperature fluctuates around target (300 +/- 5 K)
- Density stable around 1.0 g/cm3 (explicit water, NPT)

### Warning Signs
- RMSD continuously increasing → not equilibrated
- Large energy spikes → clashes or bad parameters
- Temperature drift → thermostat issues
- Box volume changing dramatically → barostat issues

---

## Reporting

Summarize results for the user:
1. System overview (atoms, box size, simulation time)
2. Stability metrics (RMSD, energy)
3. Key observations (flexible regions, ligand contacts)
4. Recommendations (extend simulation, adjust parameters, etc.)
