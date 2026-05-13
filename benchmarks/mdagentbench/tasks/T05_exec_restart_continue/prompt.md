# T05 Restart Continuation

You are evaluating an MD agent on `T05_exec_restart_continue`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve chignolin CLN025 from PDB entry 5AWL and split a short NVT MD simulation into two restart chunks. Run chunk 1 for 2500 steps, save a portable XML state or equivalent restart artifact, start a fresh simulation from that state for chunk 2 for another 2500 steps, and verify that the combined trajectory and step counters are continuous. Use 300 K, 2 fs steps, PME, HBonds constraints, and a DCD stride of about 500 steps.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

The manifest should point to trajectory, topology, and checkpoint or state artifacts when available. Metrics should report restart continuity, frame counts, finite energy, and no NaN behavior. The evidence report should describe the restart procedure, public sources retrieved, and limitations.

