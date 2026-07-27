# MDAgentBench Suite Design

This design has been promoted into two focused benchmark suites:
`MDPrepBench-v0.3` for preparation workflows and `MDStudyBench-v0.4` for the
scientific question / study-bundle tasks. The long-term goal is to keep
MDAgentBench organized around these two main suites:

1. **Preparation Workflow Battery**: can an agent turn structurally messy
   public inputs into topology-built, minimized MD-ready systems with
   a scorer-loadable raw OpenMM artifact bundle?
2. **Scientific MD Reasoning**: can an agent plan, run/analyze, and defend a
   scientific conclusion for an experimentally validated question?

Short engine sanity tasks still matter, but they should become gate or support
tasks rather than the intellectual center of the benchmark.

## Design Position

The study benchmark target is not agreement with one curator-authored plan. A
PDB choice or full sampling schedule has no defensible unique answer. The
scientific observable and a minimum evidence-adequacy floor may nevertheless
need to be task-owned so every solver answers the same question. The target is:

> Can the agent prospectively plan and execute an MD study whose independently
> recomputed confirmatory evidence logically supports a conclusion that agrees
> with held-out experimental truth?

Planning quality is therefore evaluated through its scientific consequences,
not by matching a canonical workflow.

## Suite A: Preparation Workflow Battery

The current prep implementation has **40 tasks, P01-P40**. Each task exposes
only `prompt.md` and `submission_contract.json` to the evaluated agent. The
scorer keeps `task.json`, reference structures, hidden ligand poses, expected
component truth, and any truth/rescan material private.

MDPrepBench v0.3 is scored entirely with deterministic artifact checks. There
is no preparation-specific LLM judge or agent-authored evidence axis.

The submission is agent-neutral, but MDPrepBench v0.3 requires a common
OpenMM topology artifact format for completed submissions. MDClaw/OpenMM XML
triples are accepted directly, and other workflows can be used upstream if they
export `system.xml`, `topology.pdb`, and `state.xml` for scoring. Every
completed prep submission must include the raw topology triple and
`prepared_structure.pdb`. OpenMM artifacts are reloaded and rescanned for
finite energy; native-only Amber/GROMACS validation is deferred until backend
adapters are added.

### Current Prep Contract

