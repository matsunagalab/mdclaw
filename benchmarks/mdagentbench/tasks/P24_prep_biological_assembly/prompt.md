# P24_prep_biological_assembly: Assembly/biological unit choice

You are evaluating an MD agent on `P24_prep_biological_assembly`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Assembly/biological unit choice. Use PDB `1STP` and generate or select
biological assembly `assembly_id=1` before preparing the structure for MD. Do
not submit the asymmetric unit alone. Preserve source auth/label/operator
provenance and stable chain identity.

Primary public source: PDB 1STP, biological assembly 1.

Stress/reference source for chain-identity policy: PDB 2MS2, biological assembly
1. You do not need to submit a prepared 2MS2 structure, but your provenance
should make clear how your workflow would preserve chain identity when assembly
generation creates many chains.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

Record the selected `assembly_id`, source `auth_asym_id`, source
`label_asym_id` or subchain identifier, operator id, output chain name, naming
policy, and chain identity map in `metrics.json`, `provenance.json`, or
`evidence_report.json`.

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
