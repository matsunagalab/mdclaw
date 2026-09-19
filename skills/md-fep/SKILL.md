---
name: md-fep
description: "Relative free energy of a single point mutation (ddG of folding stability) by hybrid-topology FEP with MDClaw CLI tools and OpenMM. Two legs (folded protein, capped tripeptide) share one mutation spec; each leg is prep -> build_hybrid_system -> min -> eq -> run_fep -> analyze_fep, then estimate_ddg. Before any state-changing command, follow the pre-command gate in this skill."
---

# MD FEP (single point mutation ddG)

You are a computational biophysics expert estimating how one amino-acid
substitution changes folding stability, using alchemical free energy
perturbation on a hybrid topology.

Follow `skills/common/preamble.md`, `skills/common/run-loop.md` (the canonical
node loop), `skills/common/solvent-regimes.md`, and
`skills/common/tool-output.md` for error handling.

## Pre-command gate

Before every state-changing command in this skill:

1. The mutation is **one** substitution written `[CHAIN:]<wt><resseq><mut>`
   (`A:L99A`). Multi-site, insertion, deletion, or non-protein mutations are out
   of scope: say so and stop.
2. Both legs use the **same** mutation string, force field, water model,
   protonation options, and ensemble. Any difference silently biases ddG.
3. The `topo` node is built by `build_hybrid_system`, not `build_amber_system`.
   `min`, `eq`, and `fep` descend from that hybrid topo.
4. `run_fep` runs on a GPU (`--platform CUDA`); one window is a full MD run.
   Never start the 21-window default on CPU.
5. Never quote a ddG until `analyze_fep` completed on **both** legs with no
   `fep_windows_incomplete` and `min_neighbour_overlap >= 0.03`.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target structure | PDB id / file (folded state) |
| Mutation | `[CHAIN:]<wt><resseq><mut>` |
| Execution mode | `autonomous` / `human_in_the_loop` |
| Solvent regime | `explicit` (implicit is not supported for FEP) |
| Windows | 21 (default) or an explicit `--lambda-schedule` |
| Sampling per window | user-specified; autonomous default 1 ns (sanity) |
| Unfolded model | capped tripeptide (default) or `--flank N` |

Autonomous default: proceed without asking when the mutation string is
unambiguous against the structure.

## Model

- **ddG_folding = dG_mut(folded) − dG_mut(unfolded)**; positive destabilises.
- Each leg transforms WT → mutant along lambda in three phases
  (decharge old side chain → soft-core sterics swap → recharge new side chain)
  on one hybrid System; every window evaluates the reduced potential at all
  windows and MBAR combines them.
- The unfolded state is the capped tripeptide (ACE-X(i−1)-X(i)-X(i+1)-NME)
  cut from the same prepared protein so both legs share residue numbering and
  protonation.

Charge-changing mutations (e.g. K→A, D→N) are accepted; `build_hybrid_system`
returns a `Charge-changing mutation` warning (PME neutralising background, no
finite-size correction). Report that warning verbatim with the ddG.

## Workflow

The study has two jobs: `jobs/folded` and `jobs/unfolded`. Run the folded leg
first; its prep PDB is the input for the tripeptide.

1. **Bootstrap** the study with both jobs declared in the plan (a job id
   missing from the plan is refused), then bootstrap the second job:

   ```bash
   mdclaw bootstrap_md_workflow --study-dir <study> --job-id folded --pdb-id <id> \
     --question "<ddG question>" \
     --plan '{"jobs": [{"job_id": "folded", "purpose": "folded-protein leg"},
                       {"job_id": "unfolded", "purpose": "capped tripeptide leg"}]}'
   mdclaw bootstrap_md_workflow --study-dir <study> --job-id unfolded --question "<ddG question>"
   ```

   Follow `skills/md-prepare/SKILL.md` for the folded leg through `solv`
   (`prepare_complex` → `solvate_structure`). Keep the prep node's
   `merged_pdb` artifact path; it is the tripeptide input in step 6.

2. **Hybrid topology (folded).**

   ```bash
   mdclaw create_node --job-dir <folded> --node-type topo --label "hybrid_A_L99A"
   mdclaw explain_node --job-dir <folded> --node-id <topo_id>
   mdclaw --job-dir <folded> --node-id <topo_id> build_hybrid_system \
     --mutation A:L99A --forcefield ff19SB --water-model opc --n-windows 21
   ```

   `build_hybrid_system` models the mutant side chain (HPacker, PDBFixer
   fallback), builds WT and mutant end states with `build_amber_system`,
   fuses them, relaxes the appearing atoms, and validates that the hybrid
   reproduces both end-state energies at λ=0/1. Read
   `endpoint_validation.passed` in the result; if `false`, stop and report.

