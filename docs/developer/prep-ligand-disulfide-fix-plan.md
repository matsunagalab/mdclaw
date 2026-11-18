# Fix Plan for Four Defects in the prep Stage (Disulfide Validation, CLI Types, Multi-Residue Ligands, Loop Completion)

Created: 2026-09-10, checkout `a4ad51c`. Rechecked the same day at `6168c15` (7 more commits): no changes to
`genesis/modeller.py` / `structure/*` / `guardrail_codes.py`, which are involved in the four issues; `_cli.py` only gained the receipt (C9),
and its exception path and type checks are unchanged. A pre-implementation plan.

**Status (2026-09-10 night): all five items implemented.** Unit tests added, golden files regenerated, and the three real systems rebuilt from the deposit
with `studies/wang/check_hum_builds.py` passing (details in the same-day entry of `docs/memo.md`).
One additional defect found during implementation (exact-site number restoration gives up entirely when there is a ligand block
in the template) has also been fixed in `_restore_template_frame`.
Basis: the record of building the three states of the Wang 2025 human/mouse sweet-taste receptor ECD (9OPW / 9OPZ / 9OQ1) as
`studies/hum-ecd-*`
([hum-systems.md](../research/t1r_ecd_campaign/hum-systems.md)).
Line numbers are as of `6168c15`.

## Summary of Findings

| # | Symptom | Cause | Assessment |
|---|---|---|---|
| 1 | An explicitly declared disulfide is refused at the deposit's S–S of 1.30 Å. With auto-detection the same pair is accepted | The 1.8–2.3 Å validation window applies only to explicit pairs. Auto-detection has no lower bound | Consistency defect |
| 2 | `modeller_from_alignment --disulfide-patches` crashes and the node is left running | A bare `list` type is treated as a string. The exception occurs after `begin_node` and before the `try`, and the CLI does not fail tools that do not require a node | Defect. Sealing after failure is by design |
| 3 | Using a MODELLER output PDB as the source splits sucralose into 2 residues and prep fails | The ligand unit is the gemmi subchain. For PDB, a subchain is auto-generated per hetero residue | Defect (multi-residue ligands via PDB in general) |
| 4 | The missing loop is built without the ligand present and clashes with sucralose at 0.25 Å in 9OPZ. No warning | Non-polymer chains are deleted from the template before repair | Known limitation (the 9UTC note in `docs/memo.md` 2026-09). The lack of an automatic check is the point to improve |

## Assumptions and Scope of Impact

- The login-side CLI runs through `bin/mdclaw`, which overlays the checkout with `PYTHONPATH`
  (`bin/mdclaw:154`). All four issues are in the prep stage, so the fixes can be verified without rebuilding the SIF.
- The compute-node side (min/eq/prod) uses the packages built into the SIF via `source_mode=image` in
  `.mdclaw_cluster.json`. None of the four issues concern the compute-node side.
- Do not touch the 24 jobs running in the closed campaign (old SIF).
- Do not rewrite existing terminal nodes or past prep results.

## 1. Making Disulfide Validation Symmetric

Main targets: `mdclaw/structure/clean_protein.py` (`DISULFIDE_BOND_MIN/MAX_ANGSTROM` lines 1259–1260,
`_validate_declared_disulfides` lines 1263–1316, call at line 1965),
`mdclaw/structure/prepare_complex.py` (line 2444, `_merge_disulfide_pairs` lines 1450–1459,
`sa_disulfide_pairs` line 1776), `mdclaw/structure/disulfide.py`
(`_detect_disulfide_candidates` from line 326, high classification at line 400).

- Split the validation result into "bond not formed (too long)" and "atoms overlapping (too short)".
  Too long: error as now, `modeller_disulfide_not_formed`.
  Too short: warning, new code `disulfide_sg_overlap`. Record the distance and
  `geometry: "overlap"` in `distances`, and also list it in `confirmation_needed.disulfide_bonds`.
  Reason: loop rebuilding keeps observed residues fixed, so an overlap coming from the deposit is not fixed by MODELLER;
  it is the kind of defect that minimization resolves.
- Apply the same classification on the auto-detection side. In `_detect_disulfide_candidates`, tag candidates under 1.8 Å with
  `geometry: "overlap"` and emit the same text in prep's warnings. Do not change `confidence` /
  `recommendation` (accepting the pair is itself correct).
- For both explicit and automatic modes, measure the pairs on the input structure before MODELLER repair and keep the deposit-side values as
  `declared_disulfide_validation.input_distances`. Placed alongside the post-repair values, the result JSON alone
  distinguishes "MODELLER broke it" from "it was short from the start".
- In `disulfides` of the receipt (`mdclaw/_receipt.py:_facts_prep` lines 154–216, added in 6168c15), show
  overlapping pairs in the form `A59-A102 (overlap 1.30 A)`.
- Register `disulfide_sg_overlap` in `mdclaw/guardrail_codes.py`, with the remedy text
  "Resolved by minimization. To reposition SG, …".
  Update the golden file of `tests/test_guardrail_code_registry.py`.
