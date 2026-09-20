# Analysis Node Condition Contract

Single canonical home for the `analyze` node condition contract: the
`conditions` fields an Analysis Node carries, their allowed values, the
lightweight validation applied before execution, and the comparison mapping
formats. `docs/developer/architecture.md` gives the one-paragraph concept; this
page is the field-level specification. The rationale for requiring explicit
mappings lives in
[ADR 0003](../adr/0003-cross-topology-analysis-requires-explicit-mapping.md).

`create_node --node-type analyze` enforces the required fields below; see the
`node/` section of [`tool-reference.md`](tool-reference.md) for the tool
signature and failure codes.

## Analysis Data Scope

Every Analysis Node declares an Analysis Data Scope via the `analysis_data_scope`
condition field. Supported values:

| Value | Meaning |
|---|---|
| `segment` | Analyze the parent Production Segment only. |
| `production_chain` | Analyze the full Production Chain ending at the parent leaf. |
| `comparison` | Compare exactly two parent `analyze` nodes (trajectory comparisons, or the FEP ddG node `estimate_ddg` over two `analyze_fep` nodes). |
| `alchemical` | MBAR over the lambda windows of the parent `fep` nodes (`analyze_fep`); required when every parent is a `fep` node, rejected otherwise. |

A single production parent can mean either the parent Production Segment or the
full Production Chain ending at that leaf, depending on the declared scope. A
`comparison` node consumes exactly two `analyze` parents; create one
`production_chain` (or `alchemical`) analyze node per branch first, then
compare those analysis artifacts. Both branches live in the same job: the
folding-stability ddG, for instance, is a `comparison` node whose parents are
the folded leg's and the unfolded leg's `analyze_fep` nodes, the unfolded leg
having branched from the protein's `prep` node (`extract_tripeptide`).

The resolver exposes multi-parent analyze inputs as `branches_input`; that
internal name is kept for existing tools even when the data scope is
`comparison`. Each branch carries its `analyze_node_id`, the parent's recorded
`analysis` kind (e.g. `fep_mbar`) and, for alchemical legs, `fep_result_file`
next to the trajectory fields.

## Subjects and Mapping Ownership

- `analysis_subjects` and `comparison_mapping` belong to the comparison node's
  own `conditions`. Parent analyze nodes describe their own data scope, not the
  cross-branch subject namespace or correspondence.
- For `analysis_data_scope="comparison"`, `analysis_subjects` and
  `comparison_mapping` are **optional at creation**: a comparison tool that
  can tell its two inputs apart from the DAG (`estimate_ddg` reads the legs'
  prep `leg_role`) needs neither, while trajectory comparisons that need an
  atom / residue correspondence require them and refuse to run without. When
  `comparison_mapping` is given, the two `analysis_subjects` it references
  must be declared with it.
- For `segment`, `production_chain` and `alchemical`, `analysis_subjects` is
  optional unless a metric-specific tool requires a subject.
- Subject entries only require a unique `label` at this layer. Descriptor fields
  such as `chain_id`, `selection`, `residue_range`, or `resname` remain
  metric-specific.

Initial comparison support is binary/pairwise: exactly two analyze parents and,
when declared, exactly two subjects per comparison node.

## Cross-topology Comparisons

Cross-topology comparisons are allowed only when the Analysis Subjects and
Comparison Mapping are explicit. The same-topology path may use one shared
topology file, but different-topology comparisons need per-branch topology and
mapping data rather than atom-index assumptions. Mappings are never inferred
automatically from sequence or residue similarity. Initial mapping types are
limited to `residue_number` and `atom_selection`.

### `residue_number` mapping

Keeps the lightweight string form `subject_label:residue_id` in `pairs`, with
each pair referencing both subjects exactly once. The `residue_id` part is an
opaque string, not a number, so insertion codes and source-specific residue
identifiers remain representable.

### `atom_selection` mapping

Uses a `selections` object keyed by the two subject labels. Selection values are
mdtraj selection strings; lightweight validation only checks that they are
present and non-empty.

## Validation

Analysis tools apply lightweight validation before execution: required condition
fields, supported scope and mapping-type values, subject labels, and mapping
references must be syntactically consistent. Topology-backed checks such as
referenced residue/atom existence and per-branch compatibility are
metric-specific checks, not a global gate.

## Recommended comparison conditions

```json
{
  "analysis_data_scope": "comparison",
  "analysis_subjects": [
    {"label": "apo"},
    {"label": "holo"}
  ],
  "comparison_mapping": {
    "type": "residue_number",
    "pairs": [
      ["apo:10", "holo:10"],
      ["apo:11", "holo:11"]
    ]
  }
}
```
