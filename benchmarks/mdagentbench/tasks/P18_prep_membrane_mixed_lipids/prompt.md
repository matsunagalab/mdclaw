# P18_prep_membrane_mixed_lipids: Membrane embedding and lipid composition

You are evaluating an MD agent on `P18_prep_membrane_mixed_lipids`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Membrane embedding and lipid composition: prepare TMEM14A in a mixed POPC:POPE:CHL1 membrane at a 2:1:1 species ratio.

Public source anchors: PDB 2LOP.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
