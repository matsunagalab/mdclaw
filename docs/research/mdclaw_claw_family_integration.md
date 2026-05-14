# MDClaw × ScienceClaw × OpenClaw 連携可能性

作成日: 2026-05-14

この doc は、MDClaw を「Claw 系列のエージェント群」の中でどう位置付けるか、そして具体的にどの境界で連携できるかを 3 つに絞って記録する。ScienceClaw 単独との詳細な連携計画は既存の
[`mdclaw_scienceclaw_integration_plan.md`](./mdclaw_scienceclaw_integration_plan.md) にあり、本 doc はその拡張として OpenClaw を含めた三者連携を扱う。

## 前提と用語

各エージェントの想定スコープを次のように固定して議論する。MDClaw 以外は本リポジトリ外で開発されている前提で、スコープが確定したら本 doc を更新する。

| エージェント | 想定スコープ |
|---|---|
| **MDClaw** | 原子レベル MD の study / execution / evidence。本リポジトリで管理。 |
| **ScienceClaw** | 細胞・経路スケールの推論、仮説生成、cell/pathway model の更新。MDClaw を atomistic evidence provider として呼ぶ側。 |
| **OpenClaw** | 公的データベース・実験データの統合エージェント。PDB / UniProt / AlphaFold DB / ChEMBL / cryo-EM EMDB / HDX-MS / NMR / オミクスといった外部リソースを橋渡しする層。 |

共通方針として、各エージェントの内部 DAG / 実装詳細は外に出さない。境界に置くのは次の 4 種の contract のみ:

- request（何を問うか）
- evidence report（何が分かったか、型付き metrics + 自然言語 summary + confidence + limitations）
- artifact reference（再利用可能な成果物のポインタ）
- provenance（どこから来たか）

これは既存 ScienceClaw 連携 doc と同じ原則を Claw 系列全体に拡張したもの。

## 連携可能性 3 つ

### 1. Claw 共通の request / evidence schema

**何**: MDClaw が外部に出している `evidence_type` + `summary` + `effect` + `metrics` + `confidence` + `limitations` + `provenance` の contract を、ScienceClaw / OpenClaw と共有する一段抽象化したスキーマに昇格させる。

**なぜ**: ScienceClaw が MDClaw と OpenClaw の両方から evidence を受け取り、cell/pathway model のパラメータ更新に使うとき、両者の出力が同じ形をしていれば ranking / threshold 判定 / 仮説更新ロジックを一本化できる。逆に MDClaw 専用の schema のままだと、ScienceClaw 側に MDClaw 専用 adapter を増殖させることになる。

**境界の設計**:

- 共通 envelope: `evidence_type`, `target`, `question`, `summary`, `effect`, `confidence`, `metrics`, `limitations`, `provenance`
- payload 部 (`metrics`, `effect`, `model_parameter_hints`) は evidence_type ごとにバリエーション
- evidence_type は `md_*` / `experimental_*` / `database_lookup_*` 等の prefix で source を識別

**最初の一歩**: 既存 `mdclaw/evidence_schema.py`（提案中）を `evidence_type` prefix を持つ形に切り、`evidence_type: "md_mutation_stability"` のような命名に揃える。ScienceClaw / OpenClaw 側がそれを read-only で参照できる JSON Schema ファイルを `docs/research/schemas/` に export する。

**MDClaw に閉じない設計判断**: schema の version 管理ポリシー（破壊的変更時の migration ルール）を Claw 系列で共有する。MDClaw だけで決めない。

### 2. OpenClaw → MDClaw の構造ソースバイパス

**何**: OpenClaw が外部リソース（PDB、AlphaFold DB、UniProt、Boltz-2 等の生成モデル、cryo-EM map、homology model）から候補構造を集めてきて、MDClaw の `source` node が受け取る *source bundle* の形に直接渡す。

**なぜ**: MDClaw の source node は既に「複数の候補構造を `candidates/candidate_*` に正規化し、`source_bundle.json` に rank / provenance / confidence を記録する」設計（schema v3）になっており、OpenClaw が集めた構造をここに流し込むのが最も自然な接続点。MDClaw 側で個別データソースを直接 fetch するロジックを増やさずに済む。

**境界の設計**:

