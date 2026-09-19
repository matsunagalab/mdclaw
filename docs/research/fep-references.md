# 1 点変異 ddG のための Hybrid-Topology FEP — 参考実装の調査と設計判断

作成: 2026-09-18〜19（`mdclaw/fep/` 実装と同時に記録）

MDClaw に「アミノ酸 1 点変異による折り畳み安定性 ddG」を alchemical FEP で求める
機能を載せた。外部の FEP フレームワーク（Perses / OpenFE / pmx）には依存せず、
OpenMM の標準 API だけで hybrid System を組む。本ページは参考にした実装・文献と、
そこから取った／捨てた設計判断の記録。エージェント手順は `skills/md-fep/`、
ツール契約は `docs/developer/tool-reference.md` の `fep/` 節。

## 1. 参考にした実装

| 実装 | 何を参考にしたか | 取らなかった点 |
|---|---|---|
| **pmx** (Gapsys, Seeliger, de Groot; GROMACS 用 hybrid residue ライブラリ) | 単一残基 hybrid topology の考え方。ダミー原子の bonded 項は全 λ で full strength に保ち、folded / unfolded 両 leg で相殺させる。ダミー側鎖は事前モデル（ライブラリ）から置く。 | 非平衡スイッチング（Crooks / BAR）は v1 では採らず、平衡 λ 窓 + MBAR に限定。 |
| **Perses** (Chodera lab; `HybridTopologyFactory`, `LambdaProtocol`) | 5 本の global parameter に分けた区分線形 λ プロトコル（電荷 off → 立体 swap → 電荷 on）。`NonbondedForce` の `addParticleParameterOffset` / `addExceptionParameterOffset` で core 原子の電荷・LJ・1-4 を線形補間。 | RJMC / 多重変異、`openmmtools` の `alchemy` 依存、REST 併用。Perses の core は MCS で広く取るが本実装は backbone + CB に限定（下記 §3）。 |
| **OpenFE / feflow** | 端点検証（λ=0/1 で hybrid のエネルギーが元の System と一致することをテストで固定する）という品質基準。 | `gufe` オブジェクトモデル、Protocol/DAG 抽象。MDClaw の DAG がその役目を持つ。 |
| **GROMACS free-energy code** (`sc-alpha`, `sc-power`, `couple-intramol`) | Beutler 型ソフトコア LJ。ダミー原子の非結合相互作用は off 端点で（ダミー同士も含めて）完全に消す、という GENESIS と共通の規約。 | `couple-intramol=no` 相当の「分子内ダミー–core 対を陽な排除に置き換える」処理は不要（電荷スケーリングと interaction group で自然に消える）。 |
| **GENESIS** (`fep_topology = hybrid`, `single`/`dual` topology の説明) | ダミー原子と rest の LJ を soft-core、電荷を線形、ダミー–ダミー相互作用は off 端点で消す規約。λ の 5 成分（`lambljA/B`, `lambelA/B`, `lambbondA/B`）を独立に持つ発想。 | GENESIS は bonded 項も λ でスケールする（`lambbond`）。本実装はダミー bonded を全 λ で on にして相殺に任せる（pmx 流）。 |
| **Beutler et al. 1994** (soft-core) | `U = λ·4ε(1/x² − 1/x), x = α(1−λ) + (r/σ)^6`（α=0.5）。 | — |
| **pymbar 4** (Shirts & Chodera) | MBAR、`timeseries.detect_equilibration` / `subsample_correlated_data`、overlap 行列による窓間重なりの診断。 | — |

## 2. 全体設計（DAG）

新しいノード型は `fep` の 1 つだけ。hybrid topology 構築は `topo` ノードの一種
(`build_hybrid_system`) として扱う。

```
source → prep → solv → topo(build_hybrid_system) → min → eq → fep(×1..K) → analyze(analyze_fep)
                                                                   ↑ fep → fep で延長
```

- `min` / `eq` は hybrid System をそのまま普通の topology として扱う。global
  parameter の既定値が状態 A（wild type）なので、λ=0 で平衡化している。
- `fep` ノードは窓ごとに eq 状態から短い局所平衡化 → サンプリングを行い、各
  サンプルで **全窓** の reduced potential を評価して `u_kn` を残す（MBAR 直行）。
- 折り畳み安定性 ddG は 2 つの job（`folded`, `unfolded` = capped tripeptide）を
  同じ mutation spec で流し、`estimate_ddg` で差を取る。

比較した「pmx / Perses / GENESIS の典型フロー」はいずれも
prep → hybrid 構築 → 溶媒和・力場 → 平衡化 → λ サンプリング → 推定 の一本道で、
MDClaw の既存スパインに `fep` を挿すだけで表現できると判断した。

## 3. Hybrid System の構築規則（`mdclaw/fep/hybrid.py`）

