# MDAgentBench vNext Task Design

This design has been promoted into the current prep benchmark implementation:
the former mixed v1.0 task set has been replaced by
`MDAgentBench-prep-v0.1`. Scientific MD reasoning tasks are intentionally
deferred. The long-term goal is still to organize MDAgentBench around two main
suites:

1. **Preparation Workflow Battery**: can an agent turn structurally messy
   public inputs into MD-ready systems with backend-neutral provenance?
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

## Reclassifying The Former v1.0 Tasks

| Current task | vNext suite | Keep? | Notes |
|---|---|---:|---|
| T01 engine smoke | Engine gate | Yes | Keep as a cheap participant sanity check. Do not over-weight in leaderboard. |
| T02 metalloenzyme guardrail | General MDClaw regression, outside public benchmark | No | Useful MDClaw guardrail test, but unfair as a public agent benchmark because it scores an MDClaw-specific refusal policy/code. Keep the behavior in ordinary tests. |
| T03 T4L benzene ligand pose | Preparation Workflow Battery | Yes | Good ligand pose-preservation task; keep, but score via artifact rescans more than text. |
| T04 short T4L protein MD | Engine support | Maybe | Useful end-to-end integration check, but not central if prep battery grows. |
| T05 restart continuation | Engine support | Yes | Good backend artifact/restart check; should remain a support task. |
| T06 T4L L99A stability | Scientific MD Reasoning | Yes | Keep as one scientific reasoning anchor, but grade plan/evidence/calibration separately from truth direction. |
| T07 barnase-barstar D39A | Scientific MD Reasoning | Yes | Keep as PPI mutation anchor, same scoring philosophy as T06. |
| T08 T4L dynamics communication | Scientific MD Reasoning support | Merge/reshape | Fold into scientific evidence/reporting rubric or keep as reporting-only subtask. |
| T09 WT vs L99A methods package | Scientific MD Reasoning support | Merge/reshape | Fold into plan/methods rubric or keep as plan-only calibration task. |

## Suite A: Preparation Workflow Battery

Target size: **15-25 tasks**. Start with about 20-25. Each task should expose only
`prompt.md` and `submission_contract.json` to the agent. The scorer keeps
`task.json`, reference structures, hidden ligand poses, expected component
truth, and any truth/rescan material private.

Recommended scoring split:

- 70-85% deterministic artifact checks.
- 10-20% provenance and decision trace checks.
- 0-15% LLM judge for concise rationale, only where a choice must be explained.

The submission should be backend-neutral: MDClaw XML triples, OpenMM scripts,
GROMACS outputs, or other formats are acceptable if the required public
artifacts and metrics are present and the scorer can verify the task-specific
properties.

### Proposed Prep Tasks

