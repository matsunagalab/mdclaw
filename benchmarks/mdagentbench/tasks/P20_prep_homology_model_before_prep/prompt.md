# P20_prep_homology_model_before_prep: Homology modeling before prep

You are evaluating an MD agent on `P20_prep_homology_model_before_prep`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Homology modeling before prep: generate a model from the template/alignment and then prepare the selected model, rather than only applying a post-prep mutation.

Public source anchors: PDB 2LZM.

Template: use PDB 2LZM chain A.

Target sequence/alignment:

- Target code: `T4L_L99A_M102Q_model`
- The target is aligned one-to-one with PDB 2LZM chain A; there are no gaps.
- Target sequence:

```text
MNIFEMLRIDEGLRLKIYKDTEGYYTIGIGHLLTKSPSLNAAKSELDKAIGRNCNGVITKDEAEKLFNQDVDAAVRGILRNAKLKPVYDSLDAVRRCAAINQVFQMGETGVAGFTNSLRMLQQKRWDEAAVNLAKSRWYNQTPNRAKRVITTFRTGTWDAYKNL
```

Generate a model bundle from this template/target definition before running
the preparation workflow, and record the template, target sequence, and
alignment/procedure in `metrics.json` and `provenance.json`.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
