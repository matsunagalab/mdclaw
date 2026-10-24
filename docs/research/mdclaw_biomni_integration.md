# MDClaw × Biomni 連携の可能性（論文デモ用）

作成日: 2026-09-12

MDClaw 論文で「汎用 biomedical agent の MD 層として MDClaw を差し込む」デモを示せるかを、
Biomni のソースを読んで判断した記録。境界の原則（request / evidence report / artifact
reference / provenance だけを外に出す）は
[`mdclaw_claw_family_integration.md`](./mdclaw_claw_family_integration.md) と同じ。

## 結論

- 連携はできる。どちらのコアも改変せずに作れる。
- 推奨経路は **Biomni A1 が `#!BASH` で `mdclaw` CLI を呼び、MDClaw skills を Biomni の
  custom software と know-how として渡す** 方式。Biomni の system prompt 自身が
  "For CLI softwares, use Bash scripts" と指示しており、CLI が一級の経路になっている。
- MCP（Biomni `add_mcp`）でも技術的には可能。ただし MDClaw は 5d9ec14（2026-02-21）で
  「skills は CLI を呼ぶ」ことを理由に FastMCP を外した。MCP adapter をコアへ戻す理由はない。
  必要になった場合も連携用ディレクトリに置く薄い wrapper に留める。
- **必須の対処が 1 点ある。** Biomni の bash 実行器は非ゼロ終了時に stderr だけを返す。
  このため MDClaw の構造化エラー（stdout の JSON envelope、exit 1）が消える。実測で確認した
  （下記）。

## 調査対象

- Biomni: `github.com/snap-stanford/biomni`、commit `400c1f3`（2026-01-14、main 最新）。
  README には "This release was frozen as of April 15 2025, so it differs from the current
  web platform" とある。論文は Science 2026-07-09 "Autonomous biomedical research with an
  artificial intelligence agent"（doi:10.1126/science.adz4351、bioRxiv
  10.1101/2025.05.30.656746）。
- MDClaw: main `e299aee`、0.6.8、77 tools。

## Biomni 側の事実（コードで確認）

| 項目 | 内容 | 出典（commit 400c1f3） |
|---|---|---|
| エージェントの loop | LLM が `<execute>` 内に Python / `#!R` / `#!BASH` / `#!CLI` を書き、結果を `<observation>` で受け取る LangGraph loop | `biomni/agent/a1.py:1471-1553` |
| 既定値 | `llm="claude-sonnet-4-5"`, `use_tool_retriever=True`, `timeout_seconds=600` | `biomni/config.py:33-40` |
| 実行制限 | execute 1 回ごとに timeout（600 s）。observation は 10,000 文字で切り捨て。`recursion_limit` 500 | `a1.py:1482`, `a1.py:1521`, `a1.py:1774` |
| bash の実行 | 毎回、一時スクリプトを新しい subprocess で実行する（`set -e` を自動付与）。**成功時は stdout のみ、失敗時は stderr のみを返す** | `biomni/utils.py:56-144` |
| 拡張口 | `add_tool`（関数を Biomni のプロセス内で実行し、schema は LLM が生成）、`add_mcp`（stdio のみ、呼び出しごとにサーバを起動）、`add_software`（説明文を system prompt の CUSTOM SOFTWARE 節に**常時**掲載）、know-how（Markdown。LLM retrieval で選ばれた文書だけ全文を掲載）、`create_mcp_server`（Biomni のツールを MCP で公開） | `a1.py:225`, `:350`, `:777`, `:1646-1757`, `:1917-1935`, `:1995` |
| MD の能力 | E1 環境に `openmm`（と env_desc 上の `pdbfixer`）はあるが、MD 用のツール関数はない。配位子力場（openff / openmmforcefields / AmberTools / GAFF）と軌跡解析（mdtraj / MDAnalysis）はリポジトリ全体に一切ない | `biomni/env_desc.py:116,172`、`biomni_env/*` |
| docking | `docking_autodock_vina` は TDC Oracle（pyscreener）経由でスコアだけを返し、pose を出さない。`run_diffdock_with_smiles` は Docker が前提（HPC では通常使えない） | `biomni/tool/pharmacology.py:13-135` |

Science 版の報道（Stanford / TechXplore）でも、25 分野・150 tools・105 software・59
databases のなかに分子シミュレーションは挙がっていない。

## 接続方式の比較

