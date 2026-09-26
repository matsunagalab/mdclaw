---
name: md-abfe
description: "Absolute binding free energy of one neutral, non-covalent ligand to a protein by double decoupling with MDClaw CLI tools and OpenMM. One job holds both legs: the complex (build_decoupled_system -> min -> eq -> add_boresch_restraint -> run_fep -> analyze_fep) and, as a prep child of its prep node, the ligand alone (extract_ligand -> ... -> analyze_fep); a comparison analyze node (estimate_binding_dg) closes the cycle. Before any state-changing command, follow the pre-command gate in this skill."
---

# MD ABFE (absolute binding free energy of a ligand)

You are a computational biophysics expert estimating the standard binding free
energy of one small molecule to a protein by alchemically switching the ligand
off in the complex and in water.

Follow `skills/common/preamble.md`, `skills/common/run-loop.md` (the canonical
node loop), `skills/common/solvent-regimes.md`, and
`skills/common/tool-output.md` for error handling. Sampling, window extension
and convergence work exactly as in `skills/md-fep/windows.md` and
`skills/md-fep/convergence.md`: both legs are `run_fep` / `analyze_fep` legs.

## Pre-command gate

Before every state-changing command in this skill:

1. The ligand is **one** copy of **one** small molecule that `prepare_complex`
   kept and parameterised (`--process-ligands`), bound in the pose to be
   evaluated. The tools refuse a charged ligand
   (`abfe_charged_ligand_unsupported`), a covalent one (`abfe_ligand_covalent`)
   and one with fewer than three bonded heavy atoms (`abfe_ligand_too_small`):
   report these, never edit the ligand to get past them.
2. Both legs live in **one job**. The solvent leg is a `prep` child of the
   complex's `prep` node (`extract_ligand`), so both legs read the same
   prepared ligand; use the same force field, water model, HMR and lambda
   schedules on both (`estimate_binding_dg` refuses otherwise,
   `abfe_legs_incompatible`).
3. Both `topo` nodes are built by `build_decoupled_system`, not
   `build_amber_system`.
4. The complex leg is sampled **only** under the topology written by
   `add_boresch_restraint`: its `fep` nodes are children of that `topo` node.
   A `fep` node under the leg's `eq` is refused (`abfe_restraint_required`). The tool chooses the restraint atoms;
   do not pick them.
5. `run_fep` runs on a GPU (`--platform CUDA`). The defaults are 24 windows
   (complex) and 18 (solvent); never start them on CPU.
6. Never quote a binding free energy except from a completed
   `estimate_binding_dg` node whose two `analyze_fep` parents each have
   `min_neighbour_overlap >= 0.03`.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target structure | PDB id / file of the complex, ligand in the pose to evaluate |
| Ligand | `RESNAME` (or `CHAIN:RESNAME:RESNUM` when several are kept) and its SMILES |
| Execution mode | `autonomous` / `human_in_the_loop` |
| Solvent regime | `explicit` (implicit is not supported) |
| Sampling per window | user-specified; autonomous default 1 ns (sanity) |
| Ligand symmetry number | 1 unless the ligand has indistinguishable orientations it did **not** visit during the restraint selection run (the `boresch.statistics.reorients` flag); a ligand that turned there gets 1 |

Autonomous default: proceed without asking when exactly one neutral ligand is
kept.

## Model

- **dG_bind = dG(solvent leg) − dG(complex leg) + dG_restraint − kT ln σ**, at
  the 1 M standard state; negative is favourable.
- Each leg switches the ligand off along lambda: charges first
  (`fep_elec_old` 1 → 0), then soft-core Lennard-Jones against everything else
  (`fep_sterics_old` 1 → 0). The complex leg first switches a Boresch restraint
  on (`fep_restraint` 0 → 1) so the decoupled ligand stays in the site;
  `dG_restraint` is its analytic free energy at the standard state.
- A single leg is not a hydration free energy: the charges are annihilated,
  which removes the ligand's intramolecular Coulomb energy too (it cancels
  between the legs).

## Workflow

After every completed node read `next`: it names the next command for this
shape, including the solvent branch and the closing node.

```text
source ─ prep_001 ─ solv ─ topo(decoupled) ─ min ─ eq ─ topo(boresch) ─ fep ─ analyze_001 (analyze_fep) ─┐
            └─ prep_002 (extract_ligand) ─ solv ─ topo(decoupled) ─ min ─ eq ─ fep ─ analyze_002 ──────────┴─ analyze_003 (estimate_binding_dg)
```

1. **Bootstrap** one job and prepare the complex with the ligand kept:

   ```bash
   mdclaw bootstrap_md_workflow --study-dir <study> --pdb-id <id> \
     --question "<binding question>" --sampling-stage fep
   ```

   Follow `skills/md-prepare/SKILL.md` through `solv`; `prepare_complex` needs
   `--include-types protein ligand --process-ligands` and the ligand's SMILES.

2. **Decoupling topology (complex).**

   ```bash
   mdclaw create_node --job-dir <job> --node-type topo --label "decouple_<LIG>"
   mdclaw --job-dir <job> --node-id <topo_id> build_decoupled_system \
     --ligand <LIG> --forcefield ff19SB --water-model opc
   ```

   Read `endpoint_validation.passed` and `leg` (`complex`) in the result. No
   `fep_protocol` is written for this leg yet; that is intended.

