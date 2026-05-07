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
- [ ] Modern topo artifacts exist: `system.system.xml`, `system.topology.pdb`,
      and `system.state.xml` under the topo node's `artifacts/` directory
- [ ] Topo `node.json.metadata` carries `system_artifact_kind="openmm_system_xml"`
      and a populated `forcefield_provenance` dict (`openmm_xml` list,
      `method.hmr=true`, sha256 table, OpenMM / openmmforcefields versions)

## Nucleic Acid Workflows

- [ ] Run a standard RNA/DNA preparation with `include_types` containing `nucleic`
- [ ] Confirm `prepare_complex` records nucleic chains and writes `residue_mapping.json`
- [ ] Confirm `build_amber_system` records `metadata.forcefield_provenance.openmm_xml`
      containing `amber/RNA.OL3.xml` and/or `amber/DNA.OL15.xml`
      (`metadata.nucleic_libraries` keeps the legacy leaprc names for
      evidence-report continuity)
- [ ] For a modified nucleotide input, confirm `inspect_molecules` reports chain/residue target fields under `modified_nucleic_residues`
- [ ] With `MDCLAW_MODXNA_DIR` set, run a branched `prepare_modified_nucleic` node and confirm `modified_nucleic.pdb`, `modxna_params.json`, and updated `residue_mapping.json`
- [ ] Confirm downstream topology auto-resolves the modified prep branch and Methods output includes modXNA/OL3/OL15 citations when applicable

## Claude Code Resume

- [ ] Note job_id from previous run
- [ ] Run: `/md-prepare resume job_XXXXXXXX`
- [ ] Claude reads progress.json and reports status
