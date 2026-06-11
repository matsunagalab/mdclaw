# MDAgentBench Suite Design

This design has been promoted into two focused benchmark suites:
`MDPrepBench-v0.1` for preparation workflows and `MDStudyBench-v0.1` for the
first scientific question / study-bundle tasks. The long-term goal is to keep
MDAgentBench organized around these two main suites:

1. **Preparation Workflow Battery**: can an agent turn structurally messy
   public inputs into topology-built, minimized MD-ready systems with
   agent-neutral provenance and a scorer-loadable OpenMM artifact bundle?
2. **Scientific MD Reasoning**: can an agent plan, run/analyze, and defend a
   scientific conclusion for an experimentally validated question?

Short engine sanity tasks still matter, but they should become gate or support
tasks rather than the intellectual center of the benchmark.

## Design Position

The main benchmark target should not be "did short MD reproduce experiment" as
a single score. That is too strong and confounds agent quality with convergence,
force-field limits, and observability. The better target is:

> For experimentally validated scientific questions, evaluate how well the
> agent designs the MD study, executes or stages the required artifacts,
> analyzes evidence, forms a conclusion, and calibrates uncertainty against the
> known experimental direction.

This keeps experimental truth as the anchor while avoiding a brittle
"same-result-or-zero" benchmark.

## Suite A: Preparation Workflow Battery

The current prep implementation has **25 tasks, P01-P25**. Each task exposes
only `prompt.md` and `submission_contract.json` to the evaluated agent. The
scorer keeps `task.json`, reference structures, hidden ligand poses, expected
component truth, and any truth/rescan material private.

Recommended scoring split:

- 70-85% deterministic artifact checks.
- 10-20% provenance and decision trace checks.
- 0-15% LLM judge for concise rationale, only where a choice must be explained.

The submission is agent-neutral, but prep battery v0.1 requires a common
OpenMM topology artifact format for completed submissions. MDClaw/OpenMM XML
triples are accepted directly, and other workflows can be used upstream if they
export `system.xml`, `topology.pdb`, and `state.xml` for scoring. Every
completed prep submission must include topology artifacts, a minimized
structure, and minimization evidence. OpenMM artifacts are reloaded and
rescanned for finite energy; native-only Amber/GROMACS validation is deferred
until backend adapters are added.

### Current Prep Contract

Every P01-P25 task requires these files in the submission directory:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimization_report.json`
- `minimized_structure.pdb`

Every completed prep submission must also set these manifest outputs:

- `outputs.topology`: OpenMM topology artifacts. This must include
  `system.xml`, `topology.pdb`, and `state.xml`.
- `outputs.minimized_structure`: a structure after minimization. In MDClaw DAG
  runs, prefer the `min` node artifact `minimized_structure.pdb`; when packaging
  a topology bundle directly, export the minimized coordinates from `state.xml`.
- `outputs.minimization_report`: normally `minimization_report.json`.

For MDClaw topology builds packaged without a standalone `min` node, `state.xml`
carries the topology-time minimized coordinates and `topology.pdb` carries the
atom/residue topology. To create the fixed benchmark file, export the state
explicitly:

```bash
mdclaw export_state_pdb \
  --topology-pdb-file topology/topology.pdb \
  --state-xml-file topology/state.xml \
  --output-pdb-file minimized_structure.pdb
