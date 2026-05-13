# T09 WT vs L99A Study Methods Bundle

You are evaluating an MD agent on `T09_study_t4l_wt_vs_l99a_methods`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/`.

Task: package a WT vs L99A T4 lysozyme comparison as an auditable study bundle. Use PDB entry 2LZM as the wild-type starting point and define the L99A mutant on chain A (LEU 99 to ALA). The emphasis is on clear methods, provenance, decision logging, and a calibrated evidence report rather than long simulation throughput. A reasonable planned study is 3 replicas x 200 ns each for WT and L99A, with analysis of cavity volume or packing proxy, local RMSF around residue 99, native-contact changes, and stability-direction interpretation.

Your submission directory must contain:

- `manifest.json`
- `evidence_report.json`
- `methods.md`
- `provenance.json`
- `decision_log.jsonl`

Include WT and mutant roles in `provenance.study.roles`. Write `methods.md` as a reproducible methods draft. Record key choices in `decision_log.jsonl`. The evidence report should state the stability direction, cite public sources or public literature, and include limitations without claiming unsupported MD-derived delta-delta-G.