| ID | Theme | Candidate public input | What the agent must do | Main scorer checks | Priority |
|---|---|---|---|---|---:|
| P01 | Simple monomer prep | T4 lysozyme WT, PDB 2LZM | Fetch/select one protein chain, clean, solvate, build topology. | protein atoms retained; no unintended ligands; explicit solvent; topology artifacts reload. | 1 |
| P02 | Chain + ligand selection | Adenylate kinase AP5, PDB 1AKE | Include protein chain A and the AP5 ligand even though the ligand has a separate label chain. | AP5 present; ligand chain included; ligand params/artifacts recorded; no stale ligand omission. | 1 |
| P03 | Ligand pose preservation | T4L L99A + benzene, PDB 181L | Prepare the protein-ligand complex without dropping chain A or moving the crystallographic ligand pose. | ligand heavy-atom RMSD to real 181L protein+BNZ hidden reference; A:99 L99A protein residue retained; topology artifacts; provenance. | 1 |
| P04 | Multi-ligand inclusion/exclusion | Current integration seed PDB 3PWB | Include requested BEN/GOL-like ligands while excluding buffer ions/irrelevant heterogens. | requested ligands present; excluded heterogens absent; ligand params per ligand. | 1 |
| P05 | Charged/cofactor-like ligand stress | DAP dehydrogenase + deposited NDP, PDB 1DAP | Handle the NDP/NADPH-like dinucleotide cofactor in chains C/F without hanging or silently changing charge. AP5 is already covered by P02, so this task uses a different charged cofactor family. | cofactor present; ligand charge/provenance recorded; topology completes within budget or fails with a structured ligand-parameter reason; no long-running parameterization hang pattern. | 2 |
| P06 | Supported metal ion retention | Calmodulin + Ca2+, PDB 1CLL | Prepare calcium-bound calmodulin while treating Ca2+ as supported ions rather than generic ligands. | four Ca2+ ions detected/retained; ion parameter source recorded; topology artifacts reload. | 1 |
| P07 | Crystallographic ion triage | 32 bp oligo(U) RNA, PDB 4RBQ | Prepare an RNA structure while preserving prompt-designated crystallographic K+ ions and excluding irrelevant solvent/buffer components. | RNA residue mapping; requested K+ ions retained; excluded waters/buffers absent; ion/provenance metadata recorded. | 2 |
| P08 | Point mutation branch | T4L WT 2LZM -> L99A | Prepare WT, branch mutant prep, preserve source/provenance. | mutation present; WT and mutant artifacts separated; residue numbering correct. | 1 |
| P09 | Multi-mutant branch | T4 lysozyme WT, PDB 2LZM -> L99A/M102Q | Apply two mutations from one prompt on a branched prep node. | both mutations present; no off-by-one chain/residue errors; WT and mutant artifacts separated; branch metadata. | 2 |
| P10 | Disulfide auto/override | BPTI, PDB 5PTI | Detect the canonical BPTI disulfides, or respect an explicit user override that enables/disables named pairs. | expected S-S bonds recorded; CYX/CYS consistency; topology artifacts; override provenance if supplied. | 2 |
| P11 | Specific residue protonation | Seed task: T4L Glu11 -> GLH; later add an enzyme active-site case | Set user-specified protonation states for named residues using supported Amber variants such as ASH/GLH/HID/HIE/HIP/LYN/CYM. | requested residue protonation states in output/provenance; residue identifiers match prompt; submitted structure preserves the requested residue name and required H atom; no silent default drift; unsupported residue classes fail with structured reason. | 1 |
| P12 | Phosphorylated residue restore | Ser20 phosphoubiquitin, PDB 5K9P | Detect SEP, clean standard protein, restore phosphorylation, build topology. | SEP restored at residue 20; phosaa library/provenance; topology artifacts. | 1 |
| P13 | User-requested phosphorylation | Ubiquitin WT, PDB 1UBQ, Ser20 -> SEP | Apply phosphorylation to an unmodified SER target. This pairs naturally with P12, which restores a deposited SEP at the same conceptual site. | target residue changed to SEP; phosaa provenance recorded; failed targets are fatal unless explicitly allowed. | 2 |
| P14 | Glycoprotein/glycan pass-through | TSWV glycoprotein, PDB 6YA2 | Keep N-linked glycans as glycans, not generic ligands. | glycan metadata/linkages; GLYCAM provenance; NAG-containing glycan retained. | 1 |
| P15 | Standard DNA topology | DNA dodecamer, PDB 5MVQ | Prepare nucleic acid without protein/ligand assumptions. | DNA library selected; nucleic residue mapping; topology artifacts. | 2 |
| P16 | Standard RNA topology | 32 bp oligo(U) RNA, PDB 4RBQ | Prepare RNA and choose RNA force-field library. | RNA library selected; residue mapping; potassium/water handling recorded; topology artifacts. | 2 |
| P17 | DNA duplex chain retention and neutralization | Standard B-DNA duplex, PDB 1BNA | Prepare both chains of a standard DNA duplex, choose a DNA-compatible force-field library, and record counterion neutralization. This replaces the earlier modified-nucleic-acid idea because modified DNA/RNA is outside the current supported MD-ready prep scope. | both DNA chains represented; standard DA/DC/DG/DT residues retained; DNA library and neutralization metadata recorded. | 2 |
| P18 | Membrane embedding + lipid composition | TMEM14A, PDB 2LOP, POPC:POPE:CHL1 = 2:1:1 | Prepare a small membrane protein in a specified mixed-lipid membrane rather than a default single-lipid box. | membrane flag; expected lipid species present; lipid species ratio within tolerance; water/box metadata; topology marked as membrane. | 1 |
| P19 | Candidate/model selection | Ubiquitin NMR ensemble, PDB 2K39 | Pick a specified NMR model/candidate before prep rather than silently using the first or averaged model. | selected model/candidate ID recorded; rank/selection reason; one concrete structure used; no ensemble collapse. | 2 |
| P20 | Homology modeling before prep | Template PDB 2LZM + prompt-provided T4L L99A target sequence/alignment | Generate a model from template/alignment, then prep the selected model. This must be a modeling workflow, not only a post-prep mutation branch. | model bundle; template/target alignment provenance; selected model metadata; prepared topology or structured block. | 2 |
| P21 | PDB cleanup, missing residues, and numbering | MSE/altloc cleanup case PDB 4Q5T; optional altloc/author-numbering stress PDB 1TRZ | Resolve common PDB cleanup hazards: alternate-location choice, author residue numbering with insertion codes, MSE-to-MET or structured nonstandard-residue handling, missing loops, termini/caps, and whether to model or block. | cleanup decisions recorded; selected altlocs/protonatable residues consistent; insertion-code/author numbering preserved in provenance; no silent residue renumbering; missing-residue decision recorded; topology if safe. | 2 |
| P22 | Force-field/water model fidelity | T4 lysozyme WT, PDB 2LZM, with supported ff19SB + OPC or ff14SB + TIP3P request | Honor the user-specified supported force-field/water pair rather than silently falling back to defaults. | requested force-field/water pair recorded; explicit solvent model matches prompt; topology artifacts reload; no backend-specific refusal code required. | 1 |
| P23 | Implicit vs explicit solvent | Chignolin CLN025, PDB 5AWL | Respect an explicit implicit-solvent request and avoid creating an explicit water box. | implicit model metadata; no explicit water/ion box artifacts; no mixed-mode topology. | 2 |
| P24 | Assembly/biological unit choice | Normal: streptavidin tetramer PDB 1STP, assembly 1; stress: bacteriophage MS2 capsid PDB 2MS2, assembly 1 | Generate/select the requested biological assembly by `assembly_id` rather than relying on the asymmetric unit by accident. Include normal oligomers and one many-chain stress case. | expected chains/components present; extra chains absent; `assembly_id`, source auth/label/subchain/operator provenance, output chain names, and chain identity mapping recorded; many-chain cases remain identifiable even if one-character PDB chain IDs are reused. | 1 |
| P25 | Specified ion concentration | Chignolin CLN025, PDB 5AWL, 0.30 M KCl explicit solvent | Build an explicit-solvent system that honors the user-specified salt species and ion concentration while preserving system neutrality. | K+/Cl- ion counts; net charge neutralized; requested concentration reproduced within tolerance from final box volume; metadata matches counted ions. | 1 |