- In auto-detection mode, no DISU patches are passed to MODELLER (`sa_disulfide_pairs=None`).
  This is by design as described in the skill docs, so it is not changed now, but make it visible by emitting
  `modeller_patches: 0` next to `disulfide_source` in the result JSON.

Tests: in `tests/test_disulfide_contract.py`, three cases with a synthetic PDB at SG–SG 1.30 / 2.05 / 3.53 Å
(warning / ok / error). Using the monkeypatch approach of `tests/test_complex_missing_residue_repair.py`, check that
`overall_status=success` with a warning attached even when the repair result contains an explicit pair at 1.30 Å. In
`tests/test_disulfide_schema_handoff.py`, check that the new fields are additive.

Done when: with 17 pairs declared explicitly on the 9OQ1 deposit, prep succeeds and A59–A102 / B236–B522 are
reported as `disulfide_sg_overlap` warnings. Auto-detection gives the same warning on the same two pairs.

## 2. Bare `list` Types in the CLI, and Nodes Left Running After an Unhandled Exception

Main targets: `mdclaw/genesis/modeller.py` (signature lines 724–725, `begin_node` line 934,
config generation lines 940–964, `try` line 965, `fail_node` line 1005), `mdclaw/_cli.py`
(`_takes_json` line 439, else branch line 690, `unhandled_exception` from line 1728,
`_record_cli_node_failure` line 733), `tests/data/cli_contract.json`.

2a. Type fixes
- Change to `disulfide_patches: Optional[list[list[int]]]`, `target_residue_sites: Optional[list[dict]]`.
  A full scan of `--list-json` shows these two are the only arguments with a bare `list` (audit of 2026-09-10).
- When `_tool_param_specs` detects an argument annotated with a bare `list` / `List` / unsubscripted `dict`,
  raise a `TypeError` at startup (do not silently fall back to `str`).
  Add a check "no tool argument has a bare list annotation" to `tests/test_cli_contract.py`.
- Regenerate the golden `tests/data/cli_contract.json` following the procedure at the top of the file.

2b. Node status
- `modeller_from_alignment`: move argument validation and config generation (including the unpack at line 960) to
  before `begin_node`. Wrap everything after `begin_node` in a single `try/except Exception` and always call
  `fail_node(job_dir, node_id, errors=[...])`. The current `except` covers only the
  `subprocess` execution part.
- Exception path in `_cli.py`: regardless of `requires_node`, if `effective_job_dir` /
  `effective_node_id` (resolved at lines 1504–1509 even for tools that do not require a node) are both present
  and `read_node(...).status == "running"`, call `fail_node` and then
  `_record_cli_node_failure`. The latter only saves evidence and does not change the status
  (lines 733–775).
- Keep "failed nodes are terminal and immutable" as the specification. Turn the `NodeSealedError` raised when re-running
  a sealed node into a structured error with guidance to create a new node with `create_node --parent-node-ids`
  (currently `unhandled_exception`).

Tests: in `tests/test_modeller_runner_contract.py`, check that via the CLI
`--disulfide-patches '[[3,40]]'` reaches the config as integer pairs.
In `tests/test_failure_trace.py`, call from the CLI a fake tool that raises an exception after `begin_node`, and check that
`status=failed` and a `tool_failed` event remain after it exits.

Done when: re-running with the same arguments as the first failure on 2026-09-10 passes integer pairs rather than a raw string,
and the node becomes `failed` even if execution fails.

## 3. Multi-Residue Ligands via PDB

Main targets: `mdclaw/structure/split.py` (subchain traversal at lines 338 and 1668, description of auto subchains
at line 1295, output names lines 1926–1930), `mdclaw/structure/clean_ligand.py` (multi-residue merging from line 393),
`mdclaw/genesis/modeller.py` (`_stage_template_as_pdb` line 328, `_restore_template_frame` line 189).

3-i. Group into covalent units on the split side (main fix)
- After subchain classification, group ligand-type units into connected components in the following order:
  (a) Covale records in `structure.connections` (mmCIF `struct_conn`, PDB `LINK`),
  (b) if there are no records, pairs within the same author chain with a heavy-atom distance of 1.9 Å or less.
  Ion, water and glycan types are excluded. Bonds across author chains and covalent bonds to the protein are
  not grouped but reported as `covalent_partner_outside_unit`.
- Write a grouped unit to one file, with the `unique_id` of the first residue, all residue names in `residue_names`,
  and `merged_from` kept in the split metadata. `include_ligand_resnames` selects the whole unit
  if any of its residue names matches.
- The merging in `clean_ligand` can be used as is (for a deposit mmCIF this path already produces a 23-atom
  sucralose).

3-ii. MODELLER tool side (optional)
- With `hetatm=True` and an mmCIF template, keep the Covale records of `struct_conn` and the entity types,
  and also write the model after applying `template_frame` as `<model>.cif`.
  Not required if 3-i is in place.

