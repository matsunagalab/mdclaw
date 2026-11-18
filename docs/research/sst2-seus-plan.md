# Implementation plan for SST2-based expanded ensembles (SST2 / SEUS) — revised edition

Written: 2026-09-16 (Opus's draft revised by Claude after hands-on investigation of the SST2 code and the MDClaw code. The same day, the boundary-term convention was confirmed in the gREST paper, and the point that no Abl–abltide PMF exists was incorporated)

Add two single-walker expanded ensembles to MDClaw.

- (A) **SST2** (Simulated Solute Tempering 2): REST2-type sampling with the solute restricted to a
  subregion such as CDR-H3. CV-free. The main workhorse.
- (B) **SEUS** (Serial / Expanded-ensemble Umbrella Sampling). An expanded-ensemble version of REUS in which
  a single trajectory walks the umbrella window index. Limited to uses where the CV is clear.

The target is data generation for a VHH / CDR-H3 surrogate model, but the design should also work for existing systems.

The key points of the revision (changes from Opus's draft) are summarized in §0. The rest follows the same chapter structure as Opus's draft,
with the confirmed facts and corrections woven in.

---

## 0. Main corrections to Opus's draft

| # | Opus's draft | Findings and correction |
| --- | --- | --- |
| 1 | Specify the residue range with `-solute_sel` | The CLI flag is **`-select`** (`dest="solute_sel"`). Default `"chain A"`. The selection string uses `pdb_numpy` grammar |
| 2 | An H3-only solute "needs no new implementation, only verification" | **Implementation is needed.** With Amber-family force fields, 1-4 pairs crossing the boundary (exceptions of `NonbondedForce`) are **not scaled at all**, and moreover the energy decomposition treats them as √λ terms. `LJ14_solute_boundary` is only for CHARMM's `CustomBondForce`. In addition, the PDB reconstruction of the sub-Systems creates **spurious peptide bonds** between the residues flanking the solute (§2.4). Both were fixed in the fork on 2026-09-16 and locked in with tests (§4) |
| 3 | SST2 can be loosely coupled via CLI calls | `bin/launch_SST2_pdb.py` does everything itself from the PDB: pdbfixer → solvation → System construction by force-field name. It cannot accept MDClaw's `system.xml + topology.pdb + state.xml`. Moreover, the `REST2` class **requires an `openmm.app.ForceField` object** and builds the solute-only and solvent-only sub-Systems with `forcefield.createSystem()`. A prmtop-derived System cannot be passed to it (§2.3). → Add to the fork a **copy-based sub-System builder** and a **driver that accepts the XML triple**, and have MDClaw call it as a subprocess |
| 4 | REUS version: "the converged weights = the sign-inverted PMF" | Precisely, the weights g_m are the **per-window free energies f_m** (the PMF convolved with the Gaussian of the bias). The PMF itself is obtained with pymbar MBAR from the saved (ξ_t, m_t) series (the point that no energy re-evaluation is needed is correct). Using the stability of the weights as a convergence monitor is correct (§5) |
| 5 | Acceptance criterion "checked on paper" | Checked. The current "shuffle the two neighboring candidates and test them in turn with a single random number r" is **not π-invariant** (§2.5). Since Δ is linear in λ^f, replacing it with Gibbs (independence) sampling over all rungs is correct at zero cost. The ST version in `st.py` also uses a different nonstandard rule (testing from the higher-probability candidate first) |
| 6 | Compare against the Abl–abltide umbrella PMF | That PMF does not exist (confirmed by the user). The SEUS reference is built with MDClaw's existing umbrella route + pymbar from the same system and the same windows (§6) |
| 7 | Alanine dipeptide φ can serve as an analytical comparison | It cannot. Analytical solutions exist only for a 1D artificial potential (a double well in `CustomExternalForce`). Alanine dipeptide is compared against ordinary umbrella + MBAR |
| 8 | Use the REMD Temperature Generator for the number of rungs | SST2 itself has `tools.compute_ladder_num()` (the Denschlag 2009 formula, estimated from energy fluctuations in the REST2 equilibration CSV), and the launch script uses twice that as a rule of thumb. No web tool is needed |
| 9 | Add a new `enhanced_ensemble` node type | MDClaw has a precedent: steering / umbrella are **already implemented as `prod`-labeled nodes (no node types added)** (memo 2026-09-06). A new node type requires registration in 15 places. Following the precedent, implement it as a `prod` node + a `sampling_method` condition (§7) |
| 10 | Write seus.py inside the SST2 fork | SEUS does not use REST2's energy decomposition at all. Writing it on the MDClaw side (MIT) with `CustomCVForce` + weight adaptation + Gibbs updates is more straightforward for both the GPL boundary and reuse (the sidecar approach of `restraints.py` / `steering.py`). Porting upstream will be proposed later |
| 11 | Phase 1 takes 1–2 weeks | **2–3 weeks**, since the sub-System builder, the boundary 1-4 fix, and the acceptance-rule fix are added |
| 12 | (not described) | Confirmed the boundary-term convention in the gREST paper (Kamiya & Sugita 2018, Eq. (3)): (β_m/β_0)^(k/l), with l = 2 for bonds/LJ/Coulomb, 3 for angles, 4 for dihedrals/impropers, and 8 for CMAP. SST2's k/4 for dihedrals and k/8 for CMAP **agree**. Boundary 1-4 is k=1, l=2, giving √λ. gREST also scales boundary bonds, angles, and impropers by k/l, whereas SST2, following Stirnemann–Sterpone, does not scale bonded terms other than proper torsions. Restricting `param_type` on the GENESIS side to dihedrals + LJ + Coulomb gives the same potential (§2.4) |

Assumptions on the MDClaw side (confirmed in the investigation): package 0.6.8, OpenMM ≥ 8.5.1, pymbar 4.2, ParmEd present,
`openmmtools` and `pdb_numpy` absent. Bias forces are in force group 31; the CV CSV and `.meta.json`
(already designed on the assumption of reconstructing pymbar input), the `steering.json`-style sidecar, and Slurm job arrays already exist.
`_load_state_into_simulation` does **not restore** the Context's global parameters (to allow NPT↔NVT switching).

---

## 1. Background and approach

(Unchanged from Opus's draft. Summary only)

- **Make the CV-free method the main workhorse.** The slow coordinates of CDR-H3 are spread across backbone dihedrals, the torso kink, H3–framework
  packing, salt bridges, and the H3-derived disulfide of VHHs, and there is no guarantee that a CV can be chosen in advance.
  With a CV-based method, missing coverage in orthogonal directions cannot be detected, which is the worst case for surrogate training data.
- **Use a simulated-tempering type.** What is needed is a weighted equilibrium distribution that can be reweighted with MBAR.
  A single trajectory needs no communication, so N independent runs = N independent nodes, which fits MDClaw's DAG and
  MPS packing on GB200 directly. The grounds are Rosta & Hummer's "parallel communication-free ST is equivalent to REMD".
- The REUS side is limited to uses where the CV is clear, such as binding free energies.

---

## 2. Current state of SST2 (2026-09-16, confirmed by cloning commit `31c76a4`)

Repository: https://github.com/samuelmurail/SST2 (GPL-2.0, 23 stars, no CI)
Paper: Stratmann, Moroy, Tuffery, Murail, *JCTC* **21**, 10705 (2025). https://pubs.acs.org/doi/10.1021/acs.jctc.5c00950
Latest commit `31c76a4` (2026-07-09, "implement energy recomputation script").

### 2.1 The implementation is ahead of the paper (confirmed)

- `solute_index` is an arbitrary list of atom indices. The CLI is `-select` (default `"chain A"`).
- `-only_dihed` (gREST style, scaling dihedrals only), `-nonbonded_RF`, and `-exclude_Pro_omega` exist.
- **k/4 fractional buckets** for dihedrals: a separate `CustomTorsionForce` for each number of solute atoms k=1..4,
  scaled by the global parameter `lambda_{k}_4 = λ^(k/4)`. CMAP has 8 buckets of k/8.
  The boundary 1-4 `LJ14_solute_boundary` (√λ) is **only for CHARMM's `CustomBondForce` LJ14**.
- There is CMAP separation and NBFIX scaling, so the README's "CHARMM36 not supported" is outdated.
- The paper's future work explicitly mentions "an implementation that scales only dihedrals / CMAP, gREST style".
  Murail himself is moving in the gREST direction, so a partial-solute proposal is likely to be well received.

Numbers confirmed from the paper: 10 rungs for small systems (λ 1.07 → 0.56; **there are rungs colder than T_ref**),
exchange interval 2 ps, friction 1 ps⁻¹, weight convergence 0.5–1 µs for CLN025 and 2–10 µs for Trp-cage,
OpenMM ≥ 8.3.1 recommended for charged solutes (99.9 % of exchanges agree with and without the background-charge correction).

### 2.2 Code layout

| File | Lines | Role |
| --- | --- | --- |
| `src/SST2/rest2.py` | 2167 | REST2 core. Solute definition, fractional buckets, sub-Systems, energy decomposition, `run_rest2` |
| `src/SST2/sst2.py` | 718 | SST2 driver (`SST2Reporter` handles running-average updates and exchange attempts) |
| `src/SST2/st.py` | 532 | ST (derived from Eastman's `SimulatedTempering`) |
| `src/SST2/sst1.py`, `rest1.py` | 577, 1352 | SST1 / REST1 |
| `src/SST2/tools.py` | 1407 | Preparation, `compute_ladder_num`, `get_fastest_platform_name`, etc. |
| `src/SST2/analysis/` | — | `data_plot.py` (weight RMSD, rung occupancy, exchange probability), `trajectory.py` (not in Opus's table) |
| `src/SST2/tests/` | 1147 | 4 in `test_rest2.py` + 1 in `test_sst2.py`. All use **the whole chain as the solute**. 1.1 MB of input; runs on CPU |
| `bin/` | — | `launch_SST2_pdb.py`, etc. `recompute_energy_traj.py` recomputes energies along a trajectory |

Defects in `pyproject.toml`: the classifier is BSD while `license = {text="GNUv2.0"}`;
`[project.scripts] SST2 = "SST2.__main__:main"` but `__main__.py` does not exist (the console script is broken).
Dependencies are `openmm>=7.7`, `pdb_numpy`, `pandas`, `pdbfixer`.

### 2.3 What the energy decomposition actually does (a constraint on MDClaw integration)

In PME mode it holds 3 Contexts: the main one, solute-only, and solvent-only. At each exchange attempt it
copies coordinates into the sub-Systems, evaluates them, and divides E_pw = E_nb(all) − E_nb(solute) − E_nb(solvent) by √λ.

- The sub-Systems are built with **`forcefield.createSystem(pdb_subset.topology, ..., ignoreExternalBonds=True)`**.
  RF mode also rebuilds the whole system with `forcefield.createSystem`. **`app.ForceField` is required.**
- The PME α is only read from the main System; `setPMEParameters` is not applied to the sub-Systems. The sub-Systems' α is
  determined from the cutoff and error tolerance, so **the decomposition goes wrong unless the cutoff / tolerance match the main System**.
- With a partial solute, the sub-Systems are built from a PDB of fragments cut out of the chain. If the cuts are at residue boundaries,
  `ignoreExternalBonds` should match internal residue templates. Cutting inside a residue will not match.

MDClaw's Systems are built by `build_amber_system` (prmtop-derived, no ForceField object) or by
`SystemGenerator` (with a ForceField including GAFF templates), and the run side only deserializes
`system.xml` without reconstructing a ForceField (the CLAUDE.md contract).
Therefore **a builder that creates sub-Systems by copying particle subsets from the main System** is needed.
With the copy approach, α, grid, and cutoff are also identical to the main System, and the fragility above disappears. ParmEd's
`Structure[mask].createSystem()` could be used instead, but the copy approach gives a stronger guarantee of matching PME parameters.

### 2.4 Correctness holes found with a partial solute

`find_solute_nb_index()` collects **only solute–solute exceptions**, and `update_nonbonded()` multiplies
their chargeProd and ε by λ. 1-4 exceptions crossing the boundary (in Amber, exceptions of `NonbondedForce`)
are not collected and stay at λ=1. Meanwhile, solute atom charges are multiplied by √λ, so the potential departs from
the intended REST2 potential (boundary 1-4 at √λ). Moreover, `compute_all_energies()` includes
those unscaled terms in E_pw and divides by √λ, so **the acceptance energy is self-inconsistent at λ≠1**.
The magnitude is small, a few 1-4 pairs at the 2 boundaries, but it is the kind of error that silently distorts the distribution.

Fix: collect the boundary exceptions in a separate list and multiply their chargeProd and ε by √λ. The E_pw decomposition is
(all NB − solute NB − solvent NB)/√λ, so once the boundary terms are scaled by √λ no extra accounting is needed.
**Fixed on 2026-09-16 on the fork's `mdclaw` branch** (see the measurements in §4).

**One more hole (found the same day): spurious peptide bonds in the sub-Systems.** The sub-Systems were built by writing the Topology
cut out with Modeller to PDB text once and reading it back with `PDBFile`. `PDBFile` bonds C–N between residues that are consecutive
within a chain, so in the chain with the solute removed, the two residues flanking the solute are treated as adjacent,
and a spurious peptide bond (in the test system ASP1:C–GLY5:N, 0.82 nm, 96,000 kJ/mol) plus 15 1-2 / 1-3 / 1-4
exceptions enter the solvent sub-System. Not only are the bonded terms of E_ww broken, but a λ-independent
constant of 17.2 kJ/mol leaks into E_pw, which, divided by √λ, makes the acceptance energy λ-dependent. The fix is to build with `createSystem`
directly from Modeller's Topology (which keeps the original bonds and box) without going through PDB. Fixed.
For a VHH H3, the same spurious bond would have formed between residues 94 and 103.

The λ^(k/4) convention for dihedrals **agrees** with Eq. (3) of the gREST paper (confirmed in the attached PDF). gREST uses
E_m = λ E_uu + Σ_i λ^(k_i/l_i) E_uv,i + E_vv, where l_i = 2 for bonds / LJ / Coulomb, 3 for angles,
4 for dihedrals/impropers, and 8 for CMAP, and k_i is the number of particles on the solute side. Boundary 1-4 LJ / Coulomb is k=1, l=2,
giving √λ, the same as the fix above.

A difference from GENESIS gREST remains: gREST also scales bonds (k/2), angles (k/3), and impropers (k/4) crossing the boundary,
whereas SST2, following Stirnemann & Sterpone, **scales only proper torsions among the bonded terms**
(bonds, angles, and impropers are unscaled as E_pp⁽²⁾). Both are consistent expanded ensembles, so
MBAR reweighting to λ=1 is correct, but when comparing with GENESIS in Phase 3, restrict GENESIS's
`param_type` to dihedrals + LJ + Coulomb so that both use the same potential before comparing.

### 2.5 Acceptance criterion and weights (the abstraction boundary for replacement)

`sst2.py` is split into 4 stages, and the abstraction boundary in Opus's draft is correct.

1. `SST2Reporter.report()` calls `compute_all_energies()` and updates the per-rung running averages
2. `_attemptTemperatureChange()` computes the log acceptance probability for the neighboring rungs
3. If accepted, `rest2.scale_nonbonded_torsion(λ)`
4. `_compute_weight(i, j)` computes w_j − w_i from the running averages using Park & Pande's trapezoidal rule

Confirmed facts:

- Acceptance formula Δ_ij = Σ_t (λ_i^f − λ_j^f) E_t + (λ_i^½ − λ_j^½) E_pw + (w_j − w_i),
  divided by kT_ref. Both sign and units agree with the paper.
- **The weight array `_weights` is never updated**; the difference is recomputed from the running averages each time (on-the-fly approach).
  The `weights` property always returns 0, and **the effective weights are not logged**. On restart, the averages are
  reconstructed from the CSV. There is a mode that passes fixed weights via `weights=`, but combining it with restart has a hole
  (the code contains "TO CHANGE ! This is BAD MOKAY").
- **The acceptance loop does not satisfy detailed balance.** It shuffles the probabilities p_a, p_b of neighboring candidates a, b
  and tests `r < p` in turn with a single random number r. The transition probability i→a is
  ½ p_a + ½ max(0, p_a − p_b), which depends on the other neighbor's p_b. At an end rung there is only
  one candidate, so the proposal probability is 1, asymmetric with the proposal probability ½ from the inside. Hence the joint distribution π(X, m) is not
  an invariant distribution. Since Δ is linear in λ^f and comes for free for all rungs, the fix is to replace it with **Gibbs
  (independence) sampling over all rungs** (Chodera & Shirts 2011; the same as Eastman's original implementation).
  Submit a PR upstream with a test that verifies the stationary distribution on a 3-state toy model. Apply the same fix to `st.py`.
- Random numbers come from the `random` module, unseeded. Pass a seed through for node reproducibility.
- Samples from the period when the weights change adaptively are not strictly at equilibrium. Use a **two-stage protocol: adaptive stage (weight learning) →
  fixed-weight stage (production)**, and apply MBAR to the samples of the fixed stage.
  Keep estimating weights in the production stage too, and use it as a convergence monitor.

### 2.6 Why SEUS is lighter than SST2 (unchanged from Opus's draft, correct)

Only the bias term differs between windows, so only the CV value ξ is needed. Acceptance is
`log P(i→j) = −β[w_j(ξ) − w_i(ξ)] + (g_j − g_i)`, and w is harmonic, so it is analytical.
No energy re-evaluation, no sub-Systems, and no PME handling are needed.

---

## 3. Phase 0 — fork and licensing (1 hour + email)

### Licensing

SST2 is GPL-2.0 and MDClaw is MIT. Bundling in the conda environment / SIF is fine as mere aggregation under GPL-2 section 2.
What to avoid is MDClaw itself doing `import SST2` and becoming a single program.

**Opus's draft "just call the CLI" does not work with the upstream CLI** (§2.3). Revise the approach as follows.

- Write the OpenMM-level connection MDClaw needs (loading the XML triple, copy-based sub-Systems,
  writing state to a sidecar) under GPL as **a driver under the fork's `bin/`**.
  Example: `bin/sst2_from_xml.py --system system.xml --topology topology.pdb --state state.xml
  --solute-indices idx.json --ladder ladder.json --out-dir ...`
- The MDClaw-side `run_sst2` tool prepares the inputs, calls it as a subprocess, and records the artifacts in the DAG.
  This is the same pattern as calling AmberTools. They are separated by the process boundary.
- The solute selection is resolved on the MDClaw side from the mdtraj DSL (as in `restraints.py`) to atom indices and
  passed in. The fork's driver must not depend on `pdb_numpy` (not in the MDClaw environment). When SST2 is put
  into the SIF, `pdb_numpy` comes along too (an upstream dependency), but the driver does not use it.
- SEUS is written on the MDClaw side under MIT (§5). Weight adaptation that we want to share (SAMS etc.) is our own work, so
  write it under MIT; it can be brought into the fork under a dual license as its author.

### fork

- Create `matsunagalab/SST2` as a real GitHub fork (do not detach).
- `main` is for syncing with upstream; work happens on feature branches such as `mdclaw`.
- Pin pip installs to a tag / commit: `pip install git+https://github.com/matsunagalab/SST2.git@<tag>`
- Version numbers take the form `0.0.1+mdclaw.1`. Set `__version__` in `sst2.py` to the same value.
- Record the commit hash bundled into the SIF, and keep `LICENSE` and the credits inside the image. Publish the modified version under GPL-2.0.

### Contributing back upstream (not now)

Decide on upstream PRs / issues / emails after the fixes work and the tests pass in Phase 1.
Until then, keep the work confined to the fork's feature branch. Just record the candidates to submit at that point.

- Minor: the license classifier in `pyproject.toml` (BSD → GPL-2.0), the console script pointing to the nonexistent `SST2.__main__`,
  and the README statement that CHARMM36 is not supported.
- Substantive: boundary 1-4 exceptions are not scaled with a partial solute (§2.4), and the acceptance loop does not satisfy
  detailed balance (§2.5). Submit both only once they are ready with reproduction tests and fixes.
- The paper's future work is gREST, so the partial-solute direction should be well received.

---

## 4. Phase 1 — implementation and verification on the REST2 side (2–3 weeks)

Opus's draft had "verification only", but the implementation from §2.3–2.5 is added.

### Implementation (fork side)

1. **Copy-based sub-System builder** (~200 lines). Given a set of particles from the main System, create a new System with the particles, constraints,
   Bond / Angle / Torsion / CMAP (terms whose atoms are all in the set), and `NonbondedForce` (particles and exceptions,
   copying PME α, grid, cutoff, and tolerance). Make it selectable with `REST2(subsystem="copy")`,
   keeping the upstream-compatible `"forcefield"` as the default.
   Verification: on the upstream test system (5awl), the sub-Systems' energy terms agree between the two approaches.
2. **√λ scaling of boundary 1-4 exceptions** and accounting them in the frac=0.5 bucket (~40 lines).
3. **Gibbs acceptance** and seeding (`sst2.py`, `st.py`).
4. **Logging the weights** (the effective g_m at each exchange attempt) and switching to the **fixed-weight stage**.
5. **A driver that accepts the XML triple** and the sidecar (current rung, running averages, attempt counts, random state, stage).

### Measurements on 2026-09-16 (fork `mdclaw` branch, 2HPL test system, CPU)

Added `src/SST2/tests/test_rest2_partial_solute.py` (solute: residues 2–4 of chain B, residues 11–20 of chain A).
On upstream HEAD all 3 fail; after the fix all 4 pass. The 5 upstream tests give the same results before and after the fix
(3 pass, 2 fail; the 2 failures are because commit 89a4de6 made `E_solvent` no longer include the solvent NB while
the tests were not updated, and are unrelated to the partial solute).

| State | Shift of the "unscaled" part of E_pw from λ=1 → 0.5 (chain B residues 2–4) |
| --- | --- |
| Upstream HEAD | −1132.5 → −1020.1 kJ/mol (112 kJ/mol, 10 %) |
| Boundary 1-4 at √λ | −1132.5 → −1125.4 kJ/mol (7 kJ/mol, 0.6 %) |
| + sub-Systems built directly from the Topology | Unchanged within relative 1e-4. Difference between the solvent sub-System and the ww part of the whole system 17.2 → 0.0001 kJ/mol |

When the whole chain is the solute (the upstream usage), the energies do not change before vs. after the fix (0 boundary exceptions,
no spurious bonds). Example bucket counts: for chain B residues 2–4, 1/4: 5, 2/4: 10, 3/4: 11, 4/4: 152, unscaled 15.

All 5 fork-side implementation items were finished the same day (branch `mdclaw`, 6 commits, 19 tests pass,
the 2 upstream-derived tests still the known failures):

- `SST2/subsystem.py`: the copy-based sub-System builder. With `REST2(subsystem="copy")` it works
  without a ForceField. It copies the PME settings wholesale, so the solute sub-System's NB energy
  agrees within 0.05 kJ/mol with the whole-system value with the solvent zeroed, and agrees term by term with the forcefield approach.
- `sst2.py`: acceptance changed to Gibbs sampling over all rungs (`move="neighbor"` also allows symmetric-proposal
  Metropolis). `seed`. Made the fixed weights of `weights=` actually take effect (previously the running averages were
  not initialized so the reporter crashed, and the given weights were not used for acceptance). The effective weights f_m − f_0 are
  output as columns in the report CSV.
- `SST2/driver.py` + `bin/sst2_from_xml.py`: runs one walker from `system.xml + topology.pdb + state.xml` and a solute
  atom-index JSON. Writes a sidecar JSON (rung, ladder, effective weights, per-rung
  running averages and counts, seed, provenance), and continues the same walker with `--restart-json`.
  `--weights-json` for the fixed-weight stage, `--pressure-bar 0` for NVT.

**Check on a real system (same day, 1KXV)**: confirmed `subsystem="copy"` with MDClaw topo artifacts left over from the campaign (prmtop-derived,
HMR 4 amu, 53,716 atoms, prod's `state.xml`). CDR-H3, from the sequence, is bracketed by
Kabat Cys92 (Cys95 of YYC) and WGQG (Trp111): the 13 residues 98–110 (including Cys106;
1KXV has an H3–CDR1 Cys106–Cys30 disulfide, so taking the last Cys would be wrong).

| solute | Atoms | Charge | Boundary exceptions | Buckets 1/4, 2/4, 3/4, 4/4 | λ drift of the unscaled quantity | Closure |
| --- | --- | --- | --- | --- | --- | --- |
| H3 only | 198 | +1 | 40 | 14, 12, 14, 567 | ≤ 2.4e-6 | 1e-9 |
| H3 + 5 Å shell (including Cys30) | 443 | 0 | 165 | 57, 55, 55, 1219 | ≤ 1.8e-6 | 1e-9 |

Trial run on GPU (1 GB200, CUDA) with 5 rungs (300/357/424/505/600 K), 2 ps exchange, 20 ps:
566 ns/day (including exchanges and sub-System evaluation). The walker climbed from 300 K to 600 K in 20 ps
(the initial on-the-fly weights move a lot, so this is expected behavior for the adaptive stage).
The solute selection and shell generation scripts are in the fork's `examples/mdclaw_1kxv/`.

**MDClaw side (same day)**: added the `run_sst2` tool to `mdclaw/simulation/tempering.py`. As a `prod` node with
`@node_tool(node_type="prod")`, it takes the solute as an mdtraj DSL or JSON, and the ladder, exchange interval, fixed weights, and NVT/NPT as
arguments, and runs the fork at `MDCLAW_SST2_HOME` as a subprocess via `python -m SST2.driver` (MDClaw does not
import SST2). Artifacts are `trajectory.dcd` / `energy.dat` / `state.xml` / `final_structure.pdb` /
`tempering.csv` / `tempering.json` / `solute_indices.json` / `sst2_driver.log`. `--continue-from` automatically picks up
the parent's sidecar and continues the same walker; `final_step` is cumulative. 8 guardrail codes registered.
Tests in `tests/test_tempering.py` (unit + integration tests with the fork: standalone run, run and continuation in node mode,
sealing of failures). Updated the skill `skills/md-production/sst2.md`, `tool-reference.md`, and `configuration.md`.
This gives the skeleton of the node integration part of Phase 4. What remains is the convergence check and MBAR reweighting on the `analyze` side.
SST2 is bundled in the SIF / Docker / conda: the fork's commit
(5590f4f, version `0.0.1+mdclaw.1`) is pinned via pip in `environment.yml` and both Dockerfiles, and declared with `MDCLAW_SST2_REVISION`. Bundling GPL-2.0 is
aggregation, and MDClaw calls it only via subprocess. `MDCLAW_SST2_HOME` is a development override.
The image `mdclaw-rikyu-arm64-cuda130-cufft121-sst2-0a15ed1cf7d1.sif` (2026-09-16, overlay build,
`run_sst2` GPU acceptance passed on the 1KXV DAG) has been placed in `/data1/$PROJECT`. The shared-name symlink stays at v2fix until the campaign ends.

### Verification items

- At λ=1, all energy terms agree with plain MD. The upstream `check_decomposition` (`test_rest2.py`) checks the closure of
  E_rebuilt = Σ λ^f E_t + E_pp⁽²⁾ + E_ww + √λ E_pw against the Context's energy.
  Make this pass with a **partial solute** (e.g. residues 3–7 of 5awl chain A) at λ=0.6 as well. It should fail before fix 2
  and pass after. This becomes the regression test.
- Count the k/4 bucket assignment of boundary dihedrals by hand at the VHH H3 boundary and cross-check.
- Count the number of boundary exceptions by hand and cross-check.
- Compare PME and RF acceptance rates for a charged solute (H3 is almost certainly charged). OpenMM is MDClaw's 8.5.1.
  If possible, extend or shrink the H3 selection residue by residue to reduce the net charge.
- It runs in NPT (`setup_simulation` adds a barostat), so the PME background term, through which the system's net charge
  changes with λ, becomes volume-dependent. The paper reports an effect below 0.1 %. To be safe, also run one in NVT and compare.
- For the number of rungs, apply `tools.compute_ladder_num(..., sst2_score=True)` to the REST2 equilibration CSV, and,
  following the launch script, start from twice that. With an H3-only solute of 200–300 atoms, few rungs are needed.
- Mechanically confirm in the fork's CI (adding GitHub Actions) that the 5 upstream tests pass on CPU.

### How to cut the solute (compare in the VHH trial run)

With only H3 as the solute, H3 internals scale by λ, H3–framework by √λ, and framework–framework by 1.
The VHH H3 often folds over FR2 (the former VL interface, Kabat 37 / 44 / 45 / 47), and that contact
is weakened only by √λ. If H3's slowness lies in H3–FR2 packing or contacts with H1 / H2,
making "H3 + contact residues" the solute, in the style of gREST binding sites, is more effective. Being able to keep the framework cold
is an advantage for surrogate data (the H3 ensemble within the native framework).

Compare the following 3 options on one VHH such as 1KXV. All cut at residue boundaries. **The main trial run builds the parts other than the run
with MDClaw skills too**: plan the study and `jobs/<solute condition>` with `md-study`, build each job up to `eq` with `md-prepare` →
`md-equilibration`, and submit `run_sst2` as a `prod` node to Slurm with `hpc-run`
(an independent node per seed, extended with `continue_from`). Direct use of the campaign's artifacts is limited to
the SIF acceptance smoke test; convergence measurements are recorded in the skill-driven DAG.

1. **H3 only** (equivalent to Kabat 95–102).
2. **H3 + contact shell**: residues within 5 Å of H3 heavy atoms. If there is a disulfide from H3, always include the partner Cys
   (the S–S bond and angles are unscaled anyway, but this avoids the dihedrals around the S–S falling into k/4).
3. **Dihedrals only** (`-only_dihed`, gREST's dihedral-only): charges do not change, so the PME
   net-charge issue disappears. H3–framework packing is not weakened, so it will not help if that is rate-limiting.

Comparison metrics are the λ=1 closure check, per-rung acceptance rates, the number of H3 backbone dihedral transitions,
and the number of rung round trips. Solute atom counts are expected to be 150–250 for 1 and 300–500 for 2, with 4–8 rungs.
For both 1 and 2, extend or shrink the selection residue by residue to keep the net charge small.

### Deliverables

Evidence that "the H3-only solute is constructed correctly", and a regression test that makes it permanent.
A decision on which of the 3 options above becomes the production solute definition, and its rationale.
And **a measurement of the time weight convergence takes**. Against 0.5–1 µs for CLN025 and 2–10 µs for Trp-cage, how many µs VHH H3 takes
will be known only here. If it exceeds 10 µs, reconsider the approach.

The entry point for analysis is `analyze_tempering` (§7 convergence criteria). Run it every 50 ns block, and record the point at which `verdict` becomes
`weights_converged` as "the time weight convergence takes".

---

## 5. Phase 2 — SEUS implementation (2–3 weeks, MDClaw side)

Write `mdclaw/simulation/expanded_ensemble.py` (tentative) under MIT. REST2's decomposition is not used.

**State representation**: reuse the existing `CustomCentroidBondForce` / `CustomCVForce` from `restraints.py`, and
make the center ξ₀ and force constant k global parameters. Switching windows is 2 calls to `context.setParameter()`.

**Acceptance criterion**: compute the log weights `−β w_m(ξ) + g_m` of all windows analytically, and choose the next window by Gibbs sampling
(there is no reason to restrict to neighbors; window jumps are naturally limited to what the free energy allows).

**Weight updates**: make two schemes selectable.
- Park & Pande type: trapezoidal rule g_{m+1} − g_m = β⟨w_{m+1} − w_m⟩ (the same structure as SST2).
- **SAMS** (Tan 2017, Rao-Blackwellized update): asymptotically optimal and fast when there are many windows. About 100 lines, written in-house.
  `openmmtools` is conda-only and not in the environment, so do not depend on it.

**Corrected definition of the deliverable**: the converged g_m are the window free energies f_m. The PMF F(ξ) is obtained from the saved
(ξ_t, m_t, k, ξ₀) with **pymbar 4.2's MBAR** (available in the environment; beware the 3 pitfalls of `generate_fes`
in memo 2026-08-26). The time evolution of g_m is used for the convergence decision.

The two-stage **adaptive stage → fixed stage** protocol, seeds, and sidecar share the design with SST2.

**Toy verification (CPU, self-contained in the container)**
1. A 1D double well in `CustomExternalForce`: the PMF is analytical. With 10–20 windows, the MBAR PMF should agree.
2. Alanine dipeptide φ: agreement with ordinary umbrella (the existing `steered_X → umbrella_X` route) + MBAR on the same windows.

---

## 6. Phase 3 — verification (2–3 weeks, most important)

The real risk is that it runs even when it is wrong. Do not skip this.

### SEUS side

- The reference is built by running MDClaw's existing umbrella route (`steered_X → umbrella_X`, native
  `CustomCentroidBondForce`) on the same system, same CV, and same windows, and computing the PMF with pymbar.
  The Torch bias path is 2× slower (memo 2026-08-26), so it is not used.
- Systems in two tiers: alanine dipeptide φ (shared with the Phase 2 toy verification) and one realistic-size system
  (e.g. a peptide–protein complex with a binding-distance CV, on the scale of hundreds of ns on GPU).
  **Status as of 2026-09-16/17 (see memo)**: end-to-end distance of capped Ala3 (the 3 methods agree to 0.3–1 kJ/mol over
  0.6–1.5 nm; below 0.55 nm it is undetermined because φ₁'s α_R/α_L is a hidden slow variable), deca-alanine
  (flat along ξ; helix content is a hidden slow variable and trapped the walker for 150 ns), and chignolin CLN025's
  Cα1–Cα10 distance (a real barrier along ξ, running). The p53–MDM2 dissociation distance needs a flat-bottom (funnel) restraint
  that defines the unbound state, so it waits until a cylindrical restraint of the `flat_bottom_radius_nm` type is added to
  `distance_restraints`.
- Run SEUS with the same CV and window placement, and check whether the MBAR PMF agrees within statistical error. Also record per-window residence times and
  round-trip counts, and compare against the total cost of ordinary umbrella.

### REST2 side (GPU)

On one VHH, run
- SST2 (H3-only solute, equivalent to rung × 200 ns, MBAR on the fixed-weight stage only)
- 2–3 µs of ordinary MD
- **GENESIS gREST** (same H3 selection, with the boundary dihedral convention aligned)

and see whether the H3 backbone dihedral distributions and free energy surfaces agree. GENESIS is an independent implementation, so it
serves as the reference for whether the fork-side modifications are correct. If these do not agree, no number of runs accumulated afterward will make training data.

---

## 7. Phase 4 — integration into MDClaw (1–2 weeks)

### No new node types

Follow the precedent of memo 2026-09-06 (steering / umbrella use the `prod` labels `steered_X → umbrella_X`,
extended with `continue_from`). A new node type would touch the 5 tables in `node/constants.py`, `inputs.py`,
`lifecycle.py` (`continue_from` is `prod`-only), `prod_chain.py`, `_receipt.py`, `_envelope.py`,
`guardrail_codes.py`, visualization, the study workflow, the CLI contract golden, 3 tests, and docs, about 15 places,
for little gain.

- Add `run_sst2` / `run_seus` with `@node_tool(node_type="prod")`, and put
  `sampling_method: sst2 | seus`, ladder / windows, T_ref, exchange interval, and stage (adaptive / fixed) in the condition contract.
- `_load_state_into_simulation` does not restore global parameters, so keep the current rung / window, running averages,
  attempt counts, and random state in a **sidecar JSON** like `steering.json`, and restore them on restart.
- For the integrator, `production.py` fixes T and the friction at 1 ps⁻¹. SST2 scales the solute without changing the integrator temperature,
  so this is consistent, and the friction matches the paper's setting.
- N independent runs = N `prod` nodes. The existing Slurm job array (`submit_array_job`) can be used.
- SST2 calls the fork's driver as a subprocess. SEUS runs directly inside MDClaw.

### Outputs

- Trajectory, `energy.dat`
- `tempering.csv` (step, rung, λ, E_t per fractional bucket, E_pw) / add a window column to `collective_variables.csv`
- `weights.json` (running averages, attempt counts, history of effective g_m, stage)
- Acceptance rates, round-trip counts, rung / window occupancy (computed in an `analyze` node)
- For SEUS, the pymbar MBAR PMF in `analyze`

### Convergence criteria (checks in the `analyze` node)

On the SST2 side this is handled by `analyze_tempering` (implemented 2026-09-18, `mdclaw/analyze/tempering.py`).
In an analyze node whose parents are the walkers' `run_sst2` prod leaves, it outputs MBAR over all rungs from `tempering.csv`,
`weights.json` (fixed weights for the next stage), `tempering_frames.csv` (per-frame rung and 300 K weight;
the λ-annotated data format for the surrogate), and `verdict`. Criteria:

- Every rung has been visited (if any is unvisited, insert rungs)
- Each walker's on-the-fly weights and each walker's own MBAR are within 2.5 kJ/mol
  (1 kT at 300 K) of the pooled MBAR f_k
- At least 5 round trips per walker, and a rung-change rate of at least 0.1
- On failure, extend with `continue_from` according to `verdict_reasons`; re-seed a trapped walker
  with `weights.json` as the initial weights and a different seed; if the ladder is coarse, insert rungs and redo the adaptive stage.

Verdicts on the 1KXV 50 ns blocks: H3 only has top-rung f_k differing by 15 kJ/mol between walkers, `weights_drifting`
(extension needed); H3 + shell likewise; dihedrals only has seed 1 trapped with 1 round trip.
On the SEUS side, `run_seus`'s `seus.json` and MBAR (separate session).

### Form handed to the surrogate

Do not drop the weights. Training on frames from the T_ref rung lined up as-is learns a biased distribution.
Explicitly choose between resampling by MBAR weights to make it unweighted (ESS decreases) or using a weighted loss.
Features: H3 backbone dihedrals as the main axis, with Cartesian coordinates after framework alignment and contact maps alongside.

### Dataset generation approach (2026-09-17, user decision: the surrogate learns the ensemble, not the dynamics)

The teacher for the surrogate / foundation model only needs p(x | sequence) at 300 K (time evolution is not learned).
This makes SST2's thermodynamic data usable as the teacher as-is, and a swarm of ordinary 300 K MD runs (for dynamics) is unnecessary.

- **Engine**: SST2, with the solute being H3 + an environment set defined by Kabat positions, starting from Boltz-2 predicted structures (multiple samples).
  Crystal starting structures deviate from the inference-time distribution. Packed 8 per MPS, 10 Å buffer.
- **Use all rungs**: save each frame with its λ (rung) and MBAR weight, and train a temperature-conditioned generative model.
  Reweighting to 300 K alone gives an ESS of about 25 % (1KXV H3 only: 2,449 / 10,000), but with λ conditioning every frame becomes a teacher.
- **Three tiers of sequence sets**: breadth (1,000–2,000 sequences × 1 walker × 200–300 ns, stratified by H3 length, framework family, and charge),
  depth (100 sequences × 3 × 1 µs, for convergence criteria and verification), and mutational neighborhood (20 parents × 30 variants × 300 ns, for learning Δp(x)).
  10,000–15,000 GPU hours in total. A uniform 1,000 × 3 × 1 µs is 44,000 GPU hours (1KXV measured 0.0145 GPU hours/ns), which is inefficient.
- **Select actively**: train the model once on half of the breadth tier, and run next the sequences whose model samples have low force-field reweighting ESS or
  large disagreement between models.
- **Details**: give the adaptive stage's initial weights by regression from the solute atom count to cut adaptation from 50 → 10–20 ns; output every 20–50 ps;
  1 µs as 2 blocks of 500 ns (gpu partition limit 4 days, 275 ns/day per run).

---

## 8. Risks

| Risk | Description | Response |
| --- | --- | --- |
| Weight convergence is rate-limiting | CLN025 0.5–1 µs, Trp-cage 2–10 µs. VHH H3 unknown | Measure in Phase 1. Look at the effect of fewer rungs with an H3-only solute |
| Implementation gaps for a partial solute | Boundary 1-4 exceptions unscaled; sub-Systems require a ForceField | Fix in Phase 1. Make the closure test pass with a partial solute |
| Irreversibility of the acceptance rule | The current loop is not π-invariant | Switch to Gibbs. Verify the stationary distribution on a toy model |
| Samples during adaptation are non-equilibrium | On-the-fly weight updates | Two-stage protocol. MBAR on the fixed stage only |
| PME with a charged solute | Net charge changes with λ, volume-dependent under NPT | Choose residues to keep the charge small. Compare with NVT. Compare acceptance rates with RF |
| Solute cut inside a residue | Fragment templates do not match | Cut at residue boundaries. Check and refuse in the driver |
| Biased rung occupancy | Low-temperature rungs over-occupied | Correction that tilts the weights toward high temperature |
| Orthogonal slow degrees of freedom | SEUS is CV-dependent, so χSS2-type problems remain | Use together with SST2 (CV-free). The reason to build both |
| Window starvation | Some windows dry up early | SAMS / burn-in |
| Diffusion in index space | Round trips over K windows take on the order of K² transitions | Make up for it with the number of independent runs. Gibbs is faster than neighbor-only |
| Silent errors | Sign or exponent errors distort only the distribution | Do not skip Phase 3 |
| Continuity of upstream maintenance | Single-lab tool, no CI | Fork and own it. Add CI to the fork. Loose coupling via the process boundary |
| Convention differences from GENESIS | SST2 leaves bonds, angles, and impropers unscaled; gREST scales them by k/l | Restrict GENESIS's `param_type` to dihedrals + LJ + Coulomb when comparing |

---

## 9. Effort and division of labor

| Phase | Content | Duration | Lead |
| --- | --- | --- | --- |
| 0 | Fork and licensing (upstream PRs / issues decided after Phase 1) | 1 hour | Matsunaga |
| 1 | Sub-System builder, boundary 1-4, Gibbs acceptance, driver, partial-solute verification, measurement of weight convergence | 2–3 weeks | Implementation by Claude, review by Matsunaga, running the systems by Enomoto / Kawai |
| 2 | SEUS (MDClaw side), toy verification | 2–3 weeks | Draft by Claude, review by Matsunaga |
| 3 | Comparison with ordinary umbrella + MBAR, comparison with GENESIS gREST | 2–3 weeks | GPU environment |
| 4 | MDClaw integration (`prod` labels, sidecar, analyze checks) | 1–2 weeks | — |

7–11 weeks in total. Phase 3 may stretch depending on the Phase 1 measurement of weight convergence.
The Phase 1 closure tests and the Phase 2 toy verification are self-contained on CPU inside the container.

---

## 10. Next actions

1. Fork to `matsunagalab/SST2`. Add CI (pytest, CPU).
2. Write the partial-solute closure test on 5awl and confirm it fails (evidence before the fix).
3. Write the copy-based sub-System builder and the boundary 1-4 fix, and make the closure test pass.
4. On one VHH (e.g. 1KXV from MDDataBench, already prepped), trial-run and compare the 3 ways of cutting the solute (H3 only / H3 + contact shell / dihedrals only).
5. ~~In parallel, start the SEUS toy verification (1D double well) on the MDClaw side.~~ **SEUS was removed on 2026-09-22** (see memo; succeeded by well-tempered metadynamics). History: on 2026-09-16, `run_seus`
   (`mdclaw/simulation/expanded_ensemble.py`, prod node, SAMS + all-window Gibbs) was implemented, and
   the toy verification was replaced by a comparison against the existing umbrella route on the end-to-end distance of capped Ala3 (in water, 23 windows)
   (see memo 2026-09-16).
6. Choose one realistic-size verification system for SEUS, and build the reference PMF with the existing umbrella route.

---

## References

- SST2 repository: https://github.com/samuelmurail/SST2
- SST2 paper: https://pubs.acs.org/doi/10.1021/acs.jctc.5c00950
- SST2 trajectories: https://doi.org/10.5281/zenodo.13772542
- SST2 documentation (acceptance and weight formulas in `method.rst`): https://sst2.readthedocs.io
- gREST: Kamiya & Sugita, *J. Chem. Phys.* **149**, 072304 (2018)
- Independence sampling: Chodera & Shirts, *J. Chem. Phys.* **135**, 194110 (2011)
- SAMS: Tan, *J. Comput. Graph. Stat.* **26**, 54 (2017)
- Psivant femto (an OpenMM REST2 implementation): https://github.com/Psivant/femto
- MDDataBench: https://github.com/matsunagalab/MDDataBench
- MDClaw: https://github.com/matsunagalab/mdclaw
