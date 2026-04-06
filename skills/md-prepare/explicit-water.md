# Explicit Water: Solvation, Topology & Quick MD

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 A | "buffer 20", "20A" |
| Salt concentration | 0.15M NaCl | "0.3M", "no salt" |
| Force field | ff19SB | "ff14SB" |
| Temperature | 300 K | "310K" |
| Timestep | 4 fs (HMR enabled by default) | "2 fs", "--no-hmr" |
| Simulation time | 0.1 ns (quick) | "1 ns", "10 ns" |

**Force field + water model pairing**: ff19SB + OPC (recommended), ff14SB + TIP3P.

---

## Step 4: Solvation

```bash
mdclaw solvate_structure \
  --pdb-file <merged_pdb> \
  --output-dir <job_dir> \
  --water-model opc \
  --dist 15.0 \
  --salt \
  --saltcon 0.15
```

### box_dimensions

`solvate_structure` automatically saves `box_dimensions.json` next to the solvated PDB.
`build_amber_system` automatically loads it when `--box-dimensions` is omitted.
**No manual passing is needed.**

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Topology & Quick MD

### Build Topology

`box_dimensions.json` is auto-detected from the solvated PDB directory. No need to pass `--box-dimensions`:

```bash
mdclaw build_amber_system \
  --pdb-file <solvated_pdb> \
  --output-dir <job_dir> \
  --forcefield ff19SB \
  --water-model opc \
  --no-is-membrane
```

> `build_amber_system` looks for `box_dimensions.json` in the same directory as the input PDB. If not found, it builds an implicit solvent system (no PBC).

### Equilibration + Quick MD (sanity check)

Run equilibration then a short production run starting from the equilibrated
checkpoint. When `--pressure-bar` > 0, equilibration runs NVT heating
followed by NPT density equilibration (both with CA positional restraints).
When pressure is 0 or omitted, only NVT heating runs. Both stages use
4 fs + HMR so the final state can be handed off to production via a binary
checkpoint.

```bash
# Equilibration: NVT (10ps) + NPT (20ps) at 4 fs + HMR, CA restraints.
# --pressure-bar 1.0 triggers the NPT density stage (NPT ensemble target).
# Writes equilibrated.chk from a production-matching System (no restraints,
# currentStep=0) and equilibration.xml as an audit/reproducibility backup.
mdclaw run_equilibration \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <job_dir> \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0

# Quick production (0.1 ns, 4 fs + HMR, no restraints).
# --restart-from <equilibrated.chk> loads equilibrated positions, velocities,
# and NPT-adjusted box; currentStep in the checkpoint is 0 so the full
# simulation_time_ns runs. Minimization and velocity re-randomization are
# skipped. Use the checkpoint_file path from run_equilibration's JSON output.
mdclaw run_production \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <job_dir> \
  --simulation-time-ns 0.1 \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0 \
  --output-frequency-ps 10.0 \
  --restart-from <equilibrated_chk>
```

### Domain Knowledge
- Equilibration uses positional restraints on CA atoms to prevent structural collapse
- Both NVT and NPT stages run at 4 fs with HMR, matching production's integrator.
  The final state is handed off to production via a binary checkpoint (no
  re-minimization, equilibrated velocities and NPT-adjusted box preserved).
- `run_equilibration` writes `equilibrated.chk` from a clean,
  production-matching System (no restraint force). Pass it to
  `run_production --restart-from` to inherit the equilibrated state. The
  checkpoint's `currentStep` is 0 by construction, so `--simulation-time-ns`
  is interpreted as the full production length.
- `equilibration.xml` is also written as an audit/reproducibility backup
  (OpenMM XML State). It is not used for restart — use the `.chk` instead.
- Production uses 4 fs + HMR (default) without restraints
- NPT ensemble at 300K, 1 bar for equilibration
- Energy should drop significantly during minimization (good sign)

### Protonation Notes
- pH 7.4 is physiological default
- pdb2pqr + propka assigns pH-dependent HIS states (HID/HIE/HIP)
- Fallback to pdb4amber + reduce (geometry-based) if pdb2pqr unavailable

---

## Update progress.json Metadata

After completing all steps, update progress.json with metadata collected from
tool outputs. The `commands` array is already populated by the CLI automatically.
Fill in these sections using information from the tool outputs during this workflow:

- **system**: (from prepare_complex + solvate_structure output)
  - `pdb_id`, `chains`, `num_residues`, `num_atoms_protein`
  - `num_atoms_total`, `num_waters`, `ions` (e.g., `{"Na+": 42, "Cl-": 36}`)
  - `ligands` (list of ligand names if any)

- **preparation**: copy `preparation_summary` from prepare_complex output directly.
  It contains: `protonation_method`, `protonation_ph`, `histidine_states`,
  `disulfide_bonds_applied`, `missing_residues_modeled`, `missing_residues_count`,
  `nonstandard_residues_replaced` — all at the top level, ready to use.

- **solvation**: (from solvate_structure output)
  - `type`: "explicit"
  - `water_model`, `box_shape`, `box_size_angstrom` (from `box_dimensions`)
  - `buffer_distance_angstrom`, `salt_type`, `salt_concentration_M`

- **forcefield**: (from build_amber_system parameters)
  - `protein`, `water`, `lipid`, `ligand_method`

- **artifacts**: file paths for each output file (from each tool's output)
  - `structure_file`, `merged_pdb`, `solvated_pdb`, `parm7`, `rst7`, etc.