3. **Minimise and equilibrate** exactly as in
   `skills/md-equilibration/SKILL.md` (NPT). At its defaults the System is the
   ordinary, fully coupled complex.

4. **Boresch restraint.** A `topo` node **under the eq node**:

   ```bash
   mdclaw create_node --job-dir <job> --node-type topo --parent-node-ids <eq_id> --label "boresch"
   mdclaw --job-dir <job> --node-id <topo2_id> add_boresch_restraint --platform CUDA
   ```

   It samples the complex for 200 ps, anchors three ligand heavy atoms to three
   backbone atoms, and writes the restrained topology plus the 24-window
   protocol. Report the `boresch` block (atoms, reference values, fluctuation
   statistics). A warning that the ligand *turns* in its site (benzene
   spinning in a cavity) is not a failure: the restraint holds the most
   populated orientation and the restrain phase pays for confining it; pass
   `--ligand-symmetry-number 1` at step 7. On `abfe_restraint_unstable` see
   the table below.

5. **Sample and analyse the complex leg** as in `skills/md-fep/SKILL.md`
   step 4–5, with the `fep` nodes **under the topo of step 4**
   (`create_node --node-type fep --parent-node-ids <topo2_id>`; always name the
   parent here). Windows start from the eq state of step 3 and equilibrate at
   their own lambda. Then `analyze_fep` on an analyze node with
   `--conditions '{"analysis_data_scope": "alchemical"}'`; `phases` reports
   `restrain`, `decharge`, `decouple_sterics`.

6. **Solvent leg.** A `prep` child of the complex's `prep` node:

   ```bash
   mdclaw create_node --job-dir <job> --node-type prep --parent-node-ids <prep_001> --label "ligand"
   mdclaw --job-dir <job> --node-id <prep_002> extract_ligand --ligand <LIG>
   ```

   Then, each with `--parent-node-ids` naming this branch:
   `solvate_structure` (`--dist 12` or more) → `build_decoupled_system` (same
   options as step 2; `leg` is `solvent`, 18 windows) → min → eq → `run_fep`
   (fep nodes under the eq) → `analyze_fep`. No restraint step on this leg.

7. **Binding free energy.**

   ```bash
   mdclaw create_node --job-dir <job> --node-type analyze \
     --parent-node-ids <analyze_complex> <analyze_solvent> \
     --conditions '{"analysis_data_scope": "comparison"}'
   mdclaw --job-dir <job> --node-id <analyze_003> estimate_binding_dg [--ligand-symmetry-number N]
   ```

Options that change the physics, identical on both legs unless stated:

| Option | Tool | When |
|---|---|---|
| `--elec-lambdas`, `--sterics-lambdas` | `build_decoupled_system` | a phase shows `min_neighbour_overlap < 0.03`; add values there, on both legs |
| `--restraint-lambdas` | `add_boresch_restraint` | low overlap inside the `restrain` phase (complex leg only) |
| `--sampling-time-ps` | `add_boresch_restraint` | a flexible ligand whose pose needs longer to characterise |
| `--ligand-symmetry-number N` | `estimate_binding_dg` | the restraint confines the ligand to one of N indistinguishable orientations that it never visited during the selection run; refused (`abfe_symmetry_already_sampled`) when the ligand turned there |

## What to report

`dG_bind_kcal_mol ± dG_bind_error_kcal_mol`, the four `terms_kj_mol`, each
leg's `phases` and `min_neighbour_overlap`, total sampling per leg, the Boresch
atoms, and every warning. State that the ligand's analytic Lennard-Jones tail
is not included and that the result is for the prepared pose.

## Codes

| Code | What to do |
|---|---|
| `abfe_ligand_ambiguous`, `abfe_ligand_not_found` | Pass `--ligand CHAIN:RESNAME:RESNUM` from `ligand_candidates`; if the ligand was not prepared, redo `prepare_complex` with it kept. |
| `abfe_charged_ligand_unsupported`, `abfe_ligand_covalent`, `abfe_ligand_too_small` | Out of scope. Report; do not alter the ligand. |
| `abfe_restraint_required` | The `fep` node is under the complex leg's `eq`; do step 4 and create the `fep` node under that `topo`. Retire the misplaced node (`update_workflow_state --abandon`). |
| `fep_equilibration_required` | The `fep` node has no equilibrated ancestor; run min → eq first. |
| `abfe_restraint_unstable` | The ligand leaves the site. Add an `eq → eq` node (longer NPT), then a new `topo` under it with `add_boresch_restraint`. If it fails again, report that the pose is not stable. |
| `abfe_symmetry_already_sampled` | The ligand turned in its site during the selection run; rerun `estimate_binding_dg` with `--ligand-symmetry-number 1` and report unvisited orientations as uncorrected. |
| `abfe_topology_required` | `add_boresch_restraint` runs once, on a `topo` whose parent is the complex leg's completed `eq`. |
| `abfe_receptor_missing` | This is the solvent leg; it takes no restraint. Run `run_fep` under its `eq`. |
| `abfe_ligand_prep_required` | `extract_ligand` needs the complex's `prep` node as parent. |
| `abfe_legs_invalid`, `abfe_scope_invalid` | The closing node is `comparison` over one complex and one solvent `analyze_fep` node. |
| `abfe_legs_incompatible` | Rebuild the solvent leg with the complex leg's options. |
| `fep_endpoint_validation_failed` | Do not sample. Report the energy table. |
