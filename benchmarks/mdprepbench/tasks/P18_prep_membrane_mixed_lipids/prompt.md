# P18_prep_membrane_mixed_lipids: Membrane embedding and lipid composition

You are evaluating an MD agent on `P18_prep_membrane_mixed_lipids`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Membrane embedding and lipid composition: prepare model 1 of TMEM14A from the PDB 2LOP NMR ensemble in a mixed POPC/POPE/CHL1 membrane. Use a packmol-memgen-style nominal composition request of `--lipids POPC:POPE:CHL1 --ratio 2:1:1` or an equivalent membrane-builder setting, but allow the realized integer lipid counts to follow the builder's box-size, area-per-lipid, and rounding behavior. Record the selected model rank as 1 and source-selection evidence in `source_selection.json` or structured provenance.

Public source anchors: PDB 2LOP.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short `mdclaw run_minimization` min-node step or an equivalent OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.



You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
