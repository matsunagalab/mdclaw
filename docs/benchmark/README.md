# MDAgentBench Prep Battery

MDAgentBench is an artifact-based benchmark dataset for molecular dynamics
agents. The current dataset is a prep-only battery:
`MDAgentBench-prep-v0.1`.

The benchmark is agent- and backend-agnostic. An evaluated agent receives only
the public prompt and submission contract, then writes a standard submission
directory. The scorer reads the submitted artifacts and private task metadata;
it does not inspect chat transcripts or MDClaw-internal state.

## Current Scope

The current task set replaces the former v1.0 mixed benchmark. Scientific MD
reasoning tasks are intentionally deferred to a later suite. This battery asks
whether an agent can convert messy public structural inputs into MD-ready prep
artifacts with clear provenance.

Public benchmark tasks do **not** require MDClaw-specific guardrail codes.
MDClaw guardrail behavior belongs in ordinary MDClaw unit/regression tests.

## Family

| Family | What It Tests | Scored By | Tasks |
|---|---|---|---|
| Preparation Workflow Battery | Structure retrieval, chain/ligand selection, protonation, mutations, PTMs, glycans, nucleic acids, membranes, biological assemblies, ion concentration, and provenance. | File presence, JSON metadata checks, PDB residue/component rescans, ligand-pose RMSD recomputation, and artifact integrity checks. | P01-P25 |

The machine-readable scoring axis is still `preparation`. Secondary qualitative
axes can be added later via LLM judge payloads, but deterministic artifact
checks are the default.

## Dataset Layout

```text
benchmarks/mdagentbench/
  dataset.json
  schemas/
    task.schema.json
    submission_manifest.schema.json
    score.schema.json
  tasks/<task_id>/
    prompt.md          # public prompt for the agent under test
    task.json          # runner/scorer metadata; not given to agents
    truth/             # scorer-only reference material when needed
```

Export the agent-visible package before giving tasks to an external agent:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdagentbench \
  --output-dir benchmark_public/mdagentbench
```

The exported package contains only `dataset.json`, submission-facing schemas,
and per-task `prompt.md` plus `submission_contract.json`. It omits `task.json`,
`truth/`, and `scorer/`.

## Prep Tasks

| Task | Short Name | Public Anchor | Main Requirement |
|---|---|---|---|
| P01_prep_simple_monomer_t4l | Simple monomer | PDB 2LZM | Clean one protein chain and report explicit-solvent-ready prep. |
| P02_prep_1ake_chain_ap5 | Chain + ligand | PDB 1AKE | Include chain A and AP5 despite chain-label ambiguity. |
| P03_prep_ligand_pose_t4l_benzene | Ligand pose | PDB 181L | Preserve the crystallographic benzene pose. |
| P04_prep_multi_ligand_filter_3pwb | Ligand filtering | PDB 3PWB | Include requested ligands and exclude irrelevant heterogens. |
| P05_prep_dap_dehydrogenase_nadp | Charged cofactor | PDB 1DAP | Retain and document an NADP-like cofactor. |
| P06_prep_calmodulin_ca_ions | Supported ions | PDB 1CLL | Retain four Ca2+ ions as supported ions. |
| P07_prep_rna_crystallographic_ions | Ion triage | PDB 4RBQ | Prepare RNA while preserving designated K+ ions. |
| P08_prep_t4l_l99a_branch | Point mutation | PDB 2LZM | Branch WT to L99A without renumbering drift. |
| P09_prep_t4l_double_mutant | Multi-mutant | PDB 2LZM | Apply L99A and M102Q together. |
| P10_prep_bpti_disulfides | Disulfides | PDB 5PTI | Record canonical BPTI disulfides. |
| P11_prep_site_protonation_t4l_glu11 | Protonation | PDB 2LZM | Preserve explicit A:11 GLH protonation. |
| P12_prep_restore_deposited_sep | Deposited PTM | PDB 5K9P | Restore deposited SEP and PTM provenance. |
| P13_prep_user_requested_sep | Requested PTM | PDB 1UBQ | Convert Ser20 to SEP on request. |
| P14_prep_glycoprotein_glycan | Glycan | PDB 6YA2 | Preserve N-linked glycans as glycans. |
| P15_prep_standard_dna | DNA | PDB 5MVQ | Prepare DNA without protein defaults. |
| P16_prep_standard_rna | RNA | PDB 4RBQ | Prepare RNA with RNA-compatible metadata. |
| P17_prep_modified_nucleic_5mc | Modified DNA | PDB 6JV5 | Detect and document 5-methylcytosine handling. |
| P18_prep_membrane_mixed_lipids | Membrane | PDB 2LOP | Honor POPC:POPE:CHL1 = 2:1:1. |
| P19_prep_nmr_model_selection | Candidate selection | PDB 2K39 | Select a specified NMR model before prep. |
| P20_prep_homology_model_before_prep | Homology model | PDB 2LZM | Model from template/alignment before prep. |
| P21_prep_cleanup_altloc_mse_numbering | PDB cleanup | PDB 4Q5T | Handle MSE, altloc, numbering, and missing residues. |
| P22_prep_forcefield_water_fidelity | FF/water fidelity | PDB 2LZM | Honor supported ff19SB + OPC request. |
| P23_prep_implicit_solvent_chignolin | Implicit solvent | PDB 5AWL | Avoid explicit water when implicit solvent is requested. |
| P24_prep_biological_assembly | Biological assembly | PDB 1STP / 2MS2 | Record assembly_id and chain identity provenance. |
| P25_prep_kcl_ion_concentration | Ion concentration | PDB 5AWL | Honor 0.30 M KCl and neutrality. |

## Submission Contract

Every task requires a `submission/` directory with:

```text
manifest.json
metrics.json
provenance.json
evidence_report.json
prepared_structure.pdb
```

Individual tasks may inspect specific paths inside `metrics.json`, component
counts in `prepared_structure.pdb`, or scorer-side references under `truth/`.
For example, P11 checks both
`metrics.preparation.requested_protonation_state == "GLH"` and the submitted
PDB residue state for chain A residue 11.

## Scoring

Scoring is deterministic by default:

- `required_files` / `forbidden_files`
- `json_equals`, `json_min`, `json_min_length`, `json_allowed_values`
- `structure_component_rescan`
- `pdb_residue_state`
- `rmsd_recompute`
- artifact integrity checks such as byte floors and template-marker rejection

Run validation and scoring with:

```bash
conda run -n mdclaw mdclaw validate_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/P11_prep_site_protonation_t4l_glu11/task.json \
  --submission-dir benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/submission

conda run -n mdclaw mdclaw score_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/P11_prep_site_protonation_t4l_glu11/task.json \
  --submission-dir benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/submission \
  --run-id <run_id> \
  --output-file benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/score.json
```

## Developer Validation

```bash
conda run -n mdclaw pytest tests/test_benchmark -q
conda run -n mdclaw mdclaw --list-json
```

For design rationale and future scientific-task planning, see
[`vnext_task_design.md`](vnext_task_design.md).
