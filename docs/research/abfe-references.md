# Absolute Binding Free Energy (ABFE) of a Ligand — Design Decisions and Validation Procedure

`mdclaw/fep/abfe.py`, `decouple.py`, `boresch.py` (2026-09-20). Reuses the DAG, λ-window sampling and MBAR of
the hybrid-topology FEP for single-point-mutation ddG (`fep-references.md`) as is;
**only three things are new: "a System for decoupling", "a Boresch restraint", and "a final node with the standard-state correction".**

## 1. Thermodynamic Cycle

```
dG_bind° = dG(solvent leg) − dG(complex leg) + dG_restraint − kT ln σ
```

- Each leg's dG is the MBAR value for "λ=0: fully coupled, no restraint → λ=1: decoupled (complex is restrained)".
  The complex leg's value includes the cost of turning on the restraint.
- `dG_restraint` (positive) = the free energy of placing the non-interacting ligand from the 1 M standard state under the restraint.
  Boresch 2003 eq. 32:
  `kT ln[ 8π² V° √(K_r K_θA K_θB K_φA K_φB K_φC) / (r0² sin θA0 sin θB0 (2π kT)³) ]`,
  `V° = 1.6605 nm³`. With the default force constants (K_r = 4184 kJ/mol/nm², K_angle = 83.68 kJ/mol/rad²)
  and r0 = 0.5 nm, it is about +33 kJ/mol (+7.9 kcal/mol). `tests/test_abfe.py` pins that it agrees with a numerical
  integration of the configurational integral to within 0.15 kJ/mol (including the sign).
- `−kT ln σ`: the correction when the restraint confines the ligand to one of σ indistinguishable orientations
  (Mobley 2007; benzene has σ = 12, giving −1.5 kcal/mol). Default 1, `--ligand-symmetry-number`.

## 2. DAG

```
source ─ prep ─ solv ─ topo[build_decoupled_system] ─ min ─ eq ─ topo[add_boresch_restraint] ─ fep… ─ analyze_fep ─┐
           └─ prep[extract_ligand] ─ solv ─ topo[build_decoupled_system] ─ min ─ eq ─ fep… ─ analyze_fep ──────────┴─ analyze[estimate_binding_dg]
```

