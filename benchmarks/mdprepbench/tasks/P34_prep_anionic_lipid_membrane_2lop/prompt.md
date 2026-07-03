# P34_prep_anionic_lipid_membrane_2lop: Anionic-lipid membrane preparation

You are evaluating an MD agent on `P34_prep_anionic_lipid_membrane_2lop`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Anionic-lipid membrane preparation: prepare model 1 of TMEM14A from the PDB 2LOP NMR ensemble in a mixed POPC:POPG membrane that contains anionic POPG lipids. Use a nominal 3:1 POPC:POPG composition (for example a packmol-memgen-style `--lipids POPC:POPG --ratio 3:1` request) or an equivalent membrane-builder setting, and add counterions so the anionic bilayer system is net neutral. Allow the realized integer lipid counts to follow the builder's box-size, area-per-lipid, and rounding behavior.

Public source anchors: PDB 2LOP.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
