# Agent-Orchestrated Molecular Dynamics — Research Notes

**作成日**: 2026-04-19
**著者**: 松永康佑 (RIKEN) × Claude (Opus 4.7)
**目的**: 別マシンで議論を継続するための文脈復元ドキュメント。
冒頭の「Continuation prompt」をコピペすれば、別マシンの Claude が続きから議論できる。

> **Legacy note (2026-09 update)**: 本メモが書かれた時点では `build_amber_system` が
> `tleap` を使い `parm7` / `rst7` を出力していた。現在はその経路は
> openmmforcefields-unification refactor で書き換えられ、
> `openmmforcefields.SystemGenerator` + OpenFF Pablo を介して
> `system.xml` + `topology.pdb` + `state.xml` の modern artifact triple を
> 出すようになっている。DAG / node 設計、ancestor-based artifact resolution、
> failure-as-data といったメタな議論は現行設計に概ね引き継がれているが、
> 具体的なファイル名・ツール名は現行コード（`AGENTS.md` 等）を正とする。

---

## 0. Continuation prompt（他マシンの Claude 向け）

```
@docs/research/agent_orchestrated_md.md を読んで、議論の続きをしたい。
前回の到達点は「sub-agent × REMD を agent orchestration の
科学計算ベンチマークとして立てる」ところ。
次の一歩として、(a) 最小プロトタイプのスコープ確定 / (b) DAG 緩和の Level 1-2
（エッジのロール型付け + 入力解決の宣言化）の mdclaw への先行実装 /
(c) 論文構成のドラフト、のどれから進めるかを一緒に決めたい。
```

---

## 1. Context — 出発点となった問題提起

SNS で観測された議論:

- ThoughtWorks の Technology Radar で **LangGraph が Adapt → Trial に格下げ** された
- 批判の骨子: 「エージェント状態をローカルライブラリのグラフ構造として持ってしまうと、
  そこで**耐久実行**も**他ワークフローへの展開**も死ぬ。スケールするマルチエージェントが
  最初から無理」
- より一般化すると: 「LangGraph / LangChain / deepagents の強みは自分でエージェントの
  ステート管理をしているところだが、まさにそのアーキテクチャこそが**スケーリングや耐性実行に
  致命的なインコンピテンシー**を生む。特定アーキテクチャにコミットすることで難しくなる展開が
  あるという典型例」

この批判は mdclaw（MD 計算を DAG でモデリングするフレームワーク）にも刺さるのか？ というのが
議論の出発点。

---

## 2. Position — mdclaw の DAG は別物

**結論**: 種類が違う硬直であり、LangGraph 批判は部分的にしか当たらない。

| 観点 | LangGraph 型 | mdclaw 型 |
|---|---|---|
| グラフが表現するもの | エージェント思考・制御フロー | **計算アーティファクトの系譜** |
| 状態の所在 | ライブラリ内メモリ | **ファイルシステム永続** (`node.json`, `artifacts/`, `events/`) |
| ノード追加 | コード変更（グラフ定義） | `create_node` 呼び出しだけ（エージェントが動的に生やす） |
| エージェント | グラフ内部 | **グラフの外側**（Claude が外から DAG を操作） |
| 耐久実行 | 苦しい | ネイティブ（ディスク再入で任意時点再開可） |

3 つの批判点に対する回答:

1. **耐久実行**: むしろ強い（DB 不要、ファイルシステムが state）
2. **他ワークフローへの展開**: DAG はただのファイルツリー + JSON なので外部ツールから読める
3. **マルチエージェントスケール**: `_lock.py` で同一 `job_dir` を複数プロセスが触れる設計

---

## 3. Architectural commitments in mdclaw — 本当に硬い部分

ただし mdclaw にも「アーキテクチャ・コミットメント」は確実にある。具体的には 2 箇所:

### 3.1 `NODE_TYPES` 列挙 — `mdclaw/_node.py:36`

```python
NODE_TYPES = frozenset({"source", "prep", "solv", "topo", "eq", "prod", "analyze"})
```

ここにない型は `create_node` が弾く（`_node.py:148-152`）。また型ごとのバリデーション
（source は parent 不可、prep は source 祖先が 1 つだけ、continue_from は prod 専用）が
ハードコードされている。

### 3.2 `resolve_node_inputs` の型別ロジック — `mdclaw/_node.py:680-813`