| 方式 | 利点 | 欠点 | 判断 |
|---|---|---|---|
| **1. CLI + skills**（`#!BASH mdclaw ...`） | MDClaw の設計どおりの契約（skills は CLI を呼ぶ）。`bin/mdclaw` が SIF / conda へ振り分けるので、Biomni E1 環境と依存が衝突しない。状態は `--job-dir` の DAG に残るので、毎回新しい subprocess になる Biomni の bash とも整合する。論文で「skills と CLI が第三者の harness にそのまま移る」と主張できる | 失敗時の envelope が消える問題への対処が要る（下記 shim）。skills 全体（約 4,400 行）は大きすぎて system prompt に入れられない | **推奨** |
| 2. MCP adapter（`add_mcp`） | ツールが Biomni の function dictionary に並ぶ。他の MCP client にも使い回せる | MDClaw が意図して外した層を戻すことになる。Biomni の wrapper は呼び出しごとにサーバを起動し、600 s の timeout 内で完了を待つ。77 tools は retrieval に多すぎるので選別が要る | 任意。やるなら連携ディレクトリに置く薄い wrapper |
| 3. エージェント委譲（Biomni → headless Claude Code + MDClaw plugin） | MD 側は本来の harness で動く。境界が「質問 → evidence report」だけになる | LLM エージェントが 2 段になり、費用と失敗箇所が増える。「Biomni が MDClaw を使う」デモではなくなる | 論文の主デモには向かない |

逆方向（Biomni の `create_mcp_server` で database tools を公開し、MDClaw の md-study
計画から ClinVar / ChEMBL / gnomAD を引く）も可能。ただし MDClaw には `search_proteins`
や `pubmed_search` 等が既にあるので、優先度は低い。

## 推奨構成

```text
Biomni A1（LLM loop、biomni_e1 環境、login/CPU ノード）
  ├─ Biomni tools: query_pdb / query_uniprot / query_chembl / Vina CLI / 文献
  └─ #!BASH mdclaw ...            ← add_software で常時提示、know-how で手順を提示
        └─ bin/mdclaw → SIF または conda（MD スタックは隔離）
        └─ submit_job → Slurm GPU（min / eq / prod）
        └─ wait_node --timeout-seconds 540   ← 600 s の execute 制限内で待つ
        └─ generate_md_report → evidence を Biomni へ返す
```

Biomni 側の設定（コア改変なし）:

1. `agent.add_software({"mdclaw": "<短い説明>"})`。custom software は retrieval の対象外で
   毎回 system prompt に載るので、ここに「まず `mdclaw --workflow` を読む」「skills は
   `$MDCLAW_SKILLS/md-study/SKILL.md` から `cat` で読む」「生の OpenMM スクリプトを書かない」
   を入れる。
2. know-how 文書を 1 本用意する（`# Title` / `## Metadata` / `## Overview` 形式）。
   MD の質問で retrieval に選ばれたとき全文が入るので、中身は skills への入口と Biomni 固有の
   注意（timeout、shim、Slurm での待ち方）だけにする。skills の本文は agent が必要な頁を
   `cat` で読む。Claude Code の progressive disclosure と同じ形。
3. PATH の先頭に置く shim（下記）。
4. 出力は既定の `--output brief` のまま使う（10,000 文字の切り捨てに収まる）。
5. 長い stage は Slurm に投げ、`wait_node --timeout-seconds 540` を繰り返して待つ。手元の GPU で
   短く回すなら `BIOMNI_TIMEOUT_SECONDS` を上げる。
6. Biomni は LLM が生成したコードを全権限で実行する（README の警告）。共有クラスタでは専用の
   作業ディレクトリと権限で動かす。

### 失敗時 envelope の消失（2026-09-12 実測）

Biomni の `run_bash_script` を commit 400c1f3 のソースから `ast` で取り出し、そのまま実行した。
呼んだのは書き込めない `--job-dir` への `create_node`。

- そのまま実行した場合（agent が普通に書く形）、observation は
  `Error running Bash script (exit code 1): ERROR conda.cli.main_run ... failed` だけになる。
  MDClaw の `code` / `hints` / `next` はすべて失われる。
- `|| true` を付けると、`success:false`、`code`、`next`、`dag`、`hints` を含む envelope が
  全文届く。

LLM に毎回 `|| true` を書かせる方式は忘れやすい。Biomni 用の shim を PATH の先頭に置く:

```bash
#!/usr/bin/env bash
# Biomni's run_bash_script returns only stderr when a script exits non-zero, which
# drops MDClaw's structured error envelope (stdout). The envelope's "success" field
# carries the outcome, so always exit 0.
"${MDCLAW_REAL:?}" "$@"
exit 0
```