Every P01-P40 task requires these raw files in the submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml` containing the minimized state
- `prepared_structure.pdb`

Task-specific raw files are listed explicitly in each public contract. The
evaluator derives metadata, hashes, the minimized PDB, and the minimization
report; the agent does not author those outputs.

Scoring is artifact-as-truth and graded: OpenMM is detected by deserializing the
triple (not a declared backend label), physical properties (force-field applied,
net charge, water-model fingerprint, ion molarity) are recomputed from the
artifact, and a small physical-validity gate plus per-capability partial credit
replaces blanket pass/fail. Each run records a `tooling_condition`, an
`attestation.json`, and a `verified` flag. See `docs/benchmark/fairness-protocol.md`
and `docs/benchmark/capability-coverage.md`.

The preferred MDClaw DAG path for MDPrepBench is `source -> prep -> solv -> topo
-> min`. Full equilibration and production remain outside the prep suite.
The short relaxation inside a topology builder (10 iterations by default) is
initial system validation, not the `min` node contract; its report records
`scope="topology_initial_relaxation"` and
`satisfies_min_node_contract=false`. A direct bundle may be treated as minimized
only when its exact packaged state actually underwent a post-topology
minimization equivalent to the `min` node contract. The evaluator derives the
minimized PDB, minimization report, metrics, normalized manifest, and hashes;
they are not agent outputs in v0.3.

All tasks include common deterministic checks for `topology_artifact_bundle`,
`openmm_system_load`, `openmm_energy_rescan`, `forcefield_applied_rescan`,
`minimization_report_check`, and `structure_geometry_quality` (a steric-clash /
geometry sanity scan recomputed from the artifact). Task-specific
structure/component checks are also mirrored onto the minimized structure when
applicable. If a submitted raw bundle fails a topology/minimization/geometry
critical check, the scorer gives it zero rather than partial credit. Any
deterministic check can also be promoted to this gate per task with
`hard_fail: true`.

### Current Prep Tasks

| ID | Theme | Candidate public input | Prompt | Main scorer checks | Priority |
|---|---|---|---|---|---:|
| P01 | Simple monomer preparation | PDB 2LZM | Task: retrieve T4 lysozyme chain A, clean it, and prepare an explicit-water system without unintended ligands. | explicit solvent and absence of BEN/AP5 from raw structures, common topology/minimization checks. | 1 |
| P02 | Chain and ligand selection | PDB 1AKE | Task: prepare adenylate kinase chain A while retaining AP5, including when mmCIF labels separate it from the protein. | AP5 count and absence of unrequested nonstandard residues in raw structures, common topology/minimization checks. | 1 |
| P03 | Ligand pose preservation | PDB 181L | Task: Ligand pose preservation: Prepare the T4 lysozyme L99A-benzene complex from PDB 181L. Keep protein chain A and the deposited benzene ligand (BNZ) together, and preserve the crystallographic benzene pose. Do not submit a ligand-only structure. Some tools may list BNZ separately from the protein during inspection, so make sure it is still included. | hidden protein+BNZ RMSD reference, L99A residue, BNZ retained in prepared/minimized structures, common topology/minimization checks. | 1 |
| P04 | Multi-ligand inclusion and exclusion | PDB 3PWB | Task: retain requested BEN/GOL-like ligands while excluding irrelevant buffer molecules and unrequested heterogens. | requested components present and excluded heterogens absent in raw structures, common topology/minimization checks. | 1 |
| P05 | Charged cofactor-like ligand stress | PDB 1DAP | Task: prepare DAP dehydrogenase with both deposited NDP cofactors (chains C and F, auth chains A and B) without silently dropping either cofactor. | both NDP cofactors retained in raw artifacts, common topology/minimization checks. | 2 |
| P06 | Supported metal ion retention | PDB 1CLL | Task: prepare calcium-bound calmodulin while retaining its four Ca2+ ions. | exact Ca ion count in raw structures, common topology/minimization checks. | 1 |
| P07 | Crystallographic ion triage | PDB 4RBQ | Task: Crystallographic ion triage: prepare oligo(U) RNA while retaining prompt-designated crystallographic K+ ions, excluding deposited crystallographic waters or buffer molecules as selected source components, and building an explicit-solvent topology/minimization system. | RNA and K ion retention plus deposited-water/buffer exclusion from raw artifacts, common topology/minimization checks. | 2 |
| P08 | Point mutation branch | PDB 2LZM | Task: prepare WT T4 lysozyme and a branched L99A mutant without overwriting the WT artifact. | A:99 ALA in mutant plus separate WT raw structure, common topology/minimization checks. | 1 |
| P09 | Multi-mutant branch | PDB 2LZM | Task: apply L99A and M102Q together. | A:99 ALA and A:102 GLN in raw structures, common topology/minimization checks. | 2 |
| P10 | Disulfide auto/override | PDB 5PTI | Task: Disulfide auto/override: prepare 5PTI as a standard classical MD system and preserve the canonical BPTI disulfides. | three disulfide pairs present and experimental deuterium excluded from raw structures, common topology/minimization checks. | 2 |
| P11 | Specific residue protonation | PDB 2LZM | Task: protonate chain A residue 11 as GLH. | A:11 GLH with HE2 in raw structures, common topology/minimization checks. | 1 |
| P12 | Phosphorylated residue restore | PDB 5K9P | Task: restore deposited SEP and build an MD-ready system. | A:20 SEP with P atom in raw structures, common topology/minimization checks. | 1 |
| P13 | User-requested phosphorylation | PDB 1UBQ | Task: convert ubiquitin Ser20 to SEP and prepare the resulting system. | A:20 SEP with P atom in raw structures, common topology/minimization checks. | 2 |
| P14 | Glycoprotein/glycan pass-through | PDB 6YA2 | Task: retain N-linked glycans as glycans. | NAG retained in raw structures, common topology/minimization checks. | 1 |
| P15 | Standard DNA topology | PDB 5MVQ | Task: prepare a DNA dodecamer without protein defaults. | DNA residue content in raw structures, common topology/minimization checks. | 2 |
| P16 | Standard RNA topology | PDB 4RBQ | Task: prepare an RNA system. | RNA residue content in raw structures, common topology/minimization checks. | 2 |
| P17 | DNA duplex chain retention and neutralization | PDB 1BNA | Task: retain both B-DNA duplex chains and neutralize the system. | two chains, DNA residues, and neutral topology charge recomputed from raw artifacts, common topology/minimization checks. | 2 |
| P18 | Membrane embedding and lipid composition | PDB 2LOP | Task: Membrane embedding and lipid composition: prepare TMEM14A model 1 in a mixed POPC:POPE:CHL1 membrane with nominal 2:1:1 composition. | membrane regime from topology, model-1 coordinate RMSD, POPC/POPE/CHL1 retained in topology/minimized structure, common topology/minimization checks. | 1 |
| P19 | Candidate/model selection | PDB 2K39 | Task: Candidate/model selection: select model 5 from the NMR ensemble before preparation rather than silently using model 1 or averaging the ensemble. | model-5 coordinate RMSD against scorer-private reference, common topology/minimization checks. | 2 |
| P20 | Terminal capping | PDB 5AWL | Task: prepare CLN025/chignolin with ACE and NME terminal caps. | ACE/NME retained in raw structures, common topology/minimization checks. | 1 |
| P21 | MSE cleanup | PDB 4Q5T | Task: convert deposited MSE residues to standard MET and exclude unrequested nonstandard residues. | MSE absent and MET present in raw structures, common topology/minimization checks. | 2 |
| P22 | OPC water-model fidelity | PDB 2LZM | Task: build an explicit-solvent system with the requested 4-site OPC water model. | explicit solvent and OPC water fingerprint from the raw topology, common topology/minimization checks. | 1 |
| P23 | Implicit vs explicit solvent | PDB 5AWL | Task: use implicit solvent and avoid an explicit water box. | absence of explicit waters/ions in raw structures, common topology/minimization checks. | 2 |
| P24 | Assembly/biological unit choice | PDB 1STP, stress reference PDB 2MS2 | Task: Assembly/biological unit choice: generate or select biological assembly 1 of PDB 1STP. | assembly-1 coordinate RMSD, four submitted protein chains, common topology/minimization checks. | 1 |
| P25 | Specified ion concentration | PDB 5AWL | Task: Specified ion concentration: build an explicit-solvent chignolin system that honors 0.30 M KCl while preserving net neutrality. | explicit solvent, K/CL retained in topology/minimized structure, 0.30 M KCl recomputed from ion count and box volume, net charge recomputed from OpenMM charges, common topology/minimization checks. | 1 |
| P26 | Zinc metalloenzyme retention | PDB 2CBA | Task: prepare human carbonic anhydrase II, keep the catalytic Zn2+ as a supported metal ion and its His94/96/119 shell, and neutralize. | Zn2+ retained (prepared/minimized), coordinating His94 present, net neutral, common topology/minimization checks. | 1 |
| P27 | Non-zinc multi-metal cofactor retention | PDB 3CNA | Task: prepare concanavalin A, keep both the structural Mn2+ and Ca2+ as supported metal ions and the Mn shell (His24), and neutralize. | Mn2+ and Ca2+ retained (prepared/minimized), coordinating His24 present, net neutral, common topology/minimization checks. | 1 |
| P28 | Custom drug-like ligand parameterization | PDB 1IEP | Task: prepare the Abl kinase-imatinib complex (chain A + STI), generate small-molecule parameters, and preserve the imatinib pose. | hidden imatinib-pose RMSD reference, STI retained in topology/minimized structure, force field applied to all atoms, net neutral, common topology/minimization checks. | 1 |
| P29 | Protein-protein interface retention | PDB 1EMV | Task: prepare the colicin E9 DNase-Im9 complex keeping both partners as two chains, and neutralize. | two protein chains retained (prepared/minimized), net neutral, buffer excluded, common topology/minimization checks. | 1 |
| P30 | Protein-DNA complex with structural metals | PDB 1AAY | Task: prepare the Zif268 zinc-finger-DNA complex keeping the DNA duplex, all three Zn2+, and the protein, and neutralize. | DNA duplex (>=2 chains), three Zn2+ retained (prepared/minimized), net neutral, common topology/minimization checks. | 2 |
| P31 | Histidine tautomer / protonation | PDB 2LZM | Task: prepare T4 lysozyme with chain A His31 built as the doubly protonated HIP tautomer. | A:31 HIP with HD1+HE2 (prepared/minimized), common topology/minimization checks. | 1 |
| P32 | Missing side-chain reconstruction | PDB 1CSP | Task: prepare CspB and rebuild truncated surface-glutamate side chains (Glu3/21/36/66 missing CG/CD/OE1/OE2). | A:3 and A:66 GLU with full CG/CD/OE1/OE2 (prepared/minimized), common topology/minimization checks. | 2 |
| P33 | Physiological NaCl concentration | PDB 1UBQ | Task: build an explicit-solvent ubiquitin system at 0.15 M NaCl, net neutral. | explicit solvent, NA/CL retained (topology/minimized), 0.15 M NaCl recomputed from ion count and box volume, net neutral, common topology/minimization checks. | 1 |
| P34 | Anionic-lipid membrane | PDB 2LOP | Task: embed TMEM14A model 1 in a mixed POPC:POPG membrane with anionic lipids and neutralize. | membrane regime, POPC/POPG species present (topology/minimized), net neutral, common topology/minimization checks. | 2 |
| P35 | RNA structural Mg2+ retention | PDB 1Y26 | Task: prepare the adenine riboswitch aptamer RNA while retaining structural Mg2+ and excluding the adenine ligand. | RNA aptamer retained, Mg2+ retained (prepared/minimized), net neutral, common topology/minimization checks. | 2 |
| P36 | Protein-RNA complex with zinc knuckles | PDB 1A1T | Task: prepare model 1 of HIV-1 NCp7 bound to SL3 RNA, retaining both RNA and Zn2+ zinc-knuckle ions. | RNA retained, protein/RNA partners retained, two Zn2+ retained, net neutral, common topology/minimization checks. | 2 |
| P37 | Beta-barrel membrane protein | PDB 1BXW | Task: embed OmpA in POPC, excluding crystallographic detergent. | membrane regime, beta-barrel protein retained, detergent excluded, net neutral, common topology/minimization checks. | 1 |
| P38 | Implicit protein-peptide complex | PDB 1YCR | Task: prepare the MDM2-p53 peptide complex in implicit solvent. | implicit regime, two partners retained, no explicit water box, common topology/minimization checks. | 2 |
| P39 | Oligomeric potassium-channel membrane | PDB 1BL8 | Task: embed biological tetrameric KcsA in POPC while retaining deposited pore K+ ions. | membrane regime, four protein chains retained, pore K+ retained (prepared/minimized), net neutral, common topology/minimization checks. | 1 |
| P40 | TIP3P water-model fidelity | PDB 2LZM | Task: honor a 3-site TIP3P explicit-solvent request. | explicit solvent, TIP3P water fingerprint, no unrequested nonstandard residues, common topology/minimization checks. | 1 |

Priority now indicates the preferred order for real MDClaw baseline smoke runs
and further curation. All 40 tasks are part of the active prep battery.

### Coverage Refinements

The P01-P40 list is broad enough for the first prep battery, but three details
should be treated as explicit coverage requirements rather than left implicit:

- **Assembly coverage is first-wave material.** P24 specifies `assembly_id`
  and the raw structure should let the scorer verify the generated biological
  unit. Use a normal dimer/tetramer case first, then
  add a many-chain stress case under the same P24 family or as a follow-up
  variant.
- **Many-chain identity must not depend on one-character PDB chain IDs.** In
  P24-style tasks, scoring should check stable component identity from raw
  coordinates and topology chain order. Reused PDB chain labels are acceptable
  only when the submitted artifacts remain unambiguous.
- **P21 keeps only artifact-verifiable cleanup.** It checks MSE-to-MET conversion
  and exclusion of unrequested nonstandard residues.

### Backend Neutrality

The public prep benchmark should score reproducible MD-prep artifacts, not
MDClaw-specific policy names, internal node IDs, or local refusal codes. If a
backend cannot complete a public task, the harness records the failed stage;
the solver must not invent a completed raw bundle or write status metadata into
the MDPrepBench submission.

### Prep Battery Scorer Extensions

The scorer now covers file presence, evaluator-derived metadata, solvent
rescan, RMSD recompute, caption/metrics consistency, OpenMM topology loading,
OpenMM finite-energy rescan, minimization report checks, minimized-structure
component rescans, and assembly identity checks. Remaining useful deterministic
check types include:

- `structure_component_rescan`: count required protein/nucleic/glycan/ligand/
  lipid/ion components in submitted structures. This is implemented for
  prepared and minimized structures, but more aliases will be curated as tasks
  mature.
- `residue_presence`: confirm mutation or PTM residue identity at a
  chain/residue site.
- `residue_absence`: confirm excluded ligands, waters, or heterogens are absent.
- `topology_metadata_rescan`: verify force-field, water model, membrane flag,
  ligand params, glycan library, phosaa library, or nucleic library metadata.
- `ion_concentration_check`: verify the user-specified salt species and ion
  concentration, including neutralization and approximate molarity from counted
  ions and final box volume.
- `lipid_composition_check`: count lipid residue/species names and compare the
  submitted membrane composition against the requested ratio within tolerance.
- `assembly_identity_check`: verify expected submitted chain/copy count for
  biological assemblies. P24 also uses coordinate RMSD against a scorer-private
  assembly reference, so assembly identity is judged from submitted structure
  artifacts rather than from a self-reported assembly map.
- Additional PDB-cleanup checks should be added only when their outcome can be
  recomputed from the submitted raw structures.
- `protonation_state_check`: verify user-specified residue protonation states
  where the artifact format makes that possible, including named residue sites
  rather than only global pH defaults. Implemented via `pdb_residue_state`,
  which now accepts multiple valid answers (`allowed_residue_names` /
  `accepted_atom_name_sets`) so judgment-type protonation/tautomer/capping tasks
  stay deterministic instead of needing an LLM judge.
- `structure_geometry_quality`: recompute steric clashes from the OpenMM bundle
  (VDW `r_min` overlap, bonded/exception pairs and virtual sites excluded), with
  optional bond-length/angle outlier, cis non-proline, and D-chirality checks.
  This is part of the physical-validity gate on every task.
- `rmsd_recompute`: verify ligand pose, NMR model selection, or assembly choice
  against scorer-private references when source-selection self-report would be
  too easy to fabricate.

Do not add a public prep-benchmark scorer primitive that requires MDClaw-local
codes. Backend-neutral failures belong in harness-owned execution records.

These should remain deterministic. MDPrepBench v0.3 has no LLM-judged rationale
or agent-authored evidence axis.

## Suite B: Scientific MD Reasoning

`MDStudyBench-v0.4` has one experimental S01 pilot and three inactive legacy
regression fixtures. There is no primary leaderboard task yet. The suite
should stay small unless a new task adds a genuinely distinct,
feasibility-tested scientific-answer pattern.

The v2 primary outcome is the conjunction `valid_execution AND claim_supported
AND truth_agreement`. These three gates are non-compensating and produce one
binary primary score. An unresolved study and an unsupported claim both receive
zero, while remaining distinct diagnostics.

The public task specifies the scientific entity, estimand, comparison,
conditions, measurement semantics, minimum evidence adequacy, and budget.
Structural sources, preparation, exploratory sampling, replica allocation, and
the confirmatory allocation above that minimum remain agent-selected. Planning
is not scored by matching a curator workflow. Instead, an agent submits pending
production nodes before confirmatory MD and is evaluated through the scientific
consequences of that plan.

Prior knowledge is allowed but is kept separate from the MD verdict. Held-out
truth is introduced only after truth-blind execution and claim verification.
The reported taxonomy is `grounded_correct`, `grounded_wrong`,
`unsupported_claim`, `unresolved`, or `invalid_execution`. No LLM judge
contributes to the v2 primary score.

### Current Scientific Tasks

| Tier | ID | Question class | Public comparison | Status |
|---|---|---|---|---|
| Pilot | `S01_pressure_hydration_t4l_l99a` | Dynamic equilibrium | Internal-cavity hydration at 200 MPa versus 0.1 MPa in folded T4L C54T/C97A/L99A, 300 K with a fixed pH-7 protonation model | Experimental |
| Extended | `S02_ppi_hotspot_barnase_d39a` | PPI mutation thermodynamics | barstar D39A versus WT barnase-barstar | Experimental |
| Extended | `S03_stability_nuclease_h124l` | Folding thermodynamics | staphylococcal nuclease H124L versus WT | Experimental |
| Extended | `S04_affinity_t4l_l99a_alkylbenzene` | Ligand-binding thermodynamics | n-butylbenzene versus benzene in T4L L99A | Experimental |

S02-S04 are excluded from primary aggregation until their estimands have native,
artifact-recomputable thermodynamic evidence contracts and independent
feasibility runs. The former T4L L99A folding-stability S01 is retained only as
a v0.3 regression fixture.

## Experimental-Truth Source Pools

Use curated databases as source pools, then hand-curate a small number of
agent tasks.

- Protein stability: ProTherm and ThermoMutDB. ProTherm v4.0 contains
  thermodynamic data with experimental conditions, structure, function, and
  literature links; ThermoMutDB is manually curated for wild-type and mutant
  protein thermodynamic parameters.
- Protein-protein mutation: SKEMPI 2.0. It is a manually curated benchmark of
  binding free-energy changes, kinetics, and thermodynamics for structurally
  resolved protein-protein interactions.
- Protein-ligand affinity: PDBbind. Use only carefully selected cases because
  docking/affinity datasets can have leakage, close homologs, and affinity
  comparability issues. Prefer direction/rank tasks over absolute affinity.
- Structure/prep anchors: RCSB PDB entries already covered by MDClaw tests are
  good starting points because they exercise real edge cases: 1AKE/AP5,
  5K9P/SEP, 6YA2/NAG glycan, 1BNA/DNA duplex, 2LOP membrane protein.

## Implementation Roadmap

1. Keep the public package small: expose the prompt, submission contract,
   checklist, and schemas for agent-authored files; keep `task.json`, truth,
   runner implementation, and scorer implementation private.
2. Run MDClaw as the reference baseline on each prep task and save expected
   artifact patterns for debugging scorer failures. Start with P01, P02, P03,
   P11, P24, and P25 because they exercise the main new contract surfaces.
3. Strengthen deterministic prep checks where metadata-only scoring remains
   weak, especially force-field/water fidelity, disulfides, nucleic-acid
   library selection, NMR candidate selection, terminal capping, ion
   concentration, lipid composition, and biological assembly identity.
4. Add backend adapters beyond OpenMM when there is a real external-agent need:
   Amber topology/report reload first, then GROMACS topology/report reload.
5. Export the public package and run at least one non-MDClaw baseline:
   - simple script baseline,
   - LLM-only/no-run baseline,
   - one external MD tool/harness when available.
6. Keep MDStudyBench compact: `MDStudyBench-v0.4` has one standard,
   feasibility-gated task. Promote an extended task only after its target
   estimand is independently recomputable and a blinded reference run shows it
   is resolvable within the declared budget.

### Current Prep Implementation Status

Implemented:

- P01-P40 task IDs are part of `MDPrepBench-v0.3`. P26-P40 extend coverage to
  zinc and non-zinc multi-metal cofactors, custom drug-like ligand
  parameterization, protein-protein and protein-DNA complexes, histidine
  protonation, missing side-chain reconstruction, physiological NaCl, and
  anionic-lipid membranes, RNA metal-ion systems, protein-RNA complexes,
  beta-barrel and oligomeric channel membrane proteins, implicit complexes, and
  TIP3P water-model fidelity.
- The common prep contract now includes topology artifacts and minimization
  evidence.
- P17 is the standard DNA duplex/neutralization task; modified DNA/RNA is not
  part of the core prep battery.
- P20 is the terminal capping task; homology modeling is not part of the core
  prep battery.
- P24 uses `rmsd_recompute` against the scorer-private assembly-1 reference plus
  `assembly_identity_check` on submitted chain count; it no longer accepts
  `preparation.assembly_id` or chain-identity-map JSON as scoring truth.
- OpenMM submissions are strongly checked by loading `system.xml`,
  `topology.pdb`, and `state.xml`, then rescanning finite potential energy and
  finite positions.
- Public export omits evaluator-only `task.json`, `truth/`, and `scorer/`.
- Synthetic honest/wrong fixtures cover all 40 tasks and exercise topology
  absence, broken OpenMM XML, nonfinite minimization reports, and minimized
  structure component loss.

Still to do:

- Run real MDClaw reference submissions for all P01-P40 tasks, beginning with
  P01/P02/P03/P11/P24/P25.
- Add stronger deterministic checks for force-field/water metadata, ion
  concentration from box volume and ion counts, lipid composition tolerance,
  disulfide topology, candidate selection, and cleanup decisions.
- Add Amber/GROMACS-specific artifact reload adapters when external benchmark
  runs need them.
- Decide whether P24 should gain a many-chain stress variant under the same ID
  family or become a later separate task.

### Current Study Implementation Status

Implemented (`MDStudyBench-v0.4`):

- The pilot tier contains the pressure-dependent T4L L99A cavity-hydration
  task; S02-S04 are explicitly extended and non-primary.
- `scientific_target` defines the public estimand, entity, conditions, allowed
  resolved outcomes, neutral equivalence outcome, required validity controls,
  and unresolved outcome without prescribing a workflow.
- The task owns the primary native verifier, outcome mapping, equivalence rule,
  exact cavity observable, minimum sampling adequacy, and folded-state control.
  Agents consume that contract rather than duplicating it.
- The only agent-authored files are `confirmatory_plan.json` before production
  and `claim.json` after runner continuation. The runner owns the manifest,
  episode ledger, and certified artifacts.
- The benchmark runner preflights paired pending nodes and requested physical
  time, freezes the plan, executes those nodes, and records the production
  lineage needed for deterministic replay.
- Private scoring adds held-out truth only after valid execution and claim
  support pass.
- S01 verifies the full public construct sequence, measures water oxygens within
  0.45 nm of mapped L99A CB, requires at least 10 ns and ESS 5 per condition,
  fixes an all-protein-CA folded-state control, applies the fixed
  confidence/equivalence rule, and rejects unresolved initialization as support.
- Biased or enhanced trajectories may guide exploration, but the v0.4 native
  occupancy verifier requires ordinary confirmatory trajectories with no
  declared enhanced sampling and no runner-detected production-time force
  beyond the frozen base System except the required barostat. It does not yet
  attest how that agent-chosen base System was constructed.
- The v2 primary score is the deterministic conjunction of `valid_execution`,
  `claim_supported`, and `truth_agreement`; unresolved receives zero.

Still to do:

- Run the new S01 blindly with the current OpenMM stack and confirm that pressure
  response, wet/dry transitions, folded-state retention, and initialization
  sensitivity are observable within the budget before freezing the release.
- Add artifact-recomputable free-energy and thermodynamic evidence contracts
  before promoting S02-S04 from the extended tier.
- Harden the runner/solver isolation boundary before promoting S01 to a primary
  leaderboard.
- Add independently recomputable generic metrics only when real submissions need
  them; do not turn that metric list into a prescribed analysis plan.
- Add engine-specific adapters beyond the released OpenMM/MDClaw path when
  Amber/GROMACS benchmark runs are needed.

## What Not To Do Yet

- Do not make LLM judge responsible for chemistry that can be checked from
  artifacts.
- Do not score agreement with a canonical PDB, replica count, or exact sampling
  plan. Fix only the observable and evidence floor required to make outcomes
  comparable.
- Do not let experimental agreement compensate for missing MD grounding.
- Do not expose `task.json`, hidden truth, scorer prompts, or reference poses to
  evaluated agents.
- Do not require MDClaw-internal artifact names in the public prompt; prep
  battery v0.3 requires a public OpenMM topology triple, while backend-specific native
  adapters are deferred.
- Do not add full equilibration or production MD to the prep battery. Those
  belong in execution or scientific reasoning suites.

## Source Notes

- ProTherm v4.0: thermodynamic data for proteins and mutants with experimental
  methods, structural, functional, and literature information:
  <https://academic.oup.com/nar/article/32/suppl_1/D120/2505278>
- ThermoMutDB: manually curated thermodynamic data for wild-type and mutant
  proteins:
  <https://academic.oup.com/nar/article/49/D1/D475/5937085>
- SKEMPI 2.0: binding free-energy, kinetics, and thermodynamics changes upon
  mutation for structurally resolved protein-protein interactions:
  <https://academic.oup.com/bioinformatics/article/35/3/462/5055583>
- PDBbind methodology: experimental binding affinity data linked to
  protein-ligand complex structures:
  <https://pubs.acs.org/doi/abs/10.1021/jm048957q>
- RCSB structure anchors: 1AKE/AP5, 1DAP/NDP, 1CLL/Ca2+-calmodulin,
  2LZM/T4 lysozyme, 181L/T4L L99A-benzene, 4RBQ/oligo(U) RNA,
  5MVQ/DNA dodecamer, 5PTI/BPTI disulfides, 1UBQ/ubiquitin,
  2K39/NMR ubiquitin ensemble, 4Q5T/MSE+altconf cleanup,
  1STP/streptavidin tetramer, 2MS2/many-chain capsid, 5AWL/chignolin,
  5K9P/SEP, 6YA2/NAG glycan, 1BNA/DNA duplex, 2LOP/TMEM14A membrane protein:
  <https://www.rcsb.org/structure/1AKE>,
  <https://www.rcsb.org/structure/1DAP>,
  <https://www.rcsb.org/structure/1CLL>,
  <https://www.rcsb.org/structure/2LZM>,
  <https://www.rcsb.org/structure/181L>,
  <https://www.rcsb.org/structure/4RBQ>,
  <https://www.rcsb.org/structure/5MVQ>,
  <https://www.rcsb.org/structure/5PTI>,
  <https://www.rcsb.org/structure/1UBQ>,
  <https://www.rcsb.org/structure/2K39>,
  <https://www.rcsb.org/structure/4Q5T>,
  <https://www.rcsb.org/structure/1STP>,
  <https://www.rcsb.org/structure/2MS2>,
  <https://www.rcsb.org/structure/5AWL>,
  <https://www.rcsb.org/structure/5K9P>,
  <https://www.rcsb.org/structure/6YA2>,
  <https://www.rcsb.org/structure/1BNA>,
  <https://www.rcsb.org/structure/2LOP>.