各 `node_type` ごとに「どの祖先のどの artifact を何という引数名で渡すか」が if/elif で分岐:

```python
elif node_type == "topo":
    v = find_ancestor_artifact(job_dir, node_id, "solv", "solvated_pdb")
    lp = find_ancestor_artifact(job_dir, node_id, "prep", "ligand_params")
    # ...
elif node_type == "eq":
    p7 = find_ancestor_artifact(job_dir, node_id, "topo", "parm7")
    # ...
elif node_type == "prod":
    # prod は特別扱い: continued_from の厳密解決 + eq/prod 祖先の checkpoint fallback
```

**つまり「ノード型 = データフロー契約」が 1:1 で紐付いている**。これが mdclaw の硬直の本体。

### 3.3 何が easy / 何が hard か

| 変更タイプ | 必要な作業 |
|---|---|
| パラメータのバリエーション（温度、圧力、restraint） | **コード変更不要**（`conditions` に入れるだけ） |
| 同じフローで変種を増やす | **コード変更不要**（既存型で分岐） |
| 新しい**ステップ型**（fep_window など） | `NODE_TYPES` + `resolve_node_inputs` に追記、新 tool 追加 |
| 新しい**データフロー形状**（多対一、横方向交換） | `find_ancestor_artifact`（単一祖先型を上に辿る BFS）の前提ごと書き直し |
| 非 DAG 的な構造（REMD の相互交換） | DAG モデルを捨てる必要あり |

---

## 4. DAG 緩和の 5 Levels — 次の研究の足場

補足（2026-05）: 複数 source root / 複数 physical system を 1 つの
`job_dir` に入れる方向ではなく、`job_dir` は source-bundle execution
unit として維持し、`study_dir` が複数 `job_dir` を束ねる方針に寄せる。
source bundle の中には NMR model や生成構造 ensemble のような複数候補を
入れられ、`prep` が 1 つを選んで physical system にする。これにより普通の
MD研究での単純さを保ちながら、agentic campaign や AI for Science 連携を
上位レイヤで扱える。

> **問い**: アーティファクト系譜 DAG の最小限の拡張で、FEP / REMD / アンサンブル解析 /
> 適応的サンプリングをカバーできるか？ 制御フロー DAG に退化せずに。

### Level 1: エッジのロール型付け（ほぼタダ）

今は `parent_node_ids: list[str]` が未ラベルの集合。これを
`parents: [{node_id, role}]` にするだけで:

- `analyze` ノード: N 個の prod を `role="sample"` で受け取る
- FEP の差分解析: N 個の fep_window を `role="lambda_endpoint"` で受ける
- bias-source / base-topo の区別ができる

`resolve_node_inputs` の if/elif が**型スキーマ宣言**に昇格する素地になる。

### Level 2: 入力解決の宣言化（中程度）

`_node.py:680-813` のハードコード分岐を、各 node_type が**自分で宣言**する形に:

```python
TOPO_SCHEMA = {
    "pdb_file":        from_ancestor("solv",  "solvated_pdb"),
    "ligand_params":   from_ancestor("prep",  "ligand_params", optional=True),
    "box_dimensions":  from_ancestor("solv",  "box_dimensions", loader=json),
}
```

ユーザが**コード変更せずに**新しい node_type をプラグインとして登録できる。Snakemake /
Nextflow の `input:` ブロックが近い発想。

### Level 3: fan-in アグリゲータ（小〜中）

Umbrella / FEP / bootstrap 解析はここで落ちる。必要なのは:

- `find_ancestors_of_type(node_id, type)` — 単数ではなく**複数**取る BFS
- `aggregate_artifacts(nodes, key)` — 横断で集める

umbrella の WHAM、FEP の BAR/MBAR、REUS の解析まで **DAG を壊さず**書ける。

### Level 4: 階層的コンポジション（中〜大・面白い）

FEP 実験全体 = 1 ノード（外から見ると）、内部は fep_window の DAG。

- 外の DAG では `fep_experiment → analyze` と素直に直列
- 内部の細かい並列性は外に漏らさない
- Bazel の `genrule` 内の hermetic 実行、あるいは sub-DAG / nested workflow に相当
- カテゴリー論的には**函手** — sub-DAG を outer graph の 1 点に潰して見る操作

### Level 5: REMD のような coupled state（ここが本物の研究）

選択肢:

