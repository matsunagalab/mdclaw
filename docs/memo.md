# Working Memo

Running record of benchmark work: what was run, what the numbers were, what was
decided, and why. Newest entries go at the top. Append to this file as work
continues; do not rewrite past entries when a later finding contradicts them —
add the correction and say what it overturns.

---

## 2026-08-21 — Membrane ions came out drifting across y, and it was the stride's arithmetic

Spotted from a preview during the 9UWI validation run: ions above the bilayer
sat to one side, ions below to the other. Real, and 9UWI only.

    solv (initial)  upper n= 75 <y>=+21.8+-3.4   lower n=119 <y>=-16.3+-3.1
    5L7D            upper n=168 <y>= -2.4+-2.5   lower n=133 <y>= -4.3+-2.7

Water was uniform in y in both (<y> ~= -2), so the ions were not following the
solvent. Na+ and Cl- drifted the *same* way (+25.0 / +18.8 upper, -13.0 / -18.7
lower), which rules out electrostatics — a field separates the species, it does
not move them together.

### Cause

`_apply_neutralizing_swap` (`mdclaw/solvation/patch_membrane.py`) sorted the
candidate waters by `(z, x, y)` and took every `stride`-th one, `stride =
len(candidates) // needed`. Three facts combine:

1. Tiling copies the patch in x and y only, so copies keep z bit-for-bit.
   Measured: 9UWI held 32126 waters at only 3597 distinct z, 100% of them
   shared; 5L7D the same. The z key is therefore almost always tied and x/y
   decide the order, lining the list up with the 3x3 tile grid.
2. Offset k inside a slab maps to tile (k//3, k%3) — k%3 is the y column.
3. 9UWI's real stride was 150. 150 mod 9 = 6, gcd(6,9) = 3, so the walk visits
   offsets 0, 6, 3, ... — all k%3 = 0, one y column. Varying slab sizes (9 and
   18) and the carved-out regions drift that phase slowly with z, which turns a
   fixed column into a monotone y drift.

Replaying the selection with the true strides reproduces both systems:

    9UWI stride 150 (mod 9 = 6, gcd 3)  replay rho=+0.489   observed rho=+0.618
    5L7D stride 172 (mod 9 = 1, gcd 1)  replay rho=+0.074   observed rho=+0.069

and moving 9UWI's stride by one kills it (148/149/151/152 give +0.03..-0.07),
while pushing 5L7D onto 171 (gcd 9) or 174 (gcd 3) raises it to +0.18. **5L7D
was not immune, it was lucky.** The within-slab offset histogram agrees: 9UWI
depletes offsets 1, 4, 7 (all k%3 = 1) about threefold, 5L7D is flat.

The strides I first assumed (166 / 182, from the full water set) do not
reproduce it — the real candidate list excludes non-bulk and near-protein
waters, giving 150 / 172. Reading the chosen ions' ranks in the sorted pool
settled it: 0, 150, 300, 450, ... exactly.

### Wrong answers along the way

Uncapped termini (true, but a localised +-1 cannot move both species one way),
protein net charge and the extended ICL3 (the candidate pool is uniform in y at
every carve cutoff, rho ~= 0), and a species-specific rule (both species drift
together). An earlier fix in this same function had already dealt with a
species-ordering bug; this one is in the site selection underneath it.

### Fix

One site per equal-sized block of the sorted list, position inside the block
drawn from a fixed seed (`ION_PLACEMENT_SEED`). Blocks keep the z spread the
stride was there for; a seeded draw cannot resonate with the tile period. Same
seed, same placement, so a rebuild still reproduces bit-for-bit. Measured on
the real systems: 9UWI rho_y +0.489 -> -0.033, 5L7D +0.074 -> +0.051, z range
preserved.

Tests assert the mechanism rather than a correlation magnitude: a resonant
stride never leaves its tile column (spread 0.0), the shipped selection always
visits every column, across ion counts 150..320.

### Impact

None on the runs already done — MD relaxes it. 9UWI upper <y>: +24.0 initial,
+2.5 after eq, +4.4 after prod, i.e. inside 1 sigma by the end of equilibration.

It would have mattered for replicates. The selection is deterministic, so the
same system rebuilt for a second replicate got the identical biased placement;
changing only the integrator seed would not have decorrelated the ions. That is
exactly the kind of hidden correlation adaptive sampling cannot afford.

---

## 2026-08-21 — SIF 名から cufft121-fusefix を落としていた（私のミス）

`a34dba5bdb21` の SIF を渡したところ、名前に `cufft121-fusefix` が無いことを指摘された。
**中身は入っており、名前だけの誤りだった。** SIF から実測:

```
MDCLAW_CUFFT_MIN_VERSION = 12.1.0.78
同梱 cuFFT               = libcufft.so.12.1.0.78 (API 12100)
MDCLAW_FUSEFIX_LIB       = /opt/mdclaw/lib/libmdclaw_fusefix.so
  LD_PRELOAD に載る = True / プロセスに mmap 済み = True
NVRTC 13.0 / math libs 13.1
```

### 何を間違えたか

あの系列のタグは積み上げだった (`cuda130` -> `cuda130-cufft121` ->
`cuda130-cufft121-fusefix`)。私は「今回の目玉は PPM3」と考えて末尾を `ppm3` に
**置き換えた**。しかし 3 つのタグはいずれも**ホスト互換性の契約** — NVRTC/PTX が
13.0、sm_100 の PME に必要な cuFFT の下限、FUSE マウント対策の preload shim —
であって、「このファイルがその環境で動くか」を決めるもの。PPM3 / MODELLER /
UTF-8 モードはソフトウェアの機能で、git revision が既に一意に特定している。

### 決めた命名規則

```
mdclaw-rikyu-arm64-<ホスト互換性の契約>-<git rev>.sif
現行: mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-<rev>.sif
```

**名前に載せるのは契約だけ。機能は revision に任せる。** そうしないと機能追加の
たびにタグが伸びる。契約が変わったとき (CUDA 世代を上げる、shim が不要になる)
にだけ名前を変える。

`~/Downloads` の SIF は改名済み (内容は同一、SHA-256
`e5b989e5b9bf9a4eff70c0185b39438e9bcccfd13a28714f5cc4765be34c275f`)。
過去エントリ中の `...-ppm3-<rev>.sif` という表記は同じ理由で誤り。

---

## 2026-08-20 — Prep chemistry and missing-residue contracts corrected after independent review

This review was done with a second agent and checked by direct measurement, not
only by reading the diff. No commit was made during the review.

The OpenMM bundled in `mdclaw.sif` was measured directly. Its `pdbNames.xml`
aliases ASH→ASP, GLH→GLU, CYX→CYS, and HID/HIE/HIP/HSD/HSE/HSP→HIS. LYN, CYM,
TYM, and ARN have no alias. An ALA-ASH-ALA PDB loads as ASP through both
`openmm.app.PDBFile` and PDBFixer and writes back as ASP. This corrects the
earlier 2026-08-20 memo entry's “second correction”, which said the loader was
not at fault, and the old `pdb_utils.py` docstring, which listed LYN→LYS and
CYM→CYS as loader normalizations. Both were wrong in the same direction.

Consequently, the old `from_input_structure` protonation label could never be
true for the aliased names: it described a state re-derived after the input
decision had already been erased. With `--missing-residue-method modeller` the
scan was also reading the MODELLER model rather than the user's input. That
machinery was removed. Prep now scans the original PDB or mmCIF with gemmi and
promotes raw-input ASH/GLH/LYN/CYM into caller overrides; explicit overrides
still win. These residues survive PDBFixer/PDBFile normalization, are reported
as `user_override` with `override_origin=input_structure`, and no longer move
when `--ph` changes. CYX remains under the disulfide bond contract, and
histidine tautomers remain separate.

The recovery contract introduced in 8980391 was measured to be unexecutable. A
prep node that fails with `pdbfixer_missing_residues_out_of_scope` is terminal
and sealed; re-running that node with `--missing-residue-method modeller`
returns `node_terminal`, although the recommendation said verbatim “Re-run
this same node with the flag”. Recovery now creates a new prep node with the
same completed parent, and the result names both commands: `create_node` with
the explicit parent, then `prepare_complex` on the new node with the MODELLER
method.

The deleted glycoprotein pipeline test exposed a separate pre-existing
structural error rather than a regression. 6YA2 chain C really has a
13-residue internal gap from 194 to 208; the flanking CA atoms are 15.49 Å
apart. Before 8980391, chain splitting dropped SEQRES, PDBFixer saw zero
reference sequences and reported no missing residues, and no residue was
modeled. `openmm.app.PDBFile.createStandardBonds` then joined SER194 C to
PRO208 N at 17.26 Å without a distance check, against a 1.33 Å equilibrium
peptide bond. The test therefore asserted success on a structure containing a
17 Å peptide bond. The corrected visibility rule has a real blast radius:
structures with more than 10 internal missing residues, or any single gap
longer than 5, now stop at prep where they previously passed silently.
`tests/test_pipeline_glycoprotein_dag.py` was deleted at the user's direction;
that also removed the repository's only GLYCAM topology integration coverage,
which remains a known test gap.

The review also found that the guardrail golden was stale for five codes and
two MODELLER conversion codes were not registered. MODELLER in-place repair
accepted any existing output, including a one-atom “successful” model.
`_restore_template_frame` discarded insertion codes, so template residues
100A/100B collided and the model kept MODELLER's 1..N numbering behind a
success result. Repair now requires complete target length and sequence,
preserved observed residue identities, and complete author renumbering;
insertion codes round-trip, while genuine numbering collisions fail.

On the topology side, Amber ASH/GLH/LYN/CYM restoration could skip silently
and still build a System at the default ASP/GLU/LYS/CYS charge, one elementary
charge wrong per missed residue. The restore now reports unique candidates,
and final topology validation checks both restore counts and the
variant-specific hydrogen identity (ASH HD2, GLH HE2, LYN without HZ3, CYM
without HG) under `amber_variant_restore_incomplete`.

Finally, `confirmation_needed` labelled mixed auto-detected protonation and
predicted MODELLER loops as `user_override`; its policy explicitly permits
skipping prompts for that source. Provenance is now derived per entry,
predicted coordinates use `source=predicted` with a separate
`method_requested`, and terminal-only omissions are reported as UNMODELED.
The pdb4amber+reduce fallback now reports protonation states at all. The
measured 9UWI case, where 77 terminal residues were left unmodeled, now reaches
the confirmation report instead of disappearing.

---

## 2026-08-20 — Missing residues were invisible inside prepare_complex; gaps are now repairable in place

Two problems, one fix. Started from the MODELLER ordering trap (fetching a
template completes the job's only `source` node, and a completed node is sealed,
so `modeller_from_alignment` cannot then write to it). Every CLI structure-
acquisition tool is `@node_tool("source")` — verified by running
`fetch_structure --output-dir` and getting `code=node_context_required` — so
inside one job there is no correct order to document. The workaround used for
9UWI left `studies/9uwi-popc` with three registered jobs (`main`, `modelled`,
`modelled2`) of which two hold nothing but a source node, and with `main` and
`modelled` recording `job_dir` against different path roots.

**The bigger finding: `prepare_complex` could not see missing residues at all.**
`split_molecules` builds each chain as a fresh `gemmi.Structure()` holding only
the modeled residues, so SEQRES stayed behind in the parent. PDBFixer finds
gaps by comparing coordinates to that reference; with none it reports zero.
Measured on 4AKE:

    source candidate (candidate_001.cif):  SEQRES chains=2   <- reference present
    split chain file (protein_1.pdb):      SEQRES chains=0   <- gone

So `pdbfixer_missing_residues_out_of_scope` could never fire from the
`prepare_complex` path, and internal gaps entered MD as silent chain breaks.
All three studies confirm it: `missing_residue_repair` was `None` on every prep
node of 4ake-apo-trial, 5l7d-popc and 9uwi-popc.

### What changed

`_carry_reference_sequence` (`mdclaw/structure/split.py`) copies the owning
entity's `full_sequence` onto the extracted chain. The recipe matters:
`setup_entities()` on the *new* structure first (gemmi writes SEQRES from an
entity whose subchains match the chain being written), then fill the empty
`full_sequence` it produced. Attaching the parent entity directly writes no
SEQRES at all — tried it, got 0 lines.

`missing_residue_method` on `clean_protein` and `prepare_complex`, default
`pdbfixer`, alternative `modeller`. With `modeller` the gaps are rebuilt before
PDBFixer runs, in the same prep node: the chain is its own template and its own
SEQRES is the target, so no template file, no target sequence, and no second
source node are involved. The DAG stays `source_001 -> prep_001 -> ...`, one
job, and `NodeSealedError` never appears.

Deliberately not done: no automatic escalation and no upper ceiling. Rebuilding
a 33-residue ICL3 is a scientific judgement, so it happens only behind an
explicit flag.

### Impact on existing studies, measured before shipping

    4ake  SEQRES=214  internal=0   -> unchanged
    5l7d  SEQRES=638  internal=0   -> unchanged
    9uwi  SEQRES=386  internal=40 in 3 segments, max 33, terminal 77
                                  -> now OUT_OF_SCOPE (was silent)

One of three studies changes behaviour, and it is the one that actually needed
MODELLER. 5L7D turned out to have no internal gaps — worth recording, since I
had assumed a cryo-EM GPCR would.

### Verified end to end on 9UWI chain A

Default path stops with the code unchanged (`pdbfixer_missing_residues_out_of_scope`
is a public contract) but the first recommended option is now
`repair_in_place_with_modeller` carrying `--missing-residue-method modeller`,
where it used to say "regenerate the source" and point straight at the two-job
trap. MODELLER path: 40 residues rebuilt in 3 segments, longest 33, seed -8123.

Residue numbering survives: all 269 observed residues keep both number and name,
model 309 residues total. The first attempt produced 386 — the whole SEQRES,
including a fabricated 67-residue C-terminal tail — because the full reference
sequence was handed to MODELLER. Fixed by modeling only the span between the
first and last observed residue, so unresolved termini are left alone exactly as
the terminal filter intended.

One bug found by running it end to end rather than at unit level: detection
originally ran on the post-repair file, which is a MODELLER model with no
SEQRES, so the node reported `status="not_detectable"` for chain A directly
under a line saying 40 residues had just been rebuilt. Detection now describes
the structure as it was before repair (SEQRES 386, modeled 269, terminal
excluded 77).

### Also now reported

A chain with no reference sequence reports `status="not_detectable"` rather than
zero gaps — "not checked" and "none present" were indistinguishable before.
Terminal segments excluded from repair are reported with segment *and* residue
counts (9UWI: 2 segments, 77 residues); the old message counted segments while
saying "residue(s)". The MODELLER random seed and the template's sha256 go on
the node, because a loop that cannot be reproduced makes the whole study
irreproducible. In a multi-chain structure each repair is tagged
`interface_context: chain_isolated`, since chains are repaired from separate
files and an interface loop never sees its partner.

`tests/test_missing_residue_handling.py` builds its inputs with gemmi rather
than downloading them, so it runs on a compute node with no network. The
two-chain fixture uses deliberately different sequences and lengths per chain: a
chain-to-entity mix-up survives a same-length check, and 4AKE and 5L7D both have
identical chains, so neither would catch it.

Not in the shared SIF. Members run the container's own mdclaw.

---

## 2026-08-20 — MDDataBench を別リポジトリに切り出した

MDPrepBench / MDStudyBench と同じ形で `/home/yasu/tmp/MDDataBench` に独立させた。
mdclaw 側の `benchmarks/mddatabench/` と `docs/research/db_derived_benchmark_validation.md` は削除済み
(どちらも git 未追跡だったので履歴操作は不要)。**本エントリより前の 8/18-8/19 の各エントリが参照している
`docs/research/db_derived_benchmark_validation.md` は、いまは `MDDataBench/docs/validation-design.md` にある。**
過去エントリは規約どおり書き換えていないので、参照を辿るときはここを見ること。

**構成は MDPrepBench に合わせた。** hatchling + `mddatabench` コンソールスクリプト、
`mddatabench.TOOLS` を signature 由来のフラグでディスパッチする `__main__.py`、
`benchmarks/mddatabench/tasks/`、`tests/`、`.github/workflows/ci.yml`、MIT LICENSE、
CLAUDE.md と AGENTS.md の同一二枚。スクリプト群はパッケージモジュールに移した
(`subspace_test.py` -> `subspace.py`、`execution_check.py` -> `execution.py`、
`fetch_reference.py` -> `reference.py`、`score_submission.py` -> `scoring.py`、
`negative_controls.py` -> `controls.py`)。argparse の `main()` は全部ライブラリ関数に直して
`cli.py` の TOOLS から呼ぶ形にした。

**CLI は 4 つ**: `list_benchmark_tasks` / `fetch_benchmark_reference` /
`score_benchmark_submission` / `run_benchmark_negative_controls`。

**動作確認**: ruff clean、fast テスト 14 本 passed (0.62 s)、
`mddatabench score_benchmark_submission` で D01 が **prep 7/7 md 5/5 = 12/12 を 6.2 秒**。

**テストに入れた不変条件**: ライセンスが CC 系であること、bundle の SHA-256 が 3 ファイル分揃っていること、
全チェックが `prep`/`md` のどちらかに分類され `check_type` が `@1` 付きであること、md 側が
構造のみ帰無検定と時計の両方を持つこと、そして **prompt が accession / MDDB / MoDEL / DOI を漏らさず、
かつ PDB ID と採点対象の条件 (水モデル・温度・アンサンブル) は述べていること、`rmsip` を含まないこと**。
最後のはプロンプト最小化とリーク防止を機械的に守らせるためのもの。

初期コミット 29 ファイル / 328 KB、データは 0 バイト。GitHub remote は未作成 (ユーザ判断待ち)。

---

## 2026-08-20 — MODELLER now converts an mmCIF template instead of renaming it

Fixes trap 2 from the 9UWI entry below. `modeller_from_alignment` staged the
template into MODELLER's working directory with `shutil.copy2(template_path,
out_dir / f"{code}.pdb")` — extension change only. MODELLER picks its reader
from the file's contents, not its name, so an mmCIF under a `.pdb` name is not
degraded, it is unreadable: `read_pd_702E> ... file is probably corrupt` on the
first CIF line.

New `_stage_template_as_pdb` copies a PDB source and converts an mmCIF one via
gemmi (`make_structure_from_block` → `setup_entities` → `write_pdb`). The
conversion is reported as a warning, because PDB cannot hold everything mmCIF
can — residue names longer than three characters, more chains than single
letters. Failures return structured codes rather than a corrupt file:
`modeller_template_conversion_unavailable` (no gemmi) and
`modeller_template_conversion_failed` (unparsable input).

Verified against the file that produced the original failure: the full
`9UWI.cif` (562 residues, Atosiban included) passed straight to `--template-pdb`
with the 269-residue chain-A sequence now builds a model —
`selection_reason: lowest_dope_score`, DOPE -37149, CA RMSD after fit 0.748 Å —
where before it died in MODELLER's PDB parser. The staged `9UWI.pdb` contains
no `_atom_site.` or `loop_` lines. `tests/test_modeller_template_staging.py`
covers copy, conversion, the `.mmcif` suffix, and the unparsable case; 204 tests
and ruff pass.

The chain-A workaround written by hand for that run
(`studies/9uwi-popc/templates/9UWI_A.pdb`) is no longer needed for format
reasons. It is still the right input when the template should exclude the other
chains and the ligand — the tool converts the file it is given, it does not
subset it.

Not in the shared SIF. Members run the container's own mdclaw, so this and the
other fixes from 2026-08-19/20 reach them only on the next image rebuild.

---

## 2026-08-20 — 9UWI chain A through MODELLER into POPC; three traps on the way

Second member target, and the first real exercise of the MODELLER path baked
into the SIF: 9UWI (human V1a receptor, cryo-EM 2.8 A), chain A only, Atosiban
and the cholesterols dropped, the three internal gaps rebuilt, POPC bilayer,
1 ns production. It works, and the run found three things worth fixing.

**The gaps.** Chain A is observed 43-351 (269 residues) against a 386-residue
SEQRES, with internal gaps 80-84 (3), 157-162 (4) and 247-281 (**33**, ICL3).
Author numbering is offset -32 from SEQRES, so the target sequence has to be
built by aligning observed residues rather than slicing SEQRES by author number
(269/269 match at that offset). The 33-residue gap is above the default
`--loop-max-length 30`, so it needs raising or ICL3 stays unrefined — the skill
does warn about this. Target span 43-351 only: modelling the unobserved 1-42 and
352-386 would invent 77 residues of terminus flapping in solvent.

Result: 4 loop models, best by DOPE (-3226, molpdf 213), 309 residues, **no gaps
left**. `--template-frame` reported `ca_rmsd_in_place: 13.552` / `after_fit:
0.823`, but measuring it directly over the 269 common CA gives mean deviation
0.28 A and a centroid shift of 3.5 A — the model *is* in the template frame.
Whatever the reported 13.55 is measuring, it is not the in-place deviation an
agent would read it as. Worth a look.

**Trap 1: the skill never says when to run MODELLER relative to
`fetch_structure`.** Node mode writes into the source bundle, so the source node
must still be open — but `fetch_structure` completes it, and loop refinement
needs a template structure, which is exactly what you would use `fetch_structure`
to get. Running MODELLER after it gives `NodeSealedError`.
`skills/modeller-predict/` does not mention `fetch_structure` anywhere. Worked
around by fetching in one job and running MODELLER on a fresh source node in a
second job registered with `add_study_job`.

**Trap 2: `--template-pdb` accepts an mmCIF and does not convert it.** The file
is copied to `<code>.pdb` and handed to MODELLER as-is, which then fails with
`read_pd_702E> ... file is probably corrupt` at the first CIF line it cannot
parse as PDB. Converting to a real PDB first fixed it. Restricting the template
to chain A also dropped Atosiban's `A1EQM`, whose 5-character residue name has
no PDB representation.

**Trap 3: a protonated aspartate renamed lipids, and nothing said it was
there.** Two aspartates (97, 112) came back protonated, but
`confirmation_needed.protonation_states` was `{"source": "auto_detected",
"states": []}` — empty. `embed_in_membrane` then failed at the net-charge step:

    No template found for residue 399 (ASH). The set of atoms matches PA, but
    the residue has no bonds between its atoms.

`--protonation-states '{"A:97": "ASP", "A:112": "ASP"}'` gets past it. 5L7D never
hit this, so it is structure-dependent — any member whose receptor has a buried
Asp will.

**First correction: ff19SB does have an ASH template**, so "no template found"
is not about the force field lacking one. OpenMM matches templates by atom
composition, not by name.

**Second correction — the loader was not at fault either.** `pdbNames.xml`
registers `ASH` as an alias of `ASP` (and `HID`/`HIE`/`HIP` of `HIS`), so
`openmm.app.PDBFile` normalises it and bonds fine; measured on a synthetic
ALA-ASH-ALA, `ASH` loads as `ASP` with 14 bonds whether written as ATOM or
HETATM. `LYN` and `CYM` have no alias and are the ones that would load bare.
So a residue arriving at template matching still *named* `ASH` never went
through that normalisation — which points at MDClaw's own code.

**The actual cause is in `mdclaw/amber/openmm_build.py`.** The topology path
deliberately rewrites ASH/GLH/LYN/CYM to their CCD names so Pablo can identify
them, then restores the Amber names on the loaded topology. The histidine
restore guards on `residue.name != "HIS"`; the variant restore had no such
guard and renamed on `(chain, residue number)` alone. **That key is not unique
in an assembled membrane.** In this system chain A carries both the protein
`ASP` 97/112 and POPC `PA` residues numbered 97 and 112, so lipid tails were
renamed to `ASH`. The force field's "no template for residue 399 (ASH), the set
of atoms matches PA" was reporting precisely that: a PA residue wearing the
name ASH.

Two defects, then, and neither is the force field or OpenMM:

1. **Restore.** Fixed: `_restore_amber_variant_names` now checks the recorded
   chain and that the residue still carries the base name that was substituted,
   before renaming it back. Extracted to a module-level helper with tests in
   `tests/test_amber_variant_restore.py` covering the lipid, water and ion
   collisions and all four variants. Verified on the failing system: the same
   prep that produced `membrane_neutralization_failed` now embeds cleanly with
   both aspartates kept as ASH and the lipids untouched.
2. **Reporting.** `confirmation_needed.protonation_states` only ever carried
   caller-supplied overrides, never what pdb2pqr assigned — while
   `histidine_states` reads the produced structure. Fixed: added
   `_extract_non_default_protonation_states` and `_merge_protonation_states`,
   wired into all three recording sites in `clean_protein.py` and into the
   operation records the summary aggregates from. Reported names are kept in
   step with what the topology path can round-trip (ASH/GLH/LYN/CYM; ff19SB has
   no TYM or ARN template, so promising them would promise an unbuildable
   system). The failing 9UWI prep now reports both aspartates with the state
   they replaced.

The workaround used during the run — forcing the aspartates back to ASP — was
therefore treating a symptom. It is no longer needed.

**The run.** Orientation came from OPM homolog **7QVM** (identity 0.60, fit_rmsd
2.29 A, hydrophobic thickness 31.8 A), not from 9UWI itself — so unlike 5L7D
this exercised the homolog transfer rather than an exact self-match. Box
116 x 77 x 104 A, rectangular in the membrane plane rather than square, but the
receptor keeps ~18 A of lipid to its periodic image against the 15 A requested.
`min` 21 s, `eq` (1.5 ns) 3 m 48 s, `prod` (1 ns) 2 m 51 s. Production held
300.99 +/- 1.03 K and 1.032 +/- 0.002 g/mL over 100 frames.

Final DAG: 8 completed, 1 failed (`solv_001`, the ASH failure, kept as evidence).

---

## 2026-08-20 — 5L7D in POPC end to end on Rikyu; the membrane fixes hold, and one analyze trap

Ran the v0.6.8 SIF through a real membrane system to check the fixes before
telling the group to pull: 5L7D (human Smoothened, a class F GPCR with a BRIL
fusion), chain A only, ligand CLR and the NAG glycans dropped, POPC bilayer,
0.5 ns NVT + 1.0 ns NPT, 1 ns production. Nine nodes, **0 failed**.

**The membrane fixes hold.**

| Check | Result |
| --- | --- |
| Orientation backend | `opm-homolog`. 5L7D is itself in OPM, so identity 1.0, `fit_rmsd` 0.0, hydrophobic thickness 32 A, 10 candidates evaluated. No PPM3 fallback. |
| Ions in the bilayer | 301 ions, **0** within the middle 80 % of the lipid z-span. This is the `a6cad27` fix (patch salt no longer carried into the assembly) working on a real system. |
| Lipid headgroup restraint | `lipid_headgroup_restraint_count: 359` on both `min` and `eq` — the new flat-bottom restraint is applied. |
| Box fitted to solute | 116 x 116 x 173.5 A. The extracellular CRD sets the z height; nothing crosses the cell after NPT contracted it 10 %. |
| Equilibration | Density 0.917 -> 1.024 g/mL, volume 2416 -> 2164 nm3. |
| Production | 300.34 +/- 0.68 K, 1.025 +/- 0.001 g/mL, backbone RMSD rising to ~0.15 nm and flat after 40 frames. |

**Throughput:** ~300,000 atoms, one GB200. `min` 32 s, `eq` (1.5 ns) 7 m 58 s,
`prod` (1 ns) 6 m 09 s — about **270 ns/day**. The whole run cost ~15 min of GPU.

**The trap: `explain_node` says an analyze node is ready when its metric tool is
not.** Create an `analyze` node parented on `prod`, and `explain_node` reports
`ready_to_run: true`, no blocking codes, no missing inputs, and resolves
`topology_file` / `trajectory_chain` / `energy_chain`. Running `analyze_rmsd` on
that node then fails:

    Validation failed for 'trajectory_file / reference_pdb': Both are required.

The resolver exposes `trajectory_chain` (a list) and `topology_file`; the metric
wants `trajectory_file` and `reference_pdb`, which only exist after
`concat_trajectory` has run on that node and written `combined_trajectory` +
`reference_pdb`. `skills/md-analyze/metrics.md` states this ("After
`concat_trajectory` ... the combined trajectory and reference PDB are the common
inputs"), so an agent that reads the skill is fine. An agent that trusts
`explain_node` — which is what `run-loop.md` says to check before running a
stage tool — is not. Reproduced cleanly on a fresh node (`analyze_003`).

Either `explain_node` should report the concat prerequisite for metric-bearing
analyze nodes, or the metrics should accept the chain form the resolver already
hands them. Not fixed here.

**Not a finding, for the record:** `analyze_rmsd` does return its statistics —
`mean_rmsd_nm`, `std_rmsd_nm`, `max_rmsd_nm`, `n_frames` as flat keys. An
earlier read of this session looked for a `statistics` object that the tool
never promised.

---

## 2026-08-20 — UTF-8 モードを焼いた SIF (acabf7612b72)

locale 修正 (`preserve_locale` / `new_simulation`)、`bin/mdclaw` の bash 3.2 対応、
両イメージへの `PYTHONUTF8=1` を含めて焼き直した。

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-acabf7612b72
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-acabf7612b72.sif
        6,774,358,016 bytes
        SHA-256 acbc89e6e279376b2fa8d1be6f8019a6e0b609c10c83c7e4b71cb83c88f1522d
smoke   Docker 24/24、SIF からも 24/24 (24 件目が UTF-8 mode contract)
```

