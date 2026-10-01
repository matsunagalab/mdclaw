# SST2 論文用デモ: nanobody CDR-H3 の apo/holo population shift — 調査と準備

作成: 2026-09-26（ユーザー依頼「SAbDab から同一 nanobody の単量体・複合体構造を探し、CDR-H3 が
変わるものに apo だけから SST2 をかけて両方の構造がアンサンブルに出ることを示す」への調査と準備）

study: `/data1/rkp00079/rku00161/sst2-trials/nb-apo-holo`（スクリーニングの入出力とスクリプトは
`inputs/screen/`）

---

## 1. 問い

apo（単量体）nanobody の結晶構造だけから SST2 を走らせ、300 K に再重み付けしたアンサンブルに
抗原結合型（holo）の CDR-H3 構造が有限の population で現れるか。現れるなら結合は
conformational selection（population shift）で説明でき、その自由エネルギーコスト
ΔG_conf = −kT ln(P_holo-like / P_apo-like) を数値で出す。

## 2. 先行研究と新規性

- Fernández-Quintero, Liedl ら（metadynamics + MSM、数十 µs/系）: Fab の apo/holo 対で
  「結合型は apo アンサンブルに既にある」を繰り返し主張（mAbs 2019, doi:10.1080/19420862.2019.1618676
  ほか）。nanobody では GFP enhancer（IJMS 2022, doi:10.3390/ijms23105419; H3 8 残基・変化 2–2.6 Å）と
  cAb-Lys3（Biomolecules 2023, doi:10.3390/biom13020380; apo 結晶なし）。
- Higashida & Matsunaga, Life 2021（doi:10.3390/life11121428）: gREST（8 レプリカ、CHARMM36）で
  結晶の H3 を再現。apo→holo は目的外。
- 東田修論（2023、gREST 8 レプリカ、GENESIS/CHARMM-GUI）: 5 対で apo→holo を試行。
  6HEQ–4N9O と 5E03–5E5M で BOUND 構造を探索できた。5E7B–5E7F と 6APQ–4W2O はできず、
  結論「成功は H3 RMSD 2–3 Å、失敗は 5–6 Å」。修論は 5E7B の失敗を Pro108 の cis/trans 差と
  しているが、**登録座標（5E7B:A, 5E7F:A/B, altloc 含む）の全ペプチド結合は trans**
  （`inputs/screen/cis.py`）。cis は CHARMM-GUI でのモデリング段階で入った可能性がある（未確認）。
- SST2（単一レプリカの solute tempering）を nanobody / CDR-H3 に使った論文は見つからなかった
  （文献調査サブエージェント、2026-09-26）。

新規性として言えること: (i) SAbDab 全件からの系統的な apo/holo 対の抽出、(ii) 1 本の軌跡
（レプリカ交換なし、GPU 1 枚を MPS で共有）で MBAR 再重み付けした 300 K の population、
(iii) apo 出発と holo 出発の独立な収束確認、(iv) 著者が induced fit と書いた Nb.X0 での判定。

## 3. SAbDab スクリーニング

データ: SAbDab2（React 版、2026-09-26）の `/api/download/all-single-domain-summary`
（4,804 instance。VH 配列・IMGT CDR 配列・`HEAVY_ID` = 同一 VH 配列のクラスタを含む）。
旧 URL `…/sabdab/nano/summary/all/` は SPA の HTML を返すだけなので使えない。

手順（`inputs/screen/`）:

1. SD-H のみ。抗原タイプに PROTEIN / PEPTIDE / NUCLEIC を含む instance を bound、それ以外
   （空、ION、HAPTEN、SUGAR のみ）を apo とする。
2. `HEAVY_ID`（VH 配列完全一致）で束ね、apo と bound の両方を持つ 80 群（269 PDB）。
   3 CDR 配列が一致しフレームワークだけ違う群を追加で 15 群。
3. RCSB の mmCIF で、VH 配列に局所アラインして残基を対応付け、フレームワーク Cα
   （3 CDR と両端 3 残基を除く）で重ねて H3 主鎖（N, CA, C, O）の RMSD を全ペアで計算
   （`h3rmsd.py`）。H3 を局所で重ねた RMSD（ループ形状の変化）も併記。
4. 上位群について、H3 の結晶接触（対称操作込み 4 Å）、H3/鎖の B 因子比、altloc、
   主鎖二面角の差と cis/trans（`contacts.py`, `dihed.py`, `cis.py`）。

結果の全表: `inputs/screen/sabdab_apo_holo_h3_screen.csv`（91 群。中央値 ≥ 2 Å は 21 群だが、
20 Å 超などはドメインスワップ・融合体・注釈誤りの人工物を含む。例: 3OGO の "apo" は GFP
複合体結晶の別コピー、9D7U–X は融合体）。

観察:

- **apo 結晶の H3 はほぼ全例で結晶接触に入っている**（8Q78 の 8 コピー全て、8F8V、7KKJ、
  6HEQ、5E03 …）。apo の構造そのものが格子で安定化された 1 状態である可能性は常にある。
  これは「溶液中のアンサンブル」を出す MD の動機になる一方、apo 出発の構造を「apo の基底状態」
  と呼ぶのは避ける。
- cis/trans の差がある候補は無かった（上位 19 本の鎖）。

## 4. 候補

| 対象 | apo | holo | H3 長 (IMGT) | H3 RMSD apo–holo | 特徴 |
|---|---|---|---|---|---|
| **TPP-3077**（抗 properdin, llama, ヒト化 FR） | 8Q78, 1.225 Å, 8 コピー | 8Q6R, 1.9 Å, 2 コピー | 13 | 3.9–4.4 Å（局所 2.1） | 変化は E101 ψ と G104–G105 の Gly ヒンジに集中。apo 8 コピーは 2 群（互いに 1.4–1.7 Å）。extra disulfide なし。mAbs 2024, doi:10.1080/19420862.2024.2415060（著者はコピー間差を結晶パッキングに帰す） |
| **Nb.X0**（抗 afucosylated IgG1 Fc, 合成） | 8F8V, 1.81 Å, 2 コピー | 8F8W 2.71 Å ×4 + 8F8X 2.6 Å ×2（2 結晶形） | 14 | 5.3–7.1 Å（局所 2.5） | **apo の 2 コピー同士が 4.8 Å 違う**。holo は 2 結晶形 6 コピーで 0.3–0.9 Å に揃う。holo の H3 は FR2（47, 58–60）に倒れ込む。著者は induced fit と記述（Nat Commun 2023, doi:10.1038/s41467-023-38453-1）。Cys は canonical のみ |
| mNb6（SARS-CoV-2 spike） | 7KKJ, 2.05 Å | 7KKL, cryo-EM 2.85 Å | 12 | 7.4 Å（局所 4.2） | holo は cryo-EM。apo H3 に altloc（側鎖）。Science 2020 |
| Nb484（プリオン） | 6HEQ, 1.23 Å | 4KML/4N9O/6HER/6HHD（4 結晶で 0.2 Å に揃う） | 17 | 3.7 Å（局所 3.0） | **6HEQ は FR4 の WGQG が WQQG（配列違い）**。修論で gREST 成功。比較するなら apo 側を Q→G に戻す |
| CTLA-4 Nb | 5E03, 1.69 Å | 5E5M, 2.18 Å ×4 | 6 | 3.4–3.6 Å | 短い H3。修論で gREST 成功 |
| L06（抗ファージ RBP） | 5E7B, 1.1 Å | 5E7F, 2.7 Å ×3 | 19 | 9.0 Å（局所 5.2） | H3–FR2 の extra disulfide（Cys45）。修論で gREST 失敗 |
| Marburg NP sdAb | 6APQ, 1.9 Å | 4W2O, 3.2 Å ×4 | 12 | 3.8–4.0 Å | 修論で gREST 失敗。holo 低分解能 |

推奨: **主対象 TPP-3077**（最も分解能が高く、変化が Gly ヒンジに局在して SST2 で収束させやすい
「成功が期待できる例」）と **Nb.X0**（著者が induced fit と書いた例。SST2 が holo 型を出せば
新しい主張、出なければ「収束したアンサンブルに holo 型が無い」という逆方向の結論になる。
どちらでも論文になる）。gREST と直接比較したい場合は 5E03–5E5M を 3 本目に足す
（修論と同じ系、H3 6 残基で安価）。

## 5. 準備済みのもの（2026-09-26）

各対象 2 job（apo 出発 / holo 出発）。holo 出発は複合体から nanobody 鎖だけを取り出したもの
で、apo と**原子単位で同一の系**（残基範囲・残基名・プロトン化・原子順を diff で確認済み）。

| job | 出典 | 残基 | 原子数（topo） | 状態 |
|---|---|---|---|---|
| tpp3077_apo | 8Q78 chain B | 1–119 | 102,592 | eq_001 完了 |
| tpp3077_holo | 8Q6R chain A（GCH ×2 除外） | 1–119 | 63,212 | eq_001 完了 |
| nbx0_apo | 8F8V chain A | 1–120 | 56,113 | eq_001 完了 |
| nbx0_holo | 8F8W chain C | 1–120 | 61,053 | eq_001 完了 |

共通: ff19SB + OPC、立方 15 Å、0.15 M NaCl、HMR、`--protonation-method no-prediction` で
His を HIE に固定（TPP-3077: H33, H103。Nb.X0: H107）。propka に任せると apo と holo で His の
状態が変わり得て、2 本の出発点が別のハミルトニアンになるため。min/eq は 1KXV 試走と同じ
（重原子拘束 100 で最小化、NVT 1 ns + NPT 1 ns、300 K）。eq の実測: 102k 原子で 2 ns が
4.5 分（単独 ~640 ns/day, GB200）。