| Decision | Choice | Rationale |
|---|---|---|
| Where the legs live | Two legs in one job; the solvent leg is a prep child (`extract_ligand`) of the complex's prep | Same shape as ddG. Both legs read the same `ligand_chemistry` record (= the same parameterization input). Paths in the record are made absolute by the DAG on read and relative on write (`../prep_001/artifacts/…`), so no copying is needed |
| Where the Boresch restraint lives | A **topo node under the eq** of the complex leg (`add_boresch_restraint`). `eq` is added to `_ALLOWED_PARENT_TYPES["topo"]` (excluded from auto-parent) | The six restraint atoms and their reference values must be identical in every window of the leg, and `run_fep` splits windows across multiple fep nodes, so one node must own the definition. It can only be chosen from the post-equilibration pose, so it comes after eq. This avoids adding more places where the run side adds or removes parts of the System (the "the run side does not reconstruct a System" contract). fep nodes hang directly under this topo (`topo` is added to `_ALLOWED_PARENT_TYPES["fep"]`). A window's starting state is the state of the first eq found walking up the ancestors (`_resolve_md_restart`), and each window minimizes and equilibrates at its own λ before sampling, so a restrained re-equilibration node is physically unnecessary — the first implementation inserted one eq because of the typing constraint "a fep's parent is eq / fep only", but it was removed after a user pointed this out. Instead, a fep with no eq ancestor (e.g. directly under a hybrid topo) is refused at input resolution as `fep_equilibration_required` |
| Not using PLUMED | OpenMM's `CustomCompoundBondForce` + global parameter `fep_restraint` | The restraint strength must be scaled with λ, and each sample re-evaluated at every window's λ (MBAR's u_kn). With a global parameter this works as is in the existing `run_fep` loop. A PLUMED bias cannot be controlled by an OpenMM parameter and would need per-window input plus offline re-evaluation from COLVAR |
| Not running fep on an unrestrained complex | On the complex leg, `build_decoupled_system` **does not write `fep_protocol`** (`metadata.fep.restraint_required`). A fep under it is refused at input resolution with `abfe_restraint_required` | A decoupled ligand wanders through the box; numbers come out but are meaningless. Stop it in the tool, not with a caution in the skill |
| Leg detection | Automatic from the contents (complex if anything other than the ligand, water and monatomic ions is present) | Do not create an agent judgement point |
| Charge handling | Electrostatics are **annihilated** (charges to 0; intramolecular Coulomb also vanishes), sterics are **decoupled** (intra-ligand LJ is kept at full strength at all times in a separate `CustomNonbondedForce`) | The openmmtools / YANK default. Decoupling electrostatics under PME would require adding every intramolecular pair back with a separate force. The lost intramolecular Coulomb is identical in both legs, so it cancels in the binding free energy. **The solvent leg alone is not a hydration free energy** (that needs a vacuum leg) |
| λ contract | Reuses `fep_elec_old` / `fep_sterics_old` of the five hybrid parameters (the rest are declared and fixed at 0); the complex adds a sixth, `fep_restraint`. The protocol names its own `global_parameters` and `phases` | Do not branch `run_fep` / `analyze_fep` for ABFE. Windows carry explicit parameter values, so the order (restraint on → charges off → sterics off) is a contract of the protocol file |
| Default windows | Restraint `0,0.02,0.04,0.08,0.2,0.5,1` (0.04 is the interval where a rotating ligand is narrowed into one well; effective angular width 70 → 50 → 35°), charges `1,0.75,0.5,0.25,0`, sterics 14 points (dense near 0) → 24 windows for complex, 18 windows for solvent | Charges are removed while sterics are still fully on (no bare charge enters the soft core) |

## 3. Boresch Atom Selection (Done Entirely in the Tool)

1. From the eq state, run 200 ps of NVT in the bound state (no restraint) and take a frame every 2 ps.
2. Ligand side: a chain of three bonded heavy atoms (A, B, C). A: up to 4 candidates, nearest first to the heavy-atom centroid; B: the neighbour of A with the highest degree; C: a neighbour of B (or of A if none). Bonds are **taken from the System's bonded terms and constraints** (`topology.pdb` has no CONECT records, and non-standard residues come back with no bonds when read — discovered in a real 3PWB run).
3. Receptor side: each residue's backbone (c, b, a) = (N, C, CA). Only those with an initial a–A distance of 0.4–1.5 nm.
4. From the time series of the six coordinates (minimum image; dihedrals with circular statistics), keep only candidates whose θA and θB reference values fall in [40°, 140°], and take the set with the smallest Σ(std / thermal width)². Reference values are the mean for the distance and, for the angles, **the values of the frame in the most populated orientation** (the frame with the maximum joint kernel density of the five periodic coordinates, bandwidth = thermal width. Combining per-coordinate modes can produce an orientation that never occurs, so all are taken from one frame. Same idea as the histogram mode of Mobley 2006).
5. If even the best candidate has std(r) > 0.15 nm, refuse with `abfe_restraint_unstable` (the ligand has left the site). An angular std > 25° is not refused: a ligand that stays in the site while changing orientation (benzene in the T4L cavity: r std 0.03 nm, in-plane rotation gives one dihedral a std of 86°, switching 103 times among six equivalent orientations in 200 ps) is restrained to the most populated orientation, with `statistics.reorients` and the affected coordinates recorded and a warning issued. The confinement cost is paid by the FEP of the restraint phase, so `estimate_binding_dg` refuses σ > 1 on this leg as `abfe_symmetry_already_sampled` (preventing double counting of sampled orientations). Equivalent orientations not visited in the selection run (e.g. a flip of a planar ring) are left uncorrected as an approximation, at most kT ln 2 = 0.42 kcal/mol per twofold symmetry. Up to 9/26 the implementation also refused angular std > 25°, and for benzene it gave the wrong guidance "extend equilibration".

The sign conventions of the internal coordinates match OpenMM's `distance/angle/dihedral`. The first implementation had the dihedral sign reversed,
and the test "the force's energy is 0 at the measured reference values" caught it (`test_coordinates_follow_openmm_conventions`).

## 4. Endpoint Validation (`validate_decoupling`)

- Coupled state: alchemical System (dispersion correction off) = the original System.
- Decoupled state: the energy is invariant when the whole ligand is translated on top of other atoms (proof that it does not interact with the environment;
  this also confirms that the soft core stays finite on contact).

Measured: 1e-4 kJ/mol for both on solvated ACE-ALA-NME; on the 3PWB/GOL complex (CUDA single precision, |E| ≈ 5.7e5 kJ/mol)
0.47 / 0.007 kJ/mol (tolerance 1.14).

## 5. Out of Scope for v1 (Stopped by Refusal Codes)

- Charged ligands (`abfe_charged_ligand_unsupported`). The box charge changes, and finite-size errors do not cancel between the two legs.
  Supportable by reusing the co-alchemical ion (`fep/coion.py`), but validate neutral ligands first. Checked before building from the charge in the prep record,
  and again from the sum of the assigned partial charges.
- Covalent ligands (`abfe_ligand_covalent`), fewer than three heavy atoms (`abfe_ligand_too_small`), multiple copies (`abfe_ligand_ambiguous`).
- The ligand's LJ long-range correction (the same known approximation as hybrid; the surrounding density differs between complex and solvent, so it does not fully cancel.
  YANK corrects it by endpoint reweighting).
