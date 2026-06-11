# P07_prep_rna_crystallographic_ions: Crystallographic ion triage

You are evaluating an MD agent on `P07_prep_rna_crystallographic_ions`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Crystallographic ion triage: prepare oligo(U) RNA while retaining prompt-designated crystallographic K+ ions, excluding deposited crystallographic waters or buffer molecules as selected source components, and building an explicit-solvent topology/minimization system.

Public source anchors: PDB 4RBQ.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short `mdclaw run_minimization` min-node step or an equivalent OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

Exclude deposited crystallographic waters or buffer molecules during source triage, but do not treat the later explicit-solvent water box as an error; bulk water added for topology and minimization is expected.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
