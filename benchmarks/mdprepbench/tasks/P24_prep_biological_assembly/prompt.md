# P24_prep_biological_assembly: Assembly/biological unit choice

You are evaluating an MD agent on `P24_prep_biological_assembly`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Assembly/biological unit choice: generate or select biological assembly 1 of PDB 1STP. The scorer verifies the submitted coordinates against a fixed assembly-1 reference and checks that the submitted structure contains four protein chains, so assembly identity is not accepted from self-reported JSON alone.

Public source anchors: PDB 1STP, PDB 2MS2.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

The submitted structure should represent assembly 1 rather than the asymmetric unit alone. The expected assembly chain count refers to the polymer protein chains of the biological assembly; bound cofactors or ligands may be assigned their own chain IDs and are not counted toward that total.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
