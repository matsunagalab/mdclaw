# SST2 ベースの拡張アンサンブル実装計画（SST2 / SEUS）— 改訂版

作成: 2026-09-16（Opus 案を Claude が SST2 コードと MDClaw コードを実地調査して改訂。同日、gREST 論文で境界項の規約を確認し、Abl–abltide の PMF は存在しないとの指摘を反映）

MDClaw に単一ウォーカー型の拡張アンサンブルを 2 つ載せる。

- (A) **SST2**（Simulated Solute Tempering 2）で、solute を CDR-H3 などの部分領域に限定した
  REST2 型サンプリング。CV フリー。主力。
- (B) **SEUS**（Serial / Expanded-ensemble Umbrella Sampling）。umbrella の窓インデックスを
  1 本のトラジェクトリが歩く、expanded ensemble 版の REUS。CV が明確な用途に限定。

対象は VHH / CDR-H3 の surrogate model 用データ生成だが、既存系にも使える設計にする。

改訂の要点（Opus 案からの変更）は §0 にまとめた。以降は Opus 案と同じ章立てで、
確認できた事実と修正を織り込んである。

---

## 0. Opus 案からの主な修正点

| # | Opus 案 | 調査結果と修正 |
| --- | --- | --- |
| 1 | `-solute_sel` で残基範囲を指定 | CLI フラグは **`-select`**（`dest="solute_sel"`）。既定 `"chain A"`。選択文字列は `pdb_numpy` の文法 |
| 2 | H3 限定 solute は「新規実装不要、検証のみ」 | **実装が要る。** Amber 系力場では境界をまたぐ 1-4 対（`NonbondedForce` の exception）が **一切スケールされず**、しかもエネルギー分解では √λ 項として扱われる。`LJ14_solute_boundary` は CHARMM の `CustomBondForce` 専用。さらに副 System の PDB 再構築が solute を挟む残基間に**偽のペプチド結合**を作る（§2.4）。両方とも 2026-09-16 に fork で修正し、テストで固定した（§4） |
| 3 | SST2 は CLI 呼び出しで疎結合にできる | `bin/launch_SST2_pdb.py` は PDB から pdbfixer → 溶媒和 → 力場名で System 構築まで自前でやる。MDClaw の `system.xml + topology.pdb + state.xml` を受け取れない。さらに `REST2` クラスは **`openmm.app.ForceField` オブジェクトを必須**とし、solute 単独・溶媒単独の副 System を `forcefield.createSystem()` で作る。prmtop 由来の System には渡せない（§2.3）。→ fork 側に **System 複製方式の副 System ビルダー**と **XML 三点セットを受けるドライバ** を足し、MDClaw はそれを subprocess で呼ぶ |
| 4 | REUS 版の「収束した重み = PMF の符号反転」 | 正確には重み g_m は**窓ごとの自由エネルギー f_m**（PMF をバイアスのガウスで畳み込んだもの）。PMF そのものは保存した (ξ_t, m_t) 系列から pymbar MBAR で出す（エネルギー再評価は不要という点は正しい）。重みの安定性を収束モニタに使う点は正しい（§5） |
| 5 | 受理判定は「紙で確認」 | 確認した。現行の「隣接 2 候補をシャッフルし単一乱数 r で順に判定」は **π 不変でない**（§2.5）。Δ が λ^f に線形なので全 rung の Gibbs（独立）サンプリングに置き換えるのがコストゼロで正しい。`st.py` の ST 版も別の非標準規則（確率の高い方から判定） |
| 6 | Abl–abltide の umbrella PMF と突き合わせる | その PMF は存在しない（ユーザー確認）。SEUS の基準は MDClaw の既存 umbrella ルート + pymbar で同じ系・同じ窓から作る（§6） |
| 7 | alanine dipeptide φ は解析的比較対象になる | ならない。解析解があるのは 1 次元の人工ポテンシャル（`CustomExternalForce` の二重井戸）。alanine dipeptide は通常の umbrella + MBAR と突き合わせる |
| 8 | rung 数は REMD Temperature Generator で | SST2 自身に `tools.compute_ladder_num()`（Denschlag 2009 の式、REST2 平衡化 CSV のエネルギー揺らぎから推定）があり、launch スクリプトは経験則でその 2 倍を使う。Web ツールは不要 |
| 9 | `enhanced_ensemble` ノード型を新設 | MDClaw では steering / umbrella が **`prod` ラベル付きノードで実装済み（ノード型は増やさない）** という前例がある（memo 2026-09-06）。新ノード型は 15 箇所の登録が要る。前例に従い `prod` ノード + `sampling_method` 条件で実装する（§7） |
| 10 | seus.py を SST2 fork 内に書く | SEUS は REST2 のエネルギー分解を一切使わない。MDClaw 側（MIT）に `CustomCVForce` + 重み適応 + Gibbs 更新で書く方が、GPL 境界も再利用（`restraints.py` / `steering.py` の sidecar 方式）も素直。上流には後で移植を提案する |
| 11 | Phase 1 は 1–2 週間 | 副 System ビルダーと境界 1-4 修正、受理規則修正が乗るので **2–3 週間** |
| 12 | （記述なし） | gREST 論文（Kamiya & Sugita 2018、式 (3)）で境界項の規約を確認: (β_m/β_0)^(k/l)、l は結合/LJ/Coulomb=2、角度=3、二面角・improper=4、CMAP=8。SST2 の二面角 k/4・CMAP k/8 は**一致**。境界 1-4 は k=1, l=2 で √λ。gREST は境界の結合・角度・improper も k/l でスケールするが、SST2 は Stirnemann–Sterpone 流に proper torsion 以外の結合項をスケールしない。GENESIS 側で `param_type` を二面角 + LJ + Coulomb に絞れば同じポテンシャルになる（§2.4） |