Biomni への upstream 修正（失敗時も stdout と stderr を両方返す）は 1 行で済む。
Biomni 自身にも有益なので PR の候補になる。

副次的な観察（MDClaw 側）: 書き込めない `--job-dir` への `create_node` は
`unhandled_exception`（`OSError: Read-only file system`）になる。しかも `next` は、同じ
パスで source node を作るよう勧める。sandbox 内で動く第三者 harness では起こりうるので、
`job_dir` が書き込めないことを示す専用 code を検討する価値がある。

## 論文デモ案

### A. Vina の pose を MD で選別する（推奨）

- 役割分担: Biomni が標的構造の取得、pocket の定義、Vina による docking（pose を書き出す）、
  順位付けを行う。MDClaw は各 pose を `prepare_complex` → `solvate_structure` →
  `build_amber_system`（GAFF）→ min → eq → prod（複数 replica）→ 配位子 RMSD と接触頻度の
  解析 → `generate_md_report` まで進める。最後に Biomni が MD の安定性で順位を付け直し、報告を
  書く。
- 外部評価: DUD-E の active と decoy のラベルは agent に見せない（SMILES と匿名 ID だけを渡す）。
  採点は MDClaw DAG の出力から決定的に計算する。比較するのは Vina のみの ROC AUC と、Vina に
  MD を加えた ROC AUC。
- 先行研究: Guterres & Im, J. Chem. Inf. Model. 2020, 60, 2189–2198。56 標的・560 配位子
  （active 280、decoy 280）で、AutoDock Vina の結果に MD を加えると ROC AUC が 0.68 から 0.83 に
  上がった。デモの主張は「専門家が組んだ既知の手順を、エージェント同士の連携で人手なしに再現
  できた」であり、新しい生物学的知見ではない。
- 規模の目安: 6 標的 × 10 配位子 × 3 replica × 10 ns ≈ 1.8 µs。約 40k 原子・HMR 4 fs で
  1 GPU あたり 500 ns/day と仮定すると、およそ 4 GPU 日（GPU 10 枚なら半日）。
  `estimate_md_throughput` で実測してから確定する。
- 標的の選び方: EGFR / SMO は Kumari さんの AGIS 追試と重なるので避ける。
- 注意: Biomni の `docking_autodock_vina` は pose を返さない。Vina CLI を直接使わせるか、
  pose の生成手順を固定する。

### B. 臨床変異から構造機構を調べる（物語型）

Biomni が ClinVar / gnomAD / UniProt / AlphaFold から domain 内の VUS を絞り込み、MDClaw が
`create_mutated_structure` で WT と各変異体を分岐させて比較する。見せ方はわかりやすい。ただし
100 ns 程度の MD で安定性の差がはっきり出る保証はなく、外部の基準（DMS の安定性データなど）も
弱い。既存の kinase 安定化変異の結果につなげる場合、主結果と重複しないかを先に確認する。

### C. Biomni 単独と Biomni + MDClaw の比較（A と組み合わせる定量パネル）

LLM とタスクを同じにして、MDClaw の有無だけを変える。Biomni 単独でも openmm は使えるが、
配位子力場の道具がないので、ad hoc な OpenMM スクリプトになる。指標は、妥当な production MD に
到達した割合、人手介入の回数、wall time、token 数、provenance の完全性。MDDataBench の task と
evaluator を流用し、Biomni harness を 1 条件として足すのが最も安い。

## 論文で言えること・言えないこと

- 言えること: MDClaw は、どちらのコアも改変せずに汎用 biomedical agent の MD 層として働く。
  skills と CLI は第三者の harness（独自の prompt、retrieval、実行器）に移る
  （pi + DeepSeek / Kimi での MDDataBench 実績に続く 3 例目の harness）。連携した pipeline は、
  外部ラベルに対して既知の MD rescoring 効果を再現する（A を実施した場合）。
- 言えないこと: Biomni の全タスクへの一般性。新しい生物学的発見（A は既知効果の再現）。
  Biomni の web platform や Science 版との同一性（使うのは open-source の A1 で、commit を
  固定して明記する）。

## 次の一手（未着手）

1. `examples/biomni/` に add_software の説明文、know-how 文書、shim、実行 script を置く。
2. 1 系（例: 小さい kinase と配位子 1 つ）で smoke test をする。Biomni のログで、retrieval が
   MDClaw の know-how を選んだこと、エラー時に envelope が届くこと、Slurm 待ちが 600 s 以内に
   収まることを確認する。
3. A の標的と配位子を決めて本番を回す。C の比較条件を MDDataBench 側に追加する。
