# Improvement Proposal for Integrating MD Surrogate Sources into mdclaw

> **Partly superseded (2026-08-13, commit a9c6255).** Command examples below
> that use `setup_surrogate_backend` / `check_surrogate_backend` are stale;
> use `setup_model_backend --model bioemu` / `check_model_backend --model
> bioemu`. The design rationale is unchanged.

## Conclusion

Building the BioEmu integration end to end from the start, "surrogate ensemble → sidechain reconstruction → automatic selection → k parallel MD runs → aggregate analysis", would be far too complex. The MVP is narrowed to **being able to produce multiple structures from an MD surrogate as an mdclaw `source_bundle`, as all-atom PDBs including side chains**.

> **Update (2026-05-16)**: The Phase 1 sidechain reconstruction backend was replaced with HPacker. OpenMM minimize / energy ranking remain later phases. See §Improved Architecture for details.

However, assume that MD surrogate models other than BioEmu will also be used in the future. The public CLI does not use a BioEmu-specific name; instead it passes `--model bioemu` to a CLI shared by all surrogates. BioEmu is implemented as the first backend.

```bash
mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 100 \
  --max-candidates 100 \
  --job-dir <job_dir> \
  --node-id source_001
```

This command's responsibility extends as far as running the specified surrogate model in the per-backend runtime environment, normalizing the output trajectory / structures into a set of `candidate_NNN.pdb` files, and creating `source_bundle.json`. Downstream, `prepare_complex --source-candidate-id candidate_NNN` uses the existing implementation.

## Model Swapping Design

MD surrogates are handled as a backend registry inside `surrogate_server.py`.

```python
SURROGATE_BACKENDS = {
    "bioemu": BioEmuBackend(),
    # future: "alphaflow": AlphaFlowBackend(),
    # future: "esmflow": ESMFlowBackend(),
}
```

As a rule, there is one public CLI for candidate generation.

```python
generate_surrogate_candidates(
    model: str,
    amino_acid_sequence: str,
    num_samples: int = 100,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
    output_dir: str | None = None,
    job_dir: str | None = None,
    node_id: str | None = None,
    **model_options,
) -> dict
```

Common backend contract:

1. Validate the input.
2. Call the model-specific env / subprocess / local Python API.
3. Return an MDTraj-readable topology + trajectory, or a set of PDB/mmCIF structures.
4. Normalize to `candidate_NNN.pdb` with a common helper.
5. Write `source_type="surrogate"` and `origin.kind=<model>` to `source_bundle.json`.

BioEmu-specific options such as `msa_path`, `checkpoint` and `filter_samples` are passed as `model_options` and interpreted only by the BioEmu backend. When adding a future backend, do not add CLI names or downstream DAG structure; add one entry to the backend registry.

## Making Deployment Easy

Installing the BioEmu backend does not require the user to create a conda env by hand. mdclaw manages the backend runtime. However, containers are not split. When using Docker/Singularity, the BioEmu backend is bundled into the existing mdclaw container image.

**Decision**: whether local or in a container, BioEmu is not installed into the `mdclaw` conda environment. It always goes into a venv isolated from the main mdclaw Python. `generate_surrogate_candidates --model bioemu` calls that venv's Python as a subprocess.

```bash
mdclaw setup_surrogate_backend --model bioemu --device cuda
mdclaw check_surrogate_backend --model bioemu
```

Policy:

- Do not install BioEmu into the main mdclaw env.
- For a local workstation / conda install, use a managed venv. The BioEmu backend is isolated in an mdclaw-managed venv such as `~/.cache/mdclaw/surrogates/bioemu/venv`.
- For the managed venv, use `uv venv` / `uv pip install` if `uv` is available; otherwise fall back to the standard `python -m venv` + `pip`.
- `--device cpu` runs `pip install bioemu`, and `--device cuda` runs `pip install "bioemu[cuda]"`.
- In the Docker/Singularity image, the BioEmu backend is created at build time in an isolated venv inside the image. It is not installed into the `mdclaw` conda env. At run time `setup_surrogate_backend` is generally unnecessary; `check_surrogate_backend` alone is enough.
- The AF2 / BioEmu model weights needed on the first run are left to upstream to download and cache. mdclaw only reports the cache path and download failures clearly.
- For cluster operation, allow only the cache directory to be placed on a shared filesystem.