- **(a) 時間離散化**: exchange 周期ごとにノードを切る。N replica × K exchange = N×K ノード。
  グラフは DAG のままで、交換エッジが replica 間を渡る。コスト: ノード数爆発。
- **(b) Group node + event stream**: replica 群を 1 ノードに潰し、交換は `events/` の
  append-only ログで記録。DAG は綺麗だが**ノード内部が非透明**。
- **(c) 同期点ノード**: M 交換ごとに "barrier" ノードを入れ、その間は独立なサブ DAG。
  粒度パラメータで (a)(b) を連続的に繋ぐ中間案。

**(c) が一番バランス良さそうで、粒度を自由に選べるのが研究的に面白い**（精度 vs メタデータ
コストのトレードオフ曲線を引ける）。

---

## 5. Sub-Agent × REMD 研究構想 — 最重要セクション

> 「1 replica = 1 sub-agent」と捉えると、REMD は MD の問題ではなく
> **multi-agent orchestration** の問題になる。

### 5.1 エージェント判断が効くレイヤ

- **inner loop**（1 ps ごとの積分）: 決定論的、エージェント不要
- **meta loop**（チェックポイント単位〜交換周期）: **ここが全部エージェント仕事**

従来の REMD framework（GROMACS `-multidir`, OpenMM replica）は meta loop も
ハードコードだった。そこを **judgment に置き換える**のが新規性の核。

### 5.2 各エージェントが担う役割

#### Replica worker agent

- **局所的な失敗復旧**: NaN 爆発 → 直近 trajectory を読む → clash 箇所特定 →
  少し前の checkpoint + 違う seed で再開 → coordinator に事象を報告
- **局所的な品質判断**: 「この温度で 5 ns やったが RMSD が安定しない、
  thermostat coupling 弱めたい」
- **rare event 検出**: CV をリアルタイム監視し「今珍しい状態にいる、forking 要請」
- **GPU / platform 切替**: OOM → CPU 落ちせずに別 GPU に migrate

#### Coordinator agent

- **温度ラダーの動的調整**: `rep_005 ↔ rep_006` 交換受理率が 5% まで落ちた →
  中間温度 `rep_005.5` を spawn、ラダー再構成
- **異種 enhanced sampling の混在**: rep_000 は tempering、rep_001 は metadynamics、
  rep_002 は REST2 — coordinator が「今の系なら REST2 が効きそう」と判断して構成を変える
- **予算管理**: GPU 時間を replica ごとに再配分（停滞してる replica から活発な replica へ）
- **収束判定**: 単純な time-based ではなく「ensemble として free energy surface が
  安定したか」の判断

#### 人間エージェント（あり得る）

特定の replica だけ専門家が介入 — ensemble 全体は止めない。これは従来のバッチ MD では
**構造的に不可能**だった使い方。

### 5.3 アーキテクチャ・スケッチ

```
job_dir/
  nodes/
    remd_001/                       # outer DAG: ensemble 全体は 1 ノード
      node.json                     # 温度ラダー, 交換ログの index
      replicas/
        rep_000/
          agent_session.jsonl       # この replica のエージェント対話ログ
          artifacts/trajectory.dcd
          events/                   # local incident log
        rep_001/
        ...
      coordinator/
        agent_session.jsonl         # coordinator の思考ログ
        decisions.jsonl             # ラダー変更等の意思決定履歴
  events/
    exchange_log.jsonl              # グローバル交換イベント（append-only）
```

**3 層分離**:

- **DAG** = artifact lineage（outer）
- **Events** = 時間軸に並んだ疎結合通信（exchange, incident）
- **Filesystem subtree** = 各エージェントの私有状態

Erlang の supervision tree + actor model が一番近い。ただし「各 actor が LLM エージェント」
「state が filesystem 永続」という点が novel。

### 5.4 LangGraph 批判への最強のカウンター

| 批判 | mdclaw × multi-agent REMD での回答 |
|---|---|
| エージェント状態をローカル library graph に持つと死ぬ | 各 replica agent は**独立ファイルシステム subtree** を持つ。別マシンにも動かせる |
| 耐久実行が死ぬ | 各 replica は個別に crash & resume 可能。coordinator 死んでも workers は動き続ける |
| マルチエージェント・スケールが最初から無理 | N replica = N プロセス = N GPU = N ノード、疎結合。交換だけが同期点 |

