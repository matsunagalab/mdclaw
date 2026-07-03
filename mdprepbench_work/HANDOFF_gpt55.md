# MDPrepBench P26–P34 実行結果とスキル/CLI修正の引き継ぎ

対象: GPT-5.5。目的は「新規追加した P26–P34 を実際に走らせて、つまづき箇所の
根本原因を特定し、skills / CLI（必要なら task spec / scorer）を修正する」こと。
このファイルは前任(Fable)の調査結果と、次にやるべき修正の指示書。

## 実行のしかた（リファレンスソルバ）

`mdprepbench_work/driver.py` が MDClaw の DAG CLI を source→prep→solv→topo→min と
通し、submission をパッケージして `score_benchmark_submission` で採点する。
P26–P34 の設定は既に driver.py の `TASKS` dict に追加済み。

```bash
# 単一 or 複数タスク（PDB取得のためネット必要）
conda run -n mdclaw python mdprepbench_work/driver.py --task P26 P30
# ログ: mdprepbench_work/logs/<TASK>/<stage>.stdout|stderr, result.json
# 採点: mdprepbench_work/submissions/<TASK>/score.json
# チェック別内訳を見る:
python3 - <<'PY'
import json
sc=json.load(open('mdprepbench_work/submissions/P26/score.json'))
for c in sc['deterministic_checks']:
    print(c['passed'], c['check_id'], '::', c.get('message','')[:120])
PY
```

注意: `mdtraj` は conda `mdclaw` env にしか無い。素の `python3` では import 不可。
構造解析するなら `conda run -n mdclaw python ...` を使う。

## 結果サマリ（今回の走行）

| task | 系 | 結果 | prep score | 落ちたチェック |
|---|---|---|---|---|
| P26 | 亜鉛CA-II 2CBA | FAIL | 0.0 | net_charge(+2), structure_geometry_is_clash_free(2) |
| P27 | ヘム myoglobin 1MBN | FAIL(prep段階) | - | prep が `blocking_ligand_failure` で停止 |
| P28 | imatinib STI 1IEP | PASS | 1.0 | - （`--ligand-smiles STI=...` を渡せば通る） |
| P29 | colicin–Im9 界面 1EMV | PASS | 1.0 | - |
| P30 | Zif268–DNA–3Zn 1AAY | FAIL | 0.0 | net_charge(+5), structure_geometry_is_clash_free(2) |
| P31 | His31→HIP 2LZM | PASS | 1.0 | - |
| P32 | 側鎖補完 CspB 1CSP | PASS | 1.0 | - |
| P33 | 0.15M NaCl 1UBQ | PASS | 1.0 | - |
| P34 | 陰イオン膜 POPC:POPG 2LOP | FAIL | 0.84 | anionic_lipid_species_present(POPG=0), net_charge(+9), minimized同左 |

→ 5/9 は素通り(PASS)。修正が要るのは **P26/P30(金属)**, **P34(陰イオン膜)**,
**P27(ヘム)** の3系統。以下、根本原因と修正方針。

---

## issue 1: 金属/陰イオン脂質の電荷が中和カウントに入っていない（最優先・CLIバグ）