- OpenClaw は MDClaw に `source_bundle.json` 相当の JSON + 候補構造ファイル群を渡す
- 必須フィールド: `candidates[*].source_type` (`pdb` / `alphafold` / `boltz2` / `cryoem` / `homology` 等), `candidates[*].rank`, `candidates[*].confidence_metric`, `candidates[*].provenance`
- 任意フィールド: `candidates[*].experimental_constraints`（cryo-EM map ファイルへの参照、HDX-MS protection factor、NMR chemical shift 等）
- MDClaw 側はそれをそのまま `source` node の artifact として書き込み、以降の prep 以降が同じ contract で動く

**最初の一歩**: `list_source_candidates` の出力 schema をそのまま「input contract」として確定し、`docs/research/schemas/source_bundle.schema.json` を公開する。OpenClaw 側が write してくれれば MDClaw は何も変えずに受け取れる、という関係にする。

**副次的な利点**: 実験データを `experimental_constraints` 経由で流し込めるので、flexible fitting や restrained MD への自然な拡張点になる（既存 `flexible_fit/` の知見が活きる）。

### 3. 三者クローズドループ: 仮説 → 原子論 → 実験突き合わせ

**何**: ScienceClaw が仮説を出し、MDClaw が原子論シミュレーションで evidence を作り、OpenClaw が公的データベース・実験データで突き合わせる、というクローズドループを 1 回 / 数回まわす運用パターン。

**なぜ**: MD は強力だが原理的に limitations が多い（サンプリング、力場、絶対値スケール）。OpenClaw 経由で「同じ系について既知の実験事実（変異の臨床効果、結合親和性、HDX-MS protection、cryo-EM 局所分解能）」を引き戻すと、MDClaw の confidence が `low_to_medium` レベルでも、ScienceClaw 側で「実験事実と矛盾しないか」のスクリーニングが入ることで意思決定の質が上がる。

**ループ例**:

```text
ScienceClaw:
  「変異 V148A は pathway P で活性を下げると予測。MDClaw に安定性確認、
    OpenClaw に既存実験データ確認を依頼」
       ↓
MDClaw:
  WT vs V148A の short replicate MD → evidence_type: md_mutation_stability
    effect: destabilizing, confidence: medium
       ↓
OpenClaw:
  ClinVar / UniProt variant DB / 文献抽出 → evidence_type: db_variant_effect
    effect: pathogenic (literature), confidence: high
       ↓
ScienceClaw:
  両者一致 → pathway P の rate parameter を down-regulate に更新
  (両者不一致 → flag として上げて人間に escalate)
```

**境界の設計**:

- ScienceClaw が両者に送る request は `target` + `question` を共通化（同じ UniProt ID + 変異座標、同じ「stability_effect」質問など）
- 両者の evidence_type は異なるが、共通 envelope (`effect.direction`, `effect.magnitude`, `confidence`) で比較可能
- ScienceClaw 側に「両 evidence が一致したときに自動更新、不一致なら escalate」というポリシーを置く

**最初の一歩**: 「mutation stability effect」という 1 つの question type を pilot として 3 者で実装する。MDClaw 側は既存 study type `wt_vs_mutant_comparison` で対応可能。ScienceClaw / OpenClaw 側で同じ `target` を投げて同じ envelope の evidence を返す部分が、最小実装。

**MDClaw に閉じない設計判断**: 不一致時のエスカレーション先（人間 / 別エージェント）と、その時に MDClaw 側に求められる追加情報（より長い MD、より多い replicate、強化サンプリング）の契約を ScienceClaw 側と擦り合わせる。

## まとめ: MDClaw 側に必要な準備

連携を本格化させる前に MDClaw 側で固めておくべきものは、3 つの提案を通じてほぼ共通している。

1. **Evidence schema の export**: `evidence_type` prefix と共通 envelope を確定し、JSON Schema として外部公開できる場所（`docs/research/schemas/`）に置く。
2. **Source bundle schema の input contract 化**: 既存の `source_bundle.json` を「外部が write し、MDClaw が read する」前提でも使える形に schema 化する。
3. **Confidence / limitations を必ず返す運用の徹底**: クローズドループでの不一致検知の前提条件。`confidence: low/medium/high` を caller が threshold 判定できる粒度で出す。

これらは ScienceClaw 単独連携でも必要だったもので、OpenClaw を加えた三者連携で初めて発生する新規要件は無い。つまり既存 ScienceClaw 連携計画を素直に進めることが、Claw 系列全体の連携基盤になる。
