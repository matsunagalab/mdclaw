# P20_prep_terminal_capping: N- and C-terminal capping

You are evaluating an MD agent on `P20_prep_terminal_capping`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Terminal capping: retrieve CLN025/chignolin from PDB `5AWL`, prepare the peptide for MD with an acetylated N terminus (`ACE`) and an N-methylamide C terminus (`NME`), and record the terminal-capping choices. Do not leave the requested termini as uncapped free termini.

Public source anchors: PDB 5AWL.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

For machine-readable scoring, record `preparation.source_pdb_id = "5AWL"`, `preparation.n_terminal_cap = "ACE"`, `preparation.c_terminal_cap = "NME"`, and `preparation.terminal_capping_recorded = true` in `metrics.json`.

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
