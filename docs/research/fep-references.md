# Hybrid-Topology FEP for single-point-mutation ddG — survey of reference implementations and design decisions

Written: 2026-09-18 to 19 (recorded alongside the `mdclaw/fep/` implementation)

MDClaw gained a feature that computes the "folding-stability ddG of a single amino-acid point mutation" by alchemical FEP.
It does not depend on external FEP frameworks (Perses / OpenFE / pmx);
the hybrid System is assembled with the standard OpenMM API only. This page records the implementations and literature consulted,
and the design decisions taken from or rejected from them. The agent procedure is in `skills/md-fep/`;
the tool contract is in the `fep/` section of `docs/developer/tool-reference.md`.

## 1. Implementations consulted

| Implementation | What was taken from it | What was not adopted |
|---|---|---|
| **pmx** (Gapsys, Seeliger, de Groot; hybrid residue library for GROMACS) | The idea of a single-residue hybrid topology. Dummy-atom bonded terms are kept at full strength at all λ and cancel between the folded / unfolded legs. Dummy side chains are placed from pre-built models (the library). | Nonequilibrium switching (Crooks / BAR) is not adopted in v1; limited to equilibrium λ windows + MBAR. |
| **Perses** (Chodera lab; `HybridTopologyFactory`, `LambdaProtocol`) | A piecewise-linear λ protocol split into 5 global parameters (charges off → sterics swap → charges on). Linear interpolation of core-atom charges, LJ and 1-4 via `addParticleParameterOffset` / `addExceptionParameterOffset` of `NonbondedForce`. | RJMC / multiple mutations, the dependency on `openmmtools` `alchemy`, combination with REST. Perses takes a wide core by MCS, but this implementation restricts it to backbone + CB (§3 below). |
| **OpenFE / feflow** | The quality criterion of endpoint validation (pinning down in tests that the hybrid energy at λ=0/1 matches the original System). | The `gufe` object model and the Protocol/DAG abstraction. MDClaw's DAG plays that role. |
| **GROMACS free-energy code** (`sc-alpha`, `sc-power`, `couple-intramol`) | Beutler-type soft-core LJ. The convention, shared with GENESIS, that dummy-atom nonbonded interactions (including dummy–dummy) vanish completely at the off endpoint. | The equivalent of `couple-intramol=no`, "replace intramolecular dummy–core pairs with explicit exclusions", is unnecessary (they vanish naturally through charge scaling and interaction groups). |
| **GENESIS** (`fep_topology = hybrid`, description of `single`/`dual` topology) | The convention that dummy-atom-to-rest LJ is soft-core, charges are linear, and dummy–dummy interactions vanish at the off endpoint. The idea of holding 5 independent λ components (`lambljA/B`, `lambelA/B`, `lambbondA/B`). | GENESIS also scales bonded terms with λ (`lambbond`). This implementation keeps dummy bonded terms on at all λ and relies on cancellation (the pmx way). |
| **Beutler et al. 1994** (soft-core) | `U = λ·4ε(1/x² − 1/x), x = α(1−λ) + (r/σ)^6` (α=0.5). | — |
| **pymbar 4** (Shirts & Chodera) | MBAR, `timeseries.detect_equilibration` / `subsample_correlated_data`, diagnosing inter-window overlap with the overlap matrix. | — |

## 2. Overall design (DAG)

There is only one new node type, `fep`. Building the hybrid topology is treated as a kind of `topo` node
(`build_hybrid_system`).

```
source → prep_001 → solv → topo(build_hybrid_system) → min → eq → fep(×1..K) → analyze_001(analyze_fep) ─┐
           └→ prep_002(extract_tripeptide) → solv → topo(hybrid) → min → eq → fep → analyze_002 ───────────┴→ analyze_003(estimate_ddg)
                                                                   ↑ extended by fep → fep
```

- `min` / `eq` treat the hybrid System as an ordinary topology. The default values of the global
  parameters are state A (wild type), so equilibration is at λ=0.
- A `fep` node, for each window, runs a short local equilibration from the eq state → sampling, and at each
  sample evaluates the reduced potential of **all windows**, keeping `u_kn` (straight into MBAR).