| 項目 | 規則 | 理由 |
|---|---|---|
| core 原子 | 変異残基の backbone (N, H, CA, HA, C, O) と CB、ならびに環境（残基外）の全原子 | MCS 的に側鎖を深く対応付けると sp3↔芳香環のような幾何の大変化を補間することになり収束が悪い。pmx も側鎖は原則ダミーで扱う。 |
| ダミー配置 | unique_new 原子は変異残基の最後の A 原子の直後に挿入（A 原子の順序は保持） | `PDBFile` は残基の原子が連続していることを前提にする。`topology.pdb` と System の index を一致させる。 |
| ダミー原子の PDB 名 | A 側と衝突しない名を `_unique_pdb_names` で付与 | LEU→PHE の CG など同名衝突の回避。真の対応は `hybrid_manifest.json`。 |
| ダミー bonded | 全 λ で full strength（force group 1 / 2） | 両 leg で同一の項になり ddG で相殺。端点検証では group 0+3 と比較。 |
| core bonded の差 | 同一なら 1 本、異なれば `Custom*Force` で `(1−fep_core)·E_A + fep_core·E_B` | `CustomCVForce` を全 bonded に使うと step ごとの inner-context コストが大きい。 |
| CMAP (ff19SB) | 変異残基の CMAP のみ `CustomCVForce` で 2 つの `CMAPTorsionForce` を混合 | CMAP は残基型依存で per-term スケール不可。 |
| 電荷 | ダミー: 基本電荷 0 + offset (`fep_elec_old`/`fep_elec_new` × q)。core: A→B を `fep_core` で線形。 | Perses と同じ。 |
| LJ（ダミー） | `NonbondedForce` では ε=0、Beutler soft-core の `CustomNonbondedForce` を (ダミー × rest) interaction group で追加。old と new は互いに見えない。 | endpoint catastrophe 回避。 |
| 1-4 exception | core–core: offset 補間。core–ダミー: 電荷は elec λ、ε は sterics λ でスケール。old–new: 0。 | 排除集合は全非結合 force で一致させる（CUDA の要件）。 |
| 分散補正 | 端点検証時のみ無効化 | 比較の一貫性。 |
| ダミー緩和 | 構築後、appearing 原子だけを状態 B で最小化（他は質量 0、制約は剛い調和結合に置換） | HPacker/PDBFixer 由来の側鎖はそのままでは歪んでいることがある（GLY→PRO で 8×10⁴ kJ/mol）。 |
| 端点検証 | λ=0 / λ=1 で hybrid（group 0+ダミー bonded+3）と元 System の差 ≤ 1 kJ/mol | OpenFE の品質基準。CUDA 単精度で 0.1 kJ/mol 程度の差は正常。 |

## 4. λ プロトコル（`mdclaw/fep/protocol.py`）

スカラー λ∈[0,1] を 5 成分に区分線形で写す。境界は λ=0.25 / 0.75。

| 区間 | 動く成分 |
|---|---|
| 0 → 0.25 | `fep_elec_old`: 1 → 0（旧側鎖の電荷 off） |
| 0.25 → 0.75 | `fep_sterics_old`: 1 → 0, `fep_sterics_new`: 0 → 1, `fep_core`: 0 → 1 |
| 0.75 → 1 | `fep_elec_new`: 0 → 1（新側鎖の電荷 on） |

既定は等間隔 21 窓。`--lambda-schedule` でスカラー列または成分 dict 列を明示できる。
中間窓で系の総電荷が非整数になりうる点は PME の中和背景に任せ、有限サイズ補正は
行わない（`build_hybrid_system` が警告を出す）。

## 5. 変異体側鎖のモデリング（`mdclaw/fep/mutant.py`）

HPacker（既存 `sidechain_packer.py` 経由）を既定、PDBFixer `applyMutations` を
フォールバックとする。変異残基だけを取り出して WT PDB のコピーに **テキストで**
splice し、他の原子は byte-identical に保つ（serial 再採番、CONECT 再マップ）。
これにより WT / MUT の `build_amber_system` 出力が変異残基以外で一致し、
`map_mutation` の環境一致チェックが通る。

## 6. 未折り畳み状態モデル（`mdclaw/fep/tripeptide.py`）

pmx の折り畳み安定性プロトコルに倣い、capped tripeptide (ACE-X(i−1)-X(i)-X(i+1)-NME)
を folded leg の prep PDB から切り出す。chain ID と残基番号を保つので同じ
`--mutation` 文字列がそのまま使える。キャップ付与は既存の
`prepare_complex --cap-termini` に任せる。

## 7. 解析（`mdclaw/fep/analysis.py`）

- 各窓の segment（`fep → fep` 延長）を連結し、segment ごとに先頭 10 % を捨てる。
- 各窓自身の reduced potential 系列で `detect_equilibration` → 統計非効率で
  subsample。
- MBAR で `Δf`、overlap 行列。隣接 overlap < 0.03 を警告。
- 位相ごとの寄与（decharge / sterics swap / recharge）を報告。

## 8. v1 の範囲外（意図的に外したもの）

- 非平衡スイッチング（pmx 流 Crooks/BAR）。
- Replica exchange / REST 併用（Perses）。
- 多重変異、挿入・欠失、非標準残基、リガンドの変換。
- 電荷変化変異の有限サイズ補正（Rocklin 型）。
- 結合親和性 ddG（complex / apo の 2 leg）。DAG 上は同じ形で載るが、v1 は
  折り畳み安定性のみを skill 化。

## 9. 検証状況

- ACE-X-NME（真空, amber14, Reference platform）で LEU→ALA, GLY→PRO, LEU→PHE
  ほか: 端点差 < 1e-3 kJ/mol（`tests/test_fep.py::TestHybridVacuum`）。
- 20 残基ペプチド A:W6A、ff19SB/OPC、8.5k 原子: 端点差 0.12 kJ/mol（CUDA）、
  3 窓 × 0.02 ns のサンプリングと `fep → fep` 延長が動作（`/tmp/fep_dev`、
  `docs/memo.md` 参照）。
- 調和振動子トイ問題で MBAR が解析解を再現（`tests/test_fep.py::TestAnalysis`）。
