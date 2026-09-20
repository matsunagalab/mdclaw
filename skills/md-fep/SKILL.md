---
name: md-fep
description: "Relative free energy of a single point mutation (ddG of folding stability) by hybrid-topology FEP with MDClaw CLI tools and OpenMM. One job holds both legs: the folded protein and, as a prep child of its prep node, the capped tripeptide (extract_tripeptide); each leg runs build_hybrid_system -> min -> eq -> run_fep -> analyze_fep, and a comparison analyze node (estimate_ddg) subtracts them. Before any state-changing command, follow the pre-command gate in this skill."
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
2. Both legs live in **one job** and use the **same** mutation string, force
   field, water model, windows and ensemble. The unfolded leg is a `prep`
   child of the protein's `prep` node (`extract_tripeptide`), so source,
   preparation and protonation are shared by construction; `estimate_ddg`
   refuses legs whose remaining settings differ (`fep_legs_incompatible`).
3. The `topo` node is built by `build_hybrid_system`, not `build_amber_system`.
   `min`, `eq`, and `fep` descend from that hybrid topo.
4. `run_fep` runs on a GPU (`--platform CUDA`); one window is a full MD run.
   Never start the 21-window default on CPU.
5. Never quote a ddG except from a completed `estimate_ddg` node, whose two
   `analyze_fep` parents each completed with `min_neighbour_overlap >= 0.03`.

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
  cut from the same prepared protein so both legs share residue numbering,
  chain id and protonation.
- ddG is a node: the `comparison` analyze node over the two legs'
  `analyze_fep` nodes. Its lineage holds both legs, `next` leads to it, and
  `md-report` reads it like any other analysis.

Charge-changing mutations (e.g. K→A, D→N) are accepted; `build_hybrid_system`
returns a `Charge-changing mutation` warning (PME neutralising background, no
finite-size correction). Report that warning verbatim with the ddG.

## Workflow

One job (`jobs/main`). The folded leg runs first; the unfolded leg branches
from the folded leg's `prep` node. After every completed node read `next`: it
names the next command for this shape (including the unfolded branch and the
ddG node).

```text
source ─ prep_001 ─ solv ─ topo(hybrid) ─ min ─ eq ─ fep ─ analyze_001 (analyze_fep) ─┐
            └─ prep_002 (extract_tripeptide) ─ solv ─ topo(hybrid) ─ ... ─ analyze_002 ─┴─ analyze_003 (estimate_ddg)
```

1. **Bootstrap** one job and prepare the protein:

   ```bash
   mdclaw bootstrap_md_workflow --study-dir <study> --pdb-id <id> \
     --question "<ddG question>" --sampling-stage fep
   ```

   (`--sampling-stage fep` makes the plan's `workflow_steps` say `fep`, not
   `prod`, after `eq`.) Follow `skills/md-prepare/SKILL.md` through `solv`
   (`prepare_complex` → `solvate_structure`).

2. **Hybrid topology (folded).**

   ```bash
   mdclaw create_node --job-dir <job> --node-type topo --label "hybrid_A_L99A"
   mdclaw explain_node --job-dir <job> --node-id <topo_id>
   mdclaw --job-dir <job> --node-id <topo_id> build_hybrid_system \
     --mutation A:L99A --forcefield ff19SB --water-model opc --n-windows 21
   ```

   `build_hybrid_system` models the mutant side chain (HPacker, PDBFixer
   fallback), builds WT and mutant end states with `build_amber_system`,
   fuses them, relaxes the appearing atoms, and validates that the hybrid
   reproduces both end-state energies at λ=0/1. Read
   `endpoint_validation.passed` in the result; if `false`, stop and report.
   The relaxation and end-point energies use the fastest OpenMM platform,
   i.e. a GPU when the host has one; on a shared login node pass
   `--platform CPU` (or `--device-index N`) like the run tools.

3. **Minimise and equilibrate** from the hybrid topo exactly as in
   `skills/md-equilibration/SKILL.md` (`run_minimization` → `run_equilibration`,
   NPT). The hybrid System at λ=0 is the wild type; nothing FEP-specific is
   needed here.