`generate_surrogate_candidates --model bioemu` does not install the backend on its own if it is missing. Instead it puts a reproducible `setup_surrogate_backend` command in the error. Automatic installation is convenient, but it causes unexpected network access on HPC or behind proxies, so it is split out into an explicit command.

The minimal implementation adds the following two as utility CLIs.

```python
setup_surrogate_backend(
    model: str,
    device: str = "cpu",        # "cpu" | "cuda"
    prefix: str | None = None,
    reinstall: bool = False,
) -> dict

check_surrogate_backend(
    model: str,
    prefix: str | None = None,
) -> dict
```

`check_surrogate_backend` returns the venv path or the backend path inside the container, an import check, the version, a device hint, and the cache path. It does no actual sampling.

Notes when using Docker/Singularity:

- Do not split containers. Bundle a venv for the BioEmu backend into the existing `mdclaw:latest` image.
- Keep the existing policy of building Singularity from the Docker image.
- `--nv` is required with Singularity. Without a GPU it runs as a CPU runtime, but do not expect practical speed.
- If the cache path differs inside and outside the container, the weights are downloaded repeatedly, so bind mount the BioEmu / ColabFold-equivalent cache under the mdclaw cache.

## What Needs Fixing

### 1. The MVP Is Too Large

The original plan was about to put the following into Phase 1:

- BioEmu sampling
- Splitting the trajectory into candidate PDBs
- Sidechain reconstruction for all candidates
- OpenMM minimize and energy scoring
- Source bundle update
- skill/docs/tests

This has many failure points. BioEmu has a backend-dedicated runtime, backbone-only output, a model weight cache, and GPU/Linux requirements, so the MVP should confine only BioEmu-specific uncertainty.

The improved Phase 1 is **a single shared candidate-generation CLI + source_bundle creation + HPacker repack**. OpenMM minimize / energy scoring / candidate selection / multi-branch MD are Phase 2 and later. HPacker is the same side-chain backend as the mutation workflow, so it satisfies what users expect: "an ensemble including side chains from one CLI".

### 2. Limit Sidechain Reconstruction for All Candidates to HPacker Repack

BioEmu output is backbone-only and as is does not satisfy the assumptions of `prepare_complex`. This was originally deferred to Phase 3, but since

- HPacker repack can be handled with the same backend as the mutation workflow
- HPacker is bundled into the container via `environment.yml`
- users expect "an ensemble including side chains from one CLI"

it was brought into Phase 1, and HPacker is called on each frame inside `generate_surrogate_candidates`. OpenMM minimize / energy ranking / clustering remain Phase 2 and later.

The implementation is as follows.

1. After `generate_surrogate_candidates --model bioemu` extracts the backbone candidates, they are repacked in place with HPacker.
2. The backbone-only PDBs are kept as provenance in `<artifacts>/candidates_backbone/`.
3. The candidate `tags` is `hpacker_repacked` (or `backbone_only` when disabled).
4. To opt out, use `--reconstruct-sidechains false`.

### 3. `selection.<tag>` Is Hard to See Through the Existing API

`source_bundle.json` allows extra metadata, but `list_source_candidates` does not treat an arbitrary bundle-level `selection.<tag>` as a first-class API. If selection results should be visible through the existing API, it is more robust to put them in each candidate record's `tags` / `metrics` / `rank` / `is_primary` than in a custom bundle-level schema.

The Phase 2 selection CLI should first stay at about the following level.

```bash
mdclaw select_source_candidates \
  --job-dir <job_dir> \
  --node-id source_001 \
  --strategy evenly_spaced \
  --k 10 \
  --tag bioemu_k10
```

The output appends the following to each candidate.

- `tags`: `["selected:bioemu_k10"]`
- `metrics.selection_rank`: 1, 2, ...
- `metrics.selection_strategy`: `"evenly_spaced"`, etc.

This stays within the existing freedom of the source bundle and makes it easy to extend the display of `list_source_candidates` later.

### 4. Multi-prod concat Assumes Topology Compatibility

A design of `source_001 -> prep_001..prep_K -> prod_001..prod_K -> analyze_001` within one job is consistent with the existing DAG. However, multi-prod `concat_trajectory` works on the assumption of a shared topology, so it must be stated explicitly as an operational invariant that the atom order / atom count of all branches are compatible.

BioEmu candidates are limited to monomers of the same sequence, and the `prepare_complex` settings are identical across all branches. Usage where protonation, missing-residue completion, ligands or chain selection differ per branch is not supported in the MVP.