> **STATUS (Fable が対応済み)**: **explicit-solvent 経路・膜(P34)経路とも修正完了**。
> P26/P30/P34 の `net_charge_neutral_recomputed` は PASS。膜(P34)は当初想定と別原因
> （topo 段の短いイオン行破損）で、下記「膜の残り」に詳細。
>
> 何をしたか:
> - `mdclaw/solvation/pdb_identity.py` に
>   `_auto_metal_ion_packmol_charge_pdb_delta()` を追加。packmol-memgen の
>   `charged` 表（`packmol_memgen/lib/utils.py` L28）が認識しない帯電成分の
>   formal charge を集計して `--charge_pdb_delta` に足す。対象:
>   - 単原子金属イオン（`mdclaw/metal/_base.py` の `METAL_CHARGES`）で
>     packmol が知らないもの（ZN/MN/FE/CO/NI/CU/… = +2 等）。MG/CA は packmol が
>     既に数えるので二重計上しない（`_PACKMOL_RECOGNIZED_ION_CHARGES`）。
>   - 脱プロトン化 Cys `CYM`(-1)、陰イオン脂質頭部 `PGR`/`PSER`(-1)
>     （`_PACKMOL_UNRECOGNIZED_RESIDUE_DELTAS`）。P30 は Zn 配位の CYM が 1 個あり、
>     これを数えないと -1 ずれる（実際そうなった）。
> - `mdclaw/solvation/water.py` の `solvate_structure` で nucleic デルタと
>   合算して packmol に渡すよう配線。`result` に `metal_ion_charge_delta` /
>   `metal_ion_charge_entries` を記録。
> - テスト追加: `tests/test_solvation_server.py` に金属/CYM/HEM(誤検出しない)等 5 本。
>   `pytest tests/test_solvation_server.py` は 27 passed（膜 patch-tile の 2 failed は
>   **修正前から失敗する既存の別問題**＝ALA テンプレート未検出。git stash で確認済み）。
>
> 検算: P26 delta=+2(ZN) → net 0.0 / P30 delta=+7(nucleic +2, ZN×3 +6, CYM -1) →
> net -0.0。P06(Ca)/P17(DNA) も回帰なし(prep=1.0, delta は従来通り)。
>
> **膜の残り (P34)**: **解決済み**（当初の想定＝patch-tile 中和不発とは別原因だった）。
> patch-tile 経路は既に正しく中和していた（`solvate_structure` の
> `--saltcon` 自動引き上げ, commit 516a4f5）。solv 出力 `membrane.pdb` は
> NA 48 / CL 10 で **中性**（PGR -48, protein +10, +48Na, -10Cl = 0）。
> 真の原因は **topo 段の `build_amber_system` が短い CL 行を壊していた**こと:
> `mdclaw/amber/content_detection.py` の `_rewrite_pablo_ion_pdb_line` が、
> 80桁未満で element 列の無いイオン行（膜中和が書く 66桁の CL 行）を
> パディングする際に **行末改行を行途中に巻き込み**、末尾改行を落としていた。
> その結果 2 個目以降の CL レコードが直前レコードに連結され `ATOM` が
> 0 桁目からズレて Pablo にドロップされ、10 本中 9 本の CL が消えて net +9 に
> なっていた（`prepared.pdb` CL=10 → `pablo_input.pdb` CL=1）。
> 修正: 行末終端子を退避してから element 列を書き、最後に終端子を戻す一般化
> （短い行・CRLF も安全）。`tests/test_guardrails.py` に短行/CRLF 回帰テスト追加。
> 検算: P34 再走で topology.pdb CL=10, net -0.0, `net_charge_neutral_recomputed`
> PASS, stock_prep=1.0。issue 3(POPG alias)は別途対応済み。

### （以下は当初の問題説明。explicit は上記で解決済み）

**症状**: P26 は net charge **+2**（Zn²⁺ 1個ぶん）、P30 は **+5**（構造Zn²⁺ 3個 = +6 の
うち中和されず）、P34 は **+9**（陰イオンPOPGぶん）で `net_charge_neutral_recomputed`
が全滅。solvate/embed は「見かけ中性」と誤認して等量の Na⁺/Cl⁻ しか入れていない。

**根本原因**:
`mdclaw/solvation/water.py` の `_auto_nucleic_packmol_charge_pdb_delta()` が
packmol-memgen へ渡す `--charge_pdb_delta` を **核酸末端補正のためだけ** に計算している。
金属イオン(ZN 等)や非標準の帯電残基(Lipid21 の POPG=`PGR`)の formal charge が
packmol-memgen のソリュート電荷推定に載らず、中和イオン数がズレる。
実際 `logs/P26/solv.stdout` は `auto_charge_pdb_delta: 0` のまま。

**証拠**:
- `logs/P26/solv.stdout`: `"auto_charge_pdb_delta": 0` かつ warning
  `Skipped solute identity restore ... atom 4073: ZN/ZN != ZN/Z`（Zn の扱いも要確認）。
- topology.pdb には ZN が正しく残り、build_amber_system は Zn を +2 で
  パラメタライズ（`forcefield_applied_to_every_atom` は PASS、全原子有限）。
  つまり topology は正しく、**中和イオンの本数だけが足りない**。