つまり **"multi-agent is flexible and durable" を実データで示せる科学計算ユースケース**に
なる。チャットエージェント協調より遥かに説得力がある。

---

## 6. Experiment Proposal — 最小プロトタイプ

### 実験対象

- **系**: Trp-cage (20残基のミニタンパク、フォールディングベンチマーク) か alanine dipeptide
- **比較**: 従来 REMD vs agent-supervised REMD

### 評価指標

- **convergence time**: free energy surface が収束するまでの wall time / GPU hour
- **acceptance rate stability**: 交換受理率の時間変動、低下時の自動回復
- **failure recovery rate**: 人為的に NaN / crash を注入した時のリカバリ成功率
- **sampling efficiency**: 単位 GPU hour あたりの conformational space カバレッジ

### 実装計画

1. **mdclaw 拡張**: `remd_group` ノード型 + replica sub-agent launcher を追加
2. **coordinator**: Claude Code 自身（Opus 4.7）
3. **worker**: 軽量 agent（Haiku 相当）、各 replica の失敗判断専用
4. **通信**: filesystem events (`events/`) + JSONL message log

### タイムライン

3-6 ヶ月で論文 1 本目のプロトタイプ。

---

## 7. Related Work Pointers

直接参照すべき先行研究:

- **W3C PROV モデル**: `Entity / Activity / Agent` + `wasDerivedFrom` — 役割付き
  エッジの素直な形式化
- **Pegasus / Nextflow / Snakemake**: 宣言的 I/O バインディング（Level 2 の既存解）
- **Bazel / Nix**: 純関数 artifact DAG、ハッシュベース再利用
- **CWL (Common Workflow Language)**: scatter/gather のパターン化
- **Dagster**: software-defined assets（アセット DAG として workflow を見る思想）
- **AutoGen / CrewAI**: multi-agent だが durable state なし、chat タスク中心
- **Parallel Tempering 自動ラダー最適化論文**: 統計的最適化、LLM の semantic reasoning なし
- **Erlang supervision tree**: actor model + fault tolerance via process restart

**新規性の束**:

1. LLM エージェントが replica supervisor（初？）
2. DAG + events + filesystem の 3 層分離を multi-agent durable execution の形式として提案
3. MD の meta-decision を自然言語 reasoning に置き換える具体例
4. Science benchmark としての multi-agent: chat ベンチマークと違い**客観的指標**
   （free energy 精度, sampling efficiency）がある

---

## 8. Research Questions / Open Points

### 理論

- **トークンコスト vs MD コストのバランス**: 1 replica が数日走るなら agent 介入は秒〜分粒度で
  十分 → コスト無視できる。逆に短時間 sweep だと agent overhead が効く → 閾値が研究対象
- **agent が持つべき context**: 過去の trajectory 全部は渡せない。どう要約するか
  （RMSD 時系列 + 最近のログだけで判断可能か？）
- **coordinator の権限範囲**: どこまで自律的にラダー変更を許すか、human gate をどこに置くか

### 工学

- **failure mode**: agent が暴走して replica を殺し続ける、等の病的パターン検出
- **再現性**: agent 判断が入った REMD の結果は再現できるか、seed だけでなく agent trace を
  artifact に含める必要
- **粒度選択**: Level 5 の (a)(b)(c) のどの粒度を標準にするか、ユーザに選ばせるか

### 論文戦略

論文 2 本に分けられそう:

1. **Level 1 + 2 + 3** を 1 本目（実装 + MD ケーススタディ）
2. **Level 5 (c)** と **Sub-agent × REMD** を 2 本目（理論 + 実験 + 意思決定ログ分析）

---

## 9. 関連ファイル（mdclaw 内）

議論で参照した具体的コード位置:

- `mdclaw/_node.py:36` — `NODE_TYPES` 定義
- `mdclaw/_node.py:121-314` — `create_node` 実装
- `mdclaw/_node.py:605-677` — `find_ancestor_artifact` (BFS で祖先 artifact 探索)
- `mdclaw/_node.py:680-813` — `resolve_node_inputs` (型別入力解決、ハードコード分岐)
- `CLAUDE.md` / `docs/developer/architecture.md` — プロジェクト全体像、schema v3 の説明
- `mdclaw/_event.py` — append-only イベントログ（将来 REMD 交換ログに転用可能）
- `mdclaw/_lock.py` — ファイルロック（マルチプロセス前提の設計）