### 5. Do Not Rush `branch_prep_for_candidates`

`prepare_complex` already accepts `--source-candidate-id`. In the MVP, do not add a branch-creation CLI; first select one candidate and feed it into the existing MD workflow.

When multiple branches become necessary, add `branch_prep_for_candidates` as a thin helper. It is not a new DAG convention; it just repeats `create_node(job_dir, "prep", parent_node_ids=["source_001"], conditions={"source_candidate_id": ...})`.

## Improved Architecture

### Phase 1: Surrogate Source Bundle Generation Only

There is one new CLI for candidate generation. Separately, `setup_surrogate_backend` / `check_surrogate_backend` are provided as utility CLIs for checking deployment.

```python
generate_surrogate_candidates(
    amino_acid_sequence: str,
    model: str = "bioemu",
    num_samples: int = 100,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
    output_dir: str | None = None,
    job_dir: str | None = None,
    node_id: str | None = None,
    **model_options,
) -> dict
```

Responsibilities:

1. Validate the input.
2. Select the backend by `model`.
3. Run the surrogate model as a subprocess inside the backend-specific managed venv.
4. Receive the trajectory/topology output or the set of structures.
5. Split into `candidate_NNN.pdb`.
6. Create `source_bundle.json` with `source_type="surrogate"`.
7. In node mode, complete the source node.

Files created in Phase 1:

- `mdclaw/surrogate_server.py`
- `tests/test_surrogate_server.py`
- `skills/bioemu-sample/SKILL.md`
- `docs/research/md_surrogate_integration.md`
- The surrogate backend deployment description in `docs/developer/configuration.md`

Existing files modified in Phase 1:

- `mdclaw/source_bundle.py`: helper to split a trajectory into candidate PDBs
- `mdclaw/_registry.py`: surrogate server registration
- `mdclaw/__init__.py`: export update
- `README.md`: short addition on where MD surrogate sources fit

What Phase 1 does:

- BioEmu sampling and source_bundle creation
- Side-chain repack with HPacker (in-process; can be disabled with `--reconstruct-sidechains false`)
- Keeping backbone-only PDBs as provenance

What Phase 1 does not do:

- OpenMM minimize / restrained relax
- energy ranking
- clustering
- prep fan-out
- multi-prod analysis extensions
- reweighting

### Phase 2: Selecting a Small Number of Candidates

Add one candidate-selection CLI.

```bash
mdclaw select_source_candidates \
  --job-dir <job_dir> \
  --node-id source_001 \
  --strategy evenly_spaced \
  --k 10 \
  --tag bioemu_k10
```

Keep the first strategies light.

- `evenly_spaced`: take evenly spaced entries from the source bundle order
- `random_seeded`: take randomly with a fixed seed
- `rmsd_farthest`: farthest-first sampling by Cα RMSD

`cluster_kmedoid`, energy ranking and clash scores can come later. Do not bring in sklearn or a custom k-medoid from the start.

### Phase 3: Correcting and Relaxing the Selected Candidates

HPacker repack is already done in Phase 1, so this phase adds relax / minimize corrections.

Candidates:

1. OpenMM local minimize on the k selected structures (clash removal).
2. Restrained short MD equilibration equivalent to `bioemu.sidechain_relax` (optional).
3. Share the HPacker helper with `create_mutated_structure`.

The goal of this phase is "to hand the k selected structures to prep stably", not to minimize every BioEmu sample.

### Phase 4: Multi-Branch MD

Add `branch_prep_for_candidates` when it becomes necessary.

```bash
mdclaw branch_prep_for_candidates \
  --job-dir <job_dir> \
  --source-node-id source_001 \
  --tag bioemu_k10 \
  --dry-run
```

This CLI makes dry-run the standard path. It first shows the list of nodes to be created, the candidate IDs, labels and expected `prepare_complex` arguments; on execution it only creates the `prep` nodes.

### Phase 5: Aggregate Analysis

Use the existing analyze multi-parent support. However, support it only when the following invariants hold.

- All prod parents come from the same sequence
- The topologies of all branches are atom-order-compatible
- They were built with the same solvent/modeling options
- analyze parents are prod only, with no mixed parents

Provenance extensions such as `frame_provenance.json` are added at this stage. They are not in the MVP.

## Source Bundle Design

Do not change the `source_bundle` schema. The source type is `"surrogate"`, not a backend-specific name, and the actual model name goes into `origin.kind`.

