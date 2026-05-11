# T05 Restart Continuation

You are evaluating an MD agent on `T05_exec_restart_continue`.

Use only these public files:

- `task.json`
- `input/5AWL.pdb`
- `input/restart_protocol.json`

Do not read `truth/` or `scorer/` if those directories exist.

Task: split chignolin NVT MD into two chunks and prove that restarting from a saved state preserves the simulation timeline. Run chunk 1, save a restart state, start a fresh simulation from that state for chunk 2, and verify that the combined trajectory and step counters are consistent.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

The manifest should point to trajectory, topology, and checkpoint or state artifacts when available. Metrics should report restart continuity, frame counts, finite energy, and no NaN behavior. The evidence report should describe the restart procedure and any limitations.

