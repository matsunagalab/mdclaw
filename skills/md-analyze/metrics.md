# Analysis Metrics

After `concat_trajectory` (see `skills/md-analyze/concat.md`), the combined
trajectory and reference PDB are the common inputs for these metrics. Each
metric runs on its own `analyze` node: create the node, then run the tool with
`--job-dir` and `--node-id`; the tool resolves trajectory/topology inputs from
the DAG.

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <concat_analyze_node_id> --label "rmsd"
mdclaw --job-dir <job_dir> --node-id <rmsd_analyze_node_id> analyze_rmsd
```

## Requests and tools

| Request | Tool | Notes |
|---|---|---|
| Structural drift (backbone/protein) | `analyze_rmsd` | run on a combined or fitted node |
| Per-residue flexibility | `analyze_rmsf` | report the selection used |
| Atom-pair or group distances | `analyze_distance` | residue/ligand interactions |
| Native contact fraction | `analyze_q_value` | contacts vs a reference |
| Frame alignment for viz / dim-reduction | `fit_trajectory` | prerequisite for some metrics |
| Energy / temperature / volume / density | production `energy.dat` lineage or the combined energy artifact | not a separate analyze tool |

`analyze_distance --mode com` computes a geometric (unweighted) centroid and
does not reproduce the mass-weighted, periodic OpenMM coordinate used by a
production distance restraint. For biased runs, use the prod node's
`collective_variables` artifact as the exact CV record.

Prefer DAG-resolved artifacts from the analyze node. For ad-hoc external
trajectories, explicit file paths are acceptable when the user asks for them.
`analyze_rmsd`, `analyze_distance`, and `analyze_q_value` include `time_ns` in
their CSV only when concat's DAG-resolved `frame_times_ns` artifact is
available. Direct-mode or legacy inputs without that artifact remain valid,
but their CSV is frame-only; do not infer a cadence from the frame index.

## Named biological regions

For domain-, loop-, or TM-wise analysis, separate the biological annotation
from the trajectory topology. Prefer, in order: boundaries supplied by the
user; a reviewed curated entry appropriate to the exact construct (for example
UniProtKB/Swiss-Prot); then a prediction such as a live PPM run when no suitable
curated annotation is available. PPM remains appropriate for membrane
orientation; do not silently treat its live segment calls as curated domain
boundaries.

Map the annotated sequence to the simulated chain before constructing atom
selections. Use sequence correspondence and account for unresolved residues,
engineered insertions/deletions, tags, mutations, and insertion codes. Do not
assume that a single residue-number offset is valid unless the correspondence
demonstrates it across the analyzed region. Validate that every requested
region maps to the intended sequence and selects a nonzero, plausible number
of residues/atoms.

If curated and predicted boundaries disagree, use the curated boundaries by
default and report the disagreement rather than silently choosing either one.
Record the accession/source, source-coordinate boundaries, mapped topology
boundaries or explicit residue list, unmapped residues, and selection used with
the analysis result. If the accession or mapping is ambiguous enough to change
the result, stop and ask instead of guessing.

## Interpretation hints

- RMSD plateaus usually indicate a stable sampled basin; continuous drift
  suggests the system may need longer equilibration or a longer trajectory.
- Treat thresholds as system-dependent. Report the observed trend and the exact
  selection/stride used rather than relying on fixed cutoffs.

When reporting results, include the node lineage, atom selection, stride, frame
count, and any skipped or missing source artifacts.