```json
{
  "schema_version": 1,
  "source_type": "surrogate",
  "structures": [
    {
      "candidate_id": "candidate_001",
      "rank": 1,
      "is_primary": true,
      "path": "candidates/candidate_001.pdb",
      "origin": {
        "kind": "bioemu",
        "bioemu_frame_index": 0,
        "bioemu_num_samples_requested": 100,
        "bioemu_filter_passed": true
      },
      "metrics": {},
      "tags": ["backbone_only"]
    }
  ]
}
```

Common information:

- `origin.kind`: a backend name such as `"bioemu"`
- `origin.surrogate_model`: the backend name or model id
- `origin.surrogate_version`

BioEmu-specific information goes into `origin`.

- `bioemu_frame_index`
- `bioemu_checkpoint`
- `bioemu_version`
- `msa_path_used`
- `msa_sha256`
- `filter_samples`
- `bioemu_filter_passed`

In the MVP, do not force-fill `metrics`. Represent the absence of confidence honestly.

## Error Policy

Phase 1 errors are limited to backend execution and bundle generation.

| Situation | Behaviour |
|---|---|
| Backend does not support the current OS/GPU | Early error. Linux/GPU is recommended for real BioEmu runs |
| Unsupported `--model` | Fail, returning the list of available models |
| No backend venv | Fail, returning the `setup_surrogate_backend --model ...` command |
| Backend import failure | Fail, returning the `check_surrogate_backend` result and reinstall steps |
| Input that looks like a multimer/ligand/PTM | Early reject. Point to Boltz-2 |
| Sequence too short | Early reject |
| Backend subprocess failure | Save stderr and the reproduction command as an artifact |
| Trajectory unreadable | Fail. The source node is not completed |
| Zero accepted candidates | Fail |
| Fewer accepted candidates than requested | Create the bundle with a warning |

Do not add automatic retry at first. Retry is convenient, but it hides failure causes and complicates testing.

## Test Plan

### Unit

- `generate_surrogate_candidates --model bioemu` fully mocks the subprocess.
- `setup_surrogate_backend` mocks install command construction; no real network install in unit tests.
- `check_surrogate_backend` mocks the Python execution in the managed venv.
- Confirm that `candidate_NNN.pdb` files are generated from a mock trajectory.
- Confirm that `source_bundle.json` has `source_type="surrogate"` and `origin.kind="bioemu"`.
- Confirm that `generate_surrogate_candidates` appears in CLI discovery.
- Confirm the early errors for macOS / missing env / invalid sequence.

### Smoke

Unskip only when a GPU/Linux environment is available.

```bash
mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 3 \
  --max-candidates 3 \
  --job-dir <job_dir> \
  --node-id source_001

mdclaw list_source_candidates \
  --job-dir <job_dir> \
  --node-id source_001
```

### Integration

Phase 1 integration goes only as far as passing one candidate to the existing `prepare_complex`.

```bash
mdclaw prepare_complex \
  --job-dir <job_dir> \
  --node-id prep_001 \
  --source-candidate-id candidate_001
```

The candidates are already HPacker-repacked, so failures on the prepare side caused by missing side chains are assumed not to occur. If prepare still breaks, proceed to the design that adds minimize/relax in Phase 3.

## Adopted Implementation Order

1. Build `generate_surrogate_candidates --model bioemu`.
2. Make BioEmu deployment checkable from mdclaw with `setup_surrogate_backend` / `check_surrogate_backend`.
3. Extract backbone candidates and turn them into a source bundle.
4. After extraction, repack in place with HPacker and keep the raw structures in `candidates_backbone/`.
5. Confirm the candidates are visible with `list_source_candidates`.
6. Pass one candidate to `prepare_complex` and observe where it breaks.
7. Based on the observed breakage, implement minimal minimize / relax in Phase 3.
8. Add the selection CLI.
9. Add the multi-branch prep helper.
10. Finally, add multi-prod analysis provenance.

## Decision Criteria

The value of this integration lies in "being able to plug MD surrogate models safely into mdclaw as source generators". Therefore, the first success criterion is not full automation of ensemble MD but the following three points.

- Heavy backend-specific dependencies such as BioEmu do not pollute the mdclaw env, and their installation can be checked with mdclaw's utility CLIs.
- Surrogate output fits within the existing `source_bundle` contract.
- One candidate can be passed to the existing `prepare_complex --source-candidate-id`.

Only after these three points are stable, add candidate selection, multi-branch runs and analysis.
