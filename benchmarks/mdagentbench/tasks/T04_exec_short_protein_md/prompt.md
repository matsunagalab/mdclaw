# T04 Short Protein MD

You are evaluating an MD agent on `T04_exec_short_protein_md`.

Use only these public files:

- `task.json`
- `input/2LZM.pdb`
- `input/prep_request.json`
- `input/md_protocol.json`

Do not read `truth/` or `scorer/` if those directories exist.

Task: prepare, equilibrate, and run short explicit-water MD for wild-type T4 lysozyme (PDB 2LZM). Use the force-field and water-model choices specified by the public inputs. The target is an NVT production trajectory of at least 100 ps with at least 50 frames.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

The manifest must point to the generated trajectory and topology under `outputs.trajectories` and `outputs.topology`. The topology should contain explicit water. Metrics should report finite energy and no NaN behavior. The evidence report should summarize the preparation, equilibration, production run, and limitations.

