# Explicit Water: Solvation & Topology

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 A | "buffer 20", "20A" |
| Salt concentration | 0.15M NaCl | "0.3M", "no salt" |
| Force field | ff19SB | "ff14SB" |

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

## Step 5: Build Topology

`box_dimensions.json` (in `solvate/`) and `ligand_params.json` (in the job root) are auto-detected. No need to pass them explicitly:

**Without ligands:**
```bash
mdclaw build_amber_system \
  --pdb-file <solvated_pdb> \
  --output-dir <job_dir> \
  --forcefield ff19SB \
  --water-model opc \
  --no-is-membrane
```

**With ligands** (auto-detected from `job_dir/ligand_params.json` written by `prepare_complex`):
```bash
mdclaw build_amber_system \
  --pdb-file <solvated_pdb> \
  --output-dir <job_dir> \
  --forcefield ff19SB \
  --water-model opc \
  --no-is-membrane
```

If auto-detection fails (e.g., files moved), pass ligand params explicitly via `--json-input`:
```bash
mdclaw build_amber_system --json-input '{"pdb_file": "<solvated_pdb>", "output_dir": "<job_dir>", "forcefield": "ff19SB", "water_model": "opc", "ligand_params": [{"mol2": "<mol2_path>", "frcmod": "<frcmod_path>", "residue_name": "LIG"}]}'
```

Extract `mol2_file`, `frcmod_file`, and `ligand_id` from each entry in `prepare_complex` result's `ligands` array.

> `build_amber_system` auto-detects `box_dimensions.json` and `ligand_params.json` by searching the input PDB's directory and its parent (the job root). If not found, box dimensions default to implicit solvent and ligand params default to none.

**Verify**: Check the tleap log for `loadamberparams` and `loadmol2` lines for each ligand.

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
  - `structure_file`, `merged_pdb`, `solvated_pdb`, `parm7`, `rst7`
  - `ligand_params`: array of `{mol2, frcmod, residue_name}` dicts (from prepare_complex ligands output)

## Handoff

1. Set `progress.json.next_step`:
   ```json
   {
     "skill": "md-equilibration",
     "cli_hint": "/md-equilibration <job_dir>",
     "rationale": "topology built, ready for equilibration"
   }
   ```

2. **If `params.e2e_mode` is true** (user said "end-to-end", "then run X ns",
   "全部やって", etc.): read and follow `skills/md-equilibration/SKILL.md`,
   passing the job directory and any production parameters from the original request.

3. **Otherwise**: present the next step to the user:
   ```
   Preparation complete. Next:
     /md-equilibration <job_dir>
   ```