Priority 1 tasks are the first implementation wave. Priority 2 tasks are useful
coverage but may require schema/scorer extensions or more curation.

### Coverage Refinements

The P01-P25 list is broad enough for the first prep battery, but three details
should be treated as explicit coverage requirements rather than left implicit:

- **Assembly coverage is now first-wave material.** P24 should no longer be
  provisional: the prompt can specify `assembly_id` (for example assembly 1),
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

### Guardrails Belong Outside The Public Benchmark

MDClaw-specific guardrail refusal tests should move to ordinary repo tests and
MDClaw regression coverage, not the agent-agnostic benchmark. They are valuable
for MDClaw, but unfair for other agents because the target is a local policy
and a stable MDClaw code, not a generally reproducible MD-prep artifact.

The old metal refusal case, PDB 2CBA, should be retained under
`tests/test_guardrails.py`, ligand/metal-pathway tests, or MDClaw-specific
benchmark smoke tests. The intentionally inconsistent ff19SB + TIP3P case
belongs with force-field catalog / guardrail unit tests. Public prep tasks may
still score honest blocked outcomes when a backend truly lacks an external
tool, but they should not require MDClaw guardrail names or make refusal itself
the primary success criterion.

### Prep Battery Scorer Extensions

The current v1.0 scorer already covers file presence, JSON checks, trajectory
rescan, solvent rescan, RMSD recompute, and caption/metrics consistency. Prep
battery expansion will likely need these deterministic check types:

- `structure_component_rescan`: count required protein/nucleic/glycan/ligand/
  lipid/ion components in submitted structures.
- `residue_presence`: confirm mutation/PTM/modified-nucleic residue identity at
  a chain/residue site.
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
  names, and stable identity mapping for many-chain assemblies.
- `pdb_cleanup_decision_check`: verify altloc selection, insertion-code /
  author-numbering preservation, MSE/nonstandard-residue handling, missing-loop
  decisions, and termini/capping decisions.
- `protonation_state_check`: verify user-specified residue protonation states
  where the artifact format makes that possible, including named residue sites
  rather than only global pH defaults.
- `candidate_selection_check`: verify a selected source-bundle candidate ID and
  selection reason.

Do not add a public prep-benchmark scorer primitive that requires MDClaw
guardrail codes. Backend-neutral blocked evidence can be validated through the
standard manifest/provenance contract when a task explicitly allows blocked
submissions.

These should remain deterministic. LLM judge should only evaluate whether the
brief rationale explains a non-obvious choice, not whether the chemistry is
correct.

## Suite B: Scientific MD Reasoning

Target size: **8-12 tasks** after the prep battery exists. Use experimental
truth as the anchor, but score the workflow in layers:

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

### Candidate Scientific Tasks

| ID | Question class | Candidate source | What is hidden | Scoring note | Priority |
|---|---|---|---|---|---:|
| S01 | Monomer stability mutation | T4L WT vs L99A | Experimental stability direction / ddG source | Keep current T06, but split plan/evidence/truth/calibration. | 1 |
| S02 | PPI hotspot mutation | Barnase-barstar D39A | Experimental binding direction / ddG source | Keep current T07, but require interface observables. | 1 |
| S03 | Stabilizing vs destabilizing mutation pair | ProTherm / ThermoMutDB curated pair | Direction and approximate magnitude bins | Ask agent to compare two mutations and rank direction. | 1 |
| S04 | Protein-ligand affinity trend | PDBbind-related congeneric pair TBD | Higher/lower affinity direction | Focus on interaction/water/contact evidence, not absolute affinity. | 2 |
| S05 | Apo vs holo dynamics | T4L L99A apo vs benzene-bound, or similar | Known ligand-bound structural/dynamic expectation | Grade plan and analysis consistency more than final direction. | 2 |
| S06 | PPI alanine scan mini-panel | SKEMPI 2.0 selected 3-5 mutations | Rank/order or direction bins | Strong candidate once harness supports panels. | 2 |
| S07 | PTM effect on local stability/interface | phosphoprotein case TBD | Experimental functional/stability effect | Needs careful curation; not first wave. | 3 |
| S08 | Nucleic acid modification effect | 5-formyl/5-methyl cytosine structural series | Known structural conclusion from paper | Good for plan/evidence, but not classic protein MD. | 3 |
| S09 | Membrane protein mutation or ligand state | curated GPCR/channel case TBD | Functional/structural direction | Expensive; likely advanced suite. | 3 |
| S10 | Methods-only rescue task | same as S01/S02 but plan-only | Hidden rubric, not hidden numeric truth | Good for cheap evaluation of study planning. | 1 |