MDClaw 側の前提（調査で確認）: パッケージ 0.6.8、OpenMM ≥ 8.5.1、pymbar 4.2、ParmEd あり、
`openmmtools` と `pdb_numpy` はなし。バイアス力は force group 31、CV の CSV と `.meta.json`
（pymbar 入力を再構成する前提で設計済み）、`steering.json` 型の sidecar、Slurm job array が既にある。
`_load_state_into_simulation` は Context の global parameter を**復元しない**（NPT↔NVT 切替のため）。

---

## 1. 背景と方針

（Opus 案のまま。要旨のみ）

- **CV フリーを主力にする。** CDR-H3 の遅い座標は主鎖二面角、torso の kink、H3–framework
  パッキング、塩橋、VHH の H3 由来 disulfide に分散しており、CV を事前に選べる保証がない。
  CV ベースだと直交方向の欠損が検出できず、surrogate 訓練データとして最悪。
- **simulated tempering 型にする。** 必要なのは MBAR で再重み付けできる重み付き平衡分布。
  単一トラジェクトリで通信不要なので、独立ラン N 本 = 独立ノード N 個になり、MDClaw の DAG と
  GB200 の MPS パッキングにそのまま乗る。Rosta & Hummer の「通信なし ST の並列は REMD と同等」が根拠。
- REUS 側は結合自由エネルギーなど CV が明確な用途に限定。

---

## 2. SST2 の現状（2026-09-16、commit `31c76a4` を clone して確認）

リポジトリ: https://github.com/samuelmurail/SST2 （GPL-2.0、star 23、CI なし）
論文: Stratmann, Moroy, Tuffery, Murail, *JCTC* **21**, 10705 (2025). https://pubs.acs.org/doi/10.1021/acs.jctc.5c00950
最新コミット `31c76a4`（2026-07-09, "implement energy recomputation script"）。

### 2.1 論文より実装が先行している（確認済み）

- `solute_index` は任意の原子インデックスリスト。CLI は `-select`（既定 `"chain A"`）。
- `-only_dihed`（gREST 流、二面角のみスケール）、`-nonbonded_RF`、`-exclude_Pro_omega` あり。
- 二面角の **k/4 分数バケット**: solute 原子数 k=1..4 ごとに `CustomTorsionForce` を分け、
  global parameter `lambda_{k}_4 = λ^(k/4)` でスケール。CMAP は k/8 の 8 バケット。
  境界 1-4 の `LJ14_solute_boundary`（√λ）は **CHARMM の `CustomBondForce` LJ14 専用**。
- CMAP 分離と NBFIX スケーリングがあり、README の「CHARMM36 未対応」は古い。
- 論文の future work に「gREST 流に二面角 / CMAP のみスケールする実装」が明記されている。
  Murail 氏自身が gREST 方向に進んでいるので、部分 solute の提案は受け入れられやすい。

論文から確認した数値: rung は小系で 10 本（λ 1.07 → 0.56、**T_ref より低温の rung がある**）、
交換間隔 2 ps、摩擦 1 ps⁻¹、重み収束 CLN025 0.5–1 µs、Trp-cage 2–10 µs、
荷電 solute では OpenMM ≥ 8.3.1 推奨（背景電荷補正の有無で交換の 99.9 % が一致）。

### 2.2 コード構成

| ファイル | 行数 | 役割 |
| --- | --- | --- |
| `src/SST2/rest2.py` | 2167 | REST2 本体。solute 定義、分数バケット、副 System、エネルギー分解、`run_rest2` |
| `src/SST2/sst2.py` | 718 | SST2 ドライバ（`SST2Reporter` が走行平均更新と交換試行を担う） |
| `src/SST2/st.py` | 532 | ST（Eastman の `SimulatedTempering` 由来） |
| `src/SST2/sst1.py`, `rest1.py` | 577, 1352 | SST1 / REST1 |
| `src/SST2/tools.py` | 1407 | 準備、`compute_ladder_num`、`get_fastest_platform_name` など |
| `src/SST2/analysis/` | — | `data_plot.py`（重み RMSD、rung 占有、交換確率）、`trajectory.py`（Opus 案の表にない） |
| `src/SST2/tests/` | 1147 | `test_rest2.py` 4 本 + `test_sst2.py` 1 本。全て**鎖全体を solute**にする。入力 1.1 MB、CPU で走る |
| `bin/` | — | `launch_SST2_pdb.py` など。`recompute_energy_traj.py` はトラジェクトリのエネルギー再計算 |

`pyproject.toml` の不備: `license = {text="GNUv2.0"}` に対し classifier が BSD、
`[project.scripts] SST2 = "SST2.__main__:main"` だが `__main__.py` が存在しない（console script が壊れている）。
依存は `openmm>=7.7`, `pdb_numpy`, `pandas`, `pdbfixer`。

### 2.3 エネルギー分解の実体（MDClaw 統合の制約になる）