TPP-3077 apo の箱が大きいのは、apo 結晶の H3（Lys102）が重心から 30.7 Å 突き出ているため
（holo は最大 26 Å）。

solute（`inputs/<target>_{apo,holo}_solute_{h3,h3_shell}.json`, `make_solute_and_refs.py`）:
H3（IMGT CDR3）と、apo・holo 両結晶で H3 から 5 Å 以内に重原子を持つ残基の**和集合を残基番号で**
固定。距離で 1 構造から決めると apo 出発と holo 出発で solute が変わってしまう。

| 対象 | H3 | H3 + 殻 | 殻の残基 |
|---|---|---|---|
| TPP-3077 | 96–108（192 原子） | 564 原子 | 1, 2, 4, 27–37, 45, 53, 78, 94, 95, 109, 110 |
| Nb.X0 | 96–109（207 原子） | 538 原子 | 2–4, 24, 28, 29, 32–37, 47, 58–60, 94, 95, 110, 111 |

参照構造（`inputs/<target>_<system>_ref_{apo,holo}.pdb`）: 各系の原子数を持ち、蛋白座標だけを
apo / holo の topo 構造に置き換えたもの（`analyze_tempering --reference-pdb` 用）。
MD 系での H3 主鎖 RMSD（apo vs holo）: TPP-3077 4.09 Å、Nb.X0 5.58 Å。

## 6. 本計算のプロトコル（案）

1KXV の教訓: H3 のみの solute は温度はよく巡るが 300 K で形が変わらない。H3 + 殻は 300 K でも
入れ替わるが、5 段では交換 3–5 % で粗い。→ **H3 + 殻、9 段**で始める。

- ladder: 300, 327, 357, 389, 424, 463, 505, 550, 600 K（等比 2^(1/8)）。
  `analyze_tempering` で rung 交換 < 0.1 なら段を足す。
- 各 job 3 seed。adaptive 100 ns ブロック → `analyze_tempering`（weights_converged で）
  → fixed weights で 1 µs/run まで延長。
- 対照: apo 出発の通常 MD 300 K、3 × 1 µs（SST2 1 出発分と同じ総時間）。
- パッキング: 1 job の 3 seed + 対照を MPS で 1 GPU に（102k 原子は 4 task/GPU、56–63k は 6–8）。
- ablation（1 対象だけ、任意）: H3 のみ solute と二面角のみ（`--scale-nonbonded false`）。

見積り（1KXV 実測 0.0145 GPU-h/ns @71k 原子 6 packed を原子数で比例）:
TPP-3077 は SST2 2 出発 × 3 µs ≈ 100 GPU-h + 対照 3 µs ≈ 60 GPU-h、Nb.X0 は ≈ 70 + 35 GPU-h。
合計 ≈ 270 GPU-h（約 9 万円）。1 µs/run × 3 seed に届くまで実時間で 5–7 日。

## 7. 解析と判定

- 出発点ごとに `analyze_tempering`（重み・温度の巡り・300 K 分布の seed 間一致）。
  apo 出発と holo 出発は系の水分子数が違うので MBAR には混ぜず、**独立な 2 つの推定値として比較**
  する（これが収束の最も強い確認）。
- 観測量: フレームワーク重ね合わせ後の H3 主鎖 RMSD を apo 参照・holo 参照の両方に対して。
  300 K の 2 次元自由エネルギー地図（RMSD_apo × RMSD_holo）に、apo 結晶の全コピー（8Q78 8 本、
  8F8V 2 本）と holo 結晶の全コピーを点で重ねる。
- population: holo 型 = RMSD_holo < 1.5 Å（感度として 1.0 / 2.0 Å も）、apo 型は同様。
  ΔG_conf とその seed 間・出発点間の幅。
- 通常 MD 対照で holo 型に何回入るか（入らない / 入っても平衡しないことを示す）。
- 判定:
  - support: 両出発点で holo 型が有効フレーム ≥ 10 で現れ、population が出発点間・seed 間で
    1 kT 以内に一致し、通常 MD の同時間では到達しないか population が定まらない。
  - against: 出発点間で一致した 300 K 分布に holo 型が無い（Nb.X0 ならそれ自体が induced fit の
    裏付け）。
  - inconclusive: 出発点間で分布が 1 kT 以上ずれたまま。

## 8. ツール側の不足（着手前に決めること）

- `analyze_tempering` の参照は 1 つ（`--reference-pdb`）。2 次元地図には apo / holo 両参照の
  RMSD が同じフレーム表に要る。案: `--reference-pdb` を複数受け、`rmsd_nm_<label>` 列を出す。
  `--state-a/--state-b` も参照ラベル付き（例 `holo:<0.15`）で指定できるようにする。
  それまでは analyze を参照ごとに 2 回走らせ、`tempering_frames.csv` をフレームで結合すれば済む。
- `solvate_structure` に箱の大きさ・水分子数の指定が無いので、apo 出発と holo 出発を同一系に
  できない（上の「独立な 2 推定」で回避）。