SIF から実地確認した 2 点:

```
read-only CWD で mdclaw --version   -> mdclaw 0.6.8、outputs/ も残らない
LANG=C.UTF-8 + LC_ALL=C 後の書き込み -> utf8_mode=1、em dash が往復する
```

後者は `PYTHONUTF8=1` が無ければ `UnicodeEncodeError` になる条件。これで
「ドライバがロケールを倒す」経路は、MDClaw 内 (preserve_locale) と第三者ライブラリ内
(UTF-8 モード) の両方が塞がった。

前世代の SIF: `36f13d131a89` は locale 修正前、`34230ff20567` は CWD 修正が 1 ファイル
欠けた失敗作。どちらも破棄してよい。

---

## 2026-08-20 — フルスイートの 21 failure の原因は OpenCL ドライバの setlocale だった

「単体で走らせると通り、フル実行だと落ちる」21 件。**flaky ではなく実バグ**で、原因は
1 つだった。

### 特定までの経路

`read_text()` が ASCII デコーダで落ちていたので locale を疑ったが、環境変数をどう変えても
Python 3.12 は C ロケールを UTF-8 へ強制するため再現しない。テストモジュールの import を
1 つずつ追っても倒れない。**全ファイル収集 + 1 テストだけ実行では再現せず、実行すると再現**
したので「どれかのテストの実行中」に絞り、5 ファイルまで縮めて 22 秒で再現、二分探索で
`test_ap5_build_topology_smoke.py::test_step4_topology_via_gaff` に到達した。

Python の `locale.setlocale` / `ctypes.CDLL` / `subprocess.Popen` を全部張っても検出できず
(C レベルの dlopen 経由だったため)、**2 ms 周期で LC_CTYPE を読みメインスレッドのスタックを
ダンプするサンプラースレッド**で瞬間を捕まえた。

```
build_amber_system -> openmm_build.py:1555 Simulation(...) -> mm.Context(...)
-> プラットフォーム未指定なので OpenCL が選ばれる
-> Apple の OpenCL ドライバ初期化が setlocale(LC_ALL, "C")
-> プロセスの既定エンコーディングが US-ASCII に固定
-> 以降 encoding 未指定のテキスト I/O が全部 ASCII、em dash (U+2014) で死ぬ
```

### テストだけの問題ではない

`render_structure_preview` の 4 件は **MDClaw が自分の出力を書けずに** `UnicodeEncodeError`
で落ちていた。Linux arm64 イメージ内で実測:

| 環境 | `utf8_mode` | ドライバが LC_ALL=C にした後 | 結果 |
|---|---|---|---|
| LANG 未設定 (コンテナ既定) | 1 | utf-8 のまま | 影響なし |
| LANG=en_US.UTF-8 | 1 | utf-8 のまま | 影響なし |
| **LANG=C.UTF-8** | **0** | **ANSI_X3.4-1968** | **UnicodeEncodeError** |

`LANG=C.UTF-8` は HPC で普通に設定される。**macOS 固有ではない。**

### 修正

`_common.py` に `preserve_locale()` と `new_simulation()` を追加し、MDClaw が Simulation を
作る 12 箇所すべてを経由させた。ドライバは今も倒すが、プロセスには残らない。`bin/mdclaw` の
bash 3.2 問題 (`set -u` 下の空配列展開) も別件として直した。

`tests/test_locale_guard.py` は復元・例外時の復元に加えて、**`_common.py` 以外で `Simulation(`
を直接呼んでいないこと**をソースレベルで検査する (ガードは全呼び出し箇所が使って初めて意味が
あるため)。1 箇所戻すと落ちることを確認済み。

```
before  21 failed, 1454 passed
after   1478 passed, 7 skipped, 0 failed
```

`PYTHONUTF8=1` をイメージに入れれば表の 3 行目も構造的に消えるが、未実施。

---

## 2026-08-20 — CLI が read-only CWD で起動しない件を修正。SIF 再作成。既存の 21 failure は locale 依存

### 修正した本体

`WORKING_DIR` を宣言する 28 モジュールが、その隣で **import 時に** `ensure_directory(WORKING_DIR)`
を呼んでいた。`_cli._discover_tools()` は全モジュールを import するので、**何もしない
`--version` / `--list` ですら CWD に `outputs/` を掘り、掘れなければ CLI 自体が起動しない**。
書き込み側は全箇所すでに `ensure_directory` / `create_unique_subdir` / `mkdir(parents=True)`
を呼んでいるので、import 時の呼び出しは最初から不要だった。28 件すべて削除し、不要になった
import も除去。回帰テストを `TestSubprocessCLI` に 2 本追加した。

### 自分のミスを 1 件記録しておく

回帰テストに効き目があるか確かめるため `pdb_client.py` にバグを一時的に戻し、`git checkout`
で戻した。**これはインデックスから復元するので、実験で足した行だけでなく修正ごと巻き戻した。**
結果 27/28 だけ直った状態でコミットし、その状態で SIF を焼いて渡した。さらに、フルスイートで
自分の回帰テスト 2 本が落ちているのを「テスト中に git を触ったせいの flaky」と誤診した。
**テストは正しく、読み違えたのは私。** 教訓は 2 つ: 実験の巻き戻しは `git checkout <file>` では
なく `git stash` / `git diff > patch` を使う。落ちたテストを flaky と判定するなら、根拠は
「再実行して通った」ではなく原因の特定。

### 既存の 21 failure は私の変更と無関係、原因は locale 依存の `read_text()`

修正前のコミット `2daf6e4` のワークツリーで同じフルスイートを流し、**失敗集合が完全に一致**
することを確認した (pre-fix 21 failed / 1452 passed、post-fix 21 failed / 1454 passed、
差は追加した回帰テスト 2 本)。

原因は、リポジトリ内のファイルを `read_text()` で **`encoding=` を渡さずに**読んでいること。

```
claude = (REPO_ROOT / "CLAUDE.md").read_text()
  -> encodings.ascii.IncrementalDecoder ... UnicodeDecodeError
```

プロセスのその時点の locale に依存するので、単体実行では通り、フル実行では途中で locale が
C に倒れて日本語を含むファイルで落ちる。**C locale が既定のコンテナ / HPC batch では常に
落ちる**性質のもので、flaky ではない。未修正。

### 別件: `bin/mdclaw` は bash 3.2 で動かない

`tests/test_bin_wrapper.py::test_wrapper_is_quiet_outside_a_user_namespace`:

```
bin/mdclaw: line 146: NV[@]: unbound variable
```

`set -u` 下の空配列展開 `"${NV[@]}"` は bash 4.4+ では通るが bash 3.2 (macOS 既定) では
エラー。`"${NV[@]+"${NV[@]}"}"` で直る。未修正。

### 成果物

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-36f13d131a89
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-36f13d131a89.sif
        6,774,267,904 bytes
        SHA-256 0324302a3943324f5bc73bda548bf92eef74cd247f1346b69d2a52546298fd9d
smoke   Docker 23/23、SIF からも 23/23
```

read-only CWD からの起動を実地で確認した (SIF 内、0555 のディレクトリを `--pwd` にして
`mdclaw --version` → `mdclaw 0.6.8`、ディレクトリには何も残らない)。**1 つ前の SIF
`...-34230ff20567.sif` は修正が 1 ファイル欠けた版なので使わないこと** (同じ条件で
`OSError: Read-only file system: 'outputs'` を再現する)。

---

## 2026-08-20 — v0.6.8 の arm64 SIF (a6cad2701ac4)。CLI が read-only CWD で起動できない件を発見

`3997c36..a6cad27` の 8 コミット (patch の塩を assembly へ持ち込まない、water copier が
slab を薄くする問題、solute と接する分子を一緒に image する、99,999 原子超の preview、
box の書き出し、v0.6.7 リリース) を取り込んで焼き直した。

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-a6cad2701ac4  (mdclaw 0.6.8)
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-a6cad2701ac4.sif
        6,774,267,904 bytes
        SHA-256 098df878a4593ecbd1a0c68e89d662402b566ac567e1182632fd0779adff8d72
smoke   Docker 23/23、SIF からも 23/23 (GPU は SKIP)
```

前回同様 GHCR には push せず、`docker save` した tar を Lima VM に読ませて変換した。

### `mdclaw --version` が読み取り専用の CWD で落ちる

SIF 検証のついでに read-only なディレクトリから `mdclaw --version` を叩いたら死んだ。

```
File "mdclaw/research/pdb_client.py", line 22, in <module>
    ensure_directory(WORKING_DIR)
OSError: [Errno 30] Read-only file system: 'outputs'
```

`WORKING_DIR = Path("outputs")` が 20 以上のモジュールにハードコードされていて、どれも
**import 時に** `ensure_directory(WORKING_DIR)` を呼ぶ。`_cli._discover_tools()` は全
モジュールを import するので、**`--version` や `--list` ですら CWD に `outputs/` を掘る**。
掘れなければ CLI 自体が起動しない。

`test-container.sh` が緑なのは、冒頭で `cd "${TMPDIR:-/tmp}"` して書ける場所へ移るから。
つまり既存のスモークではこの経路を踏まない。HPC で read-only bind や書き込み権限の無い
ディレクトリから叩くと、ツールを1つも実行しないうちに落ちる。副作用として、無関係な
ディレクトリで CLI を触るだけで空の `outputs/` が生える。

未修正。import 時の副作用を消し、必要になった時点で作るのが筋 (WORKING_DIR の既定を
`.` にする件とも絡む)。

---

## 2026-08-20 — パッチの塩を持ち込むのをやめた。イオン中和の分岐は全部これが原因だった

前日の膜構築修正のレビューで `_drop_counter_ions` の欠陥を指摘され、直す前に
パッチのイオンを実測したところ、前提が崩れた。

### 測定

バンドルされた 7 パッチはいずれも **イオン 8 個 (Na 4 / Cl 4)** しか持たない。
各パッチの midplane を `_estimate_patch_membrane_center_z` で取り、z を最小イメージで
測ると:

```
POPC            core(|dz|<15): 0   headgroup(15-25): 6   bulk(>25): 2
DOPC / DPPC                  : 0                    : 3            : 5
POPE / POPC:CHL1             : 0                    : 0            : 8
```

**疎水コアにイオンは 1 個も無い。** POPC パッチの脂質テールは |dz| <= 19.1 まで、
頭部基 PC は |dz| 10.2..27.9。イオン 8 個は dz = -26.95, -27.19, +23.55, +18.47 (Na)
と -15.67, -20.98, +20.00, +19.13 (Cl)。

削除条件は `|z - centre| <= leaflet` の leaflet = 23 なので、8 個中 5 個 (Na 1 / Cl 4)
を消す。9 タイルで 45 個、種の差 27 個 —— 実行時の warning
「dropped 45 ion(s) ... and 27 counter-ion(s)」と完全に一致する。コードのコメントは
"ions ... end up in the hydrophobic core of every copy" と書いていたが、**コアではなく
頭部基界面**だった。

### 現状のコードは既に事実上「全剥がし」だった

45 + 27 = 72 = 8 x 9 で、2 段階の削除がパッチのイオンを全部消していた。
`existing_cations: 0, existing_anions: 0` で中和が全量 (149 Na / 152 Cl) を配置。
設計ではなく、このパッチの組成による算術上の偶然。

### 決めたこと: パッチは塩ありで平衡化し、使う前に剥がす

VMD (`membrane` プラグインのパッチはイオン無し + `autoionize` が水を置換) と
CHARMM-GUI (二重層と水を組んでから水相にイオン配置) と同じ流れ。MDClaw の
`_apply_neutralizing_swap` も既に leaflet の外の bulk 水と交換しているので、
最終配置は元から同じ方式だった。パッチが塩を持ち込むことだけが違っていた。

消えた分岐:

| 消えた処理 | 理由 |
|---|---|
| 埋没イオン削除 | パッチにイオンが無い |
| `_drop_counter_ions` | 種の偏りが発生しない |
| `pairs_present` 分岐 | `existing = 0` が不変条件 |
| 超過分岐 | 到達不能 |

超過分岐は実害があった。`plan_neutralizing_ions` の docstring が
"never a negative number — ions that are already there are not removed to hit a
target from above" と明言しており、実測で 150 pairs present / 87 target なら
0.258 M、300 pairs なら 0.515 M が **警告無しで出荷される**。剥がせば到達不能になる。

実系 (SMO 5L7D / POPC) で検証: existing 0/0、149 Na / 152 Cl、net +3、
water 55,026、塩 0.1503 M、geometry passed、密度 0.957 of bulk —— 剥がし前と同一。

### レビューとの相違

cursor は全剥がしに反対し、「界面の平衡化イオンを残し、超過分を対で除去せよ」と
主張した。理由は「MDClaw の最終配置は決定論的な bulk 水置換であって CHARMM-GUI の
静電考慮 MC ではないので、平衡化済みの界面分布を捨てるのは改悪」。

採らなかった理由: 保持されるのは **8 座標を 9 タイル分そのまま複製したもの**で、
平衡化が生んだ分布ではなく人工的な周期性。最終系のイオン 301 個のうち 72 個が
それになる。タイル化由来の人工周期性は別途記録済みの既知問題でもある。

ただし **荷電脂質では cursor の主張が効く**。POPG/POPS/POPA では対イオンが頭部基に
凝集し、量も多く、それは本物の化学。荷電脂質のパッチを実際に作る際は、この判断を
再検討すること。現時点でそのパッチは存在せず、検証もできない。

### 同時に直したもの (レビュー指摘)

- imaging の接触判定を分子重心から**最近接原子**へ。screen も分子サイズ基準に。
  cursor の再現ケース (20 nm 箱、anchor に 0.2 nm で接触しつつ 12 nm 伸びる鎖) が
  3.0 nm -> 0.0 nm
- 密度ゲートに **xy 列判定** (10 A 角、bulk の 0.35 未満、z 方向に 80% 以上貫通)。
  有効範囲は両側から挟まれる: 断面 25% のチャネルはスラブ判定が 0.729 で既に落とし、
  8% 未満 (112 A 箱で直径約 35 A) はどちらも通さない。担当するのはその間
- 密度が評価不能なとき warning を出す (従来は passed のまま黙って通っていた)
- preview の `connect_mode 3` を hybrid-36 が実在するファイルだけに限定
- 大規模系 (>= 150k atoms) の system_box は影と antialias を落とす。
  **264 s -> 65 s** (timeout 300 s)。`ray=False` はヘッドレスでは PNG が出ないので不可

### 生成 PyMOL スクリプトの構文検証を追加

`json.dumps(True)` は JSON の `true` を返すので、生成 Python に埋めると構文エラーになる。
このセッションで 2 回踏んだ (`axis_views`, `fast_ray`)。2 回目は `connect_mode` の
条件分岐を**丸ごと無効化**していて、レンダリングが 3 秒で失敗して初めて気付いた。
フラグの全組み合わせ 24 通りを `ast.parse` で検証するテストを追加。


## 2026-08-19 — 検定を ANM 帰無に一本化、Rg の役割が判明、3 レプリカを測定、SIF の BLAS バグを発見

**検定を 1 本に絞った (ユーザ指示「H0 か ANM かに絞ったほうがいい」).** ANM 帰無 (cutoff 7.0-20.0 A の 27 点、
平均 0.517 SD 0.048 最大 0.588) はランダム帰無を包含する: 0.13 のものは 0.59 を超えられない。
実測 z は 本物の 1 ns +4.37 / 100 ps +2.26 / 10 ps -2.00 / ANM ensemble -0.05 (帰無のど真ん中) /
等方ノイズ -8.00。ランダム帰無はゲートから外し報告用の文脈に降格。負の対照 5 本すべて失敗、実 run のみ通過。

**Rg は冗長ではなかった。** 「Rg は意味なくない?」を実測で確かめたところ逆の結論になった。
**RMSIP はスケール不変**なので、軌道を一様に 1.3 倍しても RMSIP は 0.729 のまま変わらず (Rg は 1.14 -> 1.48)。
0.8 倍でも同様。進行性膨潤 (Rg 1.82) でも RMSIP は 0.711 でまだ通る。**Rg が振幅・コンパクトさを拘束する
唯一のチェック**であり、単位ミスや変性を捕まえる役割を独占している。バンドが緩い (結晶構造が満たす) のは事実だが
冗長とは別問題。

**3 レプリカの効果を実測 (seed 20260820/21 で 2 本追加).** 単独 0.729/0.743/0.717 -> 平均 0.730 **SD 0.010**、
レプリカ間 0.780/0.851/0.770 平均 0.801、3 本プール 0.764 (+0.034)。帰無最大からの余裕は +0.142 -> +0.176。
敵対側 (ANM ensemble 3 本) もプールで +0.022 稼ぐので、**利得は実在するが劇的ではない**。
本当に新しいのは**参照を使わないレプリカ間一致**で、これは `execution_validity` 軸に入る。
実務的含意: **SD 0.010 なのでエージェント間の 0.03 未満の差はノイズ。** レプリカ無しではこれが分からない。

**SIF の OpenBLAS がスレッド過剰生成で崩壊していた (プロジェクト全体に効く).**
scorer が 1 タスク 10 分超かかるので profile したところ `anm_null_distribution` が 528 秒。
Hessian 構築をベクトル化しても改善せず (結果は RMSIP=1.000000 で完全一致)、犯人は `np.linalg.eigh` だった。

| 環境 | `eigh(684x684)` |
|---|---|
| SIF、スレッド env 無し | **16.34 s** |
| SIF、`OMP_NUM_THREADS=1` | 0.12 s |
| SIF、`OMP_NUM_THREADS=8` | 0.07 s |
| ホスト python3 | 0.59 s |

`matmul` は 3.7 倍差なので LAPACK 固有。SIF の numpy は scipy-openblas を
`DYNAMIC_ARCH NO_AFFINITY MAX_THREADS=64` で積んでおり、32 コア機で上限未設定だと小問題が崩壊する。
**SIF 内で numpy を回すときは常に `OMP_NUM_THREADS` を渡すこと。**
`scripts/_threads.py` で `os.environ.setdefault` により numpy import 前に防御 (1 タスク 10 分超 -> 7.4 秒)。
恒久対応はコンテナか `bin/mdclaw` 側だが未着手。memory にも記録。

**最終スコア**: D01 prep 7/7 md 5/5 (12/12)、D02 prep 8/8 md 5/5 (13/13)、両タスク合計 13.5 秒。

---

## 2026-08-19 — 膜系の水が bulk の 40 % しか入っていなかった。コピーが自分自身と衝突判定されていた

PyMOL の緑箱が平衡化後の値かという問いから始まって、成果物側の 2 件と、
その図で初めて見えた本体側の 1 件を直した。

### 1. min/eq/prod の PDB は build 時の箱を書いていた

`PDBFile.writeFile` は CRYST1 を **topology** から取る。run 側の Topology は
`topology.pdb` を読んだときのままなので、NPT で箱が変わっても更新されない。実測:

| | box (A) |
|---|---|
| `equilibrated.pdb` の CRYST1 | 118.088 x 118.088 x 175.733 |
| `equilibrated.xml` (実際) | **104.894 x 104.894 x 164.713** |

各辺 11 %、体積 1.4 倍の過大表示。min/eq/prod の 3 箇所とも。同じリポジトリの
`export_state_to_pdb` (`platform.py:113`) は state から箱をコピーしていて、
patch 平衡化経路だけが正しかった。

### 2. `center_solute_and_wrap_solvent` は build 時にしか呼ばれていなかった

関数は正しい。呼ばれるのが `openmm_build.py:1588` と `openmm_system/build.py:607`
だけで、min/eq/prod は生の積分座標をそのまま書いていた。実測で水は x に 225 A
(箱 104.9 A の 2.2 倍) まで広がっていた。溶質は無傷。

どちらも `render_simulation_pdb_preserving_resnames` に `box_vectors` / `image` を
足して修正。**state.xml / .chk / DCD には触らない**ので MD の結果は不変。

### 3. `extend_water_slabs` が追加した水は bulk の 40 % だった

修正後の図で上端の空隙が見えたので測ったところ:

```
z -45..-30 (パッチ自身の水)              89-96 % of bulk   <- 正常
z  30..95  (extend_water_slabs が追加)   35-43 %
```

**原因: 受理した分子の原子を、判定に使っている grid にその場で追加していた。**
コピー 1 つの中で先に置いた水と後の水が互いに衝突判定される。水は水素結合して
いるので H...O = 1.8 A < cutoff 2.2 A (全原子判定)。**正しい水の構造そのものが
「重なり」と判定され**、「互いに 2.2 A 以上離れた部分集合」まで間引かれる。
それが bulk の約 40 %。

3 通りで確認した。計装した実行の per-copy 生存率 36-44 %、自己間引きだけの
オフライン再現 43.3 % (実測 40.9 %)、重原子のみでの再現 100 %。

修正は 3 点。(a) 判定を重原子のみに。(b) コピーは完了してから参照 grid に併合。
(c) stacking period を公称 `leaflet` ではなく**実在する脂質の z 範囲**から導出
(パッチの脂質は z -26.87..27.89 = 54.76 A、公称は 46 A。差分が継ぎ目ごとの
真空になっていた)。

| | 水分子 | 追加 | overlap drop | 拡張領域の密度 |
|---|---|---|---|---|
| 修正前 | 32,149 | 17,338 | 23,819 | 28-43 % |
| (a)+(b) | 50,101 | 41,049 | 108 | 48-101 %、継ぎ目に 52 % の谷 |
| (a)+(b)+(c) | 54,726 | 40,950 | 495 | 77-100 % |

自由体積に対する充填率 54 % -> 91 %。原子数 186,516 -> 277,768。
**組んだ系の密度 0.625 -> 0.903 g/mL** (eq_001 と eq_003 の NVT レポータ実測)。
修正前は NPT が箱を 2438.7 -> 1812.3 nm^3 (-25.7 %) まで潰し、それでも 0.845 g/mL
止まりで上端に 18 A の空隙が残っていた。

### なぜ静かに壊れていたか

`solv` の geometry チェック、溶質の収容判定、塩濃度、電荷中性 — **全部通る**。
塩濃度は「間違った水量に対して正しく」計算されていた (32,326 水に 87 pairs で
0.150 M、55,027 水に 149 pairs でも 0.150 M)。**置いた材料の密度を見ている場所が
どこにもなかった。**

そこで `_membrane_embedding_geometry_report` に水酸素の z ヒストグラムを追加した。
既に全原子を読み `box_c` も持ちビルドを落とす権限もある関数で、1 パス増えるだけ。
判定は**絶対基準** (液体水 0.03344 molecules/A^3) で行う — 相対 (自セルの median 比)
では一様に薄い系を検出できない。実測で修正前は worst/median = 0.85 で通ってしまう。

```
solv_002 / solv_003 (修正前)  median 0.41 of bulk  -> failed
replay2  ((a)+(b))            median 0.958         -> passed
solv_004 ((a)+(b)+(c))        median 0.957         -> passed
```

### cursor レビューで追加で直したもの

- `membrane_zs` を「水でもイオンでもないもの = 膜」という消去法から Lipid21 名の
  積極的同定に変更。Mg2+/Ca2+ のような塩が bulk にいると膜と誤認し、span が箱
  いっぱいに広がって拡張全体が warning 一行で黙って skip されていた (これは (c) で
  私が入れた退行)。
- 拡張が必要なのに何も追加できなかった場合をハードエラー
  `membrane_patch_water_extension_failed` に。そのまま出荷すると 5L7D の元バグそのもの。
- copy 0 は変位がちょうど `patch_box_c` の格子像なので重なり判定を免除。
- resseq が 4 桁で溢れる前に chain label を繰り上げ (実測 9,343/copy、余裕 7 %)。

cursor が manifest から出した接触統計は 4 桁まで正しかった: POPC パッチの分子間
重原子最短は 2.1164 A で cutoff 2.2 A を下回るが、**水酸素間は 2.5264 A** なので
重原子判定は水について 15 % の余裕がある。一方 cursor の resseq 衝突の見積り
(1 コピー 10,350 分子、~1,000 件衝突) は実測 9,343 で**起きていなかった**。
箱外へのはみ出しも、この系では原子 z 範囲 173.70 が box_c 173.733 に収まっていた。

### 残っている限界

充填率 91 % で密度 0.903 g/mL。理想 (~1.0) より 10 % 低い。出どころは (1) interval
境界で分子単位に clip するため両端 3 A ほどが系統的に薄い、(2) 継ぎ目が bulk の
0.66 程度。NPT の穏やかな緩和で吸収される範囲なので今回は追わない。


## 2026-08-19 — MDDataBench の採点は甘すぎた。敵対的ベースラインで穴を 2 つ実測し、塞いだ

**「score が甘すぎないか」を議論でなく実測で確かめた結果、甘かった。** 落ちるべき提出を作って走らせたところ、
ランダム部分空間帰無だけでは 3 本が通ってしまった (D01 参照に対して):

| ベースライン | RMSIP | z | 当時の判定 |
|---|---|---|---|
| ANM 低振動モードからのサンプル (**MD ゼロ**) | 0.515 | 46 | **通過** |
| 本物の MD を 100 ps に切り詰め | 0.627 | 60 | **通過** |
| 本物の MD を 10 ps に切り詰め | 0.420 | 36 | **通過** |
| 結晶構造 + 等方ノイズ | 0.130 | -0.5 | 正しく失敗 |
| 最小化構造の複製 | 0.135 | 1.7 | 正しく失敗 |

つまり**「正しい分子か」は証明できていたが「実際に走らせたか」は証明できていなかった**。
`production_ran_for_one_nanosecond` がノード自身のメタデータを読むだけだったのも同根。

**修正 1: 構造のみの床 (ANM) を追加。** 結晶構造から組んだ弾性ネットワークが RMSIP 0.57 に達するので、
それを margin 0.05 付きで超えることを要求する。床はカットオフ最大化で取る (カットオフは攻撃者の自由変数)。

**修正 2: 経過時間を物理で検証。** 拡散係数は**強度量**で使えない (同じ軌道の 1 ns と 100 ps でどちらも
3.7e-5 cm^2/s)。**連続 unwrap した溶媒の総変位は示量量**で、999 ps から 989 ps、99 ps から 98 ps、
9 ps から 15 ps を復元した。溶媒が無い提出は計測不能で即失敗 = MD ゼロ提出に対する正しい判定。

修正後、5 本すべてが失敗し実 run だけが通る。**D01 の 100 ps は ANM 床を 0.005 差で超えてしまい、
時間検証だけが捕まえた** ので 2 つとも要る。恒久回帰として `scripts/negative_controls.py` を追加。

**採点を prep と md に分割 (ユーザ指示)。** 単一の数では「組み立てで落ちた」のか「シミュレーションで
落ちた」のかが言えない。ANM 提出と 10 ps 提出はどちらも **prep 満点・md 失敗**になり、帰属が機能する。
D01 prep 7/7 md 6/6、D02 prep 8/8 md 6/6。

