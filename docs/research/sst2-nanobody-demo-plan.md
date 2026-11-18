# SST2 paper demo: apo/holo population shift of nanobody CDR-H3 — survey and preparation

Written: 2026-09-26 (survey and preparation for the user request "Find monomer and complex structures of
the same nanobody in SAbDab, and for one whose CDR-H3 changes, run SST2 from apo only and show that both
structures appear in the ensemble")

study: `/data1/$PROJECT/$USER/sst2-trials/nb-apo-holo` (screening inputs/outputs and scripts are in
`inputs/screen/`)

---

## 1. Question

If SST2 is run starting only from the crystal structure of an apo (monomeric) nanobody, does the
antigen-bound (holo) CDR-H3 structure appear with a finite population in the ensemble reweighted to
300 K? If it does, binding can be explained by conformational selection (population shift), and its
free-energy cost ΔG_conf = −kT ln(P_holo-like / P_apo-like) can be computed numerically.

## 2. Prior work and novelty

- Fernández-Quintero, Liedl et al. (metadynamics + MSM, tens of µs per system): repeatedly claim, for Fab
  apo/holo pairs, that "the bound form is already present in the apo ensemble" (mAbs 2019, doi:10.1080/19420862.2019.1618676
  and others). For nanobodies: the GFP enhancer (IJMS 2022, doi:10.3390/ijms23105419; H3 of 8 residues, change of 2–2.6 Å) and
  cAb-Lys3 (Biomolecules 2023, doi:10.3390/biom13020380; no apo crystal).
- Higashida & Matsunaga, Life 2021 (doi:10.3390/life11121428): gREST (8 replicas, CHARMM36)
  reproduces the crystal H3. apo→holo was not the goal.
- Higashida master's thesis (2023, gREST 8 replicas, GENESIS/CHARMM-GUI): attempted apo→holo on 5 pairs.
  The BOUND structure was sampled for 6HEQ–4N9O and 5E03–5E5M. It was not for 5E7B–5E7F and 6APQ–4W2O;
  conclusion: "successes have H3 RMSD 2–3 Å, failures 5–6 Å". The thesis attributes the 5E7B failure to a
  cis/trans difference at Pro108, but **all peptide bonds in the deposited coordinates (5E7B:A, 5E7F:A/B, including altlocs) are trans**
  (`inputs/screen/cis.py`). The cis bond may have been introduced at the CHARMM-GUI modeling stage (unverified).
- No paper was found that applies SST2 (single-replica solute tempering) to nanobodies / CDR-H3
  (literature-survey subagent, 2026-09-26).

What can be claimed as novel: (i) systematic extraction of apo/holo pairs from all of SAbDab, (ii) 300 K
populations MBAR-reweighted from one trajectory (no replica exchange, one GPU shared via MPS),
(iii) independent convergence checks from apo and holo starts, (iv) a verdict on Nb.X0, which the
authors described as induced fit.

## 3. SAbDab screening

Data: `/api/download/all-single-domain-summary` of SAbDab2 (React version, 2026-09-26)
(4,804 instances; includes the VH sequence, IMGT CDR sequences, and `HEAVY_ID` = cluster of identical VH sequences).
The old URL `…/sabdab/nano/summary/all/` only returns the SPA's HTML and cannot be used.

Procedure (`inputs/screen/`):

1. SD-H only. Instances whose antigen type includes PROTEIN / PEPTIDE / NUCLEIC are bound; all others
   (empty, or only ION, HAPTEN, SUGAR) are apo.
2. Group by `HEAVY_ID` (exact VH sequence match): 80 groups (269 PDB) that have both apo and bound.
   An additional 15 groups whose 3 CDR sequences match and that differ only in the framework.
3. On RCSB mmCIF, map residues by local alignment to the VH sequence, superpose on framework Cα
   (excluding the 3 CDRs and 3 residues at each end), and compute the H3 backbone (N, CA, C, O) RMSD for all pairs
   (`h3rmsd.py`). The RMSD after local superposition on H3 (change in loop shape) is also reported.
4. For the top groups: H3 crystal contacts (4 Å including symmetry operations), H3/chain B-factor ratio, altlocs,
   backbone dihedral differences and cis/trans (`contacts.py`, `dihed.py`, `cis.py`).

Full results table: `inputs/screen/sabdab_apo_holo_h3_screen.csv` (91 groups. 21 groups have a median ≥ 2 Å, but
those above 20 Å etc. include artifacts from domain swaps, fusions and annotation errors. Examples: the "apo" of 3OGO is another
copy in a GFP complex crystal; 9D7U–X is a fusion).

Observations:

- **The apo-crystal H3 is in a crystal contact in almost every case** (all 8 copies of 8Q78, 8F8V, 7KKJ,
  6HEQ, 5E03 …). It is always possible that the apo structure itself is one state stabilized by the lattice.
  This motivates MD that produces the "ensemble in solution", but avoid calling the apo starting structure
  "the apo ground state".
- No candidate had a cis/trans difference (top 19 chains).

## 4. Candidates

| Target | apo | holo | H3 length (IMGT) | H3 RMSD apo–holo | Features |
|---|---|---|---|---|---|
| **TPP-3077** (anti-properdin, llama, humanized FR) | 8Q78, 1.225 Å, 8 copies | 8Q6R, 1.9 Å, 2 copies | 13 | 3.9–4.4 Å (local 2.1) | Change concentrated in E101 ψ and the G104–G105 Gly hinge. The 8 apo copies form 2 groups (1.4–1.7 Å from each other). No extra disulfide. mAbs 2024, doi:10.1080/19420862.2024.2415060 (authors attribute the inter-copy differences to crystal packing) |
| **Nb.X0** (anti-afucosylated IgG1 Fc, synthetic) | 8F8V, 1.81 Å, 2 copies | 8F8W 2.71 Å ×4 + 8F8X 2.6 Å ×2 (2 crystal forms) | 14 | 5.3–7.1 Å (local 2.5) | **The 2 apo copies differ from each other by 4.8 Å**. The holo, 6 copies in 2 crystal forms, agree within 0.3–0.9 Å. The holo H3 folds over onto FR2 (47, 58–60). Authors describe it as induced fit (Nat Commun 2023, doi:10.1038/s41467-023-38453-1). Canonical Cys only |
| mNb6 (SARS-CoV-2 spike) | 7KKJ, 2.05 Å | 7KKL, cryo-EM 2.85 Å | 12 | 7.4 Å (local 4.2) | Holo is cryo-EM. altlocs in apo H3 (side chains). Science 2020 |
| Nb484 (prion) | 6HEQ, 1.23 Å | 4KML/4N9O/6HER/6HHD (4 crystals agree within 0.2 Å) | 17 | 3.7 Å (local 3.0) | **6HEQ has WQQG instead of WGQG in FR4 (sequence differs)**. gREST succeeded in the thesis. For a comparison, revert Q→G on the apo side |
| CTLA-4 Nb | 5E03, 1.69 Å | 5E5M, 2.18 Å ×4 | 6 | 3.4–3.6 Å | Short H3. gREST succeeded in the thesis |
| L06 (anti-phage RBP) | 5E7B, 1.1 Å | 5E7F, 2.7 Å ×3 | 19 | 9.0 Å (local 5.2) | Extra H3–FR2 disulfide (Cys45). gREST failed in the thesis |
| Marburg NP sdAb | 6APQ, 1.9 Å | 4W2O, 3.2 Å ×4 | 12 | 3.8–4.0 Å | gREST failed in the thesis. Holo at low resolution |

Recommendation: **primary target TPP-3077** (highest resolution, with the change localized to a Gly hinge that SST2 should
converge easily: "an example where success is expected") and **Nb.X0** (an example the authors called induced fit. If SST2 produces the holo form,
that is a new claim; if not, the conclusion goes the other way: "the converged ensemble contains no holo form".
Either way it is publishable). If a direct comparison with gREST is wanted, add 5E03–5E5M as a third
(same system as the thesis, H3 of 6 residues, cheap).

## 5. What has been prepared (2026-09-26)

2 jobs per target (apo start / holo start). The holo start takes only the nanobody chain from the complex,
and is an **atom-for-atom identical system** to the apo (residue range, residue names, protonation and atom order checked with diff).

| job | Source | Residues | Atom count (topo) | Status |
|---|---|---|---|---|
| tpp3077_apo | 8Q78 chain B | 1–119 | 102,592 | eq_001 completed |
| tpp3077_holo | 8Q6R chain A (GCH ×2 excluded) | 1–119 | 63,212 | eq_001 completed |
| nbx0_apo | 8F8V chain A | 1–120 | 56,113 | eq_001 completed |
| nbx0_holo | 8F8W chain C | 1–120 | 61,053 | eq_001 completed |

Common: ff19SB + OPC, cubic 15 Å, 0.15 M NaCl, HMR, His fixed to HIE with `--protonation-method no-prediction`
(TPP-3077: H33, H103. Nb.X0: H107). Left to propka, the His states could differ between apo and holo,
and the 2 starting points would have different Hamiltonians. min/eq are the same as the 1KXV trial run
(minimization with heavy-atom restraint 100, NVT 1 ns + NPT 1 ns, 300 K). Measured eq: 2 ns for 102k atoms in
4.5 min (~640 ns/day standalone, GB200).

The TPP-3077 apo box is large because the apo-crystal H3 (Lys102) protrudes 30.7 Å from the center of mass
(holo at most 26 Å).

solute (`inputs/<target>_{apo,holo}_solute_{h3,h3_shell}.json`, `make_solute_and_refs.py`):
H3 (IMGT CDR3) plus the **union, by residue number,** of residues with a heavy atom within 5 Å of H3 in either the apo or holo crystal,
fixed. Deciding by distance from a single structure would make the solute differ between the apo and holo starts.

| Target | H3 | H3 + shell | Shell residues |
|---|---|---|---|
| TPP-3077 | 96–108 (192 atoms) | 564 atoms | 1, 2, 4, 27–37, 45, 53, 78, 94, 95, 109, 110 |
| Nb.X0 | 96–109 (207 atoms) | 538 atoms | 2–4, 24, 28, 29, 32–37, 47, 58–60, 94, 95, 110, 111 |

Reference structures (`inputs/<target>_<system>_ref_{apo,holo}.pdb`): each has the system's atom count, with only the protein coordinates
replaced by the apo / holo topo structure (for `analyze_tempering --reference-pdb`).
H3 backbone RMSD in the MD systems (apo vs holo): TPP-3077 4.09 Å, Nb.X0 5.58 Å.

## 6. Production protocol (proposal)

Lessons from 1KXV: an H3-only solute cycles well in temperature but does not change shape at 300 K. H3 + shell
does interconvert even at 300 K, but with 5 rungs the exchange is 3–5 % and coarse. → Start with **H3 + shell, 9 rungs**.

- ladder: 300, 327, 357, 389, 424, 463, 505, 550, 600 K (geometric, 2^(1/8)).
  If `analyze_tempering` shows rung exchange < 0.1, add rungs.
- 3 seeds per job. Adaptive 100 ns block → `analyze_tempering` (at weights_converged)
  → extend with fixed weights to 1 µs/run.
- Control: plain MD at 300 K from the apo start, 3 × 1 µs (same total time as one SST2 start).
- Packing: 3 seeds of one job + the control on 1 GPU with MPS (4 tasks/GPU for 102k atoms, 6–8 for 56–63k).
- Ablation (one target only, optional): H3-only solute, and dihedrals only (`--scale-nonbonded false`).

Estimate (1KXV measured 0.0145 GPU-h/ns @71k atoms, 6 packed, scaled by atom count):
TPP-3077: SST2 2 starts × 3 µs ≈ 100 GPU-h + control 3 µs ≈ 60 GPU-h; Nb.X0: ≈ 70 + 35 GPU-h.
Total ≈ 270 GPU-h (about 90,000 yen). 5–7 days of wall time to reach 1 µs/run × 3 seeds.

## 7. Analysis and verdict

- `analyze_tempering` per starting point (weights, temperature cycling, agreement of the 300 K distribution across seeds).
  The apo and holo starts have different numbers of water molecules, so they are not mixed in MBAR; **they are compared as two independent estimates**
  (this is the strongest convergence check).
- Observables: H3 backbone RMSD after framework superposition, against both the apo and holo references.
  On the 300 K 2D free-energy map (RMSD_apo × RMSD_holo), overlay all copies of the apo crystal (8 for 8Q78,
  2 for 8F8V) and all copies of the holo crystal as points.
- population: holo-like = RMSD_holo < 1.5 Å (also 1.0 / 2.0 Å for sensitivity), apo-like likewise.
  ΔG_conf and its spread across seeds and across starting points.
- How many times the plain MD control enters the holo form (show that it does not, or that it does not equilibrate if it does).
- Verdict:
  - support: the holo form appears with ≥ 10 effective frames from both starting points, populations agree
    within 1 kT across starting points and seeds, and plain MD over the same time does not reach it or its population is undetermined.
  - against: the 300 K distribution, consistent across starting points, has no holo form (for Nb.X0 that itself
    supports induced fit).
  - inconclusive: the distributions stay ≥ 1 kT apart between starting points.

## 8. Tool-side gaps (decide before starting)

- `analyze_tempering` takes one reference (`--reference-pdb`). The 2D map needs the RMSD to both the apo and holo references
  in the same frame table. Proposal: accept multiple `--reference-pdb` and emit `rmsd_nm_<label>` columns.
  Also allow `--state-a/--state-b` to be specified with a reference label (e.g. `holo:<0.15`).
  Until then, running analyze twice, once per reference, and joining `tempering_frames.csv` on frame is enough.
- `solvate_structure` has no option for box size or number of water molecules, so the apo and holo starts cannot be made
  the same system (worked around by the "two independent estimates" above).
