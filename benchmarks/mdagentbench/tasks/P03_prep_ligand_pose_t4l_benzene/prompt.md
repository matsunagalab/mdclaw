# P03_prep_ligand_pose_t4l_benzene: Ligand pose preservation

You are evaluating an MD agent on `P03_prep_ligand_pose_t4l_benzene`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Ligand pose preservation: prepare T4 lysozyme L99A bound to benzene while preserving the crystallographic benzene pose.

Public source anchors: PDB 181L.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