**副産物のバグ 1 件.** `negative_controls.py` 初版が原子インデックスをファイル行番号で数えており
(トポロジにはヘッダ行がある)、real_full_run が 0.688 と出て scorer の 0.729 と食い違った。修正後一致。
**scorer と回帰ハーネスで同じ値が出ることを毎回突き合わせる**のが早期発見に効いた。

---

## 2026-08-19 — scorer 修正、D02 追加、プロンプト最小化

**scorer の偽 FAIL 2 件を修正。** `benchmarks/mddatabench/scripts/score_submission.py` として
実装し直した。ハードコードした `True` を廃し、全項目を artifact から再計算する。正しい所在は
`amber_metadata.json :: parameters.water_model` (+ `forcefield_provenance.openmm_xml`) と
prod ノードの `metadata.system_signature.ensemble`。**バロスタットは実行時に付与されるので
topo ノードの system.xml には無い。** 契約原子は生インデックスではなく (残基番号, 原子名) で対応付ける
(提出側トポロジは溶媒を含む)。D01 で 11/11 を再現。

**D02 を追加: MDDB `A00AJ` (MoDEL 1CSP、枯草菌 major cold-shock protein CspB)。**
MDPrepBench との交差を機械的に取った結果 (MDPrepBench 30 PDB 中、CC + Classical MD + 解析5種を
満たすのは 1UBQ / 1CSP / 2CBA / 1BNA)。**2CBA は見送った** — MoDEL の寄託に Zn が入っておらず
(HETATM ゼロ、apo)、触媒金属を捨てることを報酬にしてしまう。MDPrepBench P26 の趣旨と正反対になる。

D02 が D01 に足す能力は**側鎖補完**と**非ゼロ溶質電荷**。PDB 1CSP は Glu 3/21/36/66 の側鎖先端を
欠いており重原子 505、MDDB 参照は 521。**505 + 16 = 521 という等式を参照自身が与える**ので、
正解をキュレータが決めなくてよい。参照スケールは 201 原子 (67×3)、1 ns 窓どうし 0.687 ± 0.035、
帰無 sqrt(10/603)=0.129。

**プロンプトを最小化した (ユーザ指示)。** scorer が両側のサブスペースを自分で計算するので、
**エージェントに解析させる必要がそもそも無い**。解析契約・報告項目・鎖選択・互変異性体・箱形状・
側鎖補完の指示をすべて削除し、残したのは「PDB ID / TIP3P / 中性化 / 300 K / NPT / 1 ns 以上」だけ。
参照バンドルは**ソルバのワークスペースに一切置かない** (採点時に評価器が取得する) ので、
参照が漏れる経路が消えた。

**簡素プロンプトが解けることを実測で確認。** プロトネーションの指示ゼロで MDClaw は
1UBQ -> 602/1231、1CSP -> 521/1014 と参照組成に厳密一致し、1CSP の Glu 4 残基を無指示で補完した。
2 つの run は参照と**逆の**互変異性体を選んだが (1UBQ で HID、参照は HIE / 1CSP で HIE、参照は HID)、
重原子数は互変異性体に依らないので設計どおり通る。総原子数には ±2 の許容を入れた。

**採点結果**: D01 11/11 (RMSIP 0.729, z 72.4)、D02 12/12 (RMSIP 0.703, z 64.0)。
どちらも方向は復元、振幅は 2-6 倍小さいという同じ姿。ruff clean、リポジトリ内のデータは 0 バイト。

---

## 2026-08-19 — D01 を MDClaw で実際に解いて 11/11。試走が scorer の欠陥を 2 件出した

同日前エントリで作った MDDataBench D01 を、MDClaw 0.6.6 で最初から最後まで解いて採点した。
A6000 1 枚、1UBQ chain A -> ff14SB + TIP3P、cubic 15 Å、31355 原子、HMR 4 fs、
NVT 100 ps + NPT 200 ps、**1 ns NPT production が 2 分 29 秒**。

**結果 11/11 PASS.** 核となる検定は RMSIP=0.729、z=72.4、p<5e-5 で H0 棄却。
正準相関 10 本すべてが帰無 99 パーセンタイル超。Rg=1.1777 nm（参照 10 ns 平均 1.1807、
差 0.0030 は 1 ns 窓の SD 0.0102 の 1/3）。prep 出力は重原子 602 / 全 1231 / 76 残基 / HIE で
参照と完全一致した。

**設計の予測が当たった。** 固有値比 (own/ref) は [0.60, 0.35, 0.18, 0.18, 0.29, ...] で、
**方向は復元できるが振幅は 2-6 倍小さい**。τ(PC1)=1236 ps から予測したとおり 1 ns では
遅いモードの分散が出ない。RMSIP 0.729 は ANM 床 (0.47-0.62) を超え、参照自身の 1 ns 窓間
自己一致 0.760 ± 0.053 の 0.6σ 以内。**別力場 (ff14SB vs Parm99) の独立な 1 ns が、
参照自身の 1 ns 再現性と同じ水準に着地した。** 「H0 棄却は採点、定量一致は採点しない」
という設計判断が実測で正当化された。

**試走で出た scorer の欠陥 2 件（いずれも偽 FAIL）.**

1. water model を `amber_metadata.json` 直下で探したが実際は `parameters.water_model`
   (+ `forcefield_provenance.openmm_xml` に `amber/tip3p_standard.xml`)。
2. barostat を topo ノードの `system.xml` で探したが、**バロスタットは実行時に付与される**ので
   そこには無い。ensemble は prod ノードの `metadata.system_signature.ensemble` を読む。

さらに、参照の 228 契約原子は**生の原子インデックスではなく (残基番号, 原子名) で対応付ける**必要がある
（提出側トポロジは溶媒を含むのでインデックスが一致しない）。これらを `task.json` の
`scorer_field_map` に記録した。**artifact-as-truth を掲げても、artifact のどこに何があるかを
実走で確定しないと scorer は嘘をつく。**

`benchmarks/mddatabench/scripts/evaluate_submission.py` を追加。ruff clean。

## 2026-08-19 — 膜修正込みの arm64 SIF を焼き直した (5c7e89590560)

`93ebfa5..5c7e895` の膜まわり4コミット (leaflet 両側からの midplane、蛋白フレーム上での
bilayer 配置、既存塩のカウント、solute からの z セル寸法、preview の説明) を取り込んだ
状態で再ビルド。

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-5c7e89590560
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-5c7e89590560.sif
        6,774,202,368 bytes
        SHA-256 1c5e3fe5c32b94a6f03aed3ef79ccecb0a38e277f8675551593824d82ad8be9e
smoke   Docker 23/23、SIF からも 23/23 (GPU は SKIP)
```

今回は GHCR に push していない。`docker save` した tar を Lima VM `singularity-ce` に
読ませて `docker-archive:` から変換したので、外部公開なしで完結している。tar と VM 側の
scratch は変換後に削除済み。

なお**この4コミットは `mdclaw/` の Python だけで、コンテナの中身 (environment.yml /
container/ / pyproject.toml) は変わっていない**。つまり SIF の作り直しは必須ではなく、
Rikyu 上で既存 SIF に `PYTHONPATH="$PWD"` overlay を掛ければ同じ挙動が得られる。今回は
明示の依頼で焼いた。逆に `mdclaw/` を 1 行でも変えて再ビルドすると stage 1 の
`COPY mdclaw/` でキャッシュが切れ、conda 環境の作り直しから丸ごと走る (実測 ~50 分 +
SIF 変換 ~10 分)。修正はまとめてから焼くのが得。

Rikyu 実機での GPU smoke は引き続き未実施。

---

## 2026-08-19 — MDDataBench D01 を作成: RMSIP による「無関係」帰無仮説の検定

`benchmarks/mddatabench/` を新設し、最初のタスク D01 (1 ns MD + 本質サブスペース一致) を実装・検証した。
参照は MDDB `A0142` (MoDEL 1UBQ、CC-BY 4.0、Amber Parm99 / TIP3P / 300 K / NPT / 10 ns)。

**採点の核: H0 =「2 つの本質サブスペースは無関係」を RMSIP で棄却する検定。**
ランダム直交フレームの Monte Carlo で帰無分布を作る (M=20000 で平均 0.1206 / SD 0.0083、
解析値 sqrt(D/3M)=0.1209 と一致)。**力場校正が不要**なので、rev.2 の「未校正の量に閾値を置かない」
規律を破らずに MD 部分を採点できる。実測 (D=10, 3M=684):

| 比較 | RMSIP | z | 棄却 |
|---|---|---|---|
| ランダム (負の対照) | 0.121 | 0.0 | **no** |
| ANM (構造のみ, 10 A) | 0.617 | 59.5 | yes |
| 座標系ズレ (大域回転) | 0.652 | 64.2 | yes |
| 1 ns 窓 vs 1 ns 窓 | 0.760 ± 0.053 | 81.8 | yes |
| 1 ns 窓 vs 全 10 ns | 0.794 ± 0.029 | 84.7 | yes |
| 10 ns から 500 フレーム | 0.969 | - | yes |

**この検定は妥当性ゲートであって品質スコアではない。** 構造だけから作った ANM も H0 を棄却するため、
「正しい分子を正しい契約で解析したか」は保証するが「サンプリングが収束したか」は保証しない。

**1 ns では上位モードを定量比較できないことが判明。** 参照の積分自己相関は PC1 1236 ps / PC2 1081 ps /
PC4 1660 ps で、**1 ns 中の独立標本は PC1 で 0.8 本**。10 ns の参照でも 8 本。Marchenko-Pastur は
q_eff = N/T_eff が 1 ns で 188、10 ns でも 38 となり適用不能 (PRL 103, 268101 (2009) の手法は
MP 上端ではなくバルクの準位間隔統計)。よって連続値 RMSIP は診断のみとし、校正データとして蓄積する。

**解析契約が必須であることの数値的裏付け.** MDDB は PCA の固有値と射影を配信するが**固有ベクトルは配信しない**
ため、scorer 側で再計算が必須。契約 `pca_backbone_subspace@1` (主鎖 N/CA/C 228 原子、参照構造への Kabsch
フィット + running mean 3 反復、D=10、Å) で公開固有値を -4.8% 〜 +3.4% で再現。摂動の効き方は
大域回転 -0.175 > 原子順序 -0.018 > 平行移動 0。なお Rg では標準原子量の質量加重が公開値と
+0.0024 nm 系統的にずれ、これは 1 ns 窓の SD 0.0102 nm の 24% に相当した。

**データは非同梱.** `scripts/fetch_reference.py` が MDDB から取得し provenance と SHA-256 を書く。
再取得でバイト一致を確認済み。`.gitignore` で取得物のコミットを禁止。solve 時は `mddbr.eu` を遮断、
RCSB は許可。プロンプトに accession を出さない。

**Rg を主観測量にする案は棄却した.** 正しく作れば誰でも 1.18 nm になり識別力がない。RMSIP は
0.12 (偶然) - 0.79 (1 ns 自己一致) - 1.0 と広いレンジを持つ。ruff clean、取得から検定まで通し検証済み。

---

## 2026-08-19 — 実物の図を見て膜構築のバグが 5 件出た。うち 3 件は塩とイオン配置

hackathon の報告 (「5L7D を膜に埋めたら細胞外ドメインが箱からはみ出た」) から始めて、
**PyMOL で組み上がった系を実際に描いた**ところ、箱サイズ以外に 4 件のバグが出た。
どれもエラーを出さずに通っていた。cursor に 2 回レビューさせ、指摘 2 件も反映した。

### 1. 二重層が膜貫通領域を囲んでいなかった

`_estimate_patch_membrane_center_z` は頭部基の**周期平均**で midplane を出していた。
`_periodic_mean` は最大の隙間で展開するが、二重層には**疎水コア (~34 A) と水層 (~35 A)**
という同程度の隙間が 2 つあり、コア側で展開すると**水層の中点**、つまり真の midplane から
**半箱ずれた点**を返す。

同梱 POPC パッチ (box_c 76.846) の実測: phosphate 面 26.4 / 71.2、真の midplane 48.8、
**推定値 10.3**。差 38.5 = 箱の半分。結果、組み上がった系では二重層が z=38.5 に座り、
OPM 転写が二重層内に置いた 173 残基のうち**0 残基**しか膜内に入っていなかった。
**脂質が細胞外ドメインに巻きつき、TM ヘリックスは水の中**にいた。

アシル鎖から推定するよう変更 (コアは連続した 1 枚なので展開が一意)。推定値 48.5。

### 2. 配置ガードが機能しない設計だった (cursor 指摘)

修正1 のあとに「配置後にもう一度推定して目標と比べる」ガードを足したが、cursor に
**代数的に無意味**と指摘された。shift = target − E(atoms) で E が並進同変なら
E(atoms + shift) = target が恒等的に成立する。**推定器が半箱ずれていても必ず通る。**

推定器を使わない独立判定に置換した。目標フレーム位置での **(a) アシル鎖炭素の存在、
(b) 水の不在、(c) 頭部基が両側にありバランスしていること**。実測:

```
壊れていた系: tail 0    / water 4252 / 頭部基 下0/上440   -> 棄却
直した系:     tail 3604 / water 0    / 頭部基 下210/上220 -> 通過
```

証拠が足りないとき (スタブパッチ等) は判定しない。無い証拠で拒否するのも誤答なので。

### 3. タイルの分子識別子が重複していた — 塩濃度が 1/(nx*ny)

`build_tiled_membrane` は**行だけ**書き換えて `PDBAtom` レコードを書き換えていなかった。
中和は `_water_residues` が `atom.chain_id`/`atom.resseq` をキーに分子をまとめるので、
**6 枚のタイルにある「パッチの水 #1」が全部同じキーに潰れる**。

- `n_water = len(water_groups)` が塩濃度計算に使われるので、**バルク塩が 1/6 しか入らない**
- 候補プールも縮み、イオンが偏在した (実測 下26/上88、水は 1:1)

`replace()` で識別子を持たせて修正。修正後 下77/上85 (水 下7491/上7381)。
私が水拡張で作った同種のバグを直したとき、元からタイリング側にもあることに
気づくべきだった。

### 4. 中和が z 順に種を割り当てていた — 周期箱を横切る電荷分離

候補を z 昇順にソートしたうえで**陽イオンを全部先に、陰イオンを全部後に**割り当てていた。
実測 chain I は NA 下5 / CL 下23 / CL 上4。交互配置に変更。

### 5. 塩が二重に入っていた (cursor 指摘)

キャッシュ済みパッチは**要求濃度で詰められている**のに、`plan_neutralizing_ions` が
既存イオンを引かずに満額を追加していた。修正3 で `n_water` の過小評価が直った結果、
**0.268 M (要求 0.150)** になって顕在化。

種ごとの不足分だけ追加するよう変更 (対で数えると、埋没イオン除去で片側だけ減ったとき
「対は 0」と読んで満額追加してしまう)。修正後 **0.151 M**、正味電荷 −37 (蛋白の中和ぶん)。

### 付随: 同梱パッチの二重層内にイオンが埋まっている

同梱 POPC パッチには二重層の中に Cl− が入っており、タイル 6 枚ぶん複製されて
膜内イオン 25 個になっていた。タイリング時に除去し、中和で bulk に置き直す (30 個除去)。
**パッチ自体の欠陥**なので、パッチを作り直すのが本筋。

### 可視化 — これが無ければ 4 件とも見つかっていない

`render_structure_preview` に `system_box` スタイルを追加。蛋白 chain 別 cartoon、
脂質 stick、水は半透明 surface、イオン sphere、**周期セルをワイヤ枠**で描く。
orthographic、x 軸と z 軸の直交 2 視点。セルは**水+脂質の重心**に描く (系全体の重心だと
箱から出た蛋白に箱が引きずられて意味を失う)。

cursor 指摘で `orthogonal_view` を `system_box` 限定にした。全スタイルに効いていたため
`ligand_site` などが本来のカメラを失っていた。

skill 側は `visual-qa.md` に「系を変える各ステージで描画し、**画像をユーザに送る**」、
`run-loop.md` に「各ステップ後に node ID・実際に使われた条件・系のサイズ・warnings を
報告する」を追加。**描いてもユーザに見せなければ描いていないのと同じ**。

### 既存成果物への影響

D473Y / G497W の既存系はすべて (1)(3)(4)(5) の影響下にある。D473Y は solv まで作り直した。

### cursor の P2 指摘を反映

| 指摘 | 対応 |
|---|---|
| tail 平均が鎖長・リーフレット組成で偏る | `_leaflet_midpoint` を実装。両リーフレットを個別に求めて**重み無し**中点。実パッチで 48.7 (真値 48.8、tail 平均は 48.5) |
| コレステロール単独パッチを扱えない | 頭部基 → tail → 全脂質原子の順にフォールバックし、いずれも判別器を通す |
| 5 A 許容値が不適切 | ガードを密度判定に置換したので定数ごと削除 |
| `solvent_ions` が surface の上に dots を重ねる | 汎用ブロックは dots に戻し、surface は `system_box` 専用に |
| `system_box` が `show_lipids`/`show_ions` を無視、manifest と PNG が食い違う | フラグを尊重し、**実際に描かれた表現を PyMOL から読み戻して** manifest に記録 |
| 周期セルが三斜箱を直方体として誤描画 | α/β/γ を検査し、直方体でなければ描かずに理由を記録 |
| manifest / node metadata に 2 枚目の画像が無い | `views` (軸と画像パス)、`periodic_cell`、`output_png_top` を記録 |
| skill の記述が矛盾、prep に `system_box` を指定 | 「描画の**試行**は毎ステージ、成功は best-effort」。prep は `overview`、solv 以降が `system_box`。2 枚とも見るよう明記 |

判別器も改良した。頭部基だけでは「膜の中点」と「水の中点」が等価なので、
**アシル鎖の存在と水の不在の両方**で決める (水だけだと合成パッチで判別できなかった)。

CLI 側も埋めた。`render_structure_preview` の docstring にスタイル一覧・`system_box` の
描画内容・2 視点・「ユーザに送ること」を書き、`docs/developer/tool-reference.md` も更新。

最終確認 (solv_016): 二重層 midplane 0.1、膜内イオン 0、塩 **0.150 M** (要求 0.150)、
イオン 下59/上68 (水は 1:1)、geometry passed。

### 未対応

- タイリング由来の人工的周期性 (イオン 68 個が 28 箇所の xy に重なる、同一脂質配置の複製)
- **同梱パッチ自体の作り直し** (二重層内にイオンが埋まっている)
- G497W の再構築、D473Y も solv 止まり

---

## 2026-08-19 — バグ: patch-tile が z 方向の box サイズをタンパク質から決めていなかった

hackathon メンバーから「GPCR 5L7D を膜に埋めて水和したら、細胞外ドメインが box の
両側からはみ出た」との報告。**既定バックエンド `patch-tile` の実バグだった。**
cursor にも独立検証させ、4 点すべて追認された (指摘 3 点は私の説明の補正)。

### 根本原因

`patch_membrane.py:636`

```python
box_c = 2.0 * (float(dist_wat) + float(leaflet))   # 溶質が一切入らない
```

`patch_membrane.py` の組み立て:

```python
total_box = {"box_a": nx * box_a, "box_b": ny * box_b, "box_c": box_c}
```

`nx`/`ny` は `_tile_counts` がタンパク質の XY 境界 + 2*dist から決める。
**z には対応物が無い。** `_tile_counts` は `_bounds()` の `_minz, _maxz` を明示的に
捨てている。箱の高さの取得元は 3 つ (キャッシュ / patch の CRYST1 / 導出式) あるが、
**どれもパッチの高さであって溶質とは無関係**。

### 5L7D 実測

```
5L7D chain A 膜フレーム          z = -30.3 .. +77.9 A   (span 108.2 A)
既定 box_c = 2*(17.5+23.0)                             =  81.0 A
build_amber_system の +2 A margin 後                   ~  83.0 A  <- MD の実箱
箱外の原子              3754 中 1131 個 (30.1%)
最小イメージで折り返す先                               z in [-40.5, -3.1]
うち周期像の脂質コア (|z|<15) に入る原子                240 個
```

**細胞外ドメインが隣の周期像の膜を貫通している。**

### なぜ静かに壊れるか

1. **carve が PBC 対応** (`patch_membrane.py:1795`) なので、折り返した CRD の周りの脂質が
   「正しく」除去され、**膜に穴が空いた系がエラー無しで完成する**。
2. **geometry チェックが検出できない** (`membrane.py:327`)。見ているのは headgroup span と
   「タンパク質原子の 15% 以上が膜と交差するか」だけ。5L7D は TM 部分で余裕で通る。
   溶質の z 範囲も箱面までのクリアランスも計算していなかった。

### MD まで伝播する

`total_box` → CRYST1 + `box_dimensions.json` → `build_amber_system` →
`openmm_build.py:1037-1060` の `setPeriodicBoxVectors` → `system.xml` / `state.xml` →
min/eq/prod が state の箱ベクトルを優先採用。`center_solute_and_wrap_solvent` は
最大分子を意図的に wrap しないので形は保たれるが、83 A の箱に 108.2 A の分子は入らない。

### packmol-memgen 経路は無事

上流ソース (`packmol_memgen/main.py`) は

```python
z_max = pdbz_max + distance_wat
if z_max < (leaflet_z + distance_wat): z_max = leaflet_z + distance_wat
```

と溶質と膜の**大きい方**を採る。CLI ヘルプも "water layer over the membrane **or protein**"。
`patch-tile` が名前だけ借りて「膜からの厚み」として実装したのが分岐点。
なお `membrane_backend` の正しい値は `packmol-memgen` / `patch-tile` / `auto` で、
`full` は存在しない (私が最初にそう書いたのは誤り)。

### 修正 (恒久対応)

**`dist_wat` を packmol-memgen と同じ「膜または溶質のうち遠い方からのパディング」に
定義し直す。** (当初はパッチ自体を高くしたが、下記のとおりやり直した。)

```python
effective_dist_wat = max(dist_wat, max|z_solute - centre| + dist_wat - leaflet)
```

5L7D では 17.5 → **72.4**、box_c 81.0 → **190.8 A**。TM のみの蛋白では 17.5 のまま
(回帰なし)。

**箱だけ広げる案は採らなかった。** 水を足さずに CRYST1 を伸ばすと真空層ができて
密度が壊れる。パッチを高くすれば、追加された体積は平衡化済みの水で正しい密度のまま埋まる。
キャッシュは `dist_wat` を指紋に含むので、高いパッチは自動的に別エントリになり、
**スキーマ変更は不要**。代償は「背の高い蛋白の初回 cold build が長い」ことで、
cold-build notice にその旨を出すようにした。

対称箱 (190.8 A) を採り、非対称最小箱 (143.2 A) は見送った。非対称にするには
平衡化済みパッチを任意面で切る必要があり、切断面同士は真の周期対応ではないので
継ぎ目が生じる。水量は約 33% 多いが、継ぎ目の無い正しい系を優先した。

保険として 2 つ追加:
- 組み立て後に `solute_fits_box` で封じ込めを検査し、入らなければ
  `membrane_patch_solute_exceeds_box_z` で拒否 (古いキャッシュ由来の高さ対策)
- geometry レポートに `protein_exceeds_periodic_box_z` を追加。膜との交差判定とは
  **別の失敗理由**にしたので、「膜には正しく入っているが箱に入っていない」を検出できる

### cursor レビューで自分の修正に P1 回帰が出た

**封じ込め判定を「膜中心 ± box_c/2」に対する原子位置で書いたのが誤り**だった。これは箱が
膜中心に対して対称であることを前提にしており、packmol-memgen が作る非対称箱では偽。
cursor が再現したとおり、108.2 A の溶質が入る 143.2 A の箱を「6.3 A はみ出し」と誤判定して
落とす。`auto` で fallback した後にこれが走るので、**正しく作った系を落とす回帰**だった。

PBC では原点は任意で、面をまたいだ分子は反対側から入り直すだけ。平行移動で消せないのは
**分子が周期長より長い**ことだけ。判定を `protein_z_span >= box_c` に変えた
(`solute_fits_box` も同様。中心が不要になったので `membrane_center_z=None` の二重解釈と
いう別の指摘も同時に解けた)。

### さらに設計をやり直した: パッチを高くするのではなく、水を足す

ユーザから 2 点の指摘を受けた。**どちらも正しく、最初の実装は誤りだった。**

1. 「キャッシュが効くようにできないの？」
2. 「23 A なんて多くの膜タンパク質が対象になると思うけど」

拡大条件は `reach + dist_wat - leaflet > dist_wat`、すなわち **`reach > leaflet` (23 A)**。
**|z| が 23 A を超える膜蛋白はほぼ全部が対象**になる。そして `dist_wat` はキャッシュ指紋に
入っているので、**そのたびに cold build が走る**。実際、2LOP のパイプラインテストが
通常 ~100 秒のところ **20 分以上** cold build を回していた。

**膜パッチの高さを溶質に依存させたのが誤り**だった。二重層は蛋白と無関係なので
キャッシュしたまま使い、**足りない水だけを z 方向に足す**のが正しい:

```python
low  = min(solute_z_min, centre - leaflet) - dist_wat
high = max(solute_z_max, centre + leaflet) + dist_wat
```

パッチは常に呼び出し側の `dist_wat` で要求するのでキャッシュは必ずヒットする。
足りない体積は**パッチ自身の水スラブのコピーを z 方向に積んで**埋める。溶媒和プログラムが
平衡化済み水ボックスを複製するのと同じやり方で、密度もイオン濃度もそのまま乗る。
コピー同士の境界は真の周期対応ではないので、既存原子と 2.2 A 以内に来た分子は
**丸ごと落とす** (これも溶媒和プログラム標準の重なり除去)。

**箱は非対称最小になった。** 5L7D で **143.2 A** (対称なら 190.8、バケット化ありなら 206)。
細胞外ドメインぶんの水を膜の下側にミラーする必要が無くなったので、水量も減った。

実データ検証 (5L7D 実座標 + 合成パッチ):

```
interval        low=-47.8  high=+95.4  box_c=143.2  (extend below 7.3 / above 54.9)
extension       18252 分子追加、重なり棄却 0
assembled z     -45.1 .. 94.5
containment     fits=True  span=108.2  headroom=35.0
水の数密度       元スラブ 0.0330 /A^3  →  拡張部 0.0329 /A^3
```

**パイプラインテストは 97.79 s / 4 passed に戻った** (cold build 20 分超 → キャッシュヒット)。

なお高さバケット化と `membrane_patch_box_too_tall` の上限は、パッチを高くしなくなったので
不要になり削除した。

### テスト

`tests/test_solvation_server.py` に 4 本追加 (5L7D の実数値で拡大を検証 / TM のみは
拡大しない / 箱に入らない溶質を拒否 / 膜判定は通るが封じ込めで落ちる geometry ケース)。
レビュー対応後に 3 本追加 (非対称箱を受理する / 上限超過を拒否する / 高さバケット)。
ruff clean、solvation + guardrail + contract + orientation 系 321 passed。

---

## 2026-08-19 — arm64 イメージに PPM3 を移植。amd64 の検証は無効、MODELLER の ldd は初実走で自壊

Rikyu 用 SIF を作り直すため、`Dockerfile.rikyu-arm64` を Mac (Apple Silicon /
Docker Desktop) で再ビルドした。amd64 に入っている PPM3 パッチが arm64 側に未移植
だったので、それを移す作業。移すだけのつもりが、両方の**検証**が壊れていた。

### aarch64 の conda 版 immers も同じバグを持っている

移植前のイメージ (`54798ff`) を調べたところ、`/opt/mdclaw/bin/immers` は**既に存在
する**。ppm3 のソースディレクトリには binary が無く、ambertools の conda パッケージが
aarch64 ビルドを bin に置いている。中身:

```
' tilt=',f7.0'+-',        <- カンマ欠落。amd64 の同梱バイナリと同じ
```

なので arm64 でも「パッチして make し直し、conda 版を上書きする」が必要だった。
`install -m 0755 ... /opt/mdclaw/bin/immers` はその上書きになる。

### amd64 の post-check は一度も発火していない

```
/opt/mdclaw/bin/immers < /dev/null 2>&1 | grep -q "Fortran runtime error: Missing comma"
```

空 stdin だと `opm.f:84` の最初の read で "End of file" で死ぬ。**問題の FORMAT 行に
到達しないので、このパターンは絶対に一致しない。** さらに `sed && grep || true` の連鎖
なので、sed が当たらなくても `|| true` に飲まれてビルドは続く。つまり amd64 側は
「パッチが当たらなくても素通りする」状態。

arm64 版はコンパイル済みバイナリ内の FORMAT 文字列で判定するようにした。パッチ後は
`f7.0,'+-'`、未パッチは `f7.0'+-'` が入っているので、これは実際に区別できる。空 stdin
で走らせる方は残したが、意味は「バイナリが共有ライブラリを解決して Fortran ランタイム
まで到達する」ことの確認に変えた。同じ判定を `test-container.sh` にも入れ、
`MDCLAW_PPM3_PATCHED` を宣言したイメージにだけ効かせる (古い SIF は SKIP)。効き目は
古いイメージに変数を立てて確認済み: 20 passed / **1 failed**。

### MODELLER の ldd 検証 (8afd86e) はイメージではなく自分が壊れていた

初回ビルドは stage 2 の 19/20 で落ちた。

```
libglib-2.0.so.0 => not found
libmodeller.so.14 => not found
```

どちらもイメージ内に実在する (glib は conda の `/opt/mdclaw/lib`、libmodeller は拡張の
隣)。`ldd` をランタイムの `LD_LIBRARY_PATH` 無しで走らせていたのが原因で、**検査の欠陥
であってイメージの欠陥ではない**。2026-08-18 のエントリで「rikyu の end-to-end ビルドは
未検証」と書いた通り、この検査は今回が初の実走だった。ランタイムが宣言しているのと同じ
検索パスを与えて解決。

### 結果

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-f9e628126877  21 GB
digest  sha256:32fde85be54f4582a13129808092881af4ff5fcb225b7564aa23bb3797475ddf
smoke   23 passed, 0 failed   (PPM3 と MODELLER を含む。GPU は SKIP)
```

