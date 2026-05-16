# P17_prep_dna_duplex_neutralization: DNA duplex chain retention and neutralization

You are evaluating an MD agent on `P17_prep_dna_duplex_neutralization`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: DNA duplex chain retention and neutralization: prepare both chains of the standard B-DNA duplex from PDB `1BNA`, select a DNA-compatible force-field library, and record counterion neutralization rather than treating the duplex as a single protein-like chain.

Public source anchors: PDB 1BNA.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
