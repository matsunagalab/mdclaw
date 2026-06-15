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
export MDCLAW_CHARGE_FIT_TIMEOUT=1800
export MDCLAW_MD_SIMULATION_TIMEOUT=3600
export MDCLAW_VISUALIZATION_TIMEOUT=300
export MDCLAW_LOG_LEVEL=WARNING
export MDCLAW_CACHE_DIR="$HOME/.cache/mdclaw"
export MDCLAW_SLURM_TIMEOUT=120
export MDCLAW_MODULE_LOADS="cuda/12.0 amber/24"
export MDCLAW_MODULE_INIT="/etc/profile.d/modules.sh"
export MDCLAW_SURROGATE_DIR="$HOME/.cache/mdclaw/surrogates"
```

Notes:

- `MDCLAW_AMBER_TIMEOUT` controls the `build_amber_system` wall-time budget for
  the openmmforcefields `SystemGenerator` build + initial `LocalEnergyMinimizer`
  pass (no tleap is invoked); raise it for very large fusions and glycoproteins.
- `MDCLAW_CHARGE_FIT_TIMEOUT` bounds the in-process ligand charge-fitting step
  (antechamber/sqm AM1-BCC, triggered lazily inside `SystemGenerator`
  `create_system` when `GAFFTemplateGenerator` parameterizes a small molecule).
  A large, highly charged ligand such as AP5 can keep sqm busy for many
  minutes. The value has a hard **floor of 1800 s**: it may be *raised* for an
  exceptionally large ligand but is silently *clamped up* to the floor if set
  lower. This is deliberate — an agent driving the CLI cannot shorten the
  charge-fitting budget and induce a spurious `openmmforcefields_build_timeout`.
  There is no CLI/function argument for it, only this floored env override. On
  expiry the build fails with `code: openmmforcefields_build_timeout`; the
  documented recovery is to re-run the same build node (sqm timing varies) or
  raise this variable — never to hand-roll a custom build script.
- `MDCLAW_MODXNA_DIR` is a legacy/experimental modXNA hook only. Modified
  DNA/RNA is not supported by the standard MD-ready topology path.
- `MDCLAW_MODULE_LOADS` and `MDCLAW_MODULE_INIT` are used for HPC module setup.
- `MDCLAW_SURROGATE_DIR` controls where isolated surrogate backend venvs are
  stored. BioEmu is never installed into the conda `mdclaw` environment.

## Surrogate Backend Runtime

MD surrogate backends run outside the main conda `mdclaw` environment. The first
backend is BioEmu:

```bash
mdclaw setup_surrogate_backend --model bioemu --device cuda
mdclaw check_surrogate_backend --model bioemu
```

Local installs use an isolated venv under
`$MDCLAW_SURROGATE_DIR/bioemu/venv` (default:
`~/.cache/mdclaw/surrogates/bioemu/venv`). Container images include the same
kind of isolated venv inside the image. This keeps BioEmu's JAX/Torch stack out
of the main Amber/OpenMM runtime.

Candidate generation uses the backend venv through subprocess:

```bash
mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 100 \
  --max-candidates 20 \
  --job-dir <job_dir> \
  --node-id source_001
```

The tool writes a `source_bundle.json` with `source_type="surrogate"` and
`origin.kind="bioemu"`. BioEmu currently supports monomer sequences only; use
Boltz-2 for multimers, ligands, and PTMs.

## Generic Harness Runtime

Non-plugin harnesses do not need a special integration layer. Make `skills/`
readable, put `mdclaw` on `PATH`, and provide one runtime:

- conda: create `environment.yml` and install the package in that environment.
- SIF: set `MDCLAW_SIF=/path/to/mdclaw.sif`.
- Docker: set `MDCLAW_DOCKER_IMAGE` if using a non-default image.

`bin/mdclaw` auto-selects conda first, then SIF with Singularity/Apptainer, then
Docker, and finally a local `mdclaw` command if one is already on `PATH`.
The SIF and Docker options are packaged MD runtimes for the CLI; they do not
replace or duplicate the agent-facing skill files.

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