```

The preferred MDClaw DAG path for MDPrepBench is `source -> prep -> solv -> topo
-> min`. Full equilibration and production remain outside the prep suite.
Topology-time minimization evidence is still accepted when a workflow packages a
topology bundle directly, but provenance should record that as the `min` stage
or the legacy alias `minimization`.

The standardized metrics fields are:

- `topology.backend`, `topology.build_success`, `topology.forcefield`,
  `topology.water_model`, and `topology.solvent_model`.
- `minimization.attempted`, `minimization.completed`,
  `minimization.energy_initial_kj_mol`,
  `minimization.energy_final_kj_mol`,
  `minimization.energy_is_finite`,
  `minimization.positions_are_finite`,
  `minimization.atom_count_preserved`, and `minimization.backend`.

All tasks include common deterministic checks for `topology_artifact_bundle`,
`openmm_system_load`, `openmm_energy_rescan`, and
`minimization_report_check`. Task-specific structure/component checks are also
mirrored onto the minimized structure when applicable. If a submission declares
`manifest.status = "completed"` but fails a topology/minimization critical
check, the scorer treats it as a failed prep submission rather than a partial
success.

### Current Prep Tasks

| ID | Theme | Candidate public input | Prompt | Main scorer checks | Priority |
|---|---|---|---|---|---:|
| P01 | Simple monomer preparation | PDB 2LZM | Task: Simple monomer preparation: retrieve T4 lysozyme chain A, clean it, prepare an explicit-water-compatible topology, and report that no unintended ligands were retained. | source PDB, explicit solvent, no BEN/AP5, topology-ready metadata, common topology/minimization checks. | 1 |
| P02 | Chain and ligand selection | PDB 1AKE | Task: Chain and ligand selection: prepare adenylate kinase chain A while retaining the AP5 ligand, even if the ligand is represented under a separate mmCIF label chain. | chain A selected, AP5 retained, ligand selection metadata, common topology/minimization checks. | 1 |
| P03 | Ligand pose preservation | PDB 181L | Task: Ligand pose preservation: Prepare the T4 lysozyme L99A-benzene complex from PDB 181L. Keep protein chain A and the deposited benzene ligand (BNZ) together, and preserve the crystallographic benzene pose. Do not submit a ligand-only structure. Some tools may list BNZ separately from the protein during inspection, so make sure it is still included. | hidden protein+BNZ RMSD reference, L99A residue, BNZ retained in prepared/minimized structures, common topology/minimization checks. | 1 |
| P04 | Multi-ligand inclusion and exclusion | PDB 3PWB | Task: Multi-ligand inclusion and exclusion: retain requested BEN/GOL-like ligands while excluding irrelevant buffer molecules and unrequested heterogens. | requested BEN/GOL retained, excluded heterogens absent, filtering metadata, common topology/minimization checks. | 1 |
| P05 | Charged cofactor-like ligand stress | PDB 1DAP | Task: Charged cofactor-like ligand stress: prepare DAP dehydrogenase with both deposited NDP cofactors (NADPH dihydro-nicotinamide-adenine-dinucleotide phosphate; chains C and F, auth chains A and B) without silently dropping either cofactor or changing its charge without provenance. | both NDP cofactors retained, charge/provenance metadata, common topology/minimization checks. | 2 |
| P06 | Supported metal ion retention | PDB 1CLL | Task: Supported metal ion retention: prepare calcium-bound calmodulin while treating Ca2+ as supported ions rather than generic ligands. | four Ca ions retained, ion parameter metadata, common topology/minimization checks. | 1 |
| P07 | Crystallographic ion triage | PDB 4RBQ | Task: Crystallographic ion triage: prepare oligo(U) RNA while retaining prompt-designated crystallographic K+ ions, excluding deposited crystallographic waters or buffer molecules as selected source components, and building an explicit-solvent topology/minimization system. | RNA residue retention from artifacts, K ion retention from artifacts, deposited-water/buffer triage from provenance/evidence, common topology/minimization checks. | 2 |
| P08 | Point mutation branch | PDB 2LZM | Task: Point mutation branch: prepare WT T4 lysozyme and a branched L99A mutant without overwriting the WT artifacts or shifting residue numbering. | A:99 ALA, branch parent recorded, WT/mutant artifacts separated, common topology/minimization checks. | 1 |
| P09 | Multi-mutant branch | PDB 2LZM | Task: Multi-mutant branch: apply L99A and M102Q from one prompt on a branched prep node. | A:99 ALA, A:102 GLN, mutation count recorded, common topology/minimization checks. | 2 |
| P10 | Disulfide auto/override | PDB 5PTI | Task: Disulfide auto/override: prepare 5PTI as a standard classical MD system, detect the canonical BPTI disulfides, and record any excluded experimental components. | three disulfide pairs recorded, detection method recorded, component disposition recorded, experimental deuterium excluded from prepared/minimized structures, common topology/minimization checks. | 2 |
| P11 | Specific residue protonation | PDB 2LZM | Task: Specific residue protonation: override the default pH assignment for chain A residue 11 so Glu11 is protonated as GLH. | requested GLH metadata, A:11 GLH with HE2 in prepared/minimized structures, common topology/minimization checks. | 1 |
| P12 | Phosphorylated residue restore | PDB 5K9P | Task: Phosphorylated residue restore: detect deposited SEP, clean the standard protein, restore phosphorylation, and build a topology-ready structure. | A:20 SEP with P atom, phosphorylation library metadata, common topology/minimization checks. | 1 |
| P13 | User-requested phosphorylation | PDB 1UBQ | Task: User-requested phosphorylation: apply phosphorylation to unmodified ubiquitin Ser20 and prepare the resulting SEP-containing system. | A:20 SEP with P atom, requested phosphorylation metadata, common topology/minimization checks. | 2 |
| P14 | Glycoprotein/glycan pass-through | PDB 6YA2 | Task: Glycoprotein/glycan pass-through: keep N-linked glycans as glycans rather than treating them as ordinary small-molecule ligands. | NAG retained in prepared/minimized structures, glycan metadata, common topology/minimization checks. | 1 |
| P15 | Standard DNA topology | PDB 5MVQ | Task: Standard DNA topology: prepare a DNA dodecamer without assuming protein or ligand defaults. | DNA type and library metadata, common topology/minimization checks. | 2 |
| P16 | Standard RNA topology | PDB 4RBQ | Task: Standard RNA topology: prepare RNA and choose an RNA-compatible force-field library. | RNA type and library metadata, common topology/minimization checks. | 2 |
| P17 | DNA duplex chain retention and neutralization | PDB 1BNA | Task: DNA duplex chain retention and neutralization: prepare both chains of the standard B-DNA duplex, select a DNA-compatible force-field library, and record counterion neutralization rather than treating the duplex as a single protein-like chain. | DNA type, two chains, DA/DC/DG/DT retained in prepared/minimized structures, neutralization metadata, common topology/minimization checks. | 2 |
| P18 | Membrane embedding and lipid composition | PDB 2LOP | Task: Membrane embedding and lipid composition: prepare TMEM14A in a mixed POPC:POPE:CHL1 membrane at a 2:1:1 species ratio. | membrane metadata, POPC/POPE/CHL1 retained in prepared/minimized structures, lipid ratio metadata, common topology/minimization checks. | 1 |
| P19 | Candidate/model selection | PDB 2K39 | Task: Candidate/model selection: select a specified NMR model/candidate before preparation rather than silently using model 1 or averaging the ensemble. | selected model/candidate metadata, selection reason, common topology/minimization checks. | 2 |
| P20 | Terminal capping | PDB 5AWL | Task: Terminal capping: prepare CLN025/chignolin from PDB 5AWL with an acetylated N terminus (ACE) and an N-methylamide C terminus (NME), and record the cap choices. | ACE/NME retained in prepared/minimized structures, cap choices recorded, common topology/minimization checks. | 1 |
| P21 | PDB cleanup, missing residues, and numbering | PDB 4Q5T | Task: PDB cleanup, missing residues, and numbering: resolve altloc choice, author numbering, MSE-to-MET handling, missing loops, termini, and whether to model or block. | MSE removed, cleanup/altloc/numbering/missing-residue decisions recorded, common topology/minimization checks. | 2 |
| P22 | Force-field/water model fidelity | PDB 2LZM | Task: Force-field/water model fidelity: honor a supported user-specified force-field/water pair, such as ff19SB with OPC, rather than silently falling back to defaults. | requested force-field/water pair and explicit solvent metadata, common topology/minimization checks. | 1 |
| P23 | Implicit vs explicit solvent | PDB 5AWL | Task: Implicit vs explicit solvent: respect an explicit implicit-solvent request and avoid creating an explicit water box. | implicit solvent metadata, no explicit waters/ions in prepared/minimized structures, common topology/minimization checks. | 2 |
| P24 | Assembly/biological unit choice | PDB 1STP, stress reference PDB 2MS2 | Task: Assembly/biological unit choice: generate or select the requested biological assembly by assembly_id while preserving source auth/label/operator provenance and stable chain identity. | source PDB, assembly_id, assembly chain identity map, operator provenance, chain count, common topology/minimization checks. | 1 |
| P25 | Specified ion concentration | PDB 5AWL | Task: Specified ion concentration: build an explicit-solvent chignolin system that honors 0.30 M KCl while preserving net neutrality. | explicit solvent, K/CL retained in prepared/minimized structures, 0.30 M KCl and neutralization metadata, common topology/minimization checks. | 1 |

Priority now indicates the preferred order for real MDClaw baseline smoke runs
and further curation. All 25 tasks are part of the active prep battery.

### Coverage Refinements

The P01-P25 list is broad enough for the first prep battery, but three details
should be treated as explicit coverage requirements rather than left implicit:

- **Assembly coverage is first-wave material.** P24 specifies `assembly_id`
  and the submission should expose enough provenance for the scorer to verify
  the generated biological unit. Use a normal dimer/tetramer case first, then
  add a many-chain stress case under the same P24 family or as a follow-up
  variant.
- **Many-chain identity must not depend on one-character PDB chain IDs.** In
  P24-style tasks, scoring should check stable component identity through
  provenance, source auth/label/subchain IDs, topology chain index, or an
  equivalent chain-identity map. Reused PDB chain labels are acceptable only if
  adjacent components and submitted metadata remain unambiguous.
- **PDB cleanup hazards belong in P21.** Missing loops alone are too narrow.
  P21 should also test altloc choice, insertion codes / author numbering,
  common nonstandard cleanup such as MSE, chain breaks, termini/capping, and
  explicit decisions to model or block.

### Backend Neutrality

The public prep benchmark should score reproducible MD-prep artifacts, not
MDClaw-specific policy names, internal node IDs, or local refusal codes. If a
backend truly cannot complete a public task, the submission can still explain
the concrete blocked stage through the standard manifest, provenance, and
evidence files when that task explicitly allows blocked outcomes.

### Prep Battery Scorer Extensions

The scorer now covers file presence, JSON checks, trajectory rescan, solvent
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
- `assembly_identity_check`: verify requested `assembly_id`, expected
  component count, source auth/label/subchain/operator provenance, output chain
  names, and stable identity mapping for many-chain assemblies. The first
  implementation is used by P24 and can be strengthened for many-chain stress
  cases.
- `pdb_cleanup_decision_check`: verify altloc selection, insertion-code /
  author-numbering preservation, MSE/nonstandard-residue handling, missing-loop
  decisions, and termini/capping decisions.
- `protonation_state_check`: verify user-specified residue protonation states
  where the artifact format makes that possible, including named residue sites
  rather than only global pH defaults.
- `candidate_selection_check`: verify a selected source-bundle candidate ID and
  selection reason.

Do not add a public prep-benchmark scorer primitive that requires MDClaw-local
codes. Backend-neutral blocked evidence can be validated through the standard
manifest/provenance contract when a task explicitly allows blocked submissions.

These should remain deterministic. LLM judge should only evaluate whether the
brief rationale explains a non-obvious choice, not whether the chemistry is
correct.

## Suite B: Scientific MD Reasoning

Current size: **3 tasks** in `benchmarks/mdstudybench/`. This suite should stay
small: roughly **3-5 carefully curated tasks** is enough unless a new task
covers a genuinely distinct scientific-answer pattern. Use experimental truth
as the anchor, but score the workflow in layers:

1. Study design: correct systems, controls, mutations, apo/holo state,
   replicates, and observables.
2. Preparation/execution artifacts: evidence that required systems were staged
   or run.
3. Analysis: metrics are present and relevant to the question.
4. Evidence consistency: conclusion agrees with the submitted metrics and
   figures.
5. Experimental direction: direction agrees with hidden experimental truth.
6. Calibration: confidence and limitations are appropriate for short MD.

Recommended scoring split:

- 25% study design and controls.
- 20% artifact completeness and provenance.
- 20% analysis metrics and internal consistency.
- 20% experimental truth direction.
- 15% calibration, limitations, and report quality.

This intentionally makes truth direction important but not the sole gate.

### Current Scientific Tasks

| ID | Question class | Candidate source | What is hidden | Scoring note | Priority |
|---|---|---|---|---|---:|
| S01 | Monomer stability mutation | T4L WT vs L99A | Experimental stability direction / ddG source | Split plan, evidence, truth direction, and calibration. | 1 |
| S02 | PPI hotspot mutation | Barnase-barstar D39A | Experimental binding direction / ddG source | Require interface observables and uncertainty calibration. | 1 |
| S03 | Study methods bundle | T4L WT vs L99A | Experimental stability direction / methods rubric | Ask agent to package methods, provenance, decision log, and calibrated evidence. | 1 |

Do not expand MDStudyBench just to increase task count. Possible future
additions should replace weaker tasks or add one clearly missing scientific
answer pattern, such as a protein-ligand affinity trend or a compact
multi-mutation ranking task.

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

1. Keep the public package prompt-only and agent-neutral: expose
   `prompt.md` plus `submission_contract.json`; keep `task.json`, truth files,
   and scorer details private to the harness. This is the current export
   behavior.
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
6. Keep MDStudyBench compact after prep scorer stability; stabilize S01-S03
   before considering at most one or two additional scientific-answer tasks.

### Current Prep Implementation Status

Implemented:

- P01-P25 task IDs are preserved under `MDPrepBench-v0.1`.
- The common prep contract now includes topology artifacts and minimization
  evidence.
- P17 is the standard DNA duplex/neutralization task; modified DNA/RNA is not
  part of the core prep battery.
- P20 is the terminal capping task; homology modeling is not part of the core
  prep battery.
- P24 uses `assembly_identity_check` and requires `assembly_id`,
  source auth/label or subchain identifiers, operator IDs, output chain IDs, and
  naming policy in the chain identity map.
- OpenMM submissions are strongly checked by loading `system.xml`,
  `topology.pdb`, and `state.xml`, then rescanning finite potential energy and
  finite positions.
- Public export omits evaluator-only `task.json`, `truth/`, and `scorer/`.
- Synthetic honest/wrong fixtures cover all 25 tasks and exercise topology
  absence, broken OpenMM XML, nonfinite minimization reports, and minimized
  structure component loss.

Still to do:

- Run real MDClaw reference submissions for all P01-P25 tasks, beginning with
  P01/P02/P03/P11/P24/P25.
- Add stronger deterministic checks for force-field/water metadata, ion
  concentration from box volume and ion counts, lipid composition tolerance,
  disulfide topology, candidate selection, and cleanup decisions.
- Add Amber/GROMACS-specific artifact reload adapters when external benchmark
  runs need them.
- Decide whether P24 should gain a many-chain stress variant under the same ID
  family or become a later separate task.

## What Not To Do Yet

- Do not make LLM judge responsible for chemistry that can be checked from
  artifacts.
- Do not score scientific tasks as "experiment matched = pass, otherwise fail."
- Do not expose `task.json`, hidden truth, scorer prompts, or reference poses to
  evaluated agents.
- Do not require MDClaw-specific artifact names in the public prompt; prep
  battery v0.1 requires an OpenMM topology triple, while backend-specific native
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