4. **Sample windows.**

   ```bash
   mdclaw create_node --job-dir <job> --node-type fep --label "fep_folded"
   mdclaw --job-dir <job> --node-id <fep_id> run_fep \
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
   appearing side chain; keep `--equilibration-time-ns 0.1` (or more) for such
   windows so the box re-heats before sampling.

5. **Estimate the leg.**

   ```bash
   mdclaw create_node --job-dir <job> --node-type analyze \
     --parent-node-ids <fep_id> [<fep_id2> ...] \
     --conditions '{"analysis_data_scope": "alchemical"}'
   mdclaw --job-dir <job> --node-id <analyze_id> analyze_fep
   ```

   Parent the analyze node to every `fep` node needed to cover all windows.
   Check `min_neighbour_overlap` (≥ 0.03) and `warnings`; act per
   `skills/md-fep/convergence.md`. The result's `next` now points at the
   unfolded leg (step 6), or at the ddG node once both legs are analysed.

6. **Unfolded leg.** A `prep` node **under the protein's prep node**; the
   tool cuts the tripeptide from that node's `merged_pdb`, caps it, and keeps
   the parent's protonation states, chain id and numbering:

   ```bash
   mdclaw create_node --job-dir <job> --node-type prep --parent-node-ids <prep_001> --label "tripeptide"
   mdclaw --job-dir <job> --node-id <prep_002> extract_tripeptide --mutation A:L99A
   ```

   Then, each with `--parent-node-ids` naming this branch: `solvate_structure
   --dist 10` (a tripeptide needs no 15 Å buffer; keep the image gap above the
   10 Å cutoff), `build_hybrid_system` with the **same** `--mutation`,
   `--forcefield`, `--water-model`, `--n-windows`/`--lambda-schedule`, then
   min → eq → `run_fep` → `analyze_fep` as in steps 2–5. `create_node` without
   `--parent-node-ids` refuses (`parent_required`) while both branches are
   open: always name the parent on this leg.

7. **ddG node.** A `comparison` analyze node over the two legs' `analyze_fep`
   nodes (either order; the leg whose prep ancestor is `extract_tripeptide`
   is the unfolded one):

   ```bash
   mdclaw create_node --job-dir <job> --node-type analyze \
     --parent-node-ids <analyze_folded> <analyze_unfolded> \
     --conditions '{"analysis_data_scope": "comparison"}'
   mdclaw --job-dir <job> --node-id <analyze_ddg> estimate_ddg
   ```

   `estimate_ddg` first checks that both legs share the mutation, lambda
   protocol, force field, water model, HMR, temperature and pressure
   (`fep_legs_incompatible` otherwise, node stays pending), then writes
   `artifacts/ddg.json` and records ddG on the node. Report
   `ddG_kcal_mol ± ddG_error_kcal_mol`, the two leg dG values, sampling per
   window, and every warning carried from the legs.

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
| `fep_fragment_prep_required` | `extract_tripeptide` ran under a source node; create its prep node with `--parent-node-ids <prep_001>`. |
| `fep_tripeptide_cap_failed` | `clean_protein` could not cap the fragment; read the nested errors, fix the parent preparation, branch a new prep node. |
| `fep_ddg_scope_invalid`, `fep_ddg_parents_invalid` | The ddG node is `comparison` over exactly two completed `analyze_fep` nodes; recreate it that way. |
| `fep_leg_role_ambiguous` | Neither leg descends from `extract_tripeptide`; derive the unfolded leg with it, or declare `analysis_subjects` `[{"label": "folded"}, {"label": "unfolded"}]` in `--parent-node-ids` order. |
| `fep_legs_incompatible` | The message names the differing setting; rebuild that leg's `build_hybrid_system` / `run_fep` with the other leg's options. |
| `invalid_parameter_value` | The node is still pending: fix the argument and run the same node again. |
| low `neighbour_overlap` warning | Densify lambdas there (`skills/md-fep/convergence.md`). |

## Handoff

Report ddG only from the completed `estimate_ddg` node. Then follow the
stopping rule in `skills/common/run-loop.md`; if the request asks for a
Methods section or deposit bundle, continue with `skills/md-report/SKILL.md`
with the ddG node as the target (its lineage holds both legs).
