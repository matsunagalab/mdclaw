# P03_prep_ligand_pose_t4l_benzene: MD system preparation

You are evaluating an MD agent on `P03_prep_ligand_pose_t4l_benzene`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Ligand pose preservation: Prepare the T4 lysozyme L99A-benzene complex from PDB 181L. Keep protein chain A and the deposited benzene ligand (BNZ) together, and preserve the crystallographic benzene pose. Do not submit a ligand-only structure. Some tools may list BNZ separately from the protein during inspection, so make sure it is still included.

Public source anchors: PDB 181L.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