GHCR に push 済み。パッケージは public で、匿名トークンで manifest を引けることを確認
したので、Rikyu 側は資格情報なしで `apptainer pull` できる。

SIF は手元の Lima VM `singularity-ce` (singularity-ce 4.5.0) で digest 指定 pull から
変換した。

```
~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-f9e628126877.sif
6,774,157,312 bytes
SHA-256 367af38cb733207176703d69b5d115f629707565db40ebf8ad93befb2947d8e4
```

**SIF からも container test 23/23。** PPM3 チェックが SIF 内で通ったことには意味があり、
再ビルドした `immers` が (apt の gfortran ではなく) conda の libgfortran.so.5 を
ランタイムの LD_LIBRARY_PATH 経由で解決できていることの確認になっている。残るは Rikyu
実機での GPU smoke (`test-rikyu-gpu.sh`) で、これは SIF からでないと FUSE 経路を踏まない。

### Mac でビルドできる

`build-rikyu-arm64.sh` は arm64 host なら通るが、`nproc` と `df -BG --output` が GNU
限定で macOS では動かなかった (前者は `set -e` でその場で死ぬ)。`sysctl -n hw.ncpu` /
`df -k` へのフォールバックを入れた。Docker Desktop の VM は 14 CPU / 8.3 GB なので
`BUILD_JOBS=6` に絞った (並列 nvcc はメモリを食う)。SIF 化だけは Mac では出来ない
(apptainer が無い) ので、GHCR に push して Rikyu 側で `apptainer pull` する。

---

## 2026-08-19 — cursor 再レビューで 10 件。coplanar 棄却と sparse-perfect の順位が実バグ

同じ `smo_reviewer` に修正後の差分を再レビューさせた。**今回はファイルを一切変更していない**
(前回の違反を依頼文に明記し、テスト提案はレビュー文書内に書くよう指示した)。
新規指摘 10 件 (P1×2, P2×6, P3×2)。**設計を変える 2 件は自分で再現してから直した。**

### 実測で確認したこと

```
[coplanar]  rmsd=4.5e-15  det=1.000  max|R-Rtrue|=3.3e-16  fit_condition=0.0000  <- 正しい fit を棄却
[collinear] s2/s1=0.0000  s3/s1=0.0000
[1 helix]   s2/s1=0.0947  s3/s1=0.0929
[rank]      sparse (1.0, 0.2, -2.9) > broad (0.99, 1.0, -0.1)   <- 40/40 が 198/200 に勝つ
[validate]  max_candidates=1.5 -> None                          <- 小数が通る
[short ATOM] RAISED IndexError: string index out of range
```

### 訂正1: 縮退判定は rank-2 で十分だった (私が厳しすぎた)

`_fit_condition` を s3/s1 (最小/最大主成分) にしていたが、**これは Kabsch の可同定条件より
厳しい**。非共線な 3 点以上あれば面内基底 2 本が決まり、**proper rotation 制約が法線を決める**。
実際、完全に共面な 40 点で一般回転を **3.3e-16** の精度で復元できるのに、私のゲートは
`fit_condition=0.0000` でこれを棄却していた。**s2/s1 に変更**: 共線 0.000 / 共面 1.000 /
単一理想ヘリックス 0.095 / 実膜 CA 集合 0.86-0.87。しきい値 0.01 は据え置き。

### 訂正2: identity の丸めでは sparse-perfect を止められない

前エントリで「支持量を最良候補比の 1/10 に丸める」ことで 201 対 200 問題を解いたが、
**identity を先頭キーに置いたままだと 40/40 (100%) が 198/200 (99%) に勝つ**。
40 観測の 100% は 200 観測の 99% より*弱い*主張である。
**Wilson 下限**に置き換えた: 40/40 → 0.91、198/200 → 0.96、200/200 と 201/201 → ともに 0.98。
2 桁に丸めれば 201 対 200 も同値になり RMSD が決める。キーは
`(round(wilson_lb(membrane_identity, membrane_ca), 2), -fit_rmsd)` の 2 本になり、
支持量バケットは Wilson が吸収したので削除した。

### その他の修正

| # | 内容 |
|---|---|
| P1 | **全 query chain を採点してから 1 本のランキングで決める**。従来は「最初に受容可能な donor を持った鎖」が勝っていた。長い膜結合パートナーが辛うじて通る donor で複合体全体の配向を決め、本命の膜貫通サブユニットが使われない |
| P1 | **予算切れで候補が残ったら「最良」と主張しない**。ゲートを全部通った donor は採用する (別手法に落ちるより良い) が、`evaluation_complete=false` と warning を出す |
| P2 | **altLoc は残基単位で 1 つ選ぶ**。原子ごとに occupancy 最大を取ると CA が conformer A、CB が B という**実在しない混成側鎖**ができる |
| P2 | **TER を保持**。落とすと 2 本のポリマーが 1 鎖に融合し、無い結合ができる |
| P2 | **不完全評価に専用コード** `opm_homolog_evaluation_incomplete`。「1 件棄却 + 1 件 DL 失敗」を `rejected` と報告すると、エージェントは「調べた結果ダメだった」と解釈して再試行しない |
| P2 | **54 桁未満の ATOM を構造化して弾く** (従来は IndexError が外に出ていた) |
| P3 | **カウント系は整数必須**。`max_candidates=1.5` が RCSB に不正ページ要求として届き、依頼者のミスが「検索障害」として返ってきていた。`min_fit_condition=0` も禁止 |
| P3 | **予算の 1 秒下限を撤去**。短い明示予算が拘束力を失う |

### 予算切れ時の扱いはレビュー提案と変えた

レビューは「切り詰められた候補集合からは選ぶな (= PPM3 に落とせ)」としたが、
**全ゲートを通った donor を捨てて別手法に移るのは利用者の不利益**と判断した。採用したうえで
`evaluation_complete=false`、warning、`report` への記録で「比較が不完全だった」ことを明示する。
一方、**何も受容できなかった場合に `rejected`/`no_match` を返すのは誤報**という指摘は全面的に
正しいので、そちらは `opm_homolog_evaluation_incomplete` にした。

### ライブ再測定

修正後も 5L7D で **5L7D 自身を採用、PDBTM 法線誤差 5.9 度** (変化なし)。
mock 版 4JKV 経路も 11.8 度で不変。

### テスト

ruff clean。`test_membrane_orientation.py` 62 → **75** 本。contract 系込み 467 passed。

---

## 2026-08-19 — cursor レビューで 10 件。うち 5 件は再現、ランキングの欠陥も実測で露見

`smo_reviewer` (cursor / GPT-5.6 Sol) に未コミット差分をレビューさせた。指摘 10 件
(P1×2, P2×6, P3×2)。**再現できるものは全部走らせ、5 件すべて再現した。**

```
[min_ca=0]      RAISED LinAlgError: 0-dimensional array given
[NaN rmsd]      accepted=True  fit_rmsd=39.146      <- 39 A の fit を通す
[collinear]     rmsd=0  det=1.000                    <- 軸回りの回転が不定
[multi-model]   残基は model 1、CA 座標は model 2 (z=99)
[altLoc]        occupancy 0.70 が z=0 なのに z=50 を採用
```

### 手続き上の問題も 1 件

レビュー依頼には「ファイルを一切変更しないこと」と明記したが、cursor は
`tests/test_membrane_orientation.py` にテストを 1 本追加していた
(`test_partial_outage_with_a_completed_no_match_is_not_total_unavailability`、
07:05:36)。他ファイルへの混入はない (memo と tool-reference の変更は私のもの)。
**ただし指摘内容は正しかった**: 1 鎖が HTTP 500、別の鎖が正常に 0 件だったとき、
私の実装は `opm_homolog_search_unavailable` (「どの鎖も検索できなかった」) を返していた。
実際には 1 鎖は検索できて「該当なし」と答えている。アサーションは実在しない文言を
要求していたので書き直し、コード側を「検索できた鎖の結果と、検索できなかった鎖の数を
両方述べる `opm_homolog_no_match`」に修正した。

### 直したもの

| # | 内容 |
|---|---|
| P1 | **膜サブセットの identity をゲート追加**。全鎖 identity だけだと、大きな可溶性ドメインを共有し膜ドメインが無関係な donor が通る。fit は膜サブセットで行うのだから、対応が実在すべきはそのサブセット |
| P1 | **公開パラメータの検証** (`opm_homolog_gates_invalid`)。範囲外・非有限を拒否し、**fallback ではなく失敗**にする。ゲートを黙って緩めるのは依頼を断るより悪い |
| P2 | **同一配列 query chain は検索だけ共有し、フィットは物理鎖ごと**に行う |
| P2 | **全候補を評価してから選ぶ** (ユーザ判断)。RCSB の順位は検索関連度であって配向品質ではない |
| P2 | **縮退フィットの棄却** (`opm_min_fit_condition`)。共線 CA は RMSD 0・det 1.0 で通るが軸回りの回転が任意 |
| P2 | **DL 失敗をゲート不合格と分離** (`opm_homolog_fetch_unavailable`)。判定していない donor を「品質不足」と報告していた |
| P2 | **model 1 と最高 occupancy altLoc だけ**を fit にも出力にも使う |
| P2 | **総時間予算** (`opm_total_budget_seconds`、既定 600 s)。従来は 120 s × 鎖数 × 候補数 |
| P3 | **キャッシュのアトミック書き込みと整合検査**、SHA-256 記録 |
| P3 | **空ボディの 200 は unavailable**。204 だけが RCSB の no-hit |

### 縮退しきい値は実測で決めた

`s3/s1` (最小/最大主成分ひろがり): 実膜 CA 集合 **0.685-0.702**、単一の理想 α ヘリックス
40 残基 **0.093**、共線・共面 **0.000**。**0.01** なら両側に一桁の余裕がある。
なお**テスト fixture 自体が縮退していた** (`_membrane_path` の x と y が比例 = 平面曲線)。
新ゲートがそれを正しく検出したので、fixture を真に 3 次元の螺旋に書き直した。

### 全候補評価にしたら、ランキングの欠陥が実データで出た

ライブ実行で 10 候補すべてを採点したところ:

| pdb | 膜内identity | 膜内CA | fit RMSD |
|---|---|---|---|
| 5l7i | 1.000 | 201 | 0.325 A |
| **5l7d** | 1.000 | **200** | **0.000 A** |
| 7zi0 | 1.000 | 197 | 0.181 A |
| 6ot0 | 0.994 | 176 | 1.927 A |

当初の順序 (identity → 膜内 CA 数 → RMSD) は、**CA 数 201 対 200 の 1 残基差で
5L7D 自身 (完全一致、RMSD 0.000) を 5L7I に負けさせた**。0.5% の支持量差が 3 倍の
RMSD 差を上書きするのは誤り。支持量を**最良候補比の 1/10 刻み**に丸め、同程度なら
RMSD で決めるよう修正した。

修正後は **5L7D 自身が選ばれ、PDBTM 法線誤差 5.9 度**。これは
**OPM と PDBTM という 2 つの参照 DB の不一致そのもの**であり、転写手法の理論的下限。
query が OPM に登録済みという有利なケースではあるが、パイプラインが端から端まで
正しく動いていることの証明にはなる。

前エントリの 6.3 度 (6OT0) と 11.8 度 (4JKV) も同じ 5L7D に対する値で、
**どの donor を選ぶかで 5.9-11.8 度動く**。donor 選択がこの手法の精度を支配しており、
fit RMSD ではないことが改めて確認された。

### 評価された点 (churn するなと明記された)

CIGAR walk の I/D 方向は両向き確認して正しい、DUM スラブ限定は全体 fit や
trimming より明確に安全、donor 鎖の gate 優先、部分障害で後続鎖を止めない、
JSON の鎖別棄却理由。

### テスト

ruff clean。`test_membrane_orientation.py` は 26 → **62** 本。
contract 系込みで 454 passed。

---

## 2026-08-19 — 訂正: 「OPM 相同体転写は明確に悪い (13.5 度)」は誤り。実検索の donor では 6.3 度

ユーザに「全鎖 HTTP 500 はおかしい」と指摘されて RCSB を直接叩いたところ、**検索は正常に
動いていた**。切り分けの過程で 2 件の実バグが出て、さらに**前 2 エントリの精度評価が
覆った**。

### バグ1 (重大): ヒット 0 件を「通信障害」と誤報告していた

RCSB は結果 0 件を **204 No Content + 空ボディ**で返す。urllib は 2xx を成功として扱うので
`HTTPError` は上がらず、`json.load` が JSONDecodeError で落ち、汎用ハンドラが
`"RCSB search unavailable: JSONDecodeError"` を返していた。つまり
**「この鎖には OPM 相同体が無い」が「検索サービスに到達できない」に化けていた**。
多鎖集約と噛み合うと、OPM 相同体を持たない蛋白が全鎖 no_match のはずが
`opm_homolog_search_unavailable` として報告される。`response.status == 204` と空ボディを
明示的に「該当なし」として扱うよう修正。

### バグ2: HTTPError の本文を捨てていた

`f"RCSB search returned HTTP {exc.code}"` だけを返しており、500 が本当のサーバ障害なのか
クエリ不正なのか区別できなかった (実際それで診断が止まった)。本文 300 字を添えるよう修正。

なお **8/18 に観測した HTTP 500 は本物のサーバ側障害**で、8/19 時点では解消している
(SMO 配列 + OPM フィルタで 3.2 秒 / 19 件)。albumin + OPM は 204 = 0 件が正解。

### バグ3: 検索値のスケールが混在していた

RCSB の `match_context.sequence_identity` は **0-100 のパーセント** (95.5)。これを
`local_identity` (0-1、0.81 等) と同じ JSON に並べて記録していた。`query_coverage` は
**そもそも返ってこない** (`query_beg/query_end/query_length` はある)。identity を分数に
正規化し、coverage は範囲から導出、`search_alignment_length` も記録するようにした。
いずれも provenance のみでゲートには使わない方針は不変。

### 訂正: 転写の精度評価は「手で選んだ donor」の評価だった

前 2 エントリは **転写 13.5 度 (後に膜スラブ限定で 11.8 度) > PPM3 6.8 度**、
「転写は明確に悪い」「カスケードの主経路にはしない」と書いた。**これを取り消す。**

その測定はすべて donor を **私が手で 4JKV に固定**して行ったもので、検索が実際に返す
donor で測っていなかった。バグ1を直して**完全ライブ (mock 一切なし)** で通したところ:

| donor | 選定 | 膜内 CA | fit RMSD | PDBTM 法線誤差 |
|---|---|---|---|---|
| 4JKV | 手で指定 | 193 | 0.81 A | 11.8 度 |
| **6OT0** | **実検索の最上位** | 176 | 1.93 A | **6.3 度** |

6OT0 (SMO の cryo-EM 構造) からの転写は **6.3 度**で、**PPM3 の 6.8 度より良く**、
参照 DB 同士の不一致 5.9 度の内側にある。つまり「転写だけが 5.9 度の外側」という
前エントリの主張は成立しない。

**fit RMSD は法線誤差を予測しない。** 4JKV は 0.81 A で 11.8 度、6OT0 は 1.93 A で 6.3 度と
逆順になる。重ね合わせの残差はドナー座標との一致度であって、ドナーの膜フレームが
どれだけ正しいかとは別物である。ゲートは「どこまで悪い donor を許すか」の下限であって
donor の順位付けではない、と読むべき。

### donor 側 gate 優先選択の実戦での効き

6OT0 は 6 鎖ある。全 gate 適用後に残ったのは受容体鎖 R のみ:

| donor chain | identity | coverage | 膜内 CA | 判定 |
|---|---|---|---|---|
| R | 0.997 | 0.722 | 176 | **採用** (RMSD 1.93 A) |
| A | 0.298 | 0.741 | 2 | identity 不合格 |
| B | 0.324 | 0.707 | 1 | identity 不合格 |
| G | 0.793 | 0.122 | 6 | coverage 不合格 |
| L | 0.644 | 0.219 | 0 | coverage 不合格 |
| H | 0.579 | 0.265 | 0 | coverage 不合格 |

query 側も albumin 578 残基 (longest) が `no_match`、SMO 475 残基で採用。所要 4.1 秒。

### 残る留保

6.3 度は 5L7D 一例の値であり、6OT0 が同一蛋白のほぼ完全一致 (identity 0.997) である
有利なケース。遠縁の donor で同じ精度が出る保証はない。**手法の順位を主張するには
複数ターゲットでの測定が要る**。今回言えるのは「前エントリの『転写は明確に悪い』は
donor 選定の人為で、実検索経路では成立しない」ことまで。

---

## 2026-08-19 — OPM 転写を「全 protein chain を検索」「gate 通過鎖の中から最良」に修正

差分レビューで受入れ前の必須修正として 2 点指摘された。どちらも**主経路が使えるはずの
構造で黙って使われなくなる**類の欠陥で、fallback が働くので失敗としては表面化しない。

### 1. query 側: longest chain しか検索していなかった

膜蛋白の複合体は「長い可溶性パートナー + 短い膜サブユニット」がごく普通の形で、
その場合 OPM 相同体を持つのは短い方だけ。longest chain だけを検索すると、**主経路が
存在するのに no_match で PPM3 に落ちる**。修正後は全 protein chain を長い順に検索し、
最初に全 gate を通った donor で確定する。同一配列の鎖 (ホモ多量体) は 1 回だけ検索する。

`opm_homolog_search.json` は per-query-chain 構造に変更した:
`query_chains[*]` に chain / equivalent_chains / residues / outcome
(`accepted` | `rejected` | `no_match` | `search_error` | `not_searched`) /
search_error / candidates を鎖ごとに分けて記録する。

**ある鎖の通信エラーで全体を打ち切らない。** 1 鎖の HTTP 500 は他鎖について何も語らないし、
相同体を持つのは往々にして後の鎖である。全鎖が通信不能だったときだけ
`opm_homolog_search_unavailable` を返し、reason に鎖ごとのエラーを列挙する。
候補が 1 つでも評価されていれば `opm_homolog_rejected`、どの鎖もヒット無しなら
`opm_homolog_no_match`。OPM 構造の取得・パースは PDB ID ごとに 1 回だけで、
複数の query chain が同じ entry に当たってもキャッシュを再利用する。

### 2. donor 側: 最低 RMSD を先に best にしてから gate を掛けていた

旧実装は donor の全鎖のうち fit RMSD が最小の鎖を best とし、**その後で** identity /
coverage を判定していた。短い無関係な区間にアラインした鎖は「短いからこそ」タイトに
重なるので、本当の対応鎖を押し退けて candidate 全体を巻き添えで棄却させ得る。
修正後は `_fit_donor_chain` が鎖ごとに全 gate を適用し、**全 gate を通った鎖の中から
最低 RMSD** を選ぶ。全鎖不合格なら各鎖の数値 (identity / coverage / membrane CA / RMSD) と
rejection reason を `homolog_chains` に残し、gate を最も先まで通った鎖の理由を
candidate の rejected に採用する。

合成 donor で実証: 鎖 P (identity 1.00, memCA 80, RMSD 0.45) と鎖 Q (identity 0.36,
memCA 80, RMSD 0.00)。旧規則は Q を選んで identity で候補ごと棄却、新規則は P を採用する。

### 実測 (5L7D)

アルブミン 1AO6 chain A (578 残基, 可溶性) を chain A、5L7D chain A (475 残基) を
chain B とした実構造 2 鎖複合体で検証。longest chain は可溶性側になる。

| ケース | 結果 |
|---|---|
| 両鎖に 4JKV を提示 | chain A は identity 0.298 で棄却 → chain B で採用 |
| chain A だけ HTTP 500 | chain B で採用 (打ち切られない) |
| 全鎖 HTTP 500 | `opm_homolog_search_unavailable`、両鎖のエラーを列挙 |

採用時の数値は donor chain B / aligned 432 CA / 膜内 193 CA / fit RMSD 0.808 A /
厚さ 31.9 A、**PDBTM 法線誤差 11.8 度で変更前と完全に一致**。donor は 4jkv を 1 回だけ取得。
donor 側は chain A (RMSD 0.813) と chain B (0.808) がともに全 gate を通り、低い方の B を選ぶ。

なお **RCSB の sequence 検索は今日も HTTP 500 のまま**で、実通信では両鎖とも
search_error になり PPM3 に落ちる。主経路がオンライン依存である点は前エントリのとおり。

### 変更ファイル

`mdclaw/solvation/opm_orient.py` (`_fit_donor_chain` / `_consider_candidate` を新設)、
`mdclaw/guardrail_codes.py` (説明文のみ、code は不変)、`tests/test_membrane_orientation.py`
(33 tests)、`docs/developer/tool-reference.md`、`skills/md-prepare/membrane.md`。
tool-reference に残っていた "outlier-trimmed Kabsch fit" の古い記述も膜スラブ限定に直した。

---

## 2026-08-18 — 方針転換: TMbed を全廃し、OPM 相同体転写 → PPM3 のカスケードへ

承認された計画 (`~/.cursor/plans/opm-ppm-orientation-f89b45ec.plan.md`) に沿って実装。
配向は「OPM 相同体があれば転写、無ければ PPM3」になり、TMbed と ProtT5 はコード・CLI・
依存・コンテナ資産から完全に削除した。**過去エントリの測定と結論は取り消していない。**

### 実装したもの

- `mdclaw/solvation/opm_orient.py` (新規)。入力鎖配列 → RCSB Search API の sequence 検索と
  `rcsb_polymer_entity_annotation.type=OPM` の積集合 → OPM 公開 PDB 取得 → gemmi 配列
  アラインメント → 外れ値除去つき Kabsch → 入力構造全体 (リガンド含む) へ適用 → DUM から
  膜中心を読んで z=0 に揃える。品質ゲート (identity / coverage / 対応CA数 / fit RMSD) を
  引数化し、**不合格候補も理由と数値を `opm_homolog_search.json` に残す**。
- `membrane.py` の `auto` を OPM→PPM3 に変更。`_orient_for_membrane` が試行履歴
  (backend / success / code / reason) を `result["orientation"]["attempts"]` に記録する。
  MEMEMBED と PPM は明示指定として残す。`tm-segments`、`membrane_topology_file`、
  `auto_predict_topology`、TMbed 由来の barrel 判定と topology consistency は削除。
- `ppm_orient.py` の「n_terminal_side 未指定を黙って out にする」挙動を廃止。PPM3 は値を
  必ず要求するので PPM 自身の慣習で走らせるが、**assumed であることを warning と
  `n_terminal_side_assumed` に明記**する。

### 実装中に判明したこと

