# T09 WT vs L99A Study Methods Bundle

You are evaluating an MD agent on `T09_study_t4l_wt_vs_l99a_methods`.

Use only these public files:

- `task.json`
- `input/2LZM.pdb`
- `input/mutation_request.json`
- `input/study_brief.md`

Do not read `truth/` or `scorer/`.

Task: package a WT vs L99A T4 lysozyme comparison as an auditable study bundle. The emphasis is on clear methods, provenance, decision logging, and a calibrated evidence report rather than long simulation throughput.

Your submission directory must contain:

- `manifest.json`
- `evidence_report.json`
- `methods.md`
- `provenance.json`
- `decision_log.jsonl`

Include WT and mutant roles in `provenance.study.roles`. Write `methods.md` as a reproducible methods draft. Record key choices in `decision_log.jsonl`. The evidence report should state the stability direction, cite only public inputs or public literature, and include limitations.

