# T03 Ligand Pose Preparation

You are evaluating an MD agent on `T03_prep_ligand_pose_t4l_benzene`.

Use only these public files:

- `task.json`
- `input/181L.pdb`
- `input/ligand_reference.pdb`
- `input/prep_request.json`

Do not read `truth/` or `scorer/` if those directories exist.

Task: build an MD-ready prepared structure from PDB 181L while preserving the benzene pose in the T4 lysozyme L99A cavity. Clean and parameterize the system using a chemically reasonable workflow, then write the final prepared structure.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

Report the final benzene heavy-atom RMSD against `input/ligand_reference.pdb` in `metrics.preparation.ligand_heavy_atom_rmsd_angstrom`. The scorer will recompute this RMSD independently, so the metric and the submitted structure must agree. The evidence report should explain how the ligand pose was preserved and list relevant limitations.

