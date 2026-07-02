# MODELLER Workflow Variants

Pick the one variant that matches the request, then run it on the job's
`source` node. Create the node first:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source
```

| Situation | Variant | Key flags |
|---|---|---|
| One target chain, template + sequence | Single chain | `--target-sequence` |
| Heterodimer/complex, one sequence per chain | Multi-chain | `--target-sequences`, `--template-chains` |
| Fill/refine gaps in an existing structure | Loop refinement | `--loop-refinement`, `--loop-models` |
| You already have a PIR/ALI alignment | Explicit alignment | `--alignment-file` |

Provide either `--target-sequence` (single chain) or `--target-sequences` (one
per chain), never both.

## Single chain

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment \
  --template-pdb "/abs/template.pdb" \
  --target-sequence "MVLSPADKTNVKAAW..." \
  --num-models 3
```

## Multi-chain complex

Pass one sequence per target chain. The tool builds the complex alignment with
MODELLER `align2d` against the template (chains joined with `/`).
`--template-chains` chooses and orders the template chains mapping to the target
chains, in the same order as `--target-sequences`; its length must match. When
omitted, all template chains are used in file order.

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment \
  --template-pdb "/abs/9OPW.pdb" \
  --template-code "9OPW" \
  --template-chains A B \
  --target-sequences "<chainA seq>" "<chainB seq>" \
  --target-code "complex" \
  --num-models 3
```

## Loop refinement (fill cryo-EM / X-ray gaps)

Pass the structure as the template and its full sequence (e.g. from SEQRES) as
the target, and add `--loop-refinement`. The base model builds the missing
residues; MODELLER `LoopModel` then rebuilds every gap loop. `--loop-models`
sets how many refined loop models to generate (best by DOPE selected);
`--loop-min-length` / `--loop-max-length` bound which gap loops are refined
(defaults 1..30).

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment \
  --template-pdb "/abs/9OPW.pdb" \
  --template-code "9OPW" \
  --template-chains A B \
  --target-sequences "<chainA SEQRES>" "<chainB SEQRES>" \
  --target-code "9OPW_filled" \
  --loop-refinement \
  --num-models 1 \
  --loop-models 4
```

## Explicit alignment (any chain count; chains separated by `/`)

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment \
  --template-pdb "/abs/template.pdb" \
  --alignment-file "/abs/alignment.ali" \
  --template-code "tmpl" \
  --target-code "target" \
  --num-models 3
```

After any variant, the tool normalizes the selected model into the source
bundle (`nodes/<source_node_id>/artifacts/source_bundle.json` and
`artifacts/candidates/<candidate_id>.pdb`). List candidates before preparation:

```bash
mdclaw list_source_candidates --job-dir <job_dir> --node-id <source_node_id>
```