PME モードでは 3 つの Context を持つ: 本体、solute 単独、溶媒単独。交換試行ごとに
座標を副 System に代入して評価し、E_pw = E_nb(全) − E_nb(solute) − E_nb(溶媒) を √λ で割る。

- 副 System は **`forcefield.createSystem(pdb_subset.topology, ..., ignoreExternalBonds=True)`** で作る。
  RF モードも `forcefield.createSystem` で全系を作り直す。**`app.ForceField` が必須。**
- PME の α は本体から読むだけで副 System に `setPMEParameters` していない。副 System の α は
  cutoff と誤差許容から決まるので、**本体と cutoff / tolerance が一致していないと分解が狂う**。
- 部分 solute では、副 System は鎖を切り出した断片の PDB から作る。切断が残基境界なら
  `ignoreExternalBonds` で内部残基テンプレートに一致するはず。残基内で切ると一致しない。

MDClaw の System は `build_amber_system`（prmtop 由来、ForceField オブジェクトなし）または
`SystemGenerator`（GAFF テンプレート込みの ForceField あり）で作られ、run 側は
`system.xml` から deserialize するだけで ForceField を再構築しない（CLAUDE.md の契約）。
したがって **本体 System から粒子部分集合を複製して副 System を作るビルダー**が要る。
複製方式なら α・grid・cutoff も本体と同一になり、上の脆さも消える。ParmEd の
`Structure[mask].createSystem()` でも代替できるが、PME パラメータ一致の保証は複製方式の方が強い。

### 2.4 部分 solute で見つかった正しさの穴

`find_solute_nb_index()` は **solute–solute の exception だけ**を集め、`update_nonbonded()` は
それらの chargeProd と ε を λ 倍する。境界をまたぐ 1-4 exception（Amber では `NonbondedForce`
の exception）は集められず、λ=1 のまま残る。一方、solute 原子の電荷は √λ 倍されるので、
意図した REST2 ポテンシャル（境界 1-4 は √λ）から外れる。さらに `compute_all_energies()` は
その未スケール項を E_pw に含めて √λ で割るので、**受理エネルギーが λ≠1 で自己矛盾する**。
量は境界 2 箇所の 1-4 対数本分で小さいが、分布を静かに歪める種類の誤り。

修正: 境界 exception を別リストに集め、chargeProd と ε を √λ 倍する。E_pw の分解は
(全 NB − solute NB − 溶媒 NB)/√λ なので、境界項が √λ でスケールされていれば追加の計上は要らない。
**2026-09-16 に fork の `mdclaw` ブランチで修正済み**（§4 の実測を参照）。

**もう 1 つの穴（同日発見）: 副 System の偽ペプチド結合。** 副 System は Modeller で切り出した
Topology を一度 PDB テキストに書き、`PDBFile` で読み直して作っていた。`PDBFile` は鎖内で
連続する残基の C–N を結合するので、solute を除いた鎖では solute を挟む両隣の残基が隣接扱いに
なり、偽のペプチド結合（テスト系では ASP1:C–GLY5:N、0.82 nm、9.6 万 kJ/mol）と 1-2 / 1-3 / 1-4 の
exception 15 個が溶媒副 System に入る。E_ww の結合項が壊れるだけでなく、E_pw に λ に依らない
定数 17.2 kJ/mol が混入し、√λ で割られて受理エネルギーが λ 依存になる。修正は PDB を経由せず
Modeller の Topology（元の結合と box を保つ）から直接 `createSystem` すること。修正済み。
VHH の H3 なら残基 94 と 103 の間に同じ偽結合ができるはずだった。

二面角の λ^(k/4) 規約は gREST 論文の式 (3) と**一致する**（添付 PDF で確認）。gREST は
E_m = λ E_uu + Σ_i λ^(k_i/l_i) E_uv,i + E_vv で、l_i は結合 / LJ / Coulomb = 2、角度 = 3、
二面角・improper = 4、CMAP = 8、k_i は solute 側の粒子数。境界 1-4 の LJ / Coulomb は k=1, l=2 で
√λ となり、上の修正案と同じ。

GENESIS gREST との違いは残る: gREST は境界をまたぐ結合（k/2）、角度（k/3）、improper（k/4）も
スケールするが、SST2 は Stirnemann & Sterpone 流に **結合項のうち proper torsion しかスケールしない**
（結合・角度・improper は E_pp⁽²⁾ として非スケール）。どちらも一貫した expanded ensemble なので
λ=1 への MBAR 再重み付けは正しいが、Phase 3 で GENESIS と突き合わせるときは GENESIS 側の
`param_type` を二面角 + LJ + Coulomb に絞り、同じポテンシャルにしてから比較する。

### 2.5 受理判定と重み（差し替えの抽象境界）

`sst2.py` は 4 段に分かれており、Opus 案の抽象境界は正しい。

1. `SST2Reporter.report()` が `compute_all_energies()` を呼び、rung ごとの走行平均を更新
2. `_attemptTemperatureChange()` が隣接 rung との log 受理確率を計算
3. 受理なら `rest2.scale_nonbonded_torsion(λ)`
4. `_compute_weight(i, j)` が Park & Pande の台形則で走行平均から w_j − w_i を出す

確認した事実:

- 受理式 Δ_ij = Σ_t (λ_i^f − λ_j^f) E_t + (λ_i^½ − λ_j^½) E_pw + (w_j − w_i)、
  kT_ref で割る。符号・単位ともに論文と一致。