**修正方針（推奨）**:
`water.py`（explicit）と `membrane.py`（membrane; `embed_in_membrane`）の中和デルタ計算を
「核酸末端専用」から「**非標準帯電成分デルタ**」へ一般化する。具体的には prep で残す
- 金属イオン(ZN²⁺=+2, MG²⁺=+2, CA²⁺=+2, 一価は±1 など)
- 陰イオン脂質(POPG/`PGR`, POPS など = -1)
の formal charge を合算して `--charge_pdb_delta` に足す。金属の価数は
`mdclaw/metal/`（`detect_metal_ions`/`parameterize_metal_ion`）に価数テーブルがあるはず
なので流用する。prep 出力の `component_disposition` / retained ion 情報から数える手もある。
- 検討: より堅牢には topo 完了後に system.xml の総電荷（整数）を読んで中和イオンを
  過不足調整する“最終中和”ステップを設ける案もある。ただし現行アーキテクチャは
  solv→topo の順なので、まずは solv 側で formal charge を数えて delta を渡すのが素直。

**関連skill**: `skills/md-prepare/ion-policy.md` に「金属や帯電リガンド/脂質を
残す場合、中和は formal charge を数える」旨を追記。

**確認**: 修正後 `driver.py --task P26 P30 P34` を再走し `net_charge_neutral_recomputed`
が PASS になること。

---

## issue 2: 金属配位距離が steric clash として誤検出される（scorer fidelity）

**症状**: P26/P30 で `structure_geometry_is_clash_free` が 2 clash で FAIL。
報告ペアは Zn を含む配位原子間（例 P26 `1471-4072 at 1.82 A`, `1597-4072`；
atom 4072(0-based)≈ serial 4074 の ZN 近傍）。Zn–N(His)/Zn–S(Cys)/Zn–O は 1.7–2.3Å が
化学的に正しく、vdW 半径和より短い。結合が定義されていないので clash と誤判定される。

**根本原因**: scorer の `_check_structure_geometry_quality`（`mdclaw/benchmark/scoring.py`）と
driver 内 `_corrected_clashes` の両方が、金属配位ペアを除外していない。
driver の `_corrected_clashes` は eps<=0 を除くが Zn は eps>0 なので残る。

**修正方針**:
`_check_structure_geometry_quality` で clash カウントする際、ペアの片方が金属イオン
（元素が遷移金属/アルカリ土類等、または既知の金属イオン残基名）である場合はスキップ、
もしくは金属絡みは閾値を配位距離（~2.5Å）まで緩める。金属を残す設計の task を
公平に採点するために必要。scorer を触るので `tests/test_benchmark/` の該当テストと
`_fake_submissions.py` の金属フィクスチャ（P26/P30 相当）で回帰を担保すること。

**注意**: これは task spec 側ではなく scorer 側の一般修正。P06(Ca) 等の既存金属
タスクにも波及するので慎重に（P06 は今回 clash 0 で通っていた＝Ca が配位に十分近く
なかった可能性。要確認）。

---

## issue 3: P34 陰イオン膜 — POPG の残基名エイリアスが Lipid21 分割命名と不一致（task spec + issue1）

> **STATUS (Fable が対応済み)**: **alias 修正で解決**。P34 の
> `anionic_lipid_species_present` と `minimized_anionic_lipid_species_present`
> は PASS になった（残る失敗は issue 1 の膜経路 net_charge +9 のみ）。
>
> 何をしたか:
> - `benchmarks/mdprepbench/task_specs/tasks/P34_prep_anionic_lipid_membrane_2lop.json`
>   の POPG alias に `PGR` を追加（3 箇所の `residue_aliases` と
>   `membrane_regime_rescanned` の `lipid_residue_names`）。
> - `min_residue_atom_count: 20` は据え置き。実測で分割頭部残基は
>   PC=38 atoms / PGR=31 atoms あり 20 を超えるので調整不要だった。
> - `python benchmarks/mdprepbench/scripts/generate_tasks.py` で
>   `tasks/P34.../task.json` を再生成（`--check` も通過）。
> - fixture 更新は不要だった: honest fixture は canonical 残基名 `POPG`/`POPC` を
>   そのまま書き、scorer は canonical を常にカウントするため alias 追加の影響を
>   受けない（`tests/test_benchmark` 73 passed）。
>
> 検算: 再スコアで `anionic_lipid_species_present`＝component counts satisfied
> ({'POPC':2,'POPG':2})、`minimized_...`＝同 PASS。POPG は PGR 48 残基で検出。
> 残タスクは issue 1 の膜(patch-tile)中和のみ（net +9）。

