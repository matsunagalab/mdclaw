# Configuration

## Environment Setup

```bash
conda env create -f environment.yml
conda activate mdclaw
```

`environment.yml` is the authoritative local development environment. It
includes conda-only or conda-preferred scientific/runtime dependencies such as
AmberTools, OpenMM, RDKit, PDBFixer, and `pymol-open-source`. The PyMOL package
is required only for `render_structure_preview`; when it is missing, that tool
returns `code=pymol_not_available` without blocking MD workflow tools.

## Common Environment Variables

```bash
export MDCLAW_OUTPUT_DIR="."
export MDCLAW_DEFAULT_TIMEOUT=300
export MDCLAW_SOLVATION_TIMEOUT=7200
export MDCLAW_MEMBRANE_TIMEOUT=7200
export MDCLAW_AMBER_TIMEOUT=3600
export MDCLAW_MD_SIMULATION_TIMEOUT=3600
export MDCLAW_VISUALIZATION_TIMEOUT=300
export MDCLAW_LOG_LEVEL=WARNING
export MDCLAW_SLURM_TIMEOUT=120
export MDCLAW_GEOSTD_DIR="/path/to/amber_geostd"
export MDCLAW_MODXNA_DIR="/path/to/modXNA"
export MDCLAW_MODULE_LOADS="cuda/12.0 amber/24"
export MDCLAW_MODULE_INIT="/etc/profile.d/modules.sh"
```

Notes:

- `MDCLAW_AMBER_TIMEOUT` controls the `build_amber_system` wall-time budget for
  the openmmforcefields `SystemGenerator` build + initial `LocalEnergyMinimizer`
  pass (no tleap is invoked); raise it for very large fusions and glycoproteins.
- `MDCLAW_GEOSTD_DIR` points to the curated ligand parameter database.
- `MDCLAW_MODXNA_DIR` must contain `modxna.sh` and `dat/frcmod.modxna`.
- `MDCLAW_MODULE_LOADS` and `MDCLAW_MODULE_INIT` are used for HPC module setup.

## Generic Harness Runtime

Non-plugin harnesses do not need a special integration layer. Make `skills/`
readable, put `mdclaw` on `PATH`, and provide one runtime:

- conda: create `environment.yml` and install the package in that environment.
- SIF: set `MDCLAW_SIF=/path/to/mdclaw.sif`.
- Docker: set `MDCLAW_DOCKER_IMAGE` if using a non-default image.

`bin/mdclaw` auto-selects conda first, then SIF with Singularity/Apptainer, then
Docker, and finally a local `mdclaw` command if one is already on `PATH`.

## CLI Basics

```bash
mdclaw --list
mdclaw --version
mdclaw fetch_structure --help
mdclaw fetch_structure --source pdb --pdb-id 1AKE
mdclaw inspect_molecules --structure-file 1AKE.cif
mdclaw solvate_structure --pdb-file merged.pdb --dist 15.0 --salt --saltcon 0.15
mdclaw prepare_complex --json-input '{"structure_file": "1AKE.pdb", "select_chains": ["A"]}'
```

When preserving ligands while narrowing chains, include the ligand's chain as
reported by `inspect_molecules`; `select_chains=["A"]` alone is protein-chain
selection and can drop hetero ligands on separate subchains.

Skills reference these tools through the same CLI.
