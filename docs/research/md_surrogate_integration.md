# MD Surrogate Source Integration

MDClaw treats MD surrogate models as source generators. A surrogate backend
produces an ensemble of candidate structures, and MDClaw normalizes those
candidates into the existing `source_bundle.json` contract.

## Initial Backend: BioEmu

BioEmu samples approximate equilibrium conformations for protein monomers from
sequence. It is useful as a cheap front-end exploration step before selecting a
small number of candidates for atomistic MD.

MDClaw does not treat BioEmu as a replacement for MD. The intended workflow is:

```text
BioEmu candidates -> source_bundle -> select candidate(s) -> prepare_complex -> MD
```

## Runtime Boundary

BioEmu is installed in an isolated venv. It is never installed into the conda
`mdclaw` environment, even inside the Docker/Singularity runtime image. This
keeps BioEmu's JAX/Torch dependencies separate from the Amber/OpenMM stack.

## Source Bundle Contract

Surrogate bundles use:

- `source_type="surrogate"`
- `origin.kind="bioemu"` for BioEmu candidates
- `tags=["backbone_only"]` for initial BioEmu outputs

The schema is intentionally generic so future models can be added by extending
the surrogate backend registry rather than changing downstream DAG rules.

## Current Limitations

- BioEmu is monomer-only in the supported path.
- BioEmu outputs backbone-frame structures; side-chain reconstruction is not
  part of the initial source-generation MVP.
- Per-sample confidence is not available, so ranking is deferred to later
  candidate-selection tools or downstream physical validation.
- BioEmu model and AF2/ColabFold weights are downloaded by upstream BioEmu on
  first use and should be cached on shared storage for HPC use.
