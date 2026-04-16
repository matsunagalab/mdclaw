# Implicit Solvent: Topology

Implicit solvent models represent water as a continuum dielectric instead of explicit water molecules. Faster but less accurate than explicit water.

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| GB model | GBn2 (igb=8) | "obc", "obc2", "hct" |
| Salt concentration | 0.15M | "0.3M", "no salt" |
| Force field | ff14SB | "ff19SB" (note: ff19SB is optimized for explicit water) |

**Note**: ff14SB is recommended for implicit solvent. ff19SB was parameterized with OPC explicit water and may be less accurate with GB models.

---

## Step 4: Skip Solvation

No solvation step is needed for implicit solvent. Proceed directly to topology.

---

## Step 5: Build Topology (no box, no water)

`ligand_params.json` is auto-detected from the merged PDB directory if ligands were prepared in Step 3.

```bash
mdclaw build_amber_system \
  --pdb-file <merged_pdb> \
  --output-dir <job_dir> \
  --forcefield ff14SB \
  --no-is-membrane
```

> No `--box-dimensions` or `--water-model` needed for implicit solvent.
> Ligand params (mol2/frcmod) are auto-loaded from `ligand_params.json` if present.

### Domain Knowledge

**Generalized Born models** (fastest to most accurate):
- **HCT** (igb=1): Fastest, least accurate
- **OBC1** (igb=2): Good balance
- **OBC2** (igb=5): Better than OBC1 for most proteins
- **GBn** (igb=7): Improved neck correction
- **GBn2** (igb=8): Best accuracy, recommended default

**When to use implicit solvent**:
- Rapid conformational sampling (folding studies)
- Large systems where explicit water is too expensive
- Screening many mutants or ligands quickly

**Limitations**:
- No explicit water-mediated interactions
- Less accurate for surface-exposed residues
- Membrane systems not supported
- Salt bridge stability may differ from explicit water

---

## Handoff

1. Set `progress.json.next_step`:
   ```json
   {
     "skill": "md-equilibration",
     "cli_hint": "/md-equilibration <job_dir>",
     "rationale": "topology built, ready for equilibration"
   }
   ```

2. **If `params.e2e_mode` is true**: read and follow `skills/md-equilibration/SKILL.md`.

3. **Otherwise**: present the next step to the user:
   ```
   Preparation complete. Next:
     /md-equilibration <job_dir>
   ```