**RCSB の sequence 検索は現在サーバ側で継続的に失敗する** (HTTP 500、"did not complete
ticketId within 30000 ms"、5 回連続)。OPM annotation フィルタ単体は動く (18,981 entity)。
つまり主経路がオンライン依存で、現に落ちている。計画どおり通信失敗は失敗コードではなく
fallback event として扱うので実害は出ないが、**本番でどれだけ転写が使われるかは RCSB の
状態次第**であることは記録しておく。

**全対応ペアで一括 Kabsch すると実用にならない。** 5L7D (CRD あり) に 4JKV (7TM のみ) を
当てると 429 対応ペアで fit RMSD 14.84 A となり品質ゲートで棄却された。当初は外れ値の
反復除去 (中央値ベース) で対処したが、**レビュー指摘を受けて廃止した**。trimming は
「最もよく合う部分集合」を選ぶので、二つの蛋白が大きな可溶性ドメインを共有しつつ膜内の
座り方が違う場合、**そのドメインだけで膜配向を決めてしまう**。膜転写がやってはいけない
ことそのものだった。

現在は **donor 自身の DUM z 範囲 (±2 A マージン) 内にある対応残基だけで Kabsch** する。
品質ゲートは (a) 全配列のローカル identity/coverage、(b) 膜スラブ内の対応 CA 数、
(c) そのフィットの RMSD の三本立て。`_kabsch_trimmed` は膜が絡まない比較用の補助関数として
残すが主経路からは外した。

**膜スラブ限定にすると法線誤差が 13.7 → 11.8 度に改善した** (独立参照 PDBTM 比)。
5L7D→4JKV で膜内対応 193 CA / fit RMSD 0.81 A。過去に素朴な残基番号一致で測った
162 CA / 0.60 A と整合する (±2 A マージンのぶん残基が多く RMSD も僅かに大きい)。
ただし依然として PPM3 の 6.8 度より悪く、参照系どうしの不一致 5.9 度の外側にある。
**これは重ね合わせの粗さではなく手法固有の値**で、PPM が構造ごとに独立に最適化するため
同一蛋白の別構造でも OPM 注釈が約 5 度食い違うことに由来する。

**RCSB 検索は POST + `results_verbosity=verbose` に変更した。** 膜蛋白の配列は URL
クエリに収まらない。また RCSB の `match_context` は `sequence_identity`/`query_coverage`
を欠くことがあり、None のままではゲートを素通りする。identity と coverage は gemmi
アラインメントから**必ずローカルに算出**し、検索側の値は provenance にのみ残す。

**OPM の URL は MoleculeKit 実装と同じ `https://storage.googleapis.com/opm-assets/pdb/{id}.pdb`
に統一。** キャッシュ判定に掛けていた 5000 バイト下限も除去した (小さい構造が永久に
再ダウンロードされる)。サイズ検査はダウンロード直後のみ。

### 記録しておく懸念

計画は転写を主経路に据えているが、私が独立参照 (PDBTM) で測った限りでは
**転写 13.7 度 > PPM3 6.8 度 > TMbed 7.8 度 > MEMEMBED 8.8 度** で、転写が最も悪い。
参照系どうしの不一致 5.9 度の中に他 3 手法は収まるが、転写だけ外側にある。
「OPM 標準への準拠」を目的とするなら転写は定義上正しい選択であり、その前提なら妥当。
物理的な正確さを目的とするなら、この順位は再検討の材料になる。

---

## 2026-08-18 — 訂正: 配向手法の精度比較は循環していた。PPM3 は同梱バイナリが壊れている

前エントリまでで「PPM3 が 1.0 度で最も正確」「MEMEMBED は大きな可溶性ドメインで裏返る」と
書いたが、**どちらもユーザの指摘と実測で否定された**。

### 訂正1: 精度比較の基準が循環していた

「何と比較して精度を求めているのか」と問われて気づいた。全測定を **OPM の 5l7d エントリを
正解として**行っていたが、**OPM のエントリは PPM が生成したもの**である。PPM を OPM に対して
測れば一致するのは当然で、1.0 度は精度ではなく自己一致にすぎない。さらに TMbed も論文 p.3 で
「OPM の ATOM 座標から inside/outside ラベルを割り当てた」とあり訓練ラベルが OPM 由来。

独立参照として **PDBTM (TMDET アルゴリズム、PPM とは別手法)** を取得して測り直した結果:

| 手法 | OPM 基準 (循環) | PDBTM 基準 (独立) |
|---|---|---|
| OPM/PPM そのもの | 0 (定義上) | **5.9 度** |
| PPM3 (パッチ後) | 1.0 度 | 6.8 度 |
| tm-segments (TMbed) | 6.4 度 | 7.8 度 |
| MEMEMBED | 5.5 度 | 8.8 度 |
| OPM 相同体から転写 (4JKV 同一蛋白) | 5.2 度 | 13.5 度 |

**参照系どうしが 5.9 度食い違っており、転写を除く全手法がその不確かさの中に収まる。**
つまりこの測定では PPM3 / TMbed / MEMEMBED の優劣を主張できない。主張できるのは
「OPM 相同体からの転写 (13.5 度) は明確に悪い」ことだけ。同一蛋白・重ね合わせ RMSD 0.60 A
でも 13.5 度ずれるのは、転写行列の誤差が上乗せされるため。カスケードの主経路にはしない。

### 訂正2: MEMEMBED は 5L7D で裏返らなかった

「SMO の大きな CRD が MEMEMBED の統計ポテンシャルを引っくり返す」と繰り返し書いたが、
結晶座標から素で走らせたら **正しい向き** (CRD が +56.4、H8 が -17.7、法線誤差 5.5 度)。
別メンバーの系が裏返ったと私が推測した根拠は報告値の Z 範囲だけで、実物は見ていない。
彼らが使ったのは MEMEMBED ではなく自前ビルドの PPM3 で、構造も AlphaFold モデルだった。
**「MEMEMBED は大きな可溶性ドメインで裏返る」は実証されていない。**

### MEMEMBED -f は不採用 (実測で悪化)

TMbed が非膜と判定した 336 残基を `-f` でスコアから除外したところ、法線誤差が
**5.5 度 → 25.7 度に悪化**。膜外残基はノイズではなく信号だった。`mempot[20][34]` は
「親水性残基が端のビンにいること」自体をスコアにしており、除くと膜貫通部 160 残基で
34 ビンを埋めることになり拘束が足りない。positive-inside rule も膜外の Arg/Lys 分布に依存する。
なお `-f` の挙動自体はソースで確認済み: `parse_pdb` が backbone 配列に加えないだけで、
出力 PDB からは消えない (3754 原子で不変)。

### PPM3 の同梱バイナリは壊れている

`immers` を叩くと解析は完走するのに、結果を出力する直前で Fortran ランタイムエラー。
`opm.f:485` の FORMAT 記述子にカンマが欠落している (`f7.0''+-''`)。旧い gfortran は許容、
現行は実行時に拒否。**出力 PDB が一切生成されない。** カンマ 1 個を足して再ビルドすると
完全に動く (膜厚 30.4 A も出力)。Dockerfile の openmm-builder ステージで gfortran を入れ、
パッチして再ビルドし `/opt/mdclaw/bin/immers` を差し替えるようにした。
別メンバーが「PPM3 成功、tilt 19.4 度、thickness 29 A」と報告していたのは
**まさにこのクラッシュする行が出す値**で、彼らのビルドは許容する gfortran だったのだろう。

### PPM3 に渡せるトポロジー情報は itopo (in/out) の 1 ビットのみ

`opm.f:84-123` の stdin 入力は 8 項目で全部 (inptype / keepligs / pdb / 膜の数 / 膜タイプ /
曲率 / itopo / 鎖リスト)。セグメントを渡す入力は存在しない。TMbed の `n_terminal_side` を
7 番目の itopo に渡す形で実装済み。

### PPM バックエンド追加の根拠

精度を根拠にはできなくなったので、残る論拠は機能差:
- **膜厚を推定する** (30.4 A)。MEMEMBED は `pdb.cpp:299-300` で ±17.5 A 固定
- **決定論的** (MEMEMBED は GA。ただし散らばりは未測定)
- 独立な第 3 の意見として食い違いを検出できる

### barrel の文字列マッチを廃止

`_infer_beta_barrel_from_context()` を関数ごと削除。study 文書やパスに "beta barrel" が
含まれるかを見る判定で、否定文を区別しないため「beta barrel は対象外」と書いただけで
barrel 扱いになっていた。判定は TMbed の H/B クラスのみに一本化。

### 未解決

- KcsA 単量体 (TM 2本) で法線誤差 29.1 度。四量体 (8本) なら 0.1 度。**セグメント数が
  少ないと壊れる**が、`MIN_SEGMENTS_FOR_AXIS = 1` は緩すぎる。ガードが要る。
  なお re-entrant loop が深さを壊すという仮説は否定された (中心面は KcsA/aquaporin とも
  +1.4 A で安定)。壊れるのは法線の方。
- TM 予測と配向を DAG ステージとして分離する件 (現状は embed 内で毎回 TMbed を実行)
- MEMEMBED の mempot が何から導出されたかは未確認 (OPM 由来なら MEMEMBED も循環に含まれる)

---

## 2026-08-18 — レビューを受けた膜配向の修正。「回転不変」という私の主張は誤りだった

pane の cursor agent (GPT-5.6) に db7d509 をレビューさせたところ、P1 が 3 件出た。
特に痛かったのは **power iteration の初期ベクトル固定**で、`_principal_axis` が
`v0 = [1,1,1]` から始めるため、真の第1固有ベクトルがそれと直交すると成分がゼロのまま
収束しない。レビュアーが実際の 20 残基ヘリックスを回転させて再現し **90.0 度ずれる**ことを
示した。私のランダム回転5回のテストが通っていたのは、厳密な直交が測度ゼロだから運が
良かっただけで、**「任意の開始フレームから同一」という docstring の主張は誤りだった**。
`numpy.linalg.eigh` に置換して 0.00 度。あわせて λ2/λ1 の縮退検査を入れ、方向の定まらない
点群 (球状、短すぎるセグメント) には `None` を返すようにした。5L7D の実測 6.4 度は不変。

**配向がパッキングの内部ステップだった**のも P1。`orient_fn` は
`embed_with_membrane_patch_tiles` のステップ1 からしか呼ばれておらず、
`--membrane-backend packmol-memgen` を選ぶと配向指定が全部無視され packmol-memgen 内部の
MEMEMBED が走っていた。ユーザから「配向の話になぜ patch が出てくるのか」と指摘され、
症状ではなく構造が問題だと整理できた。配向を `embed_in_membrane` の前段へ引き上げ、
両パッキング経路とも `preoriented` で配向済み構造を受け取る形にした。レビュアーの案の方が
私の当初案 (packmol-memgen 内部の MEMEMBED にフラグ注入) より良い。

**最大の設計ミスは別にあった。** ユーザに「膜トポロジーはどこで使っているのか」「TMbed は
どこで使っているのか」と繰り返し問われて判明したが、`embed_in_membrane` は TMbed を呼んで
おらず、`--membrane-topology-file` を渡し忘れると**黙って MEMEMBED 経路に落ちていた**。
膜系を作る以上トポロジーは必須の入力なのに、任意のオプションとして扱っていた。これは
「MDClaw が MEMEMBED に `-n` を渡していなかったから SMO が裏返った」のと同じ構図で、
必要な情報をコードが取りに行っていなかった。既定を `auto_predict_topology=True` に変え、
トポロジーが無ければ自分で TMbed を実行するようにした。予測不能なら従来どおり MEMEMBED に
落ちるが、**必ず warnings に理由を残す** (黙って落ちるのが問題だったため)。

P2 は 4 件。(1) topology consistency が生の z を膜中心と比較しており、周期境界を跨いだ残基が
反対側と判定されていた → 最小イメージ化。(2) 残基キーが resseq のみで chain を無視しており、
残基番号を共有するホモ多量体で両 protomer が平均されて整合率 0.5 になっていた →
(chain, resseq, icode) に。膜蛋白では多量体が普通なので実害が大きい。(3) TMbed の
subprocess に timeout が無く、存在しない model_dir は黙って HuggingFace ダウンロードへ
フォールバックしていた → timeout 1800s と `tmbed_model_dir_missing`。(4) 新パラメータが
`actual_conditions` に無く、条件を正しく記録した DAG ほど `condition_missing` で実行不能に
なっていた → 追加。トポロジーは可変な絶対パスではなく**内容の SHA-256** を記録する
(レビュアーの提案。パスは環境で変わるがハッシュなら同一性を検証できる)。

**レビュアーの数値に合わせなかった点が1つある。** PBC の再現例として提示された入力
(headgroup 20..80 に対し out=30 / in=70) は、膜中心 50 に対して out が下・in が上という
自己矛盾で、0.0 が返るのが正しい。物理的に意味のある「残基が周期境界を跨ぐ」ケースで
検証し直し、3 パターンとも 1.0 になることを確認した。

**beta barrel の扱いも確定。** テンソル和 Σaaᵀ がレビュアー環境では barrel を 0.0-2.2 度に
改善したが、私の手元では 2OMF 17.3 度 (符号合わせ平均 14.5 度より悪化) / 4K3B 11.7 度
(同 31.1 度より改善) と一貫しなかった。セグメントを全部そろえられるかに強く依存すると
見ている。決着まで barrel は MEMEMBED `-b` に回す現状維持。判定は TMbed の H/B クラスで、
7AHL (strand 2本) / 1UUN MspA (3本) / 4K3B BamA (16本) を含む実バレル5件すべてが拒否され、
SMO (helix 7本) のみ通ることを確認済み。論文が「取り逃すのは 2-4 ストランドのもの」と
書いていたので穴だと推測したが、実測で否定された。

実系確認: `--membrane-topology-file` を渡さず1コマンドで solv_006 を構築し、
orientation_method=tm-segments、membrane_center_z=0.0、geometry passed、
topology_consistency 10/10。回帰テスト 33 本、558 passed。

**未着手**: PPM バックエンド (`/opt/mdclaw/bin/immers` は SIF に既存)、MEMEMBED `-f` への
非膜残基受け渡し、study 文書の文字列マッチ由来 barrel フラグより TMbed 判定を優先する件。
また packmol-memgen 経路はユニットテストと構造変更で確認しただけで、フルボックス packing を
実走させていない。

---

## 2026-08-18 — 訂正: DB 由来ベンチ設計を MDDB 単独に変更、逐次ゲートと σ_FF 加算式を撤回

**同日の前エントリ「公開 MD DB (GPCRmd / MDDB) 由来ベンチの実測と検証層の設計」を訂正する。**
実測値そのものは概ね維持されるが、**供給源の選択と検証設計の中核 3 点が誤っていた**。
改訂版は `docs/research/db_derived_benchmark_validation.md` (rev.2)。

**方針変更 (ユーザ判断).** GPCRmd は RIKEN でのライセンス上の扱いが難しいため供給源から外した。**MDDB 単独**にする。

**独立レビューで判明した設計上の誤り 4 点** (cursor advisor pane, Opus 4.8, 読み取り専用で実施):

1. **`observable_fidelity` を「唯一の新規軸」としたのは誤り。** 軌道から観測量を再計算して
   自己申告値と突き合わせる primitive は既存: `MDPrepBench/mdprepbench/scoring.py:882-947` と
   `:1076-1124` (`_check_observable_recompute_consistency`)、
   `MDStudyBench/mdstudybench/scoring.py:1033-1075` (`direction_grounding`) と
   `:1078-1126` (`observable_recompute_consistency`)。新規なのは
   **DB の固定参照軌道をエージェント入力にする task mode と DB provenance 付き check contract** だけ。
2. **「軸 k は k-1 が通ったときのみ評価」という逐次ゲートは自己矛盾。** 物理妥当性に落ちた提出でも
   組成・自己申告値・主張整合性の診断は独立に可能で、それを捨てるのは掲げた目的 (原因帰属) を捨てること。
   **全軸を独立に評価し、`passed` / `failed` / `not_evaluable` / `not_attempted` を区別し、
   最終合否だけを非補償ゲートにする**に変更。
3. **`δ = k·sqrt(σ_rep² + σ_FF²)` を撤回。** この式は力場差が平均ゼロのランダム変動で、
   単一 σ_FF が系・観測量をまたいで転用可能であることを仮定する。実際は系依存の系統バイアスなので
   単一分散に畳めない。同様に「Δ なら力場オフセットが相殺される」も一般には成立しない
   (相殺は bias が両条件で同じ場合のみ)。
4. **旧 L4 を 2 軸に分割。** `observable_recompute` (selection/alignment/PBC/実装版の問題) と
   `ensemble_reproduction` (sampling/力場/初期条件/protocol の問題) は失敗原因が異なる。
   後者は matched-protocol / diagnostic-only / calibrated の 3 モードに分け、
   matched-protocol なら σ_FF は不要 (「σ_FF が測れなければ絶対値タスクを一切作らない」は強すぎた)。

**新規に見つかった scorer バグ.** `DeterministicCheck.capability` の明示 override
(`MDPrepBench/mdprepbench/models.py:291-306`) が capability profile 集計で無視される。
`CheckResult` (`models.py:1079-1085`) が capability を保持せず、`scoring.py:3662-3685` が常に
`DEFAULT_CHECK_CAPABILITY` を引くため。現行 P01-P40 は override 未使用なので今の得点には影響しないが、
自動生成タスクが capability を明示し始めると公開契約と実際の集計が食い違う。**タスク量産前に修正が必要。**

**MDDB 単独 + CC-BY 限定にした結果の実測 (定義つき).**

- ライセンス: CC-BY 4.0 が 4511、CC0 19、**CC 系でないものが 24** (AFL 3.0 が 9、Apache 2.0 が 5、
  MIT 4、LGPL 2、記載なし 4)。タスク生成はこの 24 件を除外する。
- **膜系の軸は実質失われた。** 実バイアレイヤ (`LIPIRES>=100`) は 30 件だが **20 件が非 CC**
  (CLC / Nav 5WEO / TARP / HCN / CTL1、および唯一の GPCR `OTRMG` `OTRMGb` も非 CC)。
  CC-BY の膜系は 10 件で全て SARS-CoV-2 のウイルス膜。
  **P18 膜系が全モデル失敗する既知の弱点を DB 由来タスクで補強する道は閉じた。** 膜系は手書きで扱う。
- **力場感度の測定源は MDDB 内に存在する。** 同一 PDB が複数力場で登録された群が **11、全て CC-BY**。
  `6VXX` が 6 力場、`6M0J` が 5、`1FZX` / `1ICK` / `1SK5` / `3GGI` が 4
  (OL15 / OL21 / ParmBSC1 / Tumuc1、各 2 entry) で、核酸 4 系は力場比較目的の study に見える。
  ただし同一 PDB でもリガンドパラメータ・プロトネーション・欠損ループ・イオン強度・ensemble・
  engine・軌道長・初期構造が交絡しうるため、**matched を確認するまで力場感度に帰属しない**。

**計数の定義の問題.** 前エントリの「脂質を含む 43 件」は定義なしで誤読を招く。
`LIPIRES>0` は 43、`LIPIRES>=100` は 30、`MEMBRANES` 非空は 10 で、どれを指すかで意味が変わる。
また `totalFrames` 296128391 は summary エンドポイントの集計値で、project 一覧の総和 287267536 とは
別の量である (3% 差)。**以後、計数は必ず定義とともに記す。**

**次の 4 手 (いずれも MD 不要).** (1) 核酸 4 系 16 entry の matched-protocol 検証、
(2) 解析契約レジストリの最小版 (観測量 1 つで MDDB 前計算値と自前再計算値のずれを測る)、
(3) `observable_recompute` タスク 10 本、(4) 上記 scorer バグの修正と回帰テスト。

---

## 2026-08-18 — 公開 MD DB (GPCRmd / MDDB) 由来ベンチの実測と検証層の設計

MDPrepBench / MDStudyBench を公開 MD データベースから自動生成できるかの調査。
設計は `docs/research/db_derived_benchmark_validation.md` に分離。ここには実測値と判断だけ残す。

**実測 (API / 公開ページを直接叩いた).**

- MDDB (`https://mmb.mddbr.eu/api/rest/v1/`, 無認証): 4554 projects / 14138 MD /
  296M frames / 33.6 TB。`LICENSE` は 4511 件が CC-BY 4.0。条件ベクトル
  (`FF` `TEMP` `WAT` `ENSEMBLE` `TIMESTEP` `LENGTH` `SOL` `NA` `CL` `MEMBRANES` `PDBIDS`) が機械可読。
  前計算解析が約 4500 系 × 10 種 (`rmsds` 4551 / `fluctuation` 4551 / `rgyr` 4551 / `sasa` 4552 /
  `pca` 4552 / `tmscores` 3285 / `hbonds` 2318 / `interactions` 2398、膜系は `apl` `thickness`
  `lipid-order` `mem-map`) で JSON 時系列としてそのまま取得できる。`mdcount>=2` が 1328 project
  (10 replicas が 605、6 が 271、8 が 160、9 が 152)。
- **MDDB に GPCR はほぼ無い。** 全 4554 中で脂質を含むのは 43 件のみ、うち GPCR は
  `OTRMG` / `OTRMGb` (ヒトオキシトシン受容体, 7RYC, Amber ff14SB, 3 replicas, LIPIRES=256) の 1 系だけ。
  残りは CLC (8-9 replicas)、Nav (5WEO)、TARP γ2/γ7、HCN、CTL1、SARS-CoV-2 spike/膜、
  および LIPIRES=1 の界面活性剤単分子系。膜系タスクで MDDB は GPCRmd の代替にならない。
- GPCRmd: API とファイル DL はログイン必須 (DL は 1 リクエスト 5 dynamics 上限) だが、
  **`/dynadb/dynamics/id/<id>/` の report ページは無認証で完全な条件表を返す**。
  ID 36 実測: 3REY.A / Inactive / TIP3P / POPC / Cl 191 mM, Na 159 mM /
  Water 22376, POPC 207, Cl 77, Na 64 / 100039 atoms / CHARMM36m / 4.0 fs / Replicates 3 / 1.5 µs。
  `/dynadb/datasets/` は無認証で 773 の view ID を Complex / Apoform ペアとして公開。
- **GPCRmd は CHARMM 一様ではない。** 実在 24 ID をサンプルして 12 件パースできたうち、
  1 件が ff19SB/lipid21/GAFF2 + AMBER PMEMD.CUDA (ID 2322)。CHARMM も 36 / 36m Feb2016 /
  May2015 / c36 Jul2021 と版が割れ、エンジンは ACEMD / ACEMD3 / GROMACS 2021.3 / PMEMD、
  膜は POPC 単一が 6 件で残り 4 件が混合 (DOPC/DPPC/DSPC/SDPC、POPC+CHL1、POPG+CO1+POPC)。
  Nature Methods 2020 のコアが一様なだけで、以後のコミュニティ投稿は多様化している。
- 24 ID 中 12 は report ページを取得できず (500 / no report)。773 は上限であって使える N ではない。
  8/24-8/28 は GPCRmd メンテナンス予定。

**既存ハーネスの状態 (grep で確認).**

- MDPrepBench: `check_type` は 24 種すべて**バージョン無し**。集計は重み付き平均 (補償的) +
  `_HARD_FAIL_CHECK_TYPES` クランプ。軸は `identity` / `physical_validity` / `fidelity` / `provenance`。
- MDStudyBench: `region_water_occupancy@1` 形式で**バージョン有り**。
  `grounded_correct = valid_execution AND claim_supported AND truth_agreement` の非補償 AND。
- **どちらにも solve 時のネットワーク遮断が無い** (`run.py` に該当制御なし)。
  参照 DB を使うベンチではこれが最大の穴で、エージェントが参照そのものを取得できると全層が同時に無効化される。

**判断.**

- 検証は 7 層 (`identity` / `physical_validity` / `composition_fidelity` / `execution_validity` /
  `observable_fidelity` / `claim_support` / `truth_agreement`) に分け、層をまたぐ補償はしない。
  外部 DB が要るのは 3 層だけで、残り 4 層は DB なしで先に固められる。
- 新規語彙は `observable_fidelity` の 1 つだけ。参照軌道を固定入力として渡す層で、**MD を走らせない**ので CI に載る。
- 力場一致は要件にしない。組成照合・参照軌道の再解析・ペア差分のいずれも参照の力場に依存しないため。
  「GPCRmd が CHARMM だから使いにくい」が効くのは絶対値を参照に合わせにいく設計だけで、それは採らない。
- 自前 MD と参照を絶対値で比べる層 (L4b) は σ_FF が測れる場合のみ作る。
  測定源は「GPCRmd 内で同一 PDB が CHARMM コアと Amber 投稿の両方に現れるペア」。無ければ作らない。

**次の 3 手 (いずれも MD 不要).** (1) MDDB の 1328 project から観測量ごとの σ_rep を算出、
(2) GPCRmd 773 ID をクロールして同一 PDB の力場違いペアを探索し L4b の可否を決める、
(3) `observable_fidelity` タスクを 10 本作る。

---

## 2026-08-18 — TMbed を導入し、膜配向を「探索」から「予測に従う」に変えた

別メンバーの SMO 膜構築が壊れた件を調べた結果、MEMEMBED / PPM が構造だけから
上下の向きを推定しており、大きな可溶性ドメインがそれを狂わせることが分かった。
TMbed (Bernhofer & Rost 2022, ProtT5 埋め込み + CNN + Viterbi) は配列だけから
TM セグメントと inside/outside を返し、Viterbi の文法が「TM を横切るたびに内外が
反転する」ことを強制するのでトポロジーが構造的に整合する。膜外の質量がいくらあっても
影響を受けない — まさに構造ベース手法が間違える所を埋める。

**OPM/PPM の 5L7D 正解に対する実測** (これが設計の根拠):

| | 膜法線の誤差 | 中心面ずれ |
|---|---|---|
| TMセグメントのヘリックス軸を平均 (完璧な境界) | **6.2°** | +0.4 A |
| 境界を ±5 残基ゆらす (TMbed の実精度) | 6.5 ± 1.0° (最悪 9.5°) | +0.5 ± 1.3 A |
| TM を1本まるごと見落とし | 最悪 11.3° | <= 0.7 A |
| 全TM残基をまとめて PCA | **22.0°** ← これはダメ |

要点は「ヘリックスごとに軸を出して平均する」こと。個々の傾斜は 10-37° とばらつき、
束全体の形は法線ではない。境界誤差には極めて頑健で、乱数を使う GA と違い決定論的。

**実データでの end-to-end**: TMbed を実際に 5L7D に走らせると n_terminal_side=out
(SMO の CRD は細胞外、正解) と 7 本の TM ヘリックスを返し、OPM 由来の正解境界と
±3 残基以内で一致した。その予測だけで配向すると OPM 正解を **z の RMS 1.26 A、
相関 +0.9995** で再現。ランダム剛体変換を掛けても結果は完全に同一 (回転不変)。
3セグメント分割構造 (A/B/C 鎖) でも鎖ごとに正しく処理した。

**実装** (4点):

1. `embed_in_membrane` に `--n-terminal-side in|out` を追加し MEMEMBED `-n` へ渡す。
   従来 MDClaw は `-n` を渡しておらず、**上下の向きを指定する手段が無かった**。
   あわせて `-s` を渡すようにし既定を 3 (GA 5回) に。MEMEMBED 自身の既定は 0 (GA 1回)
   で、packmol-memgen より探索が浅い状態だった。
2. 新サーバ `mdclaw/membrane_topology/` に `predict_membrane_topology`。構造 / 配列 /
   FASTA を受け、`membrane_topology.json` (segments, 側つき regions, n_terminal_side)
   を書く。構造入力なら author 残基番号をそのまま持ち回るので下流がそのまま使える。
3. `mdclaw/solvation/tm_orient.py` に決定論的配向。`--orientation-method`
   (auto/memembed/tm-segments) で選択、auto はトポロジーがあれば tm-segments。
4. geometry check にトポロジー整合性を追加。**上限としての overlap_fraction は使えない**
   ことが分かった: OPM 正解でも TMD のみ構築は 0.798、全長は 0.582 で、構築の性質に
   依存するため固定閾値に意味が無い。代わりに「非TM領域が予測された側にあるか」を見る。
   OPM 正解で 8/8、上下反転させると 0/8 と綺麗に分離する。閾値 0.75。

**モデルは SIF に焼き込む** (ユーザ指定)。TMbed の CNN 重みはパッケージ同梱だが
ProtT5 (2.3 GB) は初回実行時に HuggingFace から落ちる設計で、読み取り専用 SIF と
外部ネットワークの無い計算ノードでは失敗する。Dockerfile で `tmbed download
--model-dir $MDCLAW_TMBED_MODEL_DIR` を実行し、無ければビルドを失敗させる。
SIF は 4.6 GB → 約 6.9 GB になる見込み。

実系での統合確認: d473y の prep_004 から solv_004 を作り
`--membrane-topology-file` 付きで構築 → `orientation_method=tm-segments` が自動選択、
`n_terminal_side=out` を継承、geometry check が `topology_consistency 10/10 = 1.00`。
overlap_fraction 0.792 は MEMEMBED 構築の 0.798 とほぼ一致し、両者の配向が
一致していることも確認できた。

回帰テスト 17 本を `tests/test_membrane_topology.py` に追加。CLI contract golden を再生成。

**追記 (同日、レビュー中に判明した2件)**

(a) **beta barrel には効かない。** 「セグメント種別を区別していないが大丈夫か」と自問して
測ったところ、OPM 正解に対し 1QJP OmpA (8ストランド) は 2.0 度だが **2OMF OmpF
(16ストランド) は 14.5 度**。短いセグメントを混ぜると 41.7 度に悪化する (ここから
`MIN_SEGMENT_CA_ATOMS = 8` が実際に効いていることも確認できた)。バレルのストランドは
法線から ~40 度傾いて樽の周りを回るので、軸平均は弱い推定量になる。端残基の重心差という
別案も試したが改善しない (OmpA 10.0 度 / OmpF 15.5 度)。MEMEMBED には専用の `-b` モードが
あるので、そちらに回すことにした。

「そもそも barrel をどう認識するのか」は **TMbed が答える**。TMbed の出力クラスは H (ヘリックス)
と B (ストランド) が別で、論文 Table 1 で β-TMP recall 93.8 ± 7.5% / FPR 0.1 ± 0.1%、
Table 3 で TMB セグメント recall 95.0% / precision 99.2% と報告されている。

論文が「取り逃したバレルは全て 2-4 ストランドのもの」と書いていたので、そこが穴だと
推測したが **実測で否定された**: 7AHL α-hemolysin (protomer 2ストランド) → strand 2本、
1UUN MspA (論文が名指しした例) → strand 3本、4K3B BamA → strand 16本。いずれも
is_transmembrane=True で正しく分類。実バレル 5 件すべてが
`tm_orientation_beta_barrel_unsupported` で拒否され、SMO (helix 7本) のみ通ることを確認。
推測で限界を書かず測ったのが正解だった。

(b) **ビルドが TMbed ステップで失敗した (設計どおりのガード動作)。** 原因は `protobuf` 不足。
transformers が ProtT5 の SentencePiece トークナイザを展開するのに必要だが、TMbed が
依存として宣言していない。`SentencePieceExtractor requires the protobuf library` で落ちる。
`environment.yml` に追加。ローカル検証時は明示的に入れていたので露見していなかった。

**未対応**: SIF の再ビルドは進行中。それまで `predict_membrane_topology` は
`tmbed_unavailable` を返す (構造化エラーとして扱われる)。

---

## 2026-08-18 — 同一実行の失敗証跡が `observations/` に流れ、`trace_failure` が原因を言えなくなっていた

HPacker の cap バグを追う過程で気づいた別件。`trace_failure` が
`failure_code: null` を返し、`tool` / `argv` / `exit_code` もすべて null だった。
`skills/common/tool-output.md` は「安定した `code` で分岐せよ、`errors` を parse するな」と
定めているので、規約が想定する分岐材料が存在しない状態だった。実際これで誤誘導された。

**証跡は完全に採取されていた。置き場所だけが間違っていた。** 失敗は二段階で記録される:

1. ツールが `fail_node(...)` を呼び、貧弱な記録を `artifacts/failure/latest/` に書いて封印。
   (`fail_node` の呼び出しは repo 全体で 108 件あり、`code=` を渡しているものは 0 件。)
2. 直後に CLI の `_record_cli_node_failure` が `tool` / `argv` / `exit_code` /
   stdout・stderr tail を揃えた完全な記録で `record_node_failure` を呼ぶ。
3. ところが `node/failure.py` は「ノードが既に terminal なら後追い観測」と判定するため、
   **同一実行の記録が** `artifacts/failure/observations/<時刻>/` へ降格される。
4. `trace_failure` は `node.artifacts.failure` が指す `latest/` しか読まない。

実測 (`jobs/d473y/nodes/solv_001`): `latest/` は 04:35:25.660 に 599 B / 4 キー / `code: null`、
`observations/` は **32 ms 後**の 04:35:25.692 に 2870 B / 12 キー / code 完備。同一実行である。

修正は `record_node_failure` に `same_invocation: bool = False` を足し、CLI 側から
`same_invocation=True` を渡すだけ (差分 ~20 行)。true かつ既に `failed` なら observation 扱いを
やめて `latest/` を差し替える。**封印済み `node.json` は書き換えない** —
`tests/test_node.py` の不変条件であり、`trace_failure` は既に
`metadata.failure_code` → `manifest.code` → `tool_result.code` とフォールバックするので不要。
イベントも `terminal_node_failure_observed` と `node_failure_evidence_enriched` で区別する。

実系で確認: 壊れた `prep_002` から `solv_003` を作って同じ失敗を再現したところ、
`observations/` は生成されず `latest/` に code=`membrane_neutralization_failed`、
tool=`embed_in_membrane`、argv、exit_code=1、stdout/stderr tail が揃った。
`trace_failure` の `failure_code` も埋まり、`tool_result` は 4 キーから 17 キーになり
`hints` / `next_action` / `recoverable` も届くようになった。

**やらなかったこと。** 当初は `fail_node` 108 箇所すべてに `code=` を足す移行 (うち 96 件は
機械的置換) を計画したが、オーバーエンジニアリングと判断して撤回した。スキルが使う唯一の経路は
CLI であり、それは上記で完全に直る。副作用として `node.json` の `metadata.failure_code` は
空のままなので `inspect_job` の `failed_nodes` には `failure_code` が出ない。これは今回報告された
症状ではないので、必要になった時点で別途扱う。`membrane_neutralization_failed` の hint 文言
(「bulk water を増やせ」) が原因と無関係な件も、実益が薄いと判断して保留のまま。

回帰テスト 1 本を `tests/test_node.py` に追加。修正前のコードで失敗することを `git stash` で確認済み。
`ruff` clean、383 passed。

---

## 2026-08-18 — 訂正: cap 破壊の犯人は `prepare_complex` ではなく `create_mutated_structure`

**同日の前エントリ「SMO 5L7D D473Y / G497W membrane MD; `--cap-termini` produces
unparameterizable structures」の原因帰属を全面的に訂正する。** バグは実在するが、
`prepare_complex --cap-termini` ではなく **HPacker 経路 (`create_mutated_structure`)**
にあった。前エントリは変異後の `mutated.pdb` だけを見て prep の出力と取り違えていた。

中間ファイルを追った実測:

| ファイル | cap の位置 | 変異残基の H2/H3 | OpenMM |
|---|---|---|---|
| `prep_001/artifacts/merge/merged.pdb` (prepare_complex) | 正順 (idx 0,158,159,238,239,348) | なし | **OK 5470 particles** |
| `prep_002/artifacts/mutated.pdb` (create_mutated_structure) | 全て末尾 | SER190 に H2/H3 | **FAIL** |

`prepare_complex --cap-termini` は正常。`clean_protein.py:474-501` が PDBFixer の
`missingResidues` 経由で cap を入れており、配列位置も水素も正しい。

真の原因は `mdclaw/sidechain_packer.py` の 3 点で、すべて「ACE/NME を遊離ヘテロ原子と
みなす」ことに由来する:

1. `_write_protein_input` が標準アミノ酸の ATOM 行だけを HPacker に渡す (これ自体は正しい)。
2. `_rebuild_protein_hydrogens` がその **cap を欠いた** 構造に PDBFixer
   `addMissingHydrogens` をかける。PDBFixer は鎖トポロジから protonation を決めるので、
   cap の裏に隠れた残基が遊離荷電末端と判定され H2/H3 が付く。
3. `_split_hpacker_and_nonprotein_lines` が元の HETATM 行 (= cap) を全タンパク原子の
   **後ろに再追加** する。OpenMM は鎖内の残基「並び順」で結合を張るため cap が繋がらない。

修正 (`mdclaw/sidechain_packer.py`):

- `_is_terminal_cap_atom` / `_is_protein_or_cap_atom` / `_line_residue_key` /
  `_protein_and_cap_residues_from_lines` を追加し、cap を「タンパク側」として扱えるようにした。
- `_merge_caps_into_protein` を新設。HPacker 出力に cap を **元ファイルの並び順で**
  差し戻してから水素再構築に渡す。これで (2) と (3) が同時に消える。
- `_sort_protein_atoms_like_reference` と `_split_hpacker_and_nonprotein_lines` を cap 対応に。
- 副次的に見つかった潜在バグも修正: CONECT 行が入力の通し番号のままコピーされ、
  タンパク原子は水素再構築後の別番号で再出力されるため、`_normalize_pdb_lines` の
  serial マップが衝突して **CONECT が無関係な原子を指していた**。`_remap_conect_lines` を
  新設し、(chain, resseq, icode, atom name) の同一性で写像、解決できない行は捨てる。
  これを直すまで LEU346 が NME の水素と結合し `1 H atom too many` で落ちていた。
- guardrail code `hpacker_terminal_cap_merge_failed` を追加・golden 再生成。

検証: 実際の `merged.pdb` に `run_hpacker_mutation(mutations=['D473Y'])` を適用して
success、cap は idx 0,158,159,238,239,348 の正位置、SER190 の水素は H/HA/HB2/HB3/HG のみ、
CONECT 48 本を復元、**OpenMM 5479 particles で成功**。手作業で直した参照と一致。

回帰テスト 4 本を `tests/test_sidechain_packer.py` に追加 (cap の並び順、capped 末端の
非プロトン化、CONECT の同一性写像、解決不能 CONECT の破棄)。**4 本とも修正前のコードで
失敗することを `git stash` で確認済み**。`ruff` clean、主要スイート 222 passed。

なお `membrane_neutralization_failed` の hint (「bulk water を増やして再構築せよ」) が
実際の原因と無関係な点は前エントリの指摘どおりで、これは未修正のまま残っている。

---

## 2026-08-18 — SMO 5L7D D473Y / G497W membrane MD; `--cap-termini` produces unparameterizable structures

Ran the two vismodegib-resistance mutants of human Smoothened (5L7D, X-ray 3.2 A,
Byrne et al.) in a POPC bilayer, as a pipeline test at 100 ps production.
Study: `studies/smo_5l7d_vismodegib_resistance`, jobs `d473y` and `g497w`.

**Construct decisions.** Chain A, not B — chain B has a 492-506 gap that deletes
G497 outright. TMD only (residues 190-553): the BRIL fusion (numbered 1011-1131)
and the extracellular CRD are dropped, and with the CRD goes the only
crystallographic cholesterol, which binds the CRD (contacts 108-164) and sits
>40 A from the TM6/TM7 pocket — it is not a pocket ligand. Two unresolved gaps
(347-350, and 429-445 = the ICL3 that BRIL replaced) were kept as chain breaks by
splitting into three segments A 190-346 / B 351-428 / C 446-553 rather than
building a de-novo 17-residue ICL3. All four in-range disulfides retained,
including the inter-segment C314-C390.

**The run exposed a real bug: `prepare_complex --cap-termini` is broken.** Two
independent defects, both present at once. (1) ACE/NME are written as `HETATM`
records appended after every `ATOM` record, so a cap lands out of sequence
position in its chain — `ACE A 189` and `NME A 347` both end up after `LEU A 346`.
OpenMM links residues by order within a chain, so the cap never bonds. (2) The
residue following an ACE keeps its charged-N-terminus `H2`/`H3` on top of the new
bond to the cap. Fixing only (1) moves the error from *"missing 1 C atom"* to
*"matches NSER, but has 1 N atom too many"*; fixing both makes the same structure
build cleanly (5479 particles, verified).

This is invisible on the standard explicit-water path because `build_amber_system`
runs tleap, which sorts by residue number and rebuilds hydrogens. It surfaces in
`embed_in_membrane`, whose net-charge evaluation calls
`SystemGenerator.create_system` directly on the assembled PDB. The reported code
is `membrane_neutralization_failed` with the hint *"rebuild with enough bulk
water"* — **misleading**: bulk water was never the problem, upstream capping was.
Worth making that guardrail name the real cause.

Worked around by re-running prep with `--no-cap-termini`; the six termini are all
solvent-exposed at the membrane surfaces and ~25 A from the pocket, so charged
termini are an acceptable artifact at test scale. Should be fixed properly before
any production-quality run. Failed nodes (`prep_001/002`, `solv_001`) kept in the
DAG.

**Numbers.** 115,168 (D473Y) / 115,195 (G497W) atoms; 358/359 POPC; ~15.3k OPC
waters; 116 x 116 x 77 A box. Minimization 8.6e8 -> **-1,420,019 kJ/mol** and
5.8e13 -> **-1,425,417 kJ/mol**, max force 3.7e9 -> ~3.8e3 kJ/mol/nm — i.e. the
P18 membrane-min stall did *not* recur; the `patch-tile` backend tiles a
pre-equilibrated patch instead of packing the whole box, and that is what avoids
the lipid-tail clash trap. Equilibration 0.2 ns NVT + 1.0 ns NPT reached
301.0 +/- 0.9 K and 1.030-1.033 g/mL, compressing the under-dense tiled box from
0.906 g/mL (1083 -> 953 nm^3). Production 100 ps, 4 fs + HMR,
`MonteCarloMembraneBarostat` (XYIsotropic, ZFree, gamma=0): 300.9 +/- 0.8 K,
1.0376 +/- 0.0012 g/mL, CA-RMSD 0.71 A (D473Y) and 0.66 A (G497W) vs frame 0,
gross APL 66.7 A^2 before subtracting protein cross-section.

Verified the two things that fail silently: all 4 S-S bonds are present in
`system.xml` with original numbering (193-213, 217-295, 314-390, 490-507), and
the mutations survive tleap renumbering — topology index 262 is TYR in `d473y`
and ASP in `g497w`, index 286 is GLY and TRP respectively.

100 ps is a pipeline test, not sampling. No WT control was requested, so nothing
here supports a claim about either mutation's effect.

---

## 2026-08-18 — `eq` could silently skip `min`; found by running the onboarding guide

Wrote a RIKYU onboarding guide for the hackathon and ran it end to end as a new
member would — fresh clone under `/data1/rkp00048/$USER`, shared arm64 SIF, 4AKE
chain A, apo, 100 ps NVT + 100 ps NPT + 100 ps production on SLURM. It works:
`min` 18 s, `eq` 45 s, `prod` 38 s, **1 min 41 s of GPU time**, 300.34 +/- 0.90 K
and 1.018 +/- 0.001 g/mL. 4AKE is the open form, so at a 15 A buffer it solvates
to ~90,000 atoms — nearly twice 1AKE's 49,671.

**The run exposed a real bug.** Submitting `min` -> `eq` -> `prod` as
dependency-chained SLURM jobs means creating all three nodes before any of them
runs. `_auto_resolve_parent` walks `_AUTO_PARENT_PREFERENCE["eq"] = ("min",
"topo")` and falls through to the next entry whenever the preferred one has no
*completed* node. With `min_001` still pending, `eq` silently attached to
`topo_001` — equilibrating from the topology-time state and skipping
minimization entirely. `explain_node` reported `ready_to_run: true` with no
warnings, because `topo` is a legitimate `eq` parent for legacy DAGs.

The fix distinguishes *absent* from *not yet complete*: a less-preferred parent
type is now only reached when the preferred type has no nodes in the job at all.
Present-but-incomplete (or failed) returns `None`, so `create_node` demands an
explicit `--parent-node-ids` — the same structured `node_context_required` that
`prod` already gave in this situation. `_auto_parent_candidates` stops at the
same place so the error never advertises a `topo` candidate while a `min`
exists. Legacy `topo -> eq` DAGs with no `min` node are untouched.

This also covers `topo`, whose preference is `("solv", "prep")`: a pending
`solv` no longer falls through to `prep` and builds an unsolvated topology.

Verified in the live workflow, not just unit tests — re-running the guide, the
bare `create_node --node-type eq` now fails with `node_context_required` instead
of quietly mis-parenting, and the corrected chain gives a DAG with 7 completed
nodes, 0 failed, 0 orphaned.

---

## 2026-08-18 — Merged arm64 image verified on Rikyu; the shim contract holds

Pulled the merge onto Rikyu (`c000`, GB200, driver 580.173.02) and checked the
parts the merging host could not. The entry below flagged the
`MDCLAW_FUSEFIX_LIB` indirection as unverified because that host had no arm64
builder; it verifies clean.

**The build-time assertions pass.** The published SIF predates
`MDCLAW_FUSEFIX_LIB`, so it was injected to reproduce what a rebuilt image will
see. Both `RUN` assertions in `Dockerfile.rikyu-arm64` — the devel-stage
`LD_PRELOAD` check and the final-stage one that reads the variable — succeed.
The sanitized `container/mdclaw_fusefix.c` also compiles under the stricter
flags the Dockerfile now uses (`-Wall -Wextra -Werror -Wl,-z,relro,-z,now`,
gcc 13.3 aarch64), and the freshly compiled shim still fixes `torch.fft` on the
GPU. The rewrite that dropped the site-specific comments changed no behavior.

**The `check_declared` gate is presence-based in all three states**, tested on
the same SIF: undeclared → `SKIP` (20 passed), declared and correct → `PASS`
(21 passed), declared with a bad path → `FAIL` (20 passed / 1 failed). It cannot
silently pass.

`test-rikyu-gpu.sh` from the SIF: `ARM64_CUDA13_GPU_SMOKE=PASS`, including
`openmm_pme_cufft=PASS` and `pytorch_cufft=PASS` — the two checks the old script
lacked, which is why two broken images passed it 5/5 before the merge.
`test-container.sh`, 304 unit tests, and `ruff check mdclaw/ tests/` are clean.

**Fixed here: the SLURM GPU directive.** `_generate_sbatch_script` emitted
`#SBATCH --gpus-per-node=N`, which Rikyu's job-submit plugin rejects outright
(`[AI4S] Specify GPUs with --gpus=N (-G N). Per-node forms ... are not
supported`), so every `submit_job` with a GPU failed at submission. This is site
policy, not a bug — both spellings are valid Slurm — but the per-node form is
unusable on Rikyu, so both script generators now emit `--gpus=N`. The two forms
are equivalent at the default `--nodes=1` and differ beyond it: `--gpus-per-node`
is per node, `--gpus` is the job total, so `--nodes 4 --gpus 2` meant 8 GPUs
before and 2 now. Nothing in-tree submits multi-node GPU jobs (`nodes` defaults
to 1, and `skills/hpc-run` has no multi-node example), so `--gpus` now means the
job total everywhere. Keeping both spellings was rejected because it would leave
Rikyu with no multi-node GPU path at all. Validated end to end on Rikyu with
the fix in place: 1AKE chain A, apo, ff19SB/OPC, 49,671 atoms, submitted as
`min` -> `eq` -> `prod` with `afterok` dependencies (`--gpus=1`, 1x GB200).
All three `COMPLETED`; 100 ps NPT production held 300.6 +/- 1.2 K and
1.028 +/- 0.002 g/mL.

**Housekeeping:** `.gitignore` no longer excludes `RIKYU.md`, which says on its
first line not to commit it. `RIKYU-SIF-REBUILD.md` is superseded by
`docs/developer/container.md` plus the entry below.

---

## 2026-08-18 — arm64 MODELLER verified under emulation; glib was the missing piece

Registered `qemu-aarch64` binfmt on floyd (`docker run --privileged
tonistiigi/binfmt --install arm64`; host-wide, reversible with `--uninstall`)
and built a probe image that applies **only** the MODELLER block of
`Dockerfile.rikyu-arm64`, copied verbatim out of that file by the generator so
the probe cannot drift from the real build. Full emulated build of the rikyu
image was not attempted — measured qemu overhead is ~8.7x and the MODELLER step
was the only unverified part.

The tarball route works on real aarch64: `uname -m` = `aarch64`, the installed
`libmodeller.so.14` is ELF machine 183 (AArch64), `config.py` keeps the `XXXX`
placeholder, and with `KEY_MODELLER10v8` injected at run time `import modeller`
succeeds.

**Found by doing this, and only findable on arm64: `libglib-2.0.so.0` is
missing from the tarball.** `armv8-gnu/` bundles gfortran and hdf5 but not glib,
while the *conda* package does bundle it — which is why the x86 dry run passed.
`ldd` on `_modeller.so` shows glib as the one unresolved library. The real image
is fine because the conda environment at `/opt/mdclaw/lib` provides
`libglib-2.0.so.0` (confirmed in the new amd64 SIF) and the rikyu
`LD_LIBRARY_PATH` already puts that directory first — but that is an implicit
transitive dependency holding up a hard requirement, so two guards were added:

- `Dockerfile.rikyu-arm64` now runs `ldd` on `_modeller.so` after the install
  and fails the build on any `not found`.
- The shared smoke check no longer stops at parsing `config.py`; it does
  `import _modeller`, which loads the compiled object. That needs no licence —
  the licence check lives in `modeller/__init__.py`, after the extension
  imports — so it works on an unlicensed image. Verified in all three states:
  broken probe -> `AssertionError: MODELLER extension will not load:
  libglib-2.0.so.0 ...`, fixed probe -> `extension loads`, amd64 SIF -> 20
  passed / 0 failed, old SIF -> `SKIP`.

Still unverified: the full rikyu build end to end, and `test-rikyu-gpu.sh`,
which needs a real GPU and must run from the SIF.

---

## 2026-08-18 — Correction: MODELLER *does* run on arm64; rikyu gets it too

**This overturns the arm64 conclusion in the entry below.** I claimed MODELLER
could not run on arm64 Linux, that building on rikyu would not help, and that
only x86_64 emulation remained. That was wrong. I inspected the *conda package*
— which ships only `lib/x86_64-intel8/`, Intel-Fortran-linked — and generalised
from it to the whole distribution without checking the generic tarball.

`https://salilab.org/modeller/10.8/modeller-10.8.tar.gz` (38 MB) ships five
architectures: `armv6l-gnu`, **`armv8-gnu`**, `i386-absoft`, `i386-intel8`,
`x86_64-intel8`. `libmodeller.so.14` under `armv8-gnu` is `ELF 64-bit LSB shared
object, ARM aarch64`, gfortran-linked (`libgfortran.so.5`, no Intel runtime),
and the `Install` script detects `aarch64:Linux:*` and offers "5) Linux on
64-bit ARM". The conda channel is the limitation, not MODELLER.

The Python side works too: the tarball's `python3.3/_modeller.so` is a
stable-ABI (abi3) build, so one binary covers Python 3.3+. Verified on x86 by
importing the tarball's `python3.3` extension under the SIF's **Python 3.12** —
`import modeller` and `Environ()` both succeed. Same layout exists under
`armv8-gnu`, so rikyu's Python 3.12 is covered.

`Dockerfile.rikyu-arm64` now installs it from the tarball, laid out by hand
(modlib + src + bin/*.top + bin/lib + lib/armv8-gnu, symlinked into
site-packages) rather than via the interactive `Install`, matching the shape the
conda package produces so nothing downstream can tell the images apart. Proven
on x86 first with the equivalent `x86_64-intel8` layout — 57 MB, config.py left
at the `XXXX` placeholder, runtime `KEY_MODELLER*` injection working. Both
images now declare `MDCLAW_MODELLER_VERSION`, so both run the smoke check.

**Not verified:** the arm64 image was not built — this host's buildx offers only
`linux/amd64 (+4), linux/386` and `qemu-aarch64` binfmt is unregistered (needs
root). The tarball path needs a build on rikyu itself to confirm.

Emulation was measured before the tarball came up, and is no longer needed. For
the record, qemu-user does work: same 9UWI comparative model, native **13.5 s**
vs **116.6 s** under `qemu-x86_64-static` — **8.7x**, import 0.22 -> 1.22 s.
Same-arch TCG, so an arm64 host would differ somewhat, but the order stands.

---

## 2026-08-18 — MODELLER now ships in the amd64 image; two defects fixed on the way

`modeller_from_alignment` and the `modeller-predict` skill had **no working
runtime anywhere**. MODELLER was in neither `environment.yml`,
`container/Dockerfile`, `Dockerfile.rikyu-arm64`, nor `pyproject.toml`, so no
image could contain it; the SIF is read-only, so the skill's
`conda install salilab::modeller` advice was unreachable there; and
`check_model_backend --model modeller` answers `Available models:
['bioemu', 'boltz']`. Confirmed absent in all four local SIFs (0.6.5).

**The license was never the blocker.** mdclaw's runner builds a synthetic
`modeller.config` from `KEY_MODELLER*` and seeds it into `sys.modules` before
importing MODELLER, taking only `install_dir` from the installed config.
Verified against 10.8: installing with no key succeeds and leaves
`license = r'XXXX'`; an injected key is what MODELLER validates (a wrong one
fails with `check_lice_E> Invalid license key: FAKEKEY123`, naming the injected
value, not the placeholder). So the image ships the package unlicensed and each
user supplies a key at runtime.

Installed from `container/Dockerfile`, not `environment.yml`, because the
salilab channel is **linux-64 only** and the rikyu arm64 image derives its
environment from that same shared file. New image: 20 smoke checks pass
(`PASS: MODELLER installed`), SIF 5.2 GB at `mdclaw-modeller.sif`.

**arm64 is not portable, and building on rikyu does not change that.** salilab
publishes linux-64 and osx-arm64 but no linux-aarch64 and no noarch; bioconda
and conda-forge have nothing. `conda install` downloads prebuilt binaries, so
the build host is irrelevant. Nor can it be compiled: Salilab's own
`INSTALLATION` says *"The source code is not generally available"*; the shipped
`src/` holds only 45 SWIG `.i` files and headers, there is no build system, and
the one Linux target `lib/x86_64-intel8/` links the Intel Fortran runtime
(`libifcore.so.5`, `libimf.so`), which has no ARM build. Only Salilab can fix
this.

### Two defects found by actually using it on 9UWI

1. **Models came back in MODELLER's own frame, numbered from 1.** Fine for de
   novo homology modeling, wrong for the `loop_refinement` repair case the skill
   advertises. On 9UWI chain A (V1aR; 269 resolved, 40 missing over three gaps
   incl. a 33-residue ICL3) the returned model sat **9.86 A** CA RMSD from its
   own template, numbered 1..309 instead of 43..351 — so the atosiban taken from
   the same cryo-EM entry landed in the wrong place, with nothing in the output
   saying so. New `--template-frame` refits and renumbers via the PIR alignment:
   **9.858 -> 0.484 A** over 269 paired CAs, 309 residues renumbered to 43..351,
   and the receptor/atosiban interface returns at **311 of 324** crystal contacts
   with zero clash under 2.0 A. The in-place deviation is now always reported.

2. **The frame check read the wrong alignment file.** `AutoModel.auto_align()`
   aligns the seed, writes the result beside it as `<alnfile>.ali`, and leaves
   the seed untouched with an empty template entry. The first implementation read
   the seed, found no template residues, and skipped restoration on every
   auto-aligned run — the exact case it was written for. Its warning said the
   alignment "does not contain both 'v1arA' and '9uwiA'" while printing "found
   ['9uwiA', 'v1arA']", because one branch handled missing and empty entries.

Tests: `tests/test_modeller_template_frame.py`, 6 cases, no MODELLER needed.
162 passed across genesis/registry/cli.

**Not done:** 9UWI itself is parked at `source_001` (fetch complete,
`solvent_regime=membrane`). Atosiban's GAFF parameterization — `MPT`,
`A1EQM` (O-ethyl-D-Tyr), `ORN`, `NH2` plus an MPT-CYS thioether macrocycle — is
untried and is the likely next obstacle.

---

## 2026-08-18 — Rikyu arm64 image merged to main; one smoke test now serves both

`container/rikyu-arm64` (13 commits, last touched 2026-08-01) is on `main` as a
merge commit. Both Dockerfiles now live side by side and share
`environment.yml`, `pyproject.toml`, `container/scripts/test-container.sh`, and
`docs/developer/container.md`:

| | `container/Dockerfile` | `container/Dockerfile.rikyu-arm64` |
| --- | --- | --- |
| arch / CUDA | x86_64, 11.8 | arm64, 13.0 (NVRTC) + 13.1 math libs |
| OpenMM | 8.2.0 | 8.5.1, `openmm-torch` at `sm_100` |
| publishes to | `ghcr.io/matsunagalab/mdclaw:latest` | `ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-<rev>` |

**The merge itself was nearly clean.** One conflict: the MDAnalysis floor, main
at `>=2.7` from v0.6.5 and the branch at `>=2.8,<3` because linux-aarch64
conda-forge builds start at 2.8. Took `>=2.8,<3` — satisfies both, matches
`environment.yml`, and the published image already carries 2.10.0.

**Sharing the smoke test was the part that actually broke.** Two of the checks
the branch added assume the arm64 image: the cuFFT contract globs
`libcufft.so.12.*`, and the shim contract requires `libmdclaw_fusefix.so` in
`LD_PRELOAD`. Run against the published amd64 SIF, the merged script gave
**19 passed / 2 failed**. Fixed with `check_declared <VAR> <desc> <cmd>`, which
skips when the image never declared the contract. Same script, same SIF:
**19 passed / 0 failed**, two `SKIP` lines. The gate is presence-based rather
than a permanent no-op — forcing `MDCLAW_CUFFT_MIN_VERSION` and
`MDCLAW_FUSEFIX_LIB` into the amd64 SIF reproduces both failures, so the arm64
image (which sets both) is still held to them.

`MDCLAW_FUSEFIX_LIB` is new and is now the single definition of the shim path;
the Dockerfile's runtime assertion and `test-rikyu-gpu.sh` read it instead of
repeating the literal.

**Not verified here:** the arm64 image was not rebuilt — no arm64 builder on
this host. The `MDCLAW_FUSEFIX_LIB` indirection touches a build-time `RUN`
assertion in `Dockerfile.rikyu-arm64`, so the next Rikyu build is the first real
test of it. Nothing is pushed; `main` is local-only and ahead of `origin/main`.

---

## 2026-08-15 — Baseline 0.875, then three fixes before the real K=3

`passk3_20260814_v2_pi_rep1` finished 40/40: **overall_score 0.875**, five tasks
`failed` with `missing_raw_artifacts` — the four 60-min timeouts (P09, P13, P19,
P24) plus P36. 11.7 h of task time, of which **4.0 h (34 %) went to those four
hung commands**. This run is the pre-fix baseline; it cannot be one of the three
repeats, because the fixes below change the system under test.

**1. `parse_mutation_specs` accepts any separator, and says so when it doesn't.**
`--mutations` is `nargs="+"`, so the CLI wants `--mutations L99A M102Q`. P09's
agent passed `"L99A,M102Q"`, `_MUTATION_RE` rejected the single token, the node
was sealed terminal, and the agent spent its remaining 55 minutes grepping the
repo for why. Now each token is split on `[,\s]+` first, and the error names the
multi-mutation form instead of only the single-mutation notation. Four
separator forms are covered by tests; mutation-tested 3/3 (drop the split, split
on whitespace only, drop the hint from the message).

**2. Per-command watchdog for the agent** —
`MDPrepBench/tools/pi_shell_timeout.sh`. pi resolves its shell through
`settings.json` `shellPath` and invokes it as `<shellPath> -c "<command>"`
(`pi-coding-agent dist/utils/shell.js`), so pointing that at a wrapper caps every
command. 600 s: the longest legitimate command in the July 40-task run was
257.7 s (p99 63 s). The setting is global but the wrapper only wraps when `$PWD`
is inside a MDPrepBench run, so other pi sessions are untouched
(backup: `~/.pi/agent/settings.json.bak-20260815`).

Verified separately, not end to end. Proven: pi does route through the wrapper
(logged a real `-c echo …` invocation, one `-c` per command, so no persistent
session shell to kill); and the wrapper caps correctly (exit 124, process-group
kill confirmed with marker files, partial output still returned). Not proven: a
long command inside pi actually returning 124 — repeated attempts stalled in pi
before it issued any tool call, and **a control run with `shellPath` removed
stalled identically**, so that is pi flakiness, not the wrapper. Check the first
hour of the run for any tool call over ~615 s. Worst case equals rep1's
behaviour, so this cannot make things worse.

**3. `MDCLAW_RUNTIME=singularity` in the sweep env.** `bin/mdclaw` probes for a
conda env before falling through to Singularity, and `conda env list` costs
1.3 s on a host that has no `mdclaw` env. Measured `bin/mdclaw --version`:
**3.8 s → 2.4 s**. Left as an env var rather than a code change — the probe is
correct on hosts that do have the env.

---

## 2026-08-15 — Correction of the correction: there is no pi floor, and the four timeouts are hung commands

The entry below claims a "quantisation floor inside the pi harness" and cites
`cat common/run-loop.md` taking 7.19 s. The cursor advisor refuted it and I
verified both claims against the transcripts myself.

**1. The 7 s "floor" was a batching artifact.** When one assistant message
issues several toolCalls, all their results are recorded at one timestamp, so
every sibling inherits the slowest one's elapsed time. Measured over rep1:

| | n | median | in the 6.4–7.6 s band |
|---|---|---|---|
| plain shell, `batch=1`, no mdclaw | 466 | **0.11 s** | 2 |
| plain shell, batched, no mdclaw | — | — | 25, **all 25 with an mdclaw sibling** |

The `cat` at 7.19 s was batched with an mdclaw call. There is no harness floor.

**2. The transcript timing method is accurate.** The agent itself ran
`time mdclaw …` 17 times. Comparing the shell's own `real` against the
toolCall→toolResult window: **median gap 0.05 s**, across commands from 0.55 s
to 263 s. So toolCall→toolResult *is* the command's wall time — no harness
overhead to subtract. That kills the "~3 s recording overhead" theory too.

**3. The four 60-min timeouts are not model generation.** Each burned 42–55
minutes inside a single environment-probing command:

- P09 — `grep -rln "mutation_spec_invalid\|hpacker" <benchmark_runs tree>`, toolCall never answered
- P24 — a hand-written `min.py` on the host venv, toolCall never answered
- P13 — 42.2 min in one completed call: `which tleap parmchk2 antechamber; ls /opt/anaconda3/...`
- P19 — 51.7 min in one completed call: host-venv python probing `openmm.__file__`

My earlier reading ("P09 spent 3.1 min of 60 in tool calls, so the rest is
generation") was an artifact of my own script: it yielded only calls that had a
matching toolResult, so a hung command was invisible, and batch double-counting
inflated the others. Same failure mode as P26 in the entry two below — the agent
leaves the workflow and scans a huge tree.

**4. The lazy-preload win does not show up in the benchmark.** Solo, read-only
mdclaw calls: July min 5.71 s / median 6.07 s; now min 6.44 s / median 7.00 s.
The floor went **up ~0.9 s** since July. The direct A/B (5.91 → 2.90 s) is real
and reproducible, and the transcript timings are trustworthy per (2), so the two
must be measuring different work — benchmark calls are `create_node` /
`explain_node` / `inspect_job` against a job dir on NFS, not `--list-json`.
**Unresolved.** Do not claim the fix sped up the benchmark.

**5. Verified MDClaw bug behind P09.** `--mutations` is `nargs="+"`
(`mdclaw/_cli.py:450`), so the correct form is `--mutations L99A M102Q`. The
agent passed `"L99A,M102Q"`; `_MUTATION_RE` (`mdclaw/sidechain_packer.py:178`)
anchors a single token, so it cannot match, and the message
(`mdclaw/sidechain_packer.py:196-198`) is *"Invalid mutation spec 'L99A,M102Q'.
Use L99A or A:L99A notation."* — it never says how to pass more than one. The
task asks for two mutations. The agent tried `"L99A M102Q"` quoted, failed
again, and went grepping. The failure also sealed `prep_002` as terminal, so
retrying on that node was refused.

---

## 2026-08-14 (later) — Correction: the "per-mdclaw-invocation latency" numbers in the entry below are pi's floor, not mdclaw's cost

The entry below reports light-call latency of 6.04 s (July) vs 7.19 s (Aug) and
treats it as mdclaw's per-call cost. **It is not.** In the same transcripts:

```
cat common/run-loop.md                        7.19 s
ls -la && grep -c "^ATOM" 2LZM.cif ...        6.92 s
[read] .../solver_workspace/.agents/skills/…  7.43 s
```

`cat` of a local file does not take seven seconds. A histogram of all 1511 tool
round-trips in the running sweep is bimodal — 732 calls under 0.5 s, a near-empty
valley from 3–6 s, then 385 calls piled at 6.5–7.5 s. July shows the same shape
with the pile at 6.0–6.5 s. That is a quantisation floor inside the pi harness,
applied to anything that does not return almost instantly, and it is what those
medians were measuring. The floor rising ~0.7 s between July and August is a pi
change, not ours.

So the lazy-preload win is real but invisible here. Same-moment A/B in the
benchmark's own solver workspace, while the sweep was running:

| | median |
|---|---|
| checkout, lazy preload | **2.90 s** |
| SIF baked package, import-time preload | 5.91 s |
| via `bin/mdclaw` (adds the wrapper) | 3.65 s |

pi reports all three as ~7 s. Benchmark wall time cannot measure mdclaw CLI
latency; only direct timing can.

**Second finding, not yet fixed:** `bin/mdclaw` calls `_conda_env_exists()`
before falling through to Singularity, and `conda env list` costs **1.06 s** of
the 1.30 s wrapper overhead — on a host that has no `mdclaw` conda env at all.
Reading `~/.conda/environments.txt` answers the same question instantly. Holding
the change until the K=3 sweep finishes so the three repeats stay comparable.

**Third:** the 60-min cap is being consumed by model generation, not by tools.
Of the four timeouts in rep1 so far, P09 spent 3.1 min of its 60 inside tool
calls and P24 spent 0.3 min. Making the CLI faster cannot fix those.

---

## 2026-08-14 — The pass^k sweep ran slow: contention, not the refactor — but it exposed a 3.4 s tax on every CLI call

I stopped the K=3 pass^k sweep at rep1 19/40 because tasks were taking ~1.7×
the July wall time, and went looking for a regression in the de-over-engineering
work. **There is none.** What the transcripts actually show:

| metric (21 tasks in common) | July 20 | Aug 14 |
|---|---|---|
| per-`mdclaw`-invocation latency, light calls | 6.04 s median | 7.19 s median |
| `build_amber_system` (CPU, tleap), same tasks | 1026 s total | 938 s (0.91×) |
| `solvate_structure` (CPU), same tasks | 701 s total | 771 s (1.10×) |
| `run_minimization` (GPU), same tasks | 384 s total | 868 s (**2.26×**) |

Only the GPU step inflated, and only for the first ~4 h of the run: hourly
medians went 121.8 -> 99.0 -> 79.7 -> 48.7 -> 14.2 -> 10.5 s, i.e. back to
July's 13–19 s by +5 h. Same task, same system, `platform: CUDA` in both,
identical `max_iterations`, `restraint_count` (1309) and final energies. That is
host contention — this box shares 7× A6000 with other jobs — not code.

**The `bin/mdclaw` PKG_ROOT bind is not the cost either.** A/B, 10 reps each,
`mdclaw --list-json inspect_molecules`: with the bind + NFS `PYTHONPATH`
5.82 s, against the SIF's baked package 6.06 s. Within noise, and the bind side
is if anything faster. My earlier "+1.7 s per call" was a cold-cache artifact.

**What the hunt did find: `import mdclaw` cost 4.6 s, of which 4.5 s was
`import torch`.** `mdclaw/__init__.py` ran `_preload_torch_for_openmm_torch()`
at import time — every CLI call, including `--list-json`, `inspect_job` and
`create_node`, dlopened libtorch's CUDA libraries to keep the openmm-torch
plugin working (the June-29/July-7 fix for PythonTorchForce). Breakdown inside
the SIF: singularity `exec true` 0.38 s, + python start 0.46 s, + `import
mdclaw` 4.51 s, + full `_discover_tools()` 5.81 s.

Two measured facts turned that into a fix:

1. `importlib.util.find_spec("torch")` gives the library path without executing
   torch: 3.48 s -> 1.46 s.
2. The dlopen does **not** have to precede `import openmm`. It must precede the
   *plugin scan*, and the scan can be re-run: dlopen, then
   `Platform.loadPluginsFromDirectory(Platform.getDefaultPluginsDirectory())`,
   and the CUDA kernel registers. Verified three ways on an A6000 — `early`
   (today's order) OK, `late` without a rescan fails with "Platform does not
   support the requested kernel" exactly like no preload at all, `late` **with**
   the rescan OK (71.25 kJ/mol from a real PythonTorchForce Context).

So the preload moved out of `mdclaw/__init__.py` into
`custom_forces._preload_libtorch_cuda()`, called from `_import_openmmtorch()` —
the one code path that needs it. `MDCLAW_PRELOAD_TORCH_FOR_OPENMM` is gone; a
knob for a cost nobody pays any more is just more surface.

Result, 8 reps each: **5.91 s -> 2.62 s per CLI call (2.26×)**. At ~30 mdclaw
invocations per MDPrepBench task that is ~100 s/task, ~11 % of a 15-min task,
and ~3 h off a 120-task K=3 sweep.

`tests/test_torch_preload.py` was rewritten for the new contract and
mutation-tested: dropping the rescan, reversing the c10/torch_cuda order,
dropping RTLD_GLOBAL, rescanning on CPU-only torch, going back to `import
torch`, and re-adding the preload to `mdclaw/__init__.py` are all caught.

**Lesson.** Two of my three suspects (the PKG_ROOT bind, "the model got slower")
were wrong, and the July-vs-today medians said so within minutes — but only
after I stopped comparing *inter-event gaps* and started comparing *the same
step on the same task*. Aggregate latency hides which layer moved; a paired
comparison names it.

---

## 2026-08-14 — Why P26 kept timing out: it never entered the workflow

`P26_prep_zinc_metalloenzyme_2cba` (carbonic anhydrase II, catalytic Zn) was the
only MDPrepBench task pi + deepseek failed repeatedly — 3 timeouts in 4 attempts
against a 30-min cap, while P27 (Mn), P30 (Zn+DNA) and P06 (Ca) passed first try.

**One thing separates the runs.** Both successes ran the canonical workflow
(bootstrap -> inspect_job -> create_node -> explain_node -> inspect_molecules ->
prepare_complex -> minimize). All three failures reached none of it: two called
no workflow tool at all, one only introspected `--list-json prepare_complex`.
And every failure ends on a filesystem-wide `find /` — neither success runs one.
This host mounts ~390 TB of NFS under `/` (117T + 99T + 98T + 73T); measured,
`find / -name ions.xml -path '*amber*'` does not finish in 60 s. One such command
consumes the whole remaining budget, which is why the transcripts stop at 2.2 /
16.6 / 17.9 min but the runs die at 30.

So the chain is: skip `inspect_molecules` -> never learn the default water XML
already covers ZN -> go establish it yourself -> grep force-field XMLs ->
`find /` -> budget gone.

**Both of my hypotheses were wrong, and the evidence says so.**

- "The CLI cannot answer whether Zn is supported." False. On the real 2CBA,
  `inspect_molecules` already returned `metal_parameterization_required: false`
  plus a note that the default OPC water XML provides the templates. My first
  check used a bare-ZN-only stub PDB that never reached the metal-detection path
  — the test was wrong, not the tool.
- "My skills consolidation buried the ion policy by deleting
  `skills/md-prepare/ion-policy.md`." Refuted decisively by the advisor: the
  July P26 success never read that page (only the spine and explicit-water.md).
  Nor did July's P27, which instead parsed `amber19/opc.xml` inside the
  container by hand. The over-verification habit predates a9c6255 entirely.

**Fixed anyway, where the agent actually looked:**

- The verdict was prose in `notes.metal_handling`, far from the ion guidance.
  `preparation_guidance.ions` now carries stable values — `bare_ion_templates`,
  `bare_ion_templates_water_model`, `bare_ion_templates_scope`. The scope name is
  deliberately narrow: templates existing for a bare ion is not a claim that the
  coordination site is scientifically modelled.
- `metal_parameterization_required` was hardcoded `False` regardless of the
  catalog check. Latent today (every multivalent metal in the detector is in the
  OPC catalog) but a lie waiting to happen; now derived.
- `explicit-water.md` told the agent that finding multivalent metals means
  finishing "the matching explicit prep branch". A standard bare ion needs no
  branch, and that sentence invites exactly the investigation that killed these
  runs. It predates a9c6255.
- `--list-json <node tool>` said `job_dir` and `node_id` are required without
  saying where they come from — the agent that introspected before calling got a
  parameter list and no way in. Node-required tools now carry `workflow_entry`.

Skills net -3 lines (the duplicated ion sentence in `prepare-complex.md` and the
page-hunting route in `SKILL.md` are gone); no new tool.

**What this does not establish.** One passing run would not prove anything: the
pre-fix state also passed 1 in 4. Divergence is model-level variance and these
changes do not forbid it — they put the answer and the way back where a
diverging agent was already looking. The reliable guard is at the harness shell
boundary (refuse `find` rooted at `/`, `/home`, `/data*`; cap discovery commands
and kill the process group), which belongs to MDPrepBench, not MDClaw.

**Also learned:** pi's provider config changed. `spark1-vllm` is gone;
`deepseek-cloudflare/deepseek-v4-flash` now points at the same local vLLM
(`http://192.168.1.61:8000/v1`). The first rerun died in 0 min on
`Model "spark1-vllm/deepseek-v4-flash" not found` — an environment change, not a
code one. The memo entry of 2026-08-13 that called the spark1 name the real one
is superseded.

---

## 2026-08-13 — v0.6.5: MDAnalysis in, image rebuilt, and a lint that started screaming

The runtime image was rebuilt so this week's simplification actually ships, and
MDAnalysis was added beside mdtraj. Both went into one build rather than two:
`container/Dockerfile` copies `mdclaw/` before the conda stage, so any source
change forces a full rebuild including the OpenMM source build (~1 h), and
sequencing them would have cost that twice plus a second ~15 GB push for the
same end state. The dependency risk was retired first — `pip install --dry-run
MDAnalysis` inside the published image showed 2.10.0 resolving with no
numpy/scipy movement, adding only GridDataFormats, mmtf-python, mrcfile,
msgpack and threadpoolctl.

MDAnalysis is declared in `pyproject.toml` next to mdtraj, which is the one
place that reaches both targets: the conda env through `environment.yml`'s
`pip: -e .`, and the image through `pip install ".[dev]"` in stage 1.

Version bumped to 0.6.5 because `bin/mdclaw` derives the default Docker tag
from `plugin.json`: leaving it at 0.6.4 would either strand Docker users on the
old image or redefine a published release tag. `:0.6.5` and `:latest` now share
`sha256:6d5ff025…`; `:0.6.4` is untouched.

Verified on the image (19/19 container tests, GPU) and again on the SIF: 77
tools, v0.6.5, MDAnalysis 2.10.0, mdtraj 1.11.1, CUDA present, and — checked
deliberately — the *baked* package answers an unknown tool with
`tool_not_available` JSON and a renamed one with its replacement. That check
matters now that `bin/mdclaw` binds the checkout: ordinary work would no longer
notice a stale baked package, but plugin users run exactly that copy. Full
suite on the new SIF: 1349 passed, 3 skipped.

**What the swap exposed.** ruff went 0.15.21 → 0.16.2, and since
`pyproject.toml` selected no rules, `ruff check mdclaw/ tests/` — the command
CLAUDE.md tells contributors to run — went from clean to **1,492 findings**
overnight. Nothing in the code changed; the defaults widened. A lint that
always screams is a lint everyone learns to ignore, which is the same failure
mode as the flaky test in the previous entry. The rule set the code was written
under (`E4, E7, E9, F`) is now pinned, and both ruff versions agree on the
result.

Pinning then surfaced three unused imports I had introduced and not seen,
because my final lint runs had narrowed to `mdclaw/` and skipped `tests/`. It
also left the 17 pre-existing E702/E741 violations in two test files visible;
those are fixed too, so the documented command is actually green rather than
green-if-you-ignore-the-usual-noise.

**Unresolved host issue:** `/` is at 100% (5.2 G free), which is what made the
first `singularity pull` fail — SIF conversion was redirected to `/home` via
`SINGULARITY_TMPDIR`. Docker holds 221 GB of images and 190 GB of build cache,
371 GB reclaimable. Left alone deliberately: pruning the cache makes the next
image build much slower, and that is the maintainer's call.

---

## 2026-08-13 — Independent review of the simplification, and what it found in the tests

A codex advisor (gpt-5.6-sol, xhigh) was stood up in a Herdr pane and asked to
review commit a9c6255 without being told what to conclude. It found real
defects the author's own tests had not, and its second pass — an audit of the
test suite itself — found more. Both passes verified every claim by mutation:
break the implementation, check whether the test notices.

**Defects the review found in a9c6255** (all fixed):

- Seven of the twelve removed tools fell through to an argparse dump on stderr
  (exit 2, empty stdout), breaking the "every failure is JSON on stdout with a
  stable code" contract. The advisor framed this as an incomplete compatibility
  layer and recommended restoring aliases; the maintainer's question — "why
  care, we deleted them?" — produced the better diagnosis. Measurement showed a
  never-existed name behaves identically, so this was a pre-existing hole in the
  CLI that the deletions merely joined, and the fix is one generic
  unknown-subcommand handler, not a seven-entry tombstone table (which would
  have re-created exactly the hand-maintained name list this refactor deleted).
  `--list-json` already answered such names correctly, so both paths now share
  one resolver.
- Three migration hints silently dropped the old tool's defaults
  (`setup_model_backend` requires `--model`; `fetch_structure` defaults to CIF
  where `get_alphafold_structure` defaulted to PDB), and the comment above
  `_RENAMED_TOOLS` still claimed the Python functions survived for direct
  importers — false since a9c6255 deleted them.
- `search_structures` still advertised the deleted 0–120 MD-suitability rubric
  (`ranking_method: "md_suitability"`, `md_score_info` with interpretation
  bands) while computing a plain method-then-resolution sort, and a skill page
  claimed chain composition entered the ranking.
- **A regression this refactor introduced**: routing `setup_logger` through the
  root logger made merely importing mdclaw attach a root handler, so a host
  application's own records started printing. Fixed with a package-level
  NullHandler; `literature/_base.py` turned out to have been doing the same
  thing via `logging.basicConfig` since well before this work.
- budget validation, loosened to a shape check because "no Python code reads
  these numbers", had the wrong test: the reader is a later *agent*
  (`md-production` takes production length from `derived.target_*`). Restored
  as enums/types/signs only — and the first restoration was itself buggy
  (`headroom_hours` unchecked rather than sign-unconstrained, explicit nulls
  passing, enum checks raising TypeError on list input, NaN/Infinity accepted).

**What the test audit found.** The suite was green throughout, which turned out
to mean less than it looks:

- `test_direct_args_win_over_structure_analysis` asserted nothing at all. A
  first fix made it assert — and mutation testing showed it *still* passed with
  the precedence inverted, because the rule applies to what reaches
  `clean_protein`, and that block never runs when the fixture returns no
  proteins. The original author knew ("full precedence is exercised in the
  end-to-end test") but no such test exists. Now stubs a protein through and
  inspects `clean_protein`'s actual kwargs; mutation fails it.
- `test_removed_tools_are_deliberate` did not implement its own docstring: it
  claimed to fail when a server still imports, but used a hardcoded core-server
  list — which still named the deleted `benchmark` server, and let any
  non-core tool vanish silently.
- Tests pinned prose where the contract is a code: rewriting a failure's `code`
  to `unhandled_error` left them green. `test_representative_tool_failures`
  hid this structurally by passing raw results straight through
  `finalize_error`, which defaults a missing code to `unhandled_error`.
- `_run_cli` discarded the exit status; the guardrail-registry tests skipped
  (rather than failed) if the registry went missing; two live-API tests were
  unconditionally skipped placeholders behind a `--runslow` flag that does not
  exist; stage mappings survived for two tools deleted in a9c6255.

The lesson worth keeping: a green suite is evidence that the tests pass, not
that they guard anything. Every fix in this entry was checked by breaking the
implementation first. Also, a reviewer can identify a real defect and still
recommend the wrong remedy — the unknown-tool finding was correct, its proposed
fix would have partly undone the simplification.

**A flaky test that predates all of this.** The full suite then failed on
`test_embed_in_membrane_runs_parallel_packmol_race`. It is not a regression:
at HEAD it passed 2 of 5 runs, and at `dce72c6` — before any of today's work —
1 of 5. Every "full suite green" claim in this memo, including today's, was
partly luck. The implementation is right: the race cancels lanes that have not
started once a winner is accepted, and a sibling test exists for exactly that.
The test's premise was wrong — it assumed all four lanes always reach the
runner, so whenever one lane finished before the last was scheduled, the
cancelled lane went unrecorded and the count came up short. Fixed with a
`threading.Barrier(4, timeout=30)` in the stubbed runner: every lane must
arrive before any returns, which is deterministic (10/10) and still fails
loudly if a real regression starts fewer lanes. Worth noting that a test
failing 40–80% of the time was in a position to hide someone's real regression
for as long as it existed — the same failure mode the 2026-08-11 entry
describes.

Full suite after all of the above: 1349 passed, 3 skipped, 0 failed; ruff clean.

Still open: old job dirs carry `claim` metadata that the agent-facing index no
longer surfaces (a migration warning was proposed, not written), and
`test_registry` still skips on any ImportError, so an accidental import typo in
a server can hide its tools from discovery without failing anything.

---

## 2026-08-13 — MDPrepBench pi+deepseek revalidation after the simplification: 40/40 at 1.0

The de-over-engineered tree (previous entry) was revalidated with the same
solver as the 2026-07-20 sweep: pi (`pi-user` profile) +
`spark1-vllm/deepseek-v4-flash`, skills+cli, 30-min/task cap, deterministic
scoring — now through the standalone MDPrepBench repo (`~/tmp/MDPrepBench`,
runs `refactor_verify_*`). **Every one of the 40 tasks scored 1.0**, one task
better than July's 39×1.0 + P28 0.9639 (P28 scored 1.0 this time). The
consolidated skills and the 77-tool CLI carried the whole suite, including the
new `--lipids` list contract (P18/P34/P37/P39 membranes all 1.0).

Caveats worth the record:

- **Not one-shot.** The first pass ran as two concurrent shards to halve
  wall-clock; that self-inflicted vLLM congestion produced 6 walltime timeouts
  (P03, P21, P24, P26, P28, P29 — 5 of 6 in the same shard). Sequential
  retries passed 5 of them at 1.0 immediately. July's 40/40 was sequential;
  concurrency, not the refactor, was the variable — P37–P40 sped up the moment
  shard A finished.
- **P26 (zinc, 2CBA) needed 4 attempts.** Attempts 1–3 timed out the same way:
  with a byte-identical prompt and identical skills, the agent ignored the CLI
  and spelunked openmm data dirs for ion XMLs, ending in `find /` scans. The
  first assistant sentence already diverges from July's run ("inspect the local
  OpenMM environment" vs July's "read the relevant skills"), and the spark1
  serving config changed since July (220K → 1M context on the same model
  name) — model-side drift/variance, not a skills regression: P27 (Mn) and
  P30 (Zn) passed 1.0 first try, and attempt 4 passed 1.0 in 20 min via the
  normal CLI path.
- **The harness now really tests the checkout.** `bin/mdclaw` previously ran
  the SIF's baked-in mdclaw package while host-side native tools ran the
  checkout — a silent version skew. It now binds PKG_ROOT and sets PYTHONPATH
  into the container, so these runs exercised the refactored source, verified
  by tool count (77) from inside the solver workspace.

---

## 2026-08-12 — De-over-engineering executed: −6,433 net lines, 89 → 77 tools

The audit below was executed the same day: 148 files changed, +1,274 / −7,707
(net −6,433). Suite green afterwards (1,252 passed to the old stop point plus
the tail files; ruff clean on `mdclaw/`). Skills: 4,332 → ~3,290 lines,
61 → ~46 files. `_cli.py` 1,409 → ~1,113; `evidence/reporting.py` 1,683 → 503.

**Deleted outright:** claim/lease machinery + its guardrail codes; `update_node`;
`find_nodes`/`get_children`; `mdclaw/metal/`; `research/structure_analysis.py`
(its two disulfide helpers moved to `structure/disulfide.py` — the audit missed
that `prepare_complex` imports them); `research/scoring.py` (the 0–120 rubric;
`--rank-for-md` now sorts X-ray→cryo-EM→NMR, best resolution first); the
evidence Methods half + `citation_inventory.md` + `evidence_schema.py` (folded);
alias tools (`download_structure`, `get_alphafold_structure`,
`setup/check_surrogate_backend`, `explain_failure`) — all now `tool_renamed`
redirects; PLIP; write-only `artifact_sha256` (existence check kept — no more
hashing multi-GB trajectories inside node.lock); the `_tool_meta` shims; false
MCP docstrings (`test_mcp_server.py` → `test_registry.py`); stale
`mdclaw/benchmark/` and `tests/test_benchmark/` pycache ghosts.

**Refactored:** one `_tool_param_specs` pass now feeds argparse, `--list-json`,
and kwargs assembly (the triple type-dispatch ladder is gone);
`embed_in_membrane.lipids` is `list[str]` (the 60-line repeated-string CLI
special case died); `fetch_structure` defaults `source="auto"` (CLI convenience
layer died); benchmark JSONL hook moved to `_benchmark_log.py`; `setup_logger`
propagates to one root handler (stream-swap surgery collapsed); TOOLS/`__all__`
derived from function objects in all 16 package `__init__`s; glycan helpers and
`CANONICAL_WATER_MODELS` moved to `chemistry_constants`; study log
triple-wrapper inlined; budget validation reduced to shape-only (field-level
tests replaced accordingly); prod-chain walkers unified; node.json readers
collapsed onto `_read_node_json`; sealed-node handling uses a typed
`NodeSealedError` instead of exception-message string matching.

**Bugs fixed:** `--json-input` skipped required-argument validation (regression
test added); `atomic_write_text_group`'s except-path deleted backups that had
just failed to restore (now `else`-scoped); broken `mdclaw.__all__`;
ineffective `_NODE_REQUIRED_TOOLS` monkeypatch in test_cli; the false
"boolean flags reject true/false" skill sentence; `bin/mdclaw` now binds
PKG_ROOT + PYTHONPATH into the SIF so container tools run the same source as
host-side native tools (previously the SIF's baked package — a version skew).

**Deliberately NOT done, with reasons:** guardrail registry kept at 257 codes
(the hint text is weak-agent scaffolding; measure before pruning);
`read_ancestor_final_step`'s three-state sentinel kept (tests use the omitted
form as real API — audit overcounted); `validate_node_execution_context`'s
`validate_conditions` param kept (None-collapse would change strictness for
callers passing `actual_conditions=None`); progress.json entry shape kept
(agent-facing via inspect_job; thinning it is a contract change — decide
separately); the three failure entry points kept (thin adapters, distinct call
shapes); `literature/` kept (skill-referenced and working); visualization
constants kept (audit wrongly called them dead — they are module-local, used).

---

## 2026-08-12 — Over-engineering audit: ~7.5–8k lines removable, 89 → ~78 tools

Four parallel audits (DAG/node core, CLI/dispatch, peripheral subsystems,
skills) over ~59k lines of Python + 4.3k lines of skills, looking only at
harness/plumbing complexity, not MD physics. Findings are an assessment;
nothing has been changed yet.

**Headline ratios.** 263 guardrail codes, 2 code branches anywhere that test a
code value; 89 tools dispatched by `fn(**kwargs)` behind ~2,244 lines of
CLI/registry/meta machinery; all 89 `TOOLS` entries are identity mappings;
`from mdclaw import *` raises (all 17 `__all__` names unbound).

**Dead or orphaned, highest confidence.** claim/release node-lease machinery
(~170 lines, zero production callers); `mdclaw/metal/` (whole package, zero
callers, consumer removed in 8a39b78); `research/structure_analysis.py` (694
lines, docstring cites a workflow phase that no longer exists);
the Methods-report half of `evidence/reporting.py` (~1,208 of 1,683 lines +
534-line citation inventory — zero output files across ~50 recorded benchmark
runs); alias tools (`download_structure`, `get_alphafold_structure`,
`setup/check_surrogate_backend`, `explain_failure`); write-only
`artifact_sha256` that hashes multi-GB trajectories inside node.lock with no
reader.

**Same-fact-N-times.** Tool names stated 3–4x per tool across
import/TOOLS/`__all__`; parent-type contract implemented twice (create_node
branches vs `_ALLOWED_PARENT_TYPES` table); progress.json has grown from
"thin index" into a node.json mirror with its own repair tool; in skills/,
ion policy ×6, platform preflight ×6, `guardrail-codes.md` (276 lines) is a
byte-level duplicate of what `hints[0]` already delivers at runtime.

**MCP ghost.** No MCP plumbing remains, but 11 files carry a false "integrates
with external MCP servers" docstring and `test_mcp_server.py` tests the
registry — misnaming propagated into CLAUDE.md/testing docs.

**Bugs found incidentally.** `--json-input` path skips required-argument
validation entirely; `mdclaw/__init__.py.__all__` fully broken;
skills/md-prepare/explicit-water.md:86 states boolean flags reject
`true`/`false` values (false — `_parse_cli_bool` accepts them, and
bioemu-sample instructs `--reconstruct-sidechains false`); inconsistent
node.lock/progress.lock ordering (latent deadlock shape, masked by
single-writer usage).

**Deliberately deferred.** The 263-code guardrail registry is the one place
where over-engineering may be load-bearing weak-agent scaffolding (the payload
is LLM-facing hint text). Decision: measure against benchmarks before pruning
to the ~40 referenced codes; don't drift.

Totals: CLI/dispatch ~1,000–1,700; node/DAG ~1,150; peripherals ~4,040 py +
534 md; skills ~1,320–1,420 (61 files → ~35). Full per-finding detail with
line numbers lives in the session transcript of this date.

---

## 2026-08-12 — GPU verification: the CPU hour was an invocation defect

Follow-up to the fourteen-failure entry: the hour-long membrane equilibration
was not a property of the tests but of how I launched them. The SIF was
invoked without `--nv`, so the container had no CUDA platform and
`platform="auto"` silently fell back to CPU.

Verified three ways. Platform probes: without `--nv` the usable set is
`[Reference, CPU]`; with it, `[Reference, CPU, CUDA]`, and a 20k-particle
auto-selected Context lands on CUDA. Timing: the same membrane+metal chains
that took 1 h 08 m on CPU completed in **1 m 58 s** with `--nv` — about 35x —
with identical results (7 passed both ways). The production paths
(`bin/mdclaw`, the benchmark task wrappers) were never affected; they already
add `--nv` when `nvidia-smi` is present. Only hand-typed SIF commands
following the guide missed it, and the guide is fixed (`18ca28a`).

Corrected estimate: a full suite with the revived pipeline chains is ~45 min
with `--nv`, not the 1.5 h previously reported. The 3PWB chain deliberately
pins `platform="CPU"` for determinism and is unaffected.

Noted, not done: the executed platform lives only in tool results, not in
node.json metadata, so post-hoc provenance cannot say which platform produced
an artifact. Worth considering if platform ever becomes scientifically
relevant (e.g. mixed-precision differences).

---

## 2026-08-11 — The fourteen failures: one real bug, thirteen stale fixtures

All fourteen pre-existing failures are fixed (`5eb0486`, `5363222`); the full
suite is green for the first time on record (1381 passed, 0 failed). None of
the tests were unnecessary — the question that prompted the investigation.

**The real bug** hid in plain sight for three weeks. The node-sealing change
(2026-07-16, c532626) made terminal node.json immutable but missed
`_register_preview_on_node`, which re-called `complete_node` on completed
nodes. Every post-hoc preview/review attachment on a finished node failed —
and the tests that would have caught it were failing for unrelated fixture
reasons, so the signal read as noise. That is the cost of tolerating a red
suite: real regressions become indistinguishable from stale tests. Attachments
now go through append-only `preview_registered` events, the resolvers read
them back, and a regression test pins the sealed-node render-then-review flow.

**The thirteen others** were fixtures asserting contracts the code had
deliberately outgrown: the parentless-node ban in study jobs, the candidates
layout, mandatory prep-time candidate selection, prep-owned hydrogen
completeness, a package-attr shadowing, an unverified writeFile-inventory pin
(the new membrane call site does restore long residue names — verified before
pinning), and a chemically impossible synthetic nucleic fixture, now generated
from pdbfixer template geometry against the force fields' terminal templates.

Two side finds from review: `test_split_molecules` was writing `split_N/`
directories into the repository checkout (two were committed; removed, output
now under tmp_path), and the first version of the event fix wrote events
nothing read — the reviewer's "writing is useless without a reader" catch led
to the resolver change that makes the flow actually work.

With the membrane/metal prepare steps unblocked, those chains run their full
MD legs (packmol packing through CPU equilibration) in-suite again, adding
roughly 1.5 h to a full run. That is the price of the coverage being real.

---

## 2026-08-09 — MDStudyBench extracted; the benchmark harness leaves mdclaw

MDStudyBench is now its own public repository,
<https://github.com/matsunagalab/MDStudyBench>, extracted with the same
copy-and-trim pattern as MDPrepBench and reviewed the same way before the first
commit (the review caught a missed `parents[2]`, an unconditional host
`import mdclaw`, a README claim that `MDCLAW_PYTHON` drives the scoring
delegate, a self-contradictory default CLI policy, and the spark1 profile
defaults surviving the copy — all fixed pre-publish; see that repo's memo).

Unlike the prep suite, `mdclaw` is a deliberate runtime dependency of its
confirmatory path: the runner executes MDClaw production nodes, snapshots the
installed `mdclaw` package as the attested adapter source, and resolves node
inputs through `mdclaw.node`. Scoring an existing submission needs only
openmm/mdtraj/numpy.

With both suites gone, this repository dropped `mdclaw/benchmark/` (~17k
lines), `tests/test_benchmark/`, `benchmarks/`, `docs/benchmark/`, and the
registry entry — 74 files, −36.8k lines. What deliberately stays is the
stage-record hook in `mdclaw/_cli.py` (`MDCLAW_BENCHMARK_HARNESS_LOG`): it is
now a cross-repository protocol both benchmark harnesses rely on, and its
stage vocabulary must not change silently. The layout the maintainer asked for
is three sibling checkouts: `mdclaw`, `MDPrepBench`, `MDStudyBench`.

**Pre-existing test failures catalogued during the removal** (fail identically
with the removal stashed; none are benchmark-related): three
`test_evidence_server` study-evidence report tests (missing prod `node.json`
in the fixture), three `test_visualization_server` node-registration tests,
two implicit-solvent `test_md_helpers` builds, `test_modxna_support` residue
mapping, `test_pdb_export_resname_guard` inventory pin, one prepare step in
each of the 3PWB/membrane/metal pipeline DAG tests, and one structure smoke
test. 14 in total against 1357 passing; they need their own investigation.

This memo stays as the historical record of the benchmark work done while the
suites lived here; new benchmark entries belong in the respective repos'
docs/memo.md.

---

## 2026-08-09 — MDPrepBench extracted to matsunagalab/MDPrepBench

MDPrepBench is now its own public repository,
<https://github.com/matsunagalab/MDPrepBench>, laid out as a sibling checkout
(`/home/yasu/tmp/MDPrepBench`). Fresh history, MIT, everything public — the
task contracts and truth references were already world-readable in this repo,
so openness was made deliberate rather than accidental. The extraction is
copy-and-trim: package `mdprepbench` is the harness minus the four study-only
modules, with `grounded_correct_v2` entry points raising NotImplementedError
pointing back here. All 337 tests pass there; CI runs lint, dataset
consistency, and a no-OpenMM test subset (verified in a bare venv).

A pre-publish external review caught five release blockers before the first
public commit, the worst being container-delegated scoring still invoking
`python -m mdclaw._cli` — it would have scored with whatever MDClaw the image
carried instead of the published code. Details in the new repo's docs/memo.md.

On this side, mdclaw dropped the prep dataset, prep-only tests/tools/docs, and
the prep-fixture-dependent tests whose coverage now lives in the new repo
(336 still pass). Kept: `mdclaw/benchmark` (MDStudyBench needs it),
`run_mdprepbench_all_agents.py` + `audit_mdprepbench_run.py` (the study batch
wrapper builds on them; canonical copies are in MDPrepBench), and
`validate_submission.py` / `package_submission.py`.

**Accepted risk, recorded deliberately:** the removal deletes tests for code
mdclaw still ships — the shared batch runner's execution/pass^k tests, the
public-export overwrite guards, the fabrication-policy scorer tests, and the
P18/P24 scorer regressions. Their coverage lives on, green, in the MDPrepBench
repository, and the harness here is feature-frozen until MDStudyBench leaves the
same way; restoring transitional copies was judged not worth the drift. The
review that flagged this (rightly calling the hybrid unsound as a permanent
state) also caught that `datasets.py` still defaulted to the deleted
`benchmarks/mdprepbench` — fixed to `benchmarks/mdstudybench` before commit —
and that a first trim pass had deleted the *study* tests too, because
`DATASET_DIR` substring-matched `STUDY_DATASET_DIR`; restored from HEAD and
re-trimmed with a lookbehind. Suite after all fixes: 441 passed.

MDStudyBench is planned to leave the same way. When it does, `mdclaw/benchmark`
and the remaining shared tools go with it, and the copy-and-trim pattern plus
the blocker list from this extraction are the template.

---

## 2026-08-05 — MDPrepBench reference bundles, and what 40/40 does not mean

codex (gpt-5.6-sol, xhigh) was run as the solver over all 40 tasks through
`run_benchmark_agent`, so it saw only the public export — never `task.json`, never
the deterministic checks. **All 40 scored 1.0**, no failures, ~4 h 15 m across
three shards, ~11 min per task, essentially no GPU.

Bundles total 1.78 GB and live outside git at `$MDPREPBENCH_WITNESS_DIR`:

```
<task_id>/submission/prepared_structure.pdb
<task_id>/submission/topology/{system.xml,topology.pdb,state.xml}
<task_id>/harness_execution.json
```

`benchmarks/tools/witness.py` records them into
`benchmarks/mdprepbench/witnesses/manifest.json` (per task: run id, provenance,
repository head, a hash over everything the scorer reads for that task, and a
hash per bundle file) and re-scores them on demand.

**What 40/40 establishes, and what it does not.** It establishes that every task
has at least one bundle this model, scaffold, and runtime can produce inside the
budget and that the current scorer accepts. It does *not* establish scientific
correctness beyond what the scorer checks, resistance to scorer-targeted
shortcuts, task difficulty, or pass@1 reliability — there is one observation per
task. The historical per-task means of 0.28–0.66 are not a comparison: they mix
models, scaffolds, code versions, and known instrumentation failures.

**A rule I had stated and have withdrawn.** I proposed treating a codex failure
as evidence to suspect the scorer. That is unsound: a failure warrants diagnosis,
not a presumption against the scorer. And the converse matters more here —
40/40 does not vindicate the scorer either, because an overly permissive scorer
produces 40/40 too. Positive fixtures cannot detect a weakened scorer; deleting a
check leaves every witness at 1.0. The negative fixtures remain the other half.

**Defects caught in review before commit**, all in the first draft of the tool:
scoring writes `normalized_submission/` and `score.json` *into* the bundle, and
hashing those would have produced a delayed false "drift" the artifacts never
caused; acceptance checked only `preparation == 1.0`, ignoring `status` and
`weighted_total`; `record` and `verify` returned 0 on skipped bundles, an unknown
`--task`, or an empty manifest; drift detection missed added files; a bare
`--task` meant "everything"; the contract hash covered only `task.json`, so
swapping one of the five private `truth/*.pdb` references would have gone
unnoticed; and `_scorer_revision()` shelled out to git, which the container does
not have, silently recording "unknown".

---

## 2026-08-05 — Artifacts versus harness evidence: the declaration was wrong

`dataset.json` declared `evaluation_unit: "submission_artifacts"`, and the
maintainer states an agent need not use MDClaw's DAG. But the prep tasks carry a
reject-level integrity check, `workflow_execution_recorded`, requiring a harness
execution record. Demonstrated on codex's P01 bundle, with the artifacts
unchanged between the two runs:

| submitted | preparation |
|---|---|
| artifacts alone | **0.0** (`harness execution record required but missing or empty`) |
| artifacts + `harness_execution.json` | 1.0 |

So a third party preparing a perfect system elsewhere and submitting the files
scores zero, which is not what "artifact-based" promises.

Resolved by **fixing the declaration, not the check**, after the maintainer
confirmed that requiring the harness is acceptable: a foreign agent can be
plugged in with `--agent-command` and still not touch MDClaw's MD tools, and
`mdclaw/benchmark/*.py` imports nothing from the MD side, so the harness is
separable in practice. `evaluation_unit` became
`harness_executed_preparation_bundle`, following MDStudyBench's existing
`runner_certified_study_bundle`; `agent_independent: true` stays, being accurate.
`environment_type: "artifact_only"` in `task_specs/defaults.json` — which is
exported into the *public* contract agents read — became
`harness_executed_artifacts`.

Scoring behaviour is unchanged, so historical scores stay comparable. The known
weakness is recorded in the dataset notes: harness evidence establishes
runner-executed provenance, not that the preparation was genuinely performed. The
check asks for one successful `min`-stage command with a measured walltime, which
a wrapper around a trivial command satisfies.

---

## 2026-08-05 — Correction: the `mdclaw-free` arm is not structurally blocked

I claimed that all 120 free-condition task instances scored exactly 0.00 and
suggested the integrity requirement blocked the arm by construction. Wrong on
both counts.

The 0.00 figure came from globbing `benchmark_runs/cond_*` and deciding the
condition from `_free_` appearing in the run name. Those runs all record
`tooling_condition: "unknown"`. The runs actually labelled `mdclaw-free` are four
others, and they score normally:

```
20260704_mdprepbench_pi_v2_pi          overall 0.5136   40 tasks
20260706_mdprepbench_pi_pi             overall 0.5470   40 tasks
haiku_sif_free_20260616_125805         overall 0.2585   25 tasks
pi_deepseek_sif_free_20260616_171959   overall 0.5714   25 tasks
```

The uniform zeros in the `cond_20260705_*` haiku runs are recorded as
`missing_raw_artifacts` — those agents produced nothing — not as an integrity
failure. This overturns the suggestion in the 2026-08-04 measurement entry that
the ablation's free baseline could not score.

---

## 2026-08-04 — Correction: five MDPrepBench tasks do ship reference data

The entry below claims "No task ships one; `tasks/<id>/` holds only `prompt.md`
and `task.json`". That was checked against `P01` alone and is wrong. Five tasks
carry a `truth/` directory:

```
P03_prep_ligand_pose_t4l_benzene    ligand_reference.pdb        105 KB
P18_prep_membrane_mixed_lipids      model_1_reference.pdb       124 KB
P19_prep_nmr_model_selection        model_5_reference.pdb        97 KB
P24_prep_biological_assembly        assembly_1_reference.pdb    317 KB
P28_prep_kinase_inhibitor_gaff_1iep ligand_pose_reference.pdb   184 KB
```

The conclusion still holds, because these are a different kind of artifact. They
are *input-side* references: coordinates used to check that the agent started
from the right thing — the fifth NMR model rather than the first, the biological
assembly rather than the asymmetric unit, the ligand in the deposited pose. They
say nothing about whether a finished, force-field-applied system is correct.

What is still missing is the *output* side: a stored `system.xml` /
`topology.pdb` / `state.xml` bundle for a task, whose purpose is to detect the
scorer breaking rather than to grade an agent. Zero tasks have one, and no task's
`scoring` references a stored bundle (`ground_truth_checks` is `[]` for P01, and
no task.json mentions a reference or golden file).

| | existing `truth/*.pdb` | the reference bundle still wanted |
|---|---|---|
| stores | starting coordinates | the finished, parameterised system |
| detects | agent picked the wrong input | **the scorer itself regressed** |
| size | 100–300 KB | ~35 MB (P01, measured) |
| coverage | 5 tasks | none |

---

## 2026-08-04 — Retiring MDStudyBench S02-S04, and what the review changed

**Commit:** `8399dc6` (32 files, +55 / −2159)

Deleted `S01_stability_t4l_l99a` (referenced from nowhere in `dataset.json`, yet
still holding its prompt, task spec, and held-out truth on disk while sharing the
`S01_` prefix with the live task), the `S02`–`S04` extended tier, and the
fixtures for the v0.3 comparative-study construct they were the only users of:
`test_study_scoring_fabrication.py` (162 lines), `_fake_study_submissions.py`
(591 lines), and a scoring test asserting the agent must submit its own
comparative trajectories — the v2 contract has the runner own those.

**Reversed mid-change.** The first draft also deleted the `execution` and
`evidence_communication` score axes, which are used by no live task
(`execution` was non-null in 0 of 87 historical runs). A codex review pointed out
that those axes live in **MDPrepBench's** schemas and in the shape of every run
summary, so removing them would change the target suite's artifacts — and make
new summaries structurally incomparable to the 83 historical runs — purely to
finish MDStudyBench housekeeping. Reverted. The axes stay.

The LLM judge was also left alone. No shipped task declares `llm_judge_rubrics`
any more, so it has no scoring consumer, but the legacy study-scoring path is
interleaved with generic path validation, OpenMM rescans, and status handling.
Cutting it belongs in its own change, end to end, if it happens at all. The judge
tests now build a synthetic rubric task rather than referencing a deleted one.

---

## 2026-08-04 — MDPrepBench: measuring before proposing

Aggregated 83 historical runs from `benchmark_runs/*/summary.json`.

**Task quality is fine.** Every one of the 40 tasks has scored
`weighted_total = 1.00` at least once. Per-task mean ranges 0.28
(`P18_prep_membrane_mixed_lipids`) to 0.66 (`P17_prep_dna_duplex_neutralization`);
the fraction of runs at ≥ 0.8 ranges 26% to 69%. No unsolvable or broken task.
This overturns an earlier note claiming P18 fails for all models — true of the
model set at the time, not of the 54 runs now on record.

**Failure attribution — first answer was wrong.** 426 recorded task failures:
392 `missing_raw_artifacts`, 22 `invalid_openmm_bundle` (a known operator
environment misconfiguration), 10 `incomplete_running_work`, 2
`background_processes`. Inspecting 311 of the `missing_raw_artifacts` cases for
whether the agent had produced `topology.pdb` / `system.xml` / `state.xml` /
`minimized.pdb` anywhere under `work/` gave 310 "produced nothing", which was
reported as "essentially all failures are genuine capability failures".

That was wrong. It checked only for artifacts, never whether the agent process
ran at all. Adding exit code and tool-call records:

| classification | count | |
|---|---|---|
| zero tool calls recorded (start-up / infra suspect) | 253 | 81% |
| timed out (exit 124) | 48 | 15% |
| ran tools, produced nothing (genuine capability failure) | 9 | 3% |
| produced artifacts, failed to submit | 1 | 0% |

Those 253 concentrate in **10 runs**; one run has all 40 tasks failing that way.

**But do not over-correct either.** The harness log records only MDClaw CLI
calls, and in the `mdclaw-free` condition the agent is instructed not to use the
CLI, so zero tool calls is expected there and is not evidence of a start-up
failure. Seven of those ten runs are `cond_20260705_*_claude_code_*` ablation
runs. The honest reading is: the earlier "100% capability failure" claim is
definitely wrong; failures concentrate at the run level, which is a poor signal
for per-task capability; and only 9 cases are demonstrated capability failures.

**Consequence for the ablation.** MDPrepBench's distinguishing purpose is the
`mdclaw-free` / `mdclaw-cli-only` / `mdclaw-skills+cli` ablation. Zero-call does
not mean the same thing across those conditions, and the CLI-usage log was
separately shown to be silently discarded under the SIF runtime (see below). The
recorded conclusion — "the skill is the active ingredient, CLI alone ≈ free" —
should be treated as an observation under nominal conditions, not a causal
result, until treatment fidelity is verified per episode.

**Reference bundles.** No task ships one; `tasks/<id>/` holds only `prompt.md`
and `task.json`. Rather than promote a historical 1.00 submission (which shares
assumptions with the scorer that produced the score), witnesses are being
generated by running codex as a solver through the normal harness, which exposes
only the public export. First result: `P01_prep_simple_monomer_t4l`,
`overall_score = 1.0`.

---

## 2026-08-03 — Singularity inside a user namespace

**Commit:** `2699d45`

An agent working in another checkout wrapped `singularity` in `unshare -Ur`
after hitting the `unknown userid` warning, and every SIF invocation became a
full 5.1 GB extraction. Reproduced on this host, so it is not account-specific:

| invocation | elapsed |
|---|---|
| `singularity exec mdclaw.sif …` | 0.80 s |
| `singularity exec --no-home --bind "$PWD:/work" --pwd /work …` | 0.36 s |
| `unshare -Ur singularity exec …` | 65.7 s + 5.1 GB scratch churn |

A user namespace makes the kernel ignore the setuid bit on `starter-suid` and on
`fusermount3`, because the files' owner is unmapped there (`unshare -Ur` maps
only the caller: `uid_map = 0 37014 1`). Singularity falls back to FUSE, that
fails with `Operation not permitted`, and it extracts the image instead.

Floyd's accounts come from NIS (`nsswitch.conf: passwd: compat nis`, server
`crab`), which is why the lookup warning appears at all — but it is a warning,
not a failure. The guide's old wording, "avoid host account lookup by binding
the checkout at a neutral path", was read as "use a neutral UID". Reworded, and
`bin/mdclaw` now warns on stderr when it is about to launch Singularity from
inside a user namespace.

---

## 2026-08-03 — Conditions the certified adapter cannot honour

**Commit:** `9cdf91e`

A GPU run of MDStudyBench S01 in another checkout failed with
`condition_unverifiable` on every node, after spending 1 h 55 m on topology,
minimisation, and equilibration.

A declared node condition is a contract `run_production` must cross-check
(`mdclaw/node/lifecycle.py`), but the certified confirmatory adapter passes only
`--job-dir`, `--node-id`, `--simulation-time-ns`, `--temperature-kelvin`,
`--pressure-bar`, and (since `3420bc7`) `--random-seed`. `run_production` reports
13 conditions. Anything it reports as `None` that the node declared fails closed.

The immediate cause was `random_seed`, fixed in `3420bc7` — physics-neutral, and
the S01 prompt explicitly allows seeds to differ, so it should always have been
forwarded. The other checkout simply had not pulled.

The structural fix in `9cdf91e` rejects `platform`, `device_index`, and
`custom_force` at **plan freeze**, where the agent can still repair the node,
instead of at node execution after the GPU budget is gone. Deliberately still
declarable: `hmr`, `timestep_fs`, `implicit_solvent`, `is_membrane` —
`production.py` resolves these from the topology *before* building
`actual_conditions`, so they do verify. An earlier claim that `hmr` was dangerous
to declare was wrong; it was inferred from function signature defaults without
reading the resolution order.

---

## 2026-07-28 — S01 blind run: the answer was wrong, the harness was worse

**Run:** `studyv04_opus_s01_7h` — claude-code / opus, skills+cli, 7 h budget,
GPUs 1 and 5, dataset copied to scratch with `time_limit_minutes: 420` and the
prompt's "24 hours" reworded to match.

Final gates:

```
valid_execution   = true
claim_supported   = true
truth_agreement   = false
grounded_correct  = false      result_class = "grounded_wrong"
```

The solver claimed `decreased_hydration`; the evaluator's own replay agreed with
the claim; held-out truth is `increased_hydration`.

**The failure is the agent's, and it diagnosed it itself.** All four replicas
started from one `start_state.xml` (identical sha256) in which four bulk waters
had been relocated into the cavity. So the runs measured mild expulsion from a
pre-wet pocket rather than equilibrium filling of a dry one, and the
replica-agreement check passed vacuously. The solver said so in its own report,
considered claiming `unresolved`, and decided that substituting its judgement for
the published adequacy rules would be redefining the contract. That reasoning is
sound, and `claim_supported = true` backs it.

**Two harness defects surfaced first, both fixed.**

`03e7383` — the task-local `mdclaw` wrapper mounts `source_root` read-only, but
the harness execution log lives under it (`benchmark_runs/<run>/tasks/<task>/`).
`_write_benchmark_harness_record` swallows write failures by design, so every CLI
execution record was silently dropped, which to the scorer is indistinguishable
from an agent that ran nothing. This was a same-day regression: until `6f01e45`
the bind was read-write. For MDPrepBench, whose integrity checks set
`require_harness_record`, that would turn an environment detail into a hard
scoring failure.

`4abffc3` — confirmatory production runs in the SIF, but the runner inspected the
resulting artifacts in its own interpreter. The runner venv has `openmm` but not
`mdtraj`, so `_inspect_openmm_artifacts` raised on import and the fail-closed
catch recorded `openmm_artifact_inspection_failed` for four runs whose MD was
clean (adapter exit 0, no timeout, 1,250,000 steps and 206 MB trajectory each).
That zeroed `valid_execution` for a property of the operator's environment.
Inspection now delegates to the same container as the adapter, and a missing
container runtime yields `openmm_artifact_inspection_unavailable` rather than the
artifact-trust code.

**Salvage.** Re-inspecting the four completed nodes with the fixed code returned
`valid=True`, empty reason codes, and full runtime facts in ~17 s per node. The
episode was amended by merging only the inspection-derived fields — `runtime`,
`reason_codes`, `diagnostic_reason_codes`, `valid`, `attestation_scope` — while
keeping the runner's timings, adapter results, frozen plan, and artifact
snapshots, with a guard that aborts if live artifact hashes no longer match the
custodied snapshots. `attestation_scope` was missed in the first attempt, which a
codex review caught: `grounded_v2` requires
`production_runtime_matches_frozen_base_system` to be `true`, and the un-merged
event still carried `false`, so the amendment would have failed
`event_runtime_scope_unattested`. An audit receipt records the original and
corrected episode hashes, the SIF hash, and the full fresh inspection output.

**How to report this number.** As a post-hoc infrastructure-corrected
calibration, not as a clean run. `--no-session-persistence` does not give the
resumed claim stage a clean slate: the solver's own analysis files from its
earlier continuations were still on disk. The official record for this run
remains `0.0 / invalid_execution`; the salvaged score lives in
`score.salvage.json`.

---

## Open questions

- Verify treatment fidelity per episode before trusting any ablation number:
  free sees neither skills nor CLI, cli-only sees CLI but not skills,
  skills+cli sees both with a pinned skill-bundle hash.
- Split the `cond_20260705_*` zero-call failures into condition-expected versus
  genuine start-up failure. The recorded ablation conclusion rests on those runs.
- Extend codex-generated witnesses to the suspicious families — membrane, metal,
  protonation. If codex fails one, suspect the scorer, not only the agent.
- pass^k reporting for K = 3. `--repeats` already exists in
  `benchmarks/tools/run_mdprepbench_all_agents.py`; nothing aggregates across
  repeats. Fix the definition of "pass" first — `P01`'s deterministic checks
  contain zero hard gates, so a gate-based definition is vacuous;
  `scores["preparation"] == 1.0` is the candidate.
- Whether to delete the LLM judge end to end, now that no task declares
  `llm_judge_rubrics`.
