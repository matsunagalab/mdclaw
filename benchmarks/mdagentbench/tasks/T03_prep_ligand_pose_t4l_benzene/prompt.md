# T03 Ligand Pose Preparation

You are evaluating an MD agent on `T03_prep_ligand_pose_t4l_benzene`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve PDB entry 181L and build an MD-ready prepared structure for T4 lysozyme L99A bound to benzene. Preserve the crystallographic benzene pose in the cavity while cleaning and parameterizing the system. Use a chemically reasonable workflow with ff14SB for the protein, GAFF2 or an equivalent small-molecule treatment for benzene, TIP3P explicit solvent context when needed, 0.15 M NaCl, and a roughly 12 Å solvent buffer if you build a solvated system. Minimize gently enough that the ligand pose is not displaced.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

Report the final benzene heavy-atom RMSD against the crystal pose from PDB 181L in `metrics.preparation.ligand_heavy_atom_rmsd_angstrom`. The scorer will recompute this RMSD independently from scorer-side reference coordinates, so the metric and the submitted structure must agree. The evidence report should explain how the ligand pose was preserved, list public sources retrieved, and state relevant limitations.

