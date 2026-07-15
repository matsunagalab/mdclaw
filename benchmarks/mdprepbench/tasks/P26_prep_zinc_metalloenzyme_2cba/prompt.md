# P26_prep_zinc_metalloenzyme_2cba: MD system preparation

You are evaluating an MD agent on `P26_prep_zinc_metalloenzyme_2cba`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Zinc metalloenzyme preparation: prepare human carbonic anhydrase II (PDB 2CBA) while retaining the single catalytic Zn2+ as a supported metal ion and keeping its His94/His96/His119 coordination shell, rather than dropping the zinc or treating it as a generic ligand. Neutralize the resulting system.

Public source anchors: PDB 2CBA.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
