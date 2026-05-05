# Manual Test Checklist (Level 4)

Tests that verify the Claude Code integration works end-to-end.
Run these manually after all automated tests pass.

## Prerequisite

- [ ] `pip install -e .` in mdclaw conda env

## CLI Tools

- [ ] `mdclaw --list` shows all tools grouped by server
- [ ] `mdclaw --version` shows version
- [ ] `mdclaw fetch_structure --help` shows tool-specific help

## Claude Code Interactive Mode

- [ ] Start `claude` in mdclaw directory
- [ ] Run: `/md-prepare PDB 1AKE`
- [ ] Claude asks about chain selection (A, B detected)
- [ ] Claude asks about ligand inclusion (AP5 detected)
- [ ] After answering, Claude proceeds through remaining steps
- [ ] Job directory created with progress.json

## Claude Code Autonomous Mode

- [ ] Run: `/md-prepare PDB 1AKE, chain A, no ligands, run end-to-end with defaults`
- [ ] Claude proceeds without questions
- [ ] All 5 steps complete
- [ ] parm7 and rst7 files exist

## Nucleic Acid Workflows

- [ ] Run a standard RNA/DNA preparation with `include_types` containing `nucleic`
- [ ] Confirm `prepare_complex` records nucleic chains and writes `residue_mapping.json`
- [ ] Confirm `build_amber_system` metadata records `leaprc.RNA.OL3` and/or `leaprc.DNA.OL15`
- [ ] For a modified nucleotide input, confirm `inspect_molecules` reports chain/residue target fields under `modified_nucleic_residues`
- [ ] With `MDCLAW_MODXNA_DIR` set, run a branched `prepare_modified_nucleic` node and confirm `modified_nucleic.pdb`, `modxna_params.json`, and updated `residue_mapping.json`
- [ ] Confirm downstream topology auto-resolves the modified prep branch and Methods output includes modXNA/OL3/OL15 citations when applicable

## Claude Code Resume

- [ ] Note job_id from previous run
- [ ] Run: `/md-prepare resume job_XXXXXXXX`
- [ ] Claude reads progress.json and reports status
