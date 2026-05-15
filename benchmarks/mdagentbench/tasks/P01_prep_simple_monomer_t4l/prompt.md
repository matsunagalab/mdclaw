# P01_prep_simple_monomer_t4l: Simple monomer preparation

You are evaluating an MD agent on `P01_prep_simple_monomer_t4l`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Simple monomer preparation: retrieve T4 lysozyme chain A, clean it, prepare an explicit-water-compatible topology, and report that no unintended ligands were retained.

Public source anchors: PDB 2LZM.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