- **重み配列 `_weights` は更新されず**、毎回走行平均から差分を再計算する（on-the-fly 方式）。
  `weights` プロパティは常に 0 を返し、**有効重みはログに残らない**。再開時は CSV から平均を
  再構成する。`weights=` で固定重みを渡すモードはあるが、再開との併用に穴がある
  （コード中に "TO CHANGE ! This is BAD MOKAY" とある）。
- **受理ループは詳細釣り合いを満たさない。** 隣接候補 a, b の確率 p_a, p_b をシャッフルし、
  単一の乱数 r で順に `r < p` を判定する。i→a の遷移確率は
  ½ p_a + ½ max(0, p_a − p_b) となり、もう一方の隣の p_b に依存する。端の rung では候補が
  1 つなので提案確率 1 になり、内側からの提案確率 ½ と非対称。よって結合分布 π(X, m) が不変分布に
  ならない。修正は、Δ が λ^f に線形で全 rung 分が無料で出るので、**全 rung に対する Gibbs
  （独立）サンプリング**（Chodera & Shirts 2011。Eastman の原実装と同じ）に置き換える。
  3 状態の玩具モデルで定常分布を検証するテストを付けて上流に PR する。`st.py` も同じ修正。
- 乱数は `random` モジュールで未シード。ノード再現性のためシードを通す。
- 適応的に重みが変わる期間のサンプルは厳密には平衡ではない。**適応段階（重み学習）→
  固定重み段階（生産）の 2 段プロトコル**にし、MBAR は固定段階のサンプルにかける。
  生産段階でも重み推定は続けて収束モニタに使う。

### 2.6 SEUS が SST2 より軽い理由（Opus 案のまま、正しい）

窓ごとに違うのはバイアス項だけなので必要なのは CV の値 ξ だけ。受理は
`log P(i→j) = −β[w_j(ξ) − w_i(ξ)] + (g_j − g_i)`、w は調和関数なので解析的。
エネルギー再評価も副 System も PME の扱いも不要。

---

## 3. Phase 0 — fork とライセンス（1 時間 + メール）

### ライセンス

SST2 は GPL-2.0、MDClaw は MIT。conda 環境 / SIF への同梱は GPL-2 第 2 節の mere aggregation で
問題ない。避けるのは MDClaw 本体が `import SST2` して単一プログラムになること。

**Opus 案の「CLI を呼ぶだけ」は、上流の CLI では成立しない**（§2.3）。方針を次のように改める。

- MDClaw が必要とする OpenMM レベルの接続（XML 三点セットの読込、複製方式の副 System、
  sidecar への状態書き出し）は **fork の `bin/` 配下のドライバ**として GPL で書く。
  例: `bin/sst2_from_xml.py --system system.xml --topology topology.pdb --state state.xml
  --solute-indices idx.json --ladder ladder.json --out-dir ...`
- MDClaw 側の `run_sst2` ツールは入力を整えて subprocess で呼び、成果物を DAG に記録する。
  AmberTools を呼ぶのと同じパターン。プロセス境界で分離される。
- solute 選択は MDClaw 側で mdtraj DSL（`restraints.py` と同じ）から原子インデックスに解決して
  渡す。fork のドライバは `pdb_numpy` に依存させない（MDClaw 環境にない）。SIF に SST2 を
  入れるときは `pdb_numpy` も入る（上流の依存）が、ドライバはそれを使わない。
- SEUS は MDClaw 側に MIT で書く（§5）。共通化したい重み適応（SAMS 等）は自作なので
  MIT で書き、fork へは著者としてデュアルライセンスで持ち込める。

### fork

- GitHub の本当の fork として `matsunagalab/SST2` を作る（detach しない）。
- `main` は上流同期用、作業は `mdclaw` などの feature branch。
- pip install は tag / commit 固定: `pip install git+https://github.com/matsunagalab/SST2.git@<tag>`
- 版番号は `0.0.1+mdclaw.1` 形式。`sst2.py` の `__version__` も同じ値にする。
- SIF に同梱した commit hash を記録し、`LICENSE` とクレジットをイメージ内に残す。改造版は GPL-2.0 のまま公開。

### 上流への還元（今はやらない）

上流への PR / issue / メールは、Phase 1 で修正が動いてテストが通ってから判断する。
それまでは fork の feature branch に閉じて進める。そのとき出す候補として記録だけしておく。

- 軽微: `pyproject.toml` の license classifier（BSD → GPL-2.0）、存在しない `SST2.__main__` を指す
  console script、README の CHARMM36 未対応記述。
- 実質: 部分 solute で境界 1-4 exception がスケールされない件（§2.4）、受理ループが詳細釣り合いを
  満たさない件（§2.5）。いずれも再現テストと修正を添えて出せる状態にしてから。
- 論文の future work が gREST なので、部分 solute の方向は受け入れられやすいはず。

---

## 4. Phase 1 — REST2 側の実装と検証（2–3 週間）

Opus 案では「検証のみ」だったが、§2.3–2.5 の実装が乗る。

### 実装（fork 側）

1. **複製方式の副 System ビルダー**（~200 行）。本体 System から粒子集合を指定して、粒子・拘束・
   Bond / Angle / Torsion / CMAP（全原子が集合内の項）・`NonbondedForce`（粒子と exception、
   PME α・grid・cutoff・tolerance をコピー）を持つ新 System を作る。`REST2(subsystem="copy")`
   で選べるようにし、既定は上流互換の `"forcefield"` のまま。
   検証: 上流テスト系（5awl）で両方式の副 System のエネルギー項が一致すること。
