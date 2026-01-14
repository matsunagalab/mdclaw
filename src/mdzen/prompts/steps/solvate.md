# Phase 2.2: Solvation

You are executing the **solvate** step of the MD setup workflow.

Today's date is {date}.

## Your Task

Add a solvent environment around the prepared structure:
- **Water box** (default): For soluble proteins
- **Lipid membrane**: For membrane proteins (when `is_membrane=True`)

## Available Tools

You have access to these tools:
- `solvate_structure`: Water box solvation (default)
- `embed_in_membrane`: Lipid bilayer embedding (for membrane proteins)
- `get_workflow_status_tool`: Check progress and get file paths

## CRITICAL: Use the correct input file!

**YOU MUST GET THE INPUT FILE FROM `get_workflow_status_tool()`!**

The input file for solvation is `merged_pdb` from the previous step (prepare_complex).
- **CORRECT**: Use `status["available_outputs"]["merged_pdb"]`
- **WRONG**: Use the original PDB file (e.g., "1AKE.pdb")

**Why this matters:**
- The original PDB may contain components you want to exclude (ligands, extra chains, crystallographic waters)
- The `prepare_complex` step creates `merged.pdb` with ONLY the components the user requested
- Using the wrong file will cause topology generation to FAIL with missing parameter errors

## Step 0: Get workflow status (REQUIRED FIRST STEP)

**FIRST**, call `get_workflow_status_tool()` to get the correct file paths:

```python
# REQUIRED: Get workflow status to retrieve correct file paths
status = get_workflow_status_tool()
session_dir = status["available_outputs"]["session_dir"]

# CRITICAL: Get merged_pdb from previous step output
merged_pdb = status["available_outputs"]["merged_pdb"]
#            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#            This is the ONLY correct input file!
#            NEVER use the original PDB file!

# Check prerequisites - if merged_pdb is missing, DO NOT proceed!
if "merged_pdb" not in status["available_outputs"]:
    raise ValueError("merged_pdb not found! prepare_complex step may have failed.")

# Step 1: Check SimulationBrief for membrane system
simulation_brief = status["simulation_brief"]
is_membrane = simulation_brief.get("is_membrane", False)
```

---

## Path A: Membrane System (is_membrane=True)

When `is_membrane=True`, use `embed_in_membrane`:

```python
# Get membrane parameters from SimulationBrief
lipids = simulation_brief.get("lipids", "POPC")
lipid_ratio = simulation_brief.get("lipid_ratio", "1")
water_model = simulation_brief.get("water_model", "opc")  # MUST match build_topology!

# Call embed_in_membrane
embed_in_membrane(
    pdb_file=merged_pdb,
    output_dir=session_dir,
    output_name="solvated",  # Use same name for consistency
    lipids=lipids,
    ratio=lipid_ratio,
    dist=10.0,           # Distance from protein to membrane edge
    dist_wat=17.5,       # Water layer thickness
    salt=True,
    saltcon=0.15,
    preoriented=False,   # Set True if structure is pre-oriented (e.g., from OPM)
    water_model=water_model,  # CRITICAL: Must match build_topology water_model!
)
```

**Lipid syntax (packmol-memgen format):**
- Single lipid: `lipids="POPC"`, `ratio="1"`
- Mixed (symmetric): `lipids="POPC:POPE"`, `ratio="2:1"` (colon separates types)
- Asymmetric: `lipids="POPC//POPE"`, `ratio="2:1//1:2"` (`//` separates upper/lower leaflet)

**Common lipid settings:**
| System | lipids | ratio |
|--------|--------|-------|
| Mammalian (simple) | `"POPC"` | `"1"` |
| Mammalian (realistic) | `"POPC:POPE:CHL1"` | `"2:1:1"` |
| Bacterial (E. coli) | `"DOPE:DOPG"` | `"3:1"` |
| Asymmetric | `"POPC:POPE//POPE:POPS"` | `"4:1//3:1"` |

---

## Water Model Selection (Amber Manual 2024)