Recommended first wave: S01, S02, S03, S10. The others need more curation or
runtime budget.

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

1. Replace the former mixed v1.0 task set with the prep-only dataset:
   - Keep the submission schema compatible where possible.
   - Set dataset `benchmark_version` to `MDAgentBench-prep-v0.1`.
   - Keep the public submission contract prompt-only and backend-neutral.
2. Implement scorer extensions for prep deterministic checks.
3. Build the first prep wave in three small implementation slices:
   - **Slice A, reuse current task machinery**: P03, P11, P22. These extend
     existing ligand-pose, protonation, and force-field/water metadata checks.
   - **Slice B, add component and metadata rescans**: P02, P06, P08, P12, P14,
     P25. These need residue/component presence checks, branch provenance,
     supported-ion retention, phosaa/glycan metadata, and ion-concentration
     counting.
   - **Slice C, add richer structural accounting**: P18 and P24-normal. These
     need lipid-composition counting and assembly-identity provenance checks.
   - Add P24-many-chain after P24-normal passes with at least one MDClaw and
     one non-MDClaw-style submission.
4. Then expand the second prep wave: P01, P04, P05, P07, P09, P10, P13, P15,
   P16, P17, P19, P20, P21, P23.
5. Run MDClaw as the reference baseline and save expected artifact patterns.
6. Export the public package and run at least one non-MDClaw baseline:
   - simple script baseline,
   - LLM-only/no-run baseline,
   - one external MD tool/harness when available.
7. Only after scorer stability, expand to scientific tasks S01-S03/S10.

### Concrete Prep Implementation Plan

1. **Schema and dataset metadata**: add suite/family metadata without changing
   the public submission shape; keep prompts agent-agnostic and expose only
   `prompt.md` plus `submission_contract.json`.
2. **Scorer primitives**: implement the missing deterministic checks in this
   order: `residue_presence`, `structure_component_rescan`,
   `topology_metadata_rescan`, `protonation_state_check`, `ion_concentration_check`,
   `lipid_composition_check`, `assembly_identity_check`, then
   `pdb_cleanup_decision_check`.
3. **Reference truth generation**: for each task, store scorer-side reference
   JSON under `truth/` with expected components, residue IDs, ligand IDs,
   assembly IDs, tolerated count ratios, and backend-neutral blocked reason
   categories only when a task explicitly allows blocked submissions. Do not
   put tool-specific MDClaw node IDs or guardrail codes in public benchmark
   truth unless the check is explicitly marked MDClaw-internal.
4. **Task creation order**: create or migrate tasks as
   P03 -> P11 -> P22 -> P25 -> P24-normal -> P02 -> P06 -> P08 -> P12 ->
   P14 -> P18. This gives quick scorer feedback before adding membrane and
   assembly complexity.
5. **Baseline runs**: run MDClaw on every first-wave task, then run at least a
   minimal generic baseline that writes valid-but-incomplete submissions so the
   scorer proves it distinguishes success, honest failure, blocked, and
   fabricated artifacts.
6. **Promotion gate**: mark a prep task as accepted only after
   `validate_benchmark_submission`, `score_benchmark_submission`, dry-run
   coverage, fake-submission tests, and one MDClaw reference run all agree.

## What Not To Do Yet

- Do not rewrite all current task prompts before the suite design and scorer
  extensions are accepted.
- Do not make LLM judge responsible for chemistry that can be checked from
  artifacts.
- Do not score scientific tasks as "experiment matched = pass, otherwise fail."
- Do not expose `task.json`, hidden truth, scorer prompts, or reference poses to
  evaluated agents.

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