2. **境界 1-4 exception の √λ スケーリング**と frac=0.5 バケットへの計上（~40 行）。
3. **受理の Gibbs 化**とシード（`sst2.py`、`st.py`）。
4. **重みのログ出力**（各交換試行時の有効 g_m）と **固定重み段階**への切替。
5. **XML 三点セットを受けるドライバ**と sidecar（現在 rung、走行平均、試行回数、乱数状態、段階）。

### 2026-09-16 の実測（fork `mdclaw` ブランチ、2HPL テスト系、CPU）

`src/SST2/tests/test_rest2_partial_solute.py` を追加（鎖 B の残基 2–4、鎖 A の残基 11–20 を solute）。
上流 HEAD では 3 本とも失敗、修正後は 4 本とも通る。上流の 5 本は修正前後で同じ結果
（3 pass、2 fail。失敗 2 本は commit 89a4de6 で `E_solvent` が溶媒 NB を含まなくなったのに
テストが追随していないためで、部分 solute とは無関係）。

| 状態 | E_pw「非スケール」の λ=1 → 0.5 のずれ（鎖 B 残基 2–4） |
| --- | --- |
| 上流 HEAD | −1132.5 → −1020.1 kJ/mol（112 kJ/mol、10 %） |
| 境界 1-4 を √λ に | −1132.5 → −1125.4 kJ/mol（7 kJ/mol、0.6 %） |
| + 副 System を Topology 直接構築に | 相対 1e-4 以内で不変。溶媒副 System と全系の ww 部分の差 17.2 → 0.0001 kJ/mol |

鎖全体を solute にした場合（上流の使い方）は修正前後でエネルギーが変わらない（境界 exception 0、
偽結合なし）。バケット数の例: 鎖 B 残基 2–4 で 1/4: 5、2/4: 10、3/4: 11、4/4: 152、非スケール 15。

同日中に fork 側の実装 5 件を終えた（ブランチ `mdclaw`、6 コミット、テスト 19 本 pass、
上流由来の 2 本は既知の失敗のまま）:

- `SST2/subsystem.py`: 複製方式の副 System ビルダー。`REST2(subsystem="copy")` で
  ForceField なしに動く。PME 設定を丸ごと写すので、solute 副 System の NB エネルギーは
  全系で溶媒を零にした値と 0.05 kJ/mol 以内で一致し、forcefield 方式と項ごとに一致する。
- `sst2.py`: 受理を全 rung の Gibbs サンプリングに（`move="neighbor"` で対称提案の
  Metropolis も選べる）。`seed`。`weights=` の固定重みが実際に効くようにした（従来は走行平均が
  初期化されず reporter が落ち、与えた重みも受理に使われていなかった）。有効重み f_m − f_0 を
  レポート CSV に列として出力。
- `SST2/driver.py` + `bin/sst2_from_xml.py`: `system.xml + topology.pdb + state.xml` と solute
  原子番号 JSON から 1 ウォーカーを走らせる。sidecar JSON（rung、ラダー、有効重み、rung ごとの
  走行平均と回数、seed、provenance）を書き、`--restart-json` で同じウォーカーを継続。
  `--weights-json` で固定重み段階、`--pressure-bar 0` で NVT。

**実系での確認（同日、1KXV）**: キャンペーンに残っていた MDClaw の topo 成果物（prmtop 由来、
HMR 4 amu、53,716 原子、prod の `state.xml`）で `subsystem="copy"` を確認。CDR-H3 は配列から
Kabat Cys92（YYC の Cys95）と WGQG（Trp111）で挟んで残基 98–110 の 13 残基（Cys106 を含む。
1KXV は H3–CDR1 の Cys106–Cys30 ジスルフィドを持つので、最後の Cys を取ると誤る）。

| solute | 原子数 | 電荷 | 境界 exception | バケット 1/4, 2/4, 3/4, 4/4 | 非スケール量の λ ドリフト | 閉包 |
| --- | --- | --- | --- | --- | --- | --- |
| H3 のみ | 198 | +1 | 40 | 14, 12, 14, 567 | ≤ 2.4e-6 | 1e-9 |
| H3 + 5 Å 殻（Cys30 含む） | 443 | 0 | 165 | 57, 55, 55, 1219 | ≤ 1.8e-6 | 1e-9 |

GPU（GB200 1 枚、CUDA）で 5 rung（300/357/424/505/600 K）、交換 2 ps、20 ps の試走:
566 ns/day（交換と副 System 評価込み）。ウォーカーは 20 ps で 300 K から 600 K まで登った
（初期の on-the-fly 重みは大きく動くので、これは適応段階の挙動として想定内）。
solute 選択と殻の生成スクリプトは fork の `examples/mdclaw_1kxv/` に置いた。