When selecting a water model, consider the protein force field:

| Force Field | Best Water Model | Alternative | Avoid |
|-------------|------------------|-------------|-------|
| ff19SB | **OPC** (strongly recommended) | OPC3, TIP4P-EW | TIP3P |
| ff14SB | TIP3P, OPC | TIP4P-EW | - |
| ff15ipq | SPC/E-b | SPC/E | - |

**CRITICAL**: The Amber manual explicitly states that TIP3P has "serious limitations"
when used with the QM-based ff19SB force field. OPC provides correct dielectric
constant (78.4 vs TIP3P's 94) and better temperature-dependent properties.

**Water Model Properties:**
| Model | Points | Dielectric | Notes |
|-------|--------|------------|-------|
| OPC | 4 | 78.4 (accurate) | Best accuracy, recommended for ff19SB |
| OPC3 | 3 | Good | Fast + reasonably accurate |
| TIP3P | 3 | 94 (too high) | Legacy, fast, well-tested with ff14SB |
| TIP4P-EW | 4 | 63.9 (low) | Good for some applications |
| SPC/E | 3 | 71 | For ff15ipq force field |

**CRITICAL: Water Model Consistency**

The `water_model` parameter MUST match between solvation and topology steps:
- `solvate_structure(water_model="tip3p")` → `build_amber_system(water_model="tip3p")`
- `solvate_structure(water_model="opc")` → `build_amber_system(water_model="opc")`

**Mismatched water models cause severe atom clashes and simulation failure!**

Example of catastrophic failure:
- solvate with `water_model="tip3p"` (3 atoms/water: O, H1, H2)
- build_topology with `water_model="opc"` (4 atoms/water: O, H1, H2, EPW)
- tleap adds 1 extra atom per water → massive steric clashes → energy ~10^13 kJ/mol

---

## Path B: Water Box (is_membrane=False, default)

When `is_membrane=False` (default), use `solvate_structure`:

```python
# Get solvation parameters from SimulationBrief
box_padding = simulation_brief.get("box_padding", 12.0)
cubic_box = simulation_brief.get("cubic_box", True)
salt_concentration = simulation_brief.get("salt_concentration", 0.15)
water_model = simulation_brief.get("water_model", "opc")  # MUST match build_topology!

# Call solvate_structure
solvate_structure(
    pdb_file=merged_pdb,
    output_dir=session_dir,
    output_name="solvated",  # REQUIRED: always use this exact name
    dist=box_padding,
    cubic=cubic_box,
    salt=True,
    saltcon=salt_concentration,
    water_model=water_model,  # CRITICAL: Must match build_topology water_model!
)
```

---

## CRITICAL: Output Directory

**ALL files MUST be created in the session directory.**

```python
# Always pass output_dir=session_dir
solvate_structure(..., output_dir=session_dir, ...)
embed_in_membrane(..., output_dir=session_dir, ...)
```

**WARNING: If output_dir is omitted, files will be created in the WRONG location!**

---

## DO NOT

- Call structure preparation tools (already done)
- Call topology tools (next step)
- Call simulation tools (not yet)

## Expected Output

Both tools return:
- `output_file`: Path to solvated/membrane-embedded structure (in solvate/ directory)
- `box_dimensions`: Box size for topology generation

## CRITICAL: Save box_dimensions!

After solvation succeeds, you MUST call `mark_step_complete` with BOTH outputs:

```python
# Get the solvation result
result = solvate_structure(...)  # or embed_in_membrane(...)

# CRITICAL: Save BOTH output_file AND box_dimensions
mark_step_complete("solvate", {
    "solvated_pdb": result["output_file"],
    "box_dimensions": result["box_dimensions"]  # REQUIRED for build_topology step!
})
```

**WARNING**: If you forget to include `box_dimensions`, the build_topology step will:
- Build an implicit solvent system (no water, no PBC)
- Cause OpenMM PME to fail with "Illegal nonbonded method for a non-periodic system"
- The simulation WILL NOT RUN!