Tests: in `tests/test_ligand_pathway.py`, with a small fixture of just the two sucralose residues of 9OQ1 as PDB
(`tests/data/sucralose_rry_rrj.pdb`, no protein needed), check that there is one unit with 23 atoms,
that removing the `LINK` lines gives the same result by distance, and that two unbonded ligands in the same chain
stay separate. Check that the existing path for mmCIF branched entities is unchanged.

Done when: prep using the MODELLER output PDB of `studies/wang/models/9OPZ` as the source
succeeds with the full SMILES specified, and `ligand_chemistry` is one molecule of 23 heavy atoms.

## 4. Ligand Absent During Loop Completion

Main targets: `mdclaw/structure/clean_protein.py`
(`_repair_missing_residues_with_modeller` line 1561, non-polymer deletion lines 1655–1685,
`_write_repair_alignment` line 805, recombination from line 1976, `segments`/`sites` lines 1868 and 2017),
`mdclaw/guardrail_codes.py`.

4a. Distance check (do first, small)
- After validating the repaired model (line 1965), measure the minimum distance between the heavy atoms of each rebuilt segment and the
  heavy atoms of `nonpolymer_context` (ligands, ions), and record it as
  `complex_missing_residue_repair.segments[i].min_distance_to_nonpolymer_angstrom` together with
  the nearest residue and atom.
- Under 2.2 Å: `success=False`, new code `modeller_loop_nonpolymer_clash`, with the remedy text
  "Re-run with 4b's `--repair-nonpolymer-context`, or rebuild the source with `modeller_from_alignment --hetatm`
  so that the template includes the ligand."
  2.2–3.0 Å: warning.
- Put the thresholds in constants and emit them in the result JSON. Add
  `rebuilt_segments: [{chain, range, min_distance_to_nonpolymer}]` to the prep `facts` of the receipt.

4b. Keep the ligand in the template as an obstacle (root fix, medium)
- Do not delete non-polymer residues (other than water) from `polymer_template`; keep them after the polymer chains as
  HETATM.
- `_write_repair_alignment`: append `/` and one `.` per hetero residue to the end of the template and target lines,
  and set `LAST:` in the header to the hetero chain. The runner's `hetatm` is already True.
- `_validate_modeller_repair_model`, `_restore_template_frame` and renumbering skip hetero residues.
  In the manual run on 2026-09-10 (`studies/wang/models`), with `template_frame=True`, CA correspondence for models
  containing hetero residues was obtained without problems.
- Discard the hetero coordinates MODELLER writes back and, as before, recombine the original coordinates of `nonpolymer_context`
  (BLK is treated as fixed, but using the original coordinates keeps downstream behaviour unchanged).
- Flag `--repair-nonpolymer-context` (default on). If a MODELLER run including BLK fails,
  fall back to the old path with a warning. The 4a check always runs regardless of the path.

Tests: for 4a, extract `_nonpolymer_clearance(model_path, context, segments)` into a function and test three cases,
1.0 / 2.5 / 5.0 Å, with a synthetic model and ligand coordinates. For 4b, in
`tests/test_modeller_runner_contract.py`, check the number of `.` blocks and the `LAST:` of the alignment, plus one smoke test that runs only
in environments with `KEY_MODELLER` (the skipif approach of `tests/test_gap_local_modeller_smoke.py`).

Done when: prepping the 9OPZ deposit with `--missing-residue-method modeller` stops with
`modeller_loop_nonpolymer_clash` at the 4a-only stage, and after 4b is applied it succeeds with loop 45–57 at least 3 Å
away from sucralose. Results for 9OQ1 (loop 45–57 observed) and 9OPW (no ligand) do not change.

## Order of Work and Estimates

1. 4a distance check (0.5 day): first close the only path by which a broken structure passes silently.
2. 2a + 2b (0.5 day): small, and makes re-running other work safe.
3. 1 (0.5 day).
4. 3-i (1 day).
5. 4b (1–2 days). 3-ii after 3-i if needed.

At each stage, run `conda run -n mdclaw ruff check mdclaw/` and
`pytest tests/test_registry.py tests/test_cli.py tests/test_guardrails.py
tests/test_guardrail_code_registry.py tests/test_slurm_server.py tests/test_cli_contract.py
tests/test_disulfide_contract.py tests/test_complex_missing_residue_repair.py
tests/test_modeller_runner_contract.py tests/test_ligand_pathway.py`.
For confirmation on real data, reproduce the same procedure as `studies/_attempts/` in a scratch study
(the mmCIF of deposits 9OPZ / 9OQ1, `studies/wang/disulfides_hum.json`, full SMILES).

## Out of Scope

- Changing the specification to pass DISU patches to MODELLER in auto-detection mode (only recorded as a separate issue).
- Automatically repositioning the deposit's SG coordinates (the chi1 search in `studies/wang/build_hum_templates.py` stays
  as a research-side procedure).
- Relaxing the immutability of terminal nodes.
- Rebuilding the SIF (bundled at release time).