**MDClaw 側（同日）**: `run_sst2` ツールを `mdclaw/simulation/tempering.py` に追加した。`prod` ノードとして
`@node_tool(node_type="prod")`、solute は mdtraj DSL か JSON、ラダー・交換間隔・固定重み・NVT/NPT を
引数に取り、`MDCLAW_SST2_HOME` の fork を `python -m SST2.driver` で subprocess 実行する（MDClaw は
SST2 を import しない）。成果物は `trajectory.dcd` / `energy.dat` / `state.xml` / `final_structure.pdb` /
`tempering.csv` / `tempering.json` / `solute_indices.json` / `sst2_driver.log`。`--continue-from` で
親の sidecar を自動で拾い同じウォーカーを継続、`final_step` は累積。ガードレールコード 8 個を登録。
テスト `tests/test_tempering.py`（単体 + fork ありの結合テスト: 単独実行、ノードモードで実行と継続、
失敗の封印）。スキル `skills/md-production/sst2.md`、`tool-reference.md`、`configuration.md` を更新。
Phase 4 のうちノード統合はこれで骨格ができた。残りは `analyze` 側の収束チェックと MBAR 再重み付け。
SST2 は SIF / Docker / conda に同梱する: `environment.yml` と両 Dockerfile に fork のコミット
（5590f4f、版 `0.0.1+mdclaw.1`）を pip で固定し、`MDCLAW_SST2_REVISION` で宣言。GPL-2.0 の同梱は
aggregation で、MDClaw は subprocess でしか呼ばない。`MDCLAW_SST2_HOME` は開発用オーバーライド。
イメージ `mdclaw-rikyu-arm64-cuda130-cufft121-sst2-0a15ed1cf7d1.sif`（2026-09-16、overlay ビルド、
1KXV の DAG 上で `run_sst2` の GPU 受け入れ済み）は `/data1/rkp00079` に配置済み。共有名の symlink はキャンペーン終了まで v2fix のまま。

### 検証項目

- λ=1 で素の MD と全エネルギー項が一致。上流の `check_decomposition`（`test_rest2.py`）は
  E_rebuilt = Σ λ^f E_t + E_pp⁽²⁾ + E_ww + √λ E_pw と Context のエネルギーの閉包を確認する。
  これを **部分 solute**（5awl 鎖 A の残基 3–7 など）で λ=0.6 でも通す。修正 2 の前は失敗し、
  後は通るはず。これが regression test になる。
- 境界二面角の k/4 バケット振り分けを VHH の H3 境界で手で数えて突き合わせる。
- 境界 exception の本数を手で数えて突き合わせる。
- 荷電 solute（H3 はほぼ確実に荷電）で PME と RF の受理率を比較。OpenMM は MDClaw の 8.5.1。
  可能なら H3 の選択範囲を残基単位で伸縮させて正味電荷を小さくする。
- NPT で走る（`setup_simulation` は barostat を入れる）ので、λ で系の正味電荷が変わる PME の
  背景項は体積依存になる。論文は影響 0.1 % 未満と報告。念のため NVT でも 1 本走らせて比較。
- rung 数は `tools.compute_ladder_num(..., sst2_score=True)` を REST2 平衡化 CSV に適用し、
  launch スクリプトに倣って 2 倍から始める。H3 限定なら solute 原子数 200–300 で rung は少ない。
- 上流テスト 5 本が CPU で通ることを fork の CI（GitHub Actions を追加）で機械的に確認する。

### solute の切り方（VHH 試走で比較する）

H3 だけを solute にすると、H3 内部は λ、H3–framework は √λ、framework 同士は 1 でスケールされる。
VHH の H3 は FR2（元の VL 界面、Kabat 37 / 44 / 45 / 47）に折り重なることが多く、その接触は
√λ でしか弱まらない。H3 の遅さが H3–FR2 のパッキングや H1 / H2 との接触にあるなら、
gREST の結合サイト流に「H3 + 接触残基」を solute にする方が効く。framework を冷たいままに
できるのは surrogate 用データとしては利点（native framework の中での H3 アンサンブル）。

1KXV などの VHH 1 本で次の 3 通りを比べる。いずれも残基境界で切る。**本試走は run 以外の部分も
MDClaw のスキルで組む**: `md-study` で study と `jobs/<solute 条件>` を計画し、`md-prepare` →
`md-equilibration` で各 job の `eq` まで作り、`run_sst2` を `prod` ノードとして `hpc-run` で Slurm に
投げる（seed ごとに独立ノード、`continue_from` で延長）。キャンペーンの成果物を直接使うのは
SIF 受け入れの smoke test までとし、収束の実測はスキル経由の DAG で記録する。

1. **H3 のみ**（Kabat 95–102 相当）。
2. **H3 + 接触殻**: H3 の重原子から 5 Å 以内の残基。H3 から出る disulfide があれば相手の Cys を
   必ず含める（S–S 結合・角度はどのみち非スケールだが、S–S 周りの二面角が k/4 に落ちるのを避ける）。
3. **二面角のみ**（`-only_dihed`、gREST の dihedral-only）: 電荷が変わらないので PME の
   正味電荷問題が消える。H3–framework のパッキングは弱まらないので、それが律速なら効かない。

比較指標は λ=1 の閉包チェック、rung ごとの受理率、H3 backbone dihedral の遷移数、
rung 往復回数。solute 原子数は 1 で 150–250、2 で 300–500 の見込みで、rung は 4–8 本。
正味電荷は 1・2 とも残基単位で選択を伸縮させて小さくする。

### 成果物

「H3 限定 solute が正しく構成されている」証拠と、それを恒久化した regression test。
上の 3 通りのうちどれを本番の solute 定義にするかの判断と、その根拠。
そして **重み収束にかかる時間の実測**。CLN025 0.5–1 µs、Trp-cage 2–10 µs に対し VHH H3 が
何 µs かはここで初めて分かる。10 µs を超えるなら方針再考。

---

