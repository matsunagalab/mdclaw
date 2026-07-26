# S04_affinity_t4l_l99a_alkylbenzene: T4 Lysozyme L99A Ligand Affinity

You are evaluating an MD agent on `S04_affinity_t4l_l99a_alkylbenzene`.

Use this prompt as the task statement. You may retrieve public structures and
literature, but do not read `truth/` or `scorer/` if those directories exist.

## Scientific question

In the engineered T4 lysozyme L99A apolar cavity, does n-butylbenzene bind more
strongly, more weakly, or similarly to benzene? Report the direction for
n-butylbenzene relative to benzene. Perform a comparative MD study and support
your answer with quantitative evidence calculated from the submitted
trajectories.

You choose the structural sources, preparation and ligand-parameterization
protocols, aqueous conditions, simulation length, replica count, and
observables. No PDB entry, chain ID, trajectory length, replica count, or
particular observable is prescribed. Document those choices and keep the two
complexes comparable apart from the ligand identity.

You have up to 24 hours wall-clock for source selection, preparation, MD,
analysis, and reporting.

## Required conclusion

Set `evidence_report.json` → `conclusion.direction` to one of
`stronger_binding`, `weaker_binding`, or `similar`. Also report
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