- Membrane proteins, nucleic-acid receptors (receptor anchors assume protein backbone N/CA/C → `abfe_receptor_missing`).
- Multiple binding poses, slow conformational changes on the protein side (e.g. Val111 in T4L L99A). The number is the value for "the prepared pose".

## 6. Validation Status

| Stage | Status |
|---|---|
| Unit (protocol, ligand selection, analytic correction vs numerical integration, OpenMM conventions, atom selection and refusals, DAG nodes, cycle summation) | `tests/test_abfe.py` (10 fast + 1 slow) pass |
| Wiring e2e | 3PWB (trypsin) + GOL loaded through the existing `fetch_structure → prepare_complex → solvate_structure`, both legs run through to `estimate_binding_dg` (1 window, 4 ps. **The numbers are meaningless**). BEN (+1) is refused; no `--ligand` is refused with candidates |
| Physics validation | **Not done**. A separate tester will carry out §7 below |

## 7. Physics Validation Procedure: T4 Lysozyme L99A / Benzene

The standard ABFE benchmark. Experimental value **−5.19 kcal/mol** (Morton et al. 1995). Computed values scatter between −4 and −6 kcal/mol
depending on force field and protocol (Mobley 2007 and others). Neutral, rigid, six heavy atoms: fits squarely within v1's scope.

1. Structure: PDB **181L** (T4L L99A + benzene, ligand residue name `BNZ`). Follow `skills/md-abfe/SKILL.md`.
   `prepare_complex --include-types protein ligand --process-ligands --ligand-smiles '{"BNZ": "c1ccccc1"}'`.
   Do not keep the other crystal heteroatoms (BME, Cl, etc.).
2. Force field ff19SB + OPC (or ff14SB + TIP3P; fix one and use it for both legs), HMR default, NPT 300 K.
3. complex leg: `build_decoupled_system --ligand BNZ` → min → eq (NPT 1 ns or more) → `add_boresch_restraint`
   (topo under the eq) → fep nodes under that topo, `run_fep` (aim for 23 windows × 5 ns; for job arrays see
   `skills/md-fep/windows.md`) → `analyze_fep`.
4. solvent leg: `extract_ligand --ligand BNZ` → `solvate_structure --dist 12` → `build_decoupled_system` → min → eq
   → `run_fep` (18 windows × 2 ns) → `analyze_fep`.
5. `estimate_binding_dg --ligand-symmetry-number 1` (benzene rotates in the selection run, so do not apply σ).

Please report:

- `dG_bind_kcal_mol ± error` and the four terms of `terms_kj_mol`, each leg's `phases`, `min_neighbour_overlap`, and the sampling time per window.
- The `boresch` block of `add_boresch_restraint` (names of the six selected atoms, r0 and angles, `statistics.std`).
  **Benzene rotates in-plane inside the cavity**, so the `reorients` warning will appear. In that case σ is 1 (`--ligand-symmetry-number 12` is refused).
- Convergence: the change in dG_bind when the time per window is halved, and the overlap of the `restrain` phase.
- Any place where a refusal or guidance (`code` / `next_action` / `next`) did not determine the next step.

Acceptance guide: within ±1.5 kcal/mol of experiment, the implementation is sound. If it is systematically off by several kcal, suspect, in order,
(1) signs and how the terms are summed (compute by hand from `terms_kj_mol`), (2) the restraint is too strong and distorts the bound state (the `restrain` phase dG
exceeds several kcal), (3) the missing LJ long-range correction (§5), (4) insufficient sampling of Val111 rotamers.

## References (bibliographic details verified with Crossref, `citation-audit-2026-09-06.md` 3rd addendum)

- Boresch, Tettinger, Leitgeb, Karplus, J. Phys. Chem. B 107, 9535 (2003) — six-coordinate restraint and analytic correction.
- Gilson, Given, Bush, McCammon, Biophys. J. 72, 1047 (1997) — double decoupling and the standard state.
- Mobley, Graves, Chodera, McReynolds, Shoichet, Dill, J. Mol. Biol. 371, 1118 (2007) — T4L model site, symmetry correction.
- Beutler et al., Chem. Phys. Lett. 222, 529 (1994) — soft core (shared with hybrid).