## 5. Phase 2 — SEUS の実装（2–3 週間、MDClaw 側）

`mdclaw/simulation/expanded_ensemble.py`（仮）を MIT で書く。REST2 の分解は使わない。

**状態の表現**: 既存の `restraints.py` の `CustomCentroidBondForce` / `CustomCVForce` を流用し、
中心 ξ₀ と力定数 k を global parameter にする。窓の切替は `context.setParameter()` 2 回。

**受理判定**: 全窓の log 重み `−β w_m(ξ) + g_m` を解析的に出し、Gibbs サンプリングで次の窓を選ぶ
（隣接限定にする理由がない。窓の跳躍は自由エネルギーが許す範囲で自然に制限される）。

**重み更新**: 2 方式を選べるようにする。
- Park & Pande 型: g_{m+1} − g_m = β⟨w_{m+1} − w_m⟩ の台形則（SST2 と同じ構造）。
- **SAMS**（Tan 2017、Rao-Blackwellized 更新）: 漸近最適で窓数が多いとき速い。約 100 行で自作。
  `openmmtools` は conda 専用で環境にないので依存させない。

**成果物の定義を訂正**: 収束した g_m は窓の自由エネルギー f_m。PMF F(ξ) は保存した
(ξ_t, m_t, k, ξ₀) から **pymbar 4.2 の MBAR**（環境にある。memo 2026-08-26 の `generate_fes`
の落とし穴 3 点に注意）で出す。g_m の時間変化を収束判定に使う。

**適応段階 → 固定段階**の 2 段プロトコル、シード、sidecar は SST2 と共通の設計。

**玩具検証（CPU、コンテナ内で完結）**
1. `CustomExternalForce` の 1 次元二重井戸: PMF が解析的。窓数 10–20 で MBAR の PMF が一致すること。
2. alanine dipeptide の φ: 同じ窓で通常 umbrella（既存の `steered_X → umbrella_X` ルート）+ MBAR と一致。

---

## 6. Phase 3 — 検証（2–3 週間、最重要）

間違っていても動いてしまうのが本当のリスク。省略しない。

### SEUS 側

- 基準は MDClaw の既存 umbrella ルート（`steered_X → umbrella_X`、native の
  `CustomCentroidBondForce`）で同じ系・同じ CV・同じ窓を回し、pymbar で PMF を出して作る。
  Torch バイアス経路は 2 倍遅い（memo 2026-08-26）ので使わない。
- 系は 2 段: alanine dipeptide の φ（Phase 2 の玩具検証と共用）と、実サイズの 1 系
  （結合距離 CV のペプチド–タンパク質複合体など、GPU で数百 ns 規模）。
- 同じ CV・窓配置で SEUS を走らせ、MBAR の PMF が統計誤差内で一致するか。窓ごとの滞在時間と
  往復回数も記録し、通常 umbrella の総コストと比較する。

### REST2 側（GPU）

VHH 1 本で
- SST2（H3 限定 solute、rung × 200 ns 相当、固定重み段階のみ MBAR）
- 通常 MD 2–3 µs
- **GENESIS gREST**（同じ H3 選択。境界二面角の規約を揃える）

を回し、H3 の backbone dihedral 分布と自由エネルギー面が一致するか見る。GENESIS は独立実装なので
fork 側改造の正否の基準になる。ここが合わなければ、その後何本溜めても訓練データにならない。

---

## 7. Phase 4 — MDClaw への統合（1–2 週間）

### ノード型は増やさない

memo 2026-09-06 の前例（steering / umbrella は `prod` ラベル `steered_X → umbrella_X`、
`continue_from` で延長）に従う。新ノード型は `node/constants.py` の 5 表、`inputs.py`、
`lifecycle.py`（`continue_from` は `prod` 限定）、`prod_chain.py`、`_receipt.py`、`_envelope.py`、
`guardrail_codes.py`、可視化、study workflow、CLI 契約 golden、テスト 3 本、docs と約 15 箇所に
及び、得るものが少ない。

- `run_sst2` / `run_seus` を `@node_tool(node_type="prod")` で追加し、条件契約に
  `sampling_method: sst2 | seus`、ラダー / 窓、T_ref、交換間隔、段階（adaptive / fixed）を載せる。
- `_load_state_into_simulation` は global parameter を復元しないので、現在 rung / 窓、走行平均、
  試行回数、乱数状態は `steering.json` と同じ **sidecar JSON** に持ち、再開時に復元する。
- 積分器は `production.py` が T 固定・摩擦 1 ps⁻¹ を固定している。SST2 は積分器温度を変えず
  solute をスケールするので整合し、摩擦も論文の設定と同じ。
- 独立ラン N 本 = `prod` ノード N 個。既存の Slurm job array（`submit_array_job`）が使える。
- SST2 は fork のドライバを subprocess で呼ぶ。SEUS は MDClaw 内で直接走らせる。

### 出力

- トラジェクトリ、`energy.dat`
- `tempering.csv`（step, rung, λ, 分数バケットごとの E_t, E_pw）/ `collective_variables.csv` に窓列を追加
- `weights.json`（走行平均、試行回数、有効 g_m の履歴、段階）
- 受理率、往復回数、rung / 窓占有率（`analyze` ノードで算出）
- SEUS は `analyze` で pymbar MBAR の PMF

### 収束判定（`analyze` ノードのチェック）

