# P02_prep_1ake_chain_ap5: Chain and ligand selection

You are evaluating an MD agent on `P02_prep_1ake_chain_ap5`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Chain and ligand selection: prepare adenylate kinase chain A while retaining the AP5 ligand, even if the ligand is represented under a separate mmCIF label chain.

Public source anchors: PDB 1AKE.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