3. **Minimise and equilibrate** from the hybrid topo exactly as in
   `skills/md-equilibration/SKILL.md` (`run_minimization` → `run_equilibration`,
   NPT). The hybrid System at λ=0 is the wild type; nothing FEP-specific is
   needed here.

4. **Sample windows.**

   ```bash
   mdclaw create_node --job-dir <folded> --node-type fep --label "fep_1ns"
   mdclaw --job-dir <folded> --node-id <fep_id> run_fep \
     --sampling-time-ns 1 --equilibration-time-ns 0.1 --platform CUDA
   ```

   One `fep` node may sample all windows serially (default) or a subset with
   `--lambda-indices 0-6`. Over ~1 ns per window, or on HPC, create one `fep`
   node per subset under the same `eq` parent and submit them with
   `submit_array_job` (`skills/md-fep/windows.md`): a killed node then loses
   one subset, and its finished windows are recoverable from the partial
   `fep_windows.json`. Extend converged-but-noisy windows with a child `fep`
   node (`--parent-node-ids <fep_id> ... --equilibration-time-ns 0`); samples
   are chained. Each window starting from the eq state is minimised at its own
   λ first, so small→large mutations do not start from solvent overlapping the
   appearing side chain.

5. **Estimate the leg.**

   ```bash
   mdclaw create_node --job-dir <folded> --node-type analyze \
     --parent-node-ids <fep_id>[,<fep_id2>,...] \
     --conditions '{"analysis_data_scope": "alchemical"}'
   mdclaw --job-dir <folded> --node-id <analyze_id> analyze_fep
   ```

   Parent the analyze node to every `fep` node needed to cover all windows.
   Check `min_neighbour_overlap` (≥ 0.03) and `warnings`; act per
   `skills/md-fep/convergence.md`.

6. **Unfolded leg.** Cut the tripeptide from the folded prep PDB, register it
   as the `unfolded` job's source, cap it, and repeat 2–5:

   ```bash
   mdclaw extract_tripeptide --pdb-file <folded>/nodes/<prep_id>/artifacts/merge/merged.pdb \
     --mutation A:L99A
   mdclaw --job-dir <unfolded> --node-id <source_id> fetch_structure \
     --source local --file-path <tripeptide.pdb>
   mdclaw --job-dir <unfolded> --node-id <prep_id> prepare_complex \
     --cap-termini true <same --ph / protonation options as folded>
   ```

   Then `solvate_structure --dist 10` (a tripeptide needs no 15 Å buffer; keep
   the image gap above the 10 Å cutoff), `build_hybrid_system`
   with the **same** `--mutation`, `--forcefield`, `--water-model`,
   `--n-windows`/`--lambda-schedule`, then min → eq → `run_fep` → `analyze_fep`.

7. **Combine.**

   ```bash
   mdclaw estimate_ddg --folded <folded>/nodes/<an>/artifacts/fep_result.json \
     --unfolded <unfolded>/nodes/<an>/artifacts/fep_result.json --study-dir <study>
   ```

   Report `ddG_kcal_mol ± ddG_error_kcal_mol`, the two leg dG values, sampling
   per window, and every warning carried from the legs.

## Failure codes

| Code | Action |
|---|---|
| `fep_mutation_spec_invalid`, `fep_mutation_residue_not_found`, `fep_mutation_residue_ambiguous` | Fix the mutation string against the prep PDB residue numbering; add the chain id. |
| `fep_mutant_model_failed` | Retry with `--mutant-backend pdbfixer`; report if that also fails. |
| `fep_environment_mismatch` | WT/mutant end states differ outside the residue; rebuild from the same solv node with identical options. |
| `fep_endpoint_validation_failed` | Do not sample. Report the energy table from the failure manifest. |
| `fep_hybrid_topology_required` | The topo ancestor is not a hybrid; create a topo node with `build_hybrid_system` and re-branch min/eq. |
| `fep_windows_incomplete` | Create `fep` nodes for the listed indices (same eq parent), parent the analyze node to all of them. |
| `fep_sampling_failed` | Read `artifacts/failure/latest`; recover the finished windows with a new `fep` node under the eq parent and `--restart-windows-file` (`skills/md-fep/windows.md`). |
| `fep_windows_incompatible`, `fep_parent_ambiguous`, `fep_lambda_index_invalid` on extension | One fep parent per child, same `--pressure-bar`, only the parent's windows. |
| `invalid_parameter_value` | The node is still pending: fix the argument and run the same node again. |
| low `neighbour_overlap` warning | Densify lambdas there (`skills/md-fep/convergence.md`). |

## Handoff

Report ddG only from `estimate_ddg` output. Then follow the stopping rule in
`skills/common/run-loop.md`; if the request asks for a Methods section or
deposit bundle, continue with `skills/md-report/SKILL.md` on both job
directories.
