# S03_ppi_evidence_bundle_barnase: Barnase-Barstar D39A Study Evidence Bundle

You are evaluating an MD agent on `S03_ppi_evidence_bundle_barnase`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: package a wild-type vs D39A barnase-barstar binding comparison as an auditable study bundle. Use PDB entry 1BRS as the complex starting point and define the D39A mutant on barstar (ASP 39 to ALA; barstar is chain D in 1BRS, paired with barnase chain A). The emphasis is on clear methods, provenance, decision logging, and a calibrated evidence report rather than long simulation throughput. A reasonable planned study is 3 replicas x 200 ns each for the WT and D39A complexes, with analysis of interface SASA, inter-chain contacts, hydrogen bonds and salt bridges, interface water occupancy, and a binding-direction interpretation.

Public source anchors: PDB 1BRS.

Your submission directory must contain:

- `manifest.json`
- `evidence_report.json`
- `methods.md`
- `provenance.json`
- `decision_log.jsonl`

Include reference (WT complex) and variant (D39A complex) roles in `provenance.study.roles`. Write `methods.md` as a reproducible methods draft. Record key choices in `decision_log.jsonl`. The evidence report should state the binding direction (`weakened_binding`, `strengthened_binding`, or `neutral`), cite public sources or public literature, and include limitations without claiming an unsupported MD-derived binding delta-delta-G.