- The 2 legs of a folding-stability ddG live together in **one job**. The unfolded leg is a
  `prep` child node derived from the folded leg's `prep` (`extract_tripeptide`,
  `leg_role = unfolded`), and structurally shares the source, preprocessing, protonation and `--mutation`.
  The ddG is a `comparison` analyze node (`estimate_ddg`) whose parents are the 2 `analyze_fep` nodes;
  it checks that the settings match between legs (mutation, λ protocol, force field, water,
  HMR, T, P) before taking the difference. No cross-job parent references are introduced (design decision of 2026-09-20:
  it can be expressed with only the existing prep → prep branching and the
  comparison scope, without breaking `read_node`'s same-job assumption).

The "typical flows of pmx / Perses / GENESIS" that were compared are all a single path,
prep → hybrid construction → solvation and force field → equilibration → λ sampling → estimation, and we judged that
they can be expressed by simply inserting `fep` into MDClaw's existing spine.

## 3. Hybrid System construction rules (`mdclaw/fep/hybrid.py`)

| Item | Rule | Reason |
|---|---|---|
| core atoms | The backbone (N, H, CA, HA, C, O) and CB of the mutated residue, plus all atoms of the environment (outside the residue) | Mapping side chains deeply, MCS-style, means interpolating large geometric changes such as sp3↔aromatic ring, and convergence is poor. pmx also treats side chains as dummies in principle. |
| Dummy placement | unique_new atoms are inserted right after the last A atom of the mutated residue (the order of A atoms is preserved) | `PDBFile` assumes a residue's atoms are contiguous. Keeps the indices of `topology.pdb` and the System consistent. |
| PDB names of dummy atoms | Names that do not collide with the A side are assigned by `_unique_pdb_names` | Avoids same-name collisions such as CG in LEU→PHE. The true mapping is in `hybrid_manifest.json`. |
| Dummy bonded | Full strength at all λ (force groups 1 / 2) | They are identical terms in both legs and cancel in ddG. Endpoint validation compares against groups 0+3. |
| core bonded differences | One term if identical; if different, `(1−fep_core)·E_A + fep_core·E_B` via `Custom*Force` | Using `CustomCVForce` for all bonded terms makes the per-step inner-context cost large. |
| CMAP (ff19SB) | Only the mutated residue's CMAP mixes 2 `CMAPTorsionForce`s via `CustomCVForce` | CMAP depends on residue type and cannot be scaled per term. |
| Charges | Dummies: base charge 0 + offset (`fep_elec_old`/`fep_elec_new` × q). core: A→B linear in `fep_core`. | Same as Perses. |
| LJ (dummies) | ε=0 in `NonbondedForce`; a Beutler soft-core `CustomNonbondedForce` is added with a (dummy × rest) interaction group. old and new do not see each other. | Avoids the endpoint catastrophe. |
| 1-4 exceptions | core–core: offset interpolation. core–dummy: charge scaled by elec λ, ε by sterics λ. old–new: 0. | The exclusion sets are made identical across all nonbonded forces (a CUDA requirement). |
| Asymmetric core exceptions | A core pair that is excluded / 1-4 in only one state is interpolated to the ordinary pair interaction via a direct-Coulomb exception (warned as `asymmetric_core_exceptions`) | This exception has no PME reciprocal term, so the endpoint deviates from the original System by the erf part. A path that practically never occurs with a backbone+CB core. |
| Dispersion correction | Disabled in all Systems during endpoint validation. **Known approximation**: dummies have ε=0 in `NonbondedForce`, and the soft-core force has no long-range correction, so the LJ tail of the mutated side chain is missing at all λ (tail_B − tail_A drops out of ΔG) | Small, being a few side-chain atoms, and the same term in both legs, so it cancels in ddG. |
| CONECT in `topology.pdb` | The hybrid residue keeps the WT residue name, so `PDBFile.writeFile` does not write CONECT records for bonds of the added atoms (by the convention that bonds of standard residue names are inferred from templates) | The physics is in `system.xml`; only imaging and visualization are affected. Documented as a known limitation in the `hybrid_topology` docstring. |
| Residue names in `topology.pdb` | Reading an endpoint `topology.pdb` with `PDBFile` normalizes HIE/CYX/ASH/GLH/LYN/WAT to HIS/CYS/ASP/GLU/LYS/HOH, so `restore_topology_resnames_from_pdb` (restores names by atom order) is applied to both endpoint states before deriving the hybrid Topology | In a solvated `topology.pdb`, water uses the same chain and residue numbers as the protein, so restoration by residue key (`restore_resnames_by_residue_key`) is refused because every key is ambiguous. `PDBFile` preserves atom order, so this is exact. |
| Window starting configuration | A window starting from the eq (λ=0) state runs a short `LocalEnergyMinimizer` (200 iterations) at that window's λ before equilibration (`run_fep`) | During eq, appearing atoms are ghosts and solvent moves into their volume, so starting with hard LJ at λ≥0.75 goes non-finite from overlaps. Not needed for windows continuing from a fep parent. On NaN, one retry with the timestep halved (`nan_retry`). |
| Dummy relaxation | After construction, only appearing atoms are minimized in state B (others have mass 0; constraints are replaced by stiff harmonic bonds) | Side chains from HPacker/PDBFixer can be distorted as-is (8×10⁴ kJ/mol for GLY→PRO). |
| Charge-changing mutations | In the mutant endpoint System, the O of the bulk water molecule(s) farthest from the mutation site (|Δq| of them, at most 2) is rewritten to the ion's (q, σ, ε), and the H (and EP for 4-point water) to charge 0, before the hybrid is assembled (`fep/coion.py`, the co-alchemical ion of Chen et al. JCTC 2018). Differences in environment atoms are interpolated linearly by the `fep_core` offset, and the molecule is harmonically restrained to its construction-time coordinates (k = 1000 kJ/mol/nm², force group 4). The ion parameters are copied from a same-sign monovalent ion in the system | The finite-size error from PME's uniform background charge depends on the box size and contents (water number density, low-dielectric solute volume), which differ between the folded and unfolded boxes, so it does not cancel in ddG. Making the box charge the same in both endpoint states removes the error source itself. The Rocklin correction is not adopted because it adds an APBS dependency and judgment points for the representative structure and dielectric constant. The water → ion free energy cancels because both legs have the same bulk environment. Endpoint validation compares against the rewritten mutant System, so the ion end is validated as is. At intermediate λ the box charge is not 0 (the ion is in the core phase, the new side-chain charges in the third phase), but the free energy is determined by the endpoint states alone. If there is no salt or the box is too small, it is refused rather than falling back to uncorrected. |
| Endpoint validation | At λ=0 / λ=1, the difference between the hybrid (group 0 + dummy bonded + 3) and the original System ≤ 1 kJ/mol | OpenFE's quality criterion. Differences around 0.1 kJ/mol in CUDA single precision are normal. |

## 4. λ protocol (`mdclaw/fep/protocol.py`)

A scalar λ∈[0,1] is mapped piecewise-linearly onto 5 components. The boundaries default to λ=0.25 / 0.75
(changeable with `build_hybrid_system --phase-bounds p1,p2`; recorded in `phase_bounds` of `fep_protocol.json`,
and the phase decomposition of `analyze_fep` and `protocols_equivalent` read that value.
Used to lengthen the decharge phase for charge-changing mutations).

| Interval | Components that move |
|---|---|
| 0 → 0.25 | `fep_elec_old`: 1 → 0 (old side-chain charges off) |
| 0.25 → 0.75 | `fep_sterics_old`: 1 → 0, `fep_sterics_new`: 0 → 1, `fep_core`: 0 → 1 |
| 0.75 → 1 | `fep_elec_new`: 0 → 1 (new side-chain charges on) |

The default is 21 evenly spaced windows. `--lambda-schedule` accepts only a strictly increasing sequence of scalar λ from 0 to 1 (CSV / JSON).
Sequences of component dicts are not accepted: adjacent overlap and phase decomposition assume "index order = λ order",
so non-monotonic or non-scalar windows are refused with `fep_protocol_invalid`.
That the system's total charge can be non-integer in intermediate windows is left to PME's neutralizing background; no finite-size correction
is applied (`build_hybrid_system` emits a warning).

## 5. Mutant side-chain modeling (`mdclaw/fep/mutant.py`)

HPacker (via the existing `sidechain_packer.py`) is the default, with PDBFixer `applyMutations` as the
fallback. Only the mutated residue is extracted and spliced **as text** into a copy of the WT PDB,
keeping all other atoms byte-identical (serials renumbered, CONECT remapped).
This makes the WT / MUT `build_amber_system` outputs identical outside the mutated residue,
so the environment-match check of `map_mutation` passes.

## 6. Unfolded-state model (`mdclaw/fep/tripeptide.py`)

Following pmx's folding-stability protocol, a `prep`-stage tool that cuts a capped tripeptide (ACE-X(i−1)-X(i)-X(i+1)-NME)
out of the `merged_pdb` of the folded leg's prep node.
It preserves chain IDs and residue numbers, so the same `--mutation` string works as is (`merge_structures`
reassigns chains starting from A, so it is not used; a 1-component `chain_identity_map` is written directly).
Capping and completing cap hydrogens are left to `clean_protein` (`cap_termini`,
`preserve_input_protonation=True`, default `protonation_method="no-prediction"`),
and the parent prep's protonation variant names (ASH / GLH / HID …) are shared by both legs.

## 7. Analysis (`mdclaw/fep/analysis.py`)

- Concatenate each window's segments (`fep → fep` extensions), discarding the first 10 % of each segment.
- `detect_equilibration` on each window's own reduced-potential series → subsample by the statistical
  inefficiency.
- `Δf` and the overlap matrix by MBAR. Warn on adjacent overlap < 0.03.
- Report the per-phase contributions (decharge / sterics swap / recharge).

## 8. Out of scope for v1 (intentionally excluded)

- Nonequilibrium switching (pmx-style Crooks/BAR).
- Replica exchange / combination with REST (Perses).
- Multiple mutations, insertions/deletions, nonstandard residues, ligand transformations.
- Rocklin-type finite-size correction for charge-changing mutations (a post-hoc correction involving a PB calculation). Instead,
  a co-alchemical ion was introduced (the §3 table, 2026-09-20).
- A skill for binding-affinity ddG. `estimate_ddg --cycle binding` (complex / apo,
  legs named via `analysis_subjects`) landed on 2026-09-20, but there is not yet a prep tool that derives the apo leg
  (ligand removal + `leg_role = apo`) nor a procedure.
- Sampling with positional restraints became possible with `run_fep --restraint-atoms` (2026-09-20):
  the same harmonic restraint (referenced to eq coordinates) in all windows within a leg; it cancels out of the λ-difference
  reduced potentials but does change the ensemble. The practice of applying it only to the folded side is described in the skill.
- The endpoint builder can also select `build_openmm_system` with `--endstate-builder openmm --forcefield-xml …`
  (2026-09-20). Endpoint validation confirmed on vacuum ACE-X-NME.
  The solvated-system path (writing CRYST1 and passing it) was first actually run during the co-alchemical ion validation,
  which fixed a bug where `build_openmm_system` crashed in `PDBFile.writeFile` on the solvent's int chain id
  (same day).

## 9. Validation status

- ACE-X-NME (vacuum, amber14, Reference platform), LEU→ALA, GLY→PRO, LEU→PHE
  and others: endpoint difference < 1e-3 kJ/mol (`tests/test_fep.py::TestHybridVacuum`).
- 20-residue peptide A:W6A, ff19SB/OPC, 8.5k atoms: endpoint difference 0.12 kJ/mol (CUDA);
  both legs 21 windows × 0.2–0.25 ns give ddG = +4.67 ± 0.49 kcal/mol (`docs/memo.md`).
  W6A is large→small and its only appearing atom is HB1, so the window-starting-configuration problem
  does not surface in this system.
- End-to-end in direct mode through `run_fep` → `fep_windows.json` → `analyze_fep` with 3 windows of vacuum ACE-LEU-NME → ALA
  (relative paths, recovery from a partial index, segment concatenation,
  re-analysis after moving the directory; `tests/test_fep.py::TestVacuumPipeline`).
- MBAR reproduces the analytical solution on a harmonic-oscillator toy problem (`tests/test_fep.py::TestAnalysis`).
- For the small→large smoke (A:A6W, solvated tripeptide), see the entry for that date in
  `docs/memo.md`.
