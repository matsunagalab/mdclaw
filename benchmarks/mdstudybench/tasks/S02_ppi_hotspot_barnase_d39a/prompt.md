# S02_ppi_hotspot_barnase_d39a: Barnase-Barstar D39A Binding Effect

You are evaluating an MD agent on `S02_ppi_hotspot_barnase_d39a`.

Use this prompt as the task statement. You may retrieve public structures and
literature, but do not read `truth/` or `scorer/` if those directories exist.

## Scientific question

Does replacing barstar Asp39 with alanine weaken, strengthen, or have a neutral
effect on binding to barnase? Asp39 here is the residue in barstar, independent
of how a selected structure labels its chains. Perform a comparative MD study
of the wild-type and D39A complexes and support your answer with quantitative
evidence calculated from the submitted trajectories.

You choose the structural source, preparation protocol, aqueous conditions,
simulation length, replica count, and observables. No PDB entry, chain ID,
trajectory length, replica count, or particular observable is prescribed.
Document those choices and keep the reference and variant conditions comparable
apart from the D39A perturbation.

You have up to 24 hours wall-clock for source selection, preparation, MD,
analysis, and reporting.

## Required conclusion

Set `evidence_report.json` → `conclusion.direction` to one of
`weakened_binding`, `strengthened_binding`, or `neutral`. Also report
`conclusion.evidence_status` as `supported`, `inconclusive`, or `contradicted`
and give a confidence from 0 to 1.

Public literature is allowed, but put prior knowledge in `prior_knowledge` and
keep it separate from `reasoning`, which must explain what the submitted MD
does and does not support. If the MD is inconclusive, report that honestly
rather than forcing a literature-consistent conclusion. State explicit
limitations.

## Submission

Submit `manifest.json`, `metrics.json`, `provenance.json`,
`evidence_report.json`, and `study_index.json`. In `study_index.json`, identify
one `reference` system and one `variant` system and list every submitted replica
with its matching topology and trajectory file or segments. In
`evidence_report.json`, report each relied-upon observable with its `metric`,
atom `selection` (and `selection_b` when needed), `reference` and `variant`
values, uncertainty, and optional unit. The evaluator will recompute supported
observables from the raw trajectories before judging the reasoning.