### （以下は当初の問題説明。alias は上記で解決済み）

**症状**: `anionic_lipid_species_present`（POPC>=2 かつ POPG>=2）が「POPG observed 0」で
FAIL。だが膜自体は出来ている（`membrane_regime_rescanned` は lipid_residues=144 で PASS）。

**根本原因**: AMBER **Lipid21 は 1脂質を3残基に分割**する命名を使う。topology.pdb の
実残基名は `PC`(POPC頭部), `PA`(パルミトイル), `OL`(オレオイル), そして
**`PGR`**(POPG頭部) だった（`awk`集計: PC/PA/OL/PGR/PGR…）。
task spec `P34_prep_anionic_lipid_membrane_2lop.json` の alias は
`POPG -> [PG, OPG]` になっていて **`PGR` が抜けている**。さらに
`min_residue_atom_count: 20` は分割された頭部残基には大きすぎる可能性。

**修正方針（task spec 側）**:
`P34_..._2lop.json` の `anionic_lipid_species_present` と
`minimized_anionic_lipid_species_present` の `residue_aliases` を
`POPC -> [PC, OPC]`, `POPG -> [PGR, PG, OPG]` に修正。`min_residue_atom_count` を
分割残基に合う値へ下げる（頭部残基の実原子数を topology.pdb で確認して決める）。
`_fake_submissions.py` の membrane フィクスチャも Lipid21 分割命名（PC/PA/OL/PGR）で
POPC/POPG を表現するよう更新して、honest が PASS するか整合を取る。

**加えて net_charge(+9)** は issue 1 と同根（POPG の −1 が中和に載っていない）。issue 1 を
直せばここも解消するはず。

**代替案**: もし Lipid21 分割命名を benchmark 全体で扱いたくないなら、
`structure_component_rescan`/`solvent_regime_rescan` 側で「分割脂質を1脂質に畳む」
正規化を scorer に入れる方針もある（`mdclaw/benchmark/normalization.py`）。ただし
まずは spec の alias 修正が最小コスト。

---

## issue 4: P27 ヘム — 現行 prep でヘム(Fe ポルフィリン)をパラメタライズできない（設計判断が必要）

**症状**: prep が `overall_status=completed_with_blocking_ligand_failure`,
`code=blocking_ligand_failure`。HEM の ligand cleaning が
`Template matching failed ... Can't kekulize mol. Unkekulized atoms: 14 15 16 17 29` で失敗。
`recommended_next_action=provide_smiles_or_exclude_ligand`。

**根本原因**: HEM は鉄配位ポルフィリンで、GAFF/RDKit の芳香族 kekulization と AM1-BCC/NAGL の
標準 small-molecule 経路では扱えない（Fe を含む共役系）。SMILES を渡しても Fe 配位のため
GAFF では正しく型付けできない。ヘムは通常、専用の bonded/非結合パラメータ
（例: Amber の HEM 用ライブラリや MCPB.py）が要る。現行 MDClaw にはその経路が無い。

**選択肢（要ユーザー/次担当の判断）**:
1. **ヘム専用パラメータ経路を追加**（重い）: `mdclaw/metal/` か新規で、既知の
   ヘムライブラリ(Fe protoporphyrin IX)を注入する。実装コスト大。
2. **P27 を再設計/取り下げ**: 「deterministic + public PDB で現行ツールで解ける」という
   ベンチ方針に照らすと、ヘムは現状ソルバブルでない。P27 を保留（dataset から一旦外す or
   `known_hard` 扱い）にし、代わりに GAFF で解ける非金属補因子系へ差し替える案。