- 有効重みの後半区間でのドリフト
- rung / 窓占有率の偏り（論文は低温側 rung の過剰占有を報告。重みを高温側に傾ける補正を検討）
- 往復回数
- 落ちたら `continue_from` で延長ブランチを張る。ラダーや窓の自動調整（受理率を見て rung を挿す）は
  エージェントに判断させる。

### surrogate へ渡す形

重みを落とさない。T_ref の rung のフレームを並べて学習させると biased な分布を学ぶ。
MBAR 重みで resample して非重み付きにする（ESS は減る）か、重み付き損失にするかを明示的に選ぶ。
特徴量は H3 の backbone dihedral を主軸に、framework align 後の Cartesian と接触マップを併記。

---

## 8. リスク

| リスク | 内容 | 対応 |
| --- | --- | --- |
| 重み収束が律速 | CLN025 0.5–1 µs、Trp-cage 2–10 µs。VHH H3 は未知 | Phase 1 で実測。H3 限定で rung が減る効果を見る |
| 部分 solute の実装ギャップ | 境界 1-4 exception 未スケール、副 System が ForceField 必須 | Phase 1 で修正。閉包テストを部分 solute で通す |
| 受理規則の非可逆性 | 現行ループは π 不変でない | Gibbs 化。玩具モデルで定常分布を検証 |
| 適応中のサンプルは非平衡 | on-the-fly 重み更新 | 2 段プロトコル。MBAR は固定段階のみ |
| 荷電 solute の PME | λ で正味電荷が変わり、NPT では体積依存 | 残基単位で電荷を小さく選ぶ。NVT との比較。RF との受理率比較 |
| 残基内で切る solute | 断片テンプレートが一致しない | 残基境界で切る。ドライバで検査して拒否 |
| rung 占有の偏り | 低温側 rung が過剰占有 | 重みを高温側に傾ける補正 |
| 直交する遅い自由度 | SEUS は CV 依存で χSS2 型の問題が残る | SST2（CV フリー）と併用。両方作る理由 |
| 窓の飢餓 | 初期に一部の窓が枯れる | SAMS / burn-in |
| インデックス空間の拡散 | K 窓の往復に K² 程度の遷移 | 独立ラン本数で稼ぐ。Gibbs なら隣接限定より速い |
| 静かな誤り | 符号・指数の誤りは分布だけ狂う | Phase 3 を省略しない |
| 上流の保守継続性 | 単一ラボのツール、CI なし | fork して持つ。fork に CI を足す。プロセス境界で疎結合 |
| GENESIS との規約差 | SST2 は結合・角度・improper を非スケール、gREST は k/l でスケール | GENESIS の `param_type` を二面角 + LJ + Coulomb に絞って比較 |

---

## 9. 工数と分担

| Phase | 内容 | 期間 | 主担当 |
| --- | --- | --- | --- |
| 0 | fork・ライセンス整理（上流への PR / issue は Phase 1 の後に判断） | 1 時間 | 松永 |
| 1 | 副 System ビルダー、境界 1-4、Gibbs 化、ドライバ、部分 solute 検証、重み収束の実測 | 2–3 週間 | 実装は Claude、レビューは松永、系を回すのは榎本 / 川合 |
| 2 | SEUS（MDClaw 側）、玩具検証 | 2–3 週間 | ドラフトは Claude、レビューは松永 |
| 3 | 通常 umbrella + MBAR との突き合わせ、GENESIS gREST との突き合わせ | 2–3 週間 | GPU 環境 |
| 4 | MDClaw 統合（`prod` ラベル、sidecar、analyze チェック） | 1–2 週間 | — |

合計 7–11 週間。Phase 1 の重み収束の実測次第で Phase 3 が伸びる。
Phase 1 の閉包テストと Phase 2 の玩具検証はコンテナ内 CPU で完結する。

---

## 10. 次のアクション

1. `matsunagalab/SST2` へ fork。CI（pytest、CPU）を足す。
2. 5awl で部分 solute の閉包テストを書き、失敗を確認（修正前の証拠）。
3. 複製方式の副 System ビルダーと境界 1-4 修正を書き、閉包テストを通す。
4. VHH 1 本（MDDataBench の 1KXV など、prep 済み）で solute の切り方 3 通り（H3 のみ / H3 + 接触殻 / 二面角のみ）を試走し比較。
5. 並行して SEUS の玩具検証（1 次元二重井戸）を MDClaw 側で着手。
6. SEUS の実サイズ検証系を 1 つ決め、既存 umbrella ルートで基準 PMF を作る。

---

## 参考

- SST2 リポジトリ: https://github.com/samuelmurail/SST2
- SST2 論文: https://pubs.acs.org/doi/10.1021/acs.jctc.5c00950
- SST2 トラジェクトリ: https://doi.org/10.5281/zenodo.13772542
- SST2 ドキュメント（`method.rst` に受理式と重み式）: https://sst2.readthedocs.io
- gREST: Kamiya & Sugita, *J. Chem. Phys.* **149**, 072304 (2018)
- 独立サンプリング: Chodera & Shirts, *J. Chem. Phys.* **135**, 194110 (2011)
- SAMS: Tan, *J. Comput. Graph. Stat.* **26**, 54 (2017)
- Psivant femto（OpenMM の REST2 実装）: https://github.com/Psivant/femto
- MDDataBench: https://github.com/matsunagalab/MDDataBench
- MDClaw: https://github.com/matsunagalab/mdclaw
