# P10_prep_bpti_disulfides: Disulfide auto/override

You are evaluating an MD agent on `P10_prep_bpti_disulfides`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Disulfide auto/override: detect the canonical BPTI disulfides, or respect an explicit user override for named pairs.

Public source anchors: PDB 5PTI.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