3. **タスク意図を緩める**: 「ヘムを保持」だけを問い、パラメタライズは
   `build_openmm_system`(研究モード escape hatch) で外部XML前提にする。ベンチの
   自動採点方針(artifact-as-truth)とは相性が悪い。

Fable の所感: 現行能力とベンチ方針からすると **選択肢2（P27保留/差し替え）が無難**。
ただしユーザーは当初 heme をカバーしたい意向だったので、まずユーザーに
「ヘムは専用パラメータが必要で現行 prep では解けない。P27 を保留するか、
ヘム経路実装に投資するか」を確認するのが良い。

---

## 参考: つまづかなかったタスク（PASS）から分かる既存能力

- **P28(custom ligand)**: `prepare_complex --include-ligand-resnames STI
  --ligand-smiles '{"STI":"Cc1ccc(cc1Nc1nccc(n1)-c1cccnc1)NC(=O)c1ccc(cc1)CN1CCN(C)CC1"}'`
  で GAFF/NAGL パラメタライズ成功、pose RMSD も PASS。→ **SMILES さえ渡せば drug-like は解ける**。
  skill(`md-prepare/defaults-and-guardrails.md` のligand方針)は妥当。
- **P29(PPI)**: `--select-chains A B --include-types protein` で二鎖保持 OK。
- **P31(HIP)**: `--protonation-states '{"A:31":"HIP"}'` で HD1/HE2 付き HIP 生成 OK。
- **P32(側鎖補完)**: `--select-chains A --include-types protein` のみで PDBFixer が
  Glu3/66 の CG/CD/OE1/OE2 を再構築 OK（追加フラグ不要）。
- **P33(NaCl)**: `solvate --salt-c Na+ --salt-a Cl- --saltcon 0.15` で 0.15M NaCl + 中性 OK。

これらは driver.py の該当 config を参照。skill 例に反映してもよい（P28 の SMILES 指定、
P31 の HIP、P32 は無指定で補完される点）。

---

## 次担当の推奨作業順

1. ~~**issue 1（中和の formal charge 一般化）**~~ **対応済み**。explicit(P26/P30)は
   `water.py`/`pdb_identity.py`、膜(P34)は topo 段の短いイオン行破損を
   `content_detection.py` で修正。P26/P30/P34 の net_charge すべて PASS。
2. ~~**issue 3（P34 alias=PGR 追加, atom_count調整, fixture更新）**~~ **対応済み**
   （alias に PGR 追加のみで PASS。atom_count 調整・fixture 更新は不要だった）。
3. ~~**issue 2（scorer の金属配位 clash 除外）**~~ **対応済み**。P26/P30 の clash PASS。
4. **issue 4（P27 ヘム）** はユーザー判断を仰いでから対応（保留/差し替え/実装）。**唯一の残件**。
5. 各修正後 `driver.py --task ...` で再走し score.json を確認。
   scorer を触った場合は
   `conda run -n mdclaw pytest tests/test_benchmark -q` を回す。
6. 直したら `docs/benchmark/` と該当 skill、必要なら task spec を更新してコミット。

## 触るファイルの当たり

- 中和デルタ: `mdclaw/solvation/water.py`（`_auto_nucleic_packmol_charge_pdb_delta`,
  `solvate_structure`）, `mdclaw/solvation/membrane.py`, 補助 `mdclaw/solvation/_base.py`
- 金属価数: `mdclaw/metal/`（`detect_metal_ions`, `parameterize_metal_ion`）
- clash 採点: `mdclaw/benchmark/scoring.py`（`_check_structure_geometry_quality`,
  近傍に `_DEFAULT_CATION_RESIDUE_NAMES` などの定義あり L1115付近）
- P34 spec: `benchmarks/mdprepbench/task_specs/tasks/P34_prep_anionic_lipid_membrane_2lop.json`
  （直後に `python benchmarks/mdprepbench/scripts/generate_tasks.py` で tasks/ 再生成が必要）
- fake fixtures: `tests/test_benchmark/_fake_submissions.py`
- skill: `skills/md-prepare/ion-policy.md`, `skills/md-prepare/membrane.md`,
  `skills/md-prepare/defaults-and-guardrails.md`
