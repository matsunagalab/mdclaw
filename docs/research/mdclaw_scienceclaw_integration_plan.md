# MDClaw and ScienceClaw Integration Plan

作成日: 2026-05-01

## 目的

MDClaw を、ScienceClaw や細胞シミュレータ foundation model エージェントから自然に呼び出せる「原子レベル evidence provider」として発展させる。

ここでの重要な方針は、MDClaw の内部 DAG を ScienceClaw の DAG と無理に互換化しないこと。ScienceClaw から見えるべきものは、MDClaw の内部実行計画ではなく、次の2つでよい。

- 入力: 何を、どんな科学的問いで MD してほしいか
- 出力: その MD から何が分かったか

MDClaw 内部の `source -> prep -> solv -> topo -> eq -> prod -> analyze` DAG は、MDClaw が正しく再開可能に MD を実行し、provenance を残すための実装詳細として維持する。

## 基本構想

ScienceClaw / 細胞シミュレータ側は、MDClaw を「trajectory generator」ではなく「cell model に渡せる atomistic evidence generator」として使う。

```text
ScienceClaw / Cell Simulator Agent
  └─ asks biological or mechanistic question
       ↓
MDClaw Study Layer
  └─ turns question into MD workflow
       ↓
MDClaw Execution Layer
  └─ runs source/prep/solv/topo/eq/prod/analyze DAG
       ↓
MDClaw Evidence Layer
  └─ returns natural-language summary + typed evidence
       ↓
ScienceClaw / Cell Simulator Agent
  └─ updates hypothesis, pathway model, or cell-scale parameters
```

## 3つのレイヤ

### 1. Execution Layer

現在の MDClaw DAG。役割は「MD を正しく・再開可能に・記録付きで流す」こと。

既存の主な構成要素:

- `mdclaw/_node.py`
- `create_node`
- `resolve_node_inputs`
- `run_equilibration`
- `run_production`
- `node.json`
- `events/`
- `artifacts/`

この層は低レベル API として維持する。ScienceClaw や細胞 foundation model エージェントは、原則としてこの内部構造を直接理解しなくてよい。

### 2. Study Layer

新しく必要な層。外部エージェントやユーザーの科学的問いを、MDClaw の execution DAG に変換する。

例: 外部からの request

```json
{
  "study_type": "mutation_stability",
  "target": {
    "uniprot_id": "P12345",
    "mutation": "V148A"
  },
  "comparison": "WT_vs_mutant",
  "budget": {
    "simulation_time_ns": 20,
    "replicates": 3
  }
}
```

Study Layer が内部で作る DAG の例:

```text
WT:
  fetch_wt -> prep_wt -> solv_wt -> topo_wt -> eq_wt -> prod_wt_rep1/2/3

Mutant:
  fetch_wt -> prep_wt -> mutate_V148A -> solv_mut -> topo_mut -> eq_mut -> prod_mut_rep1/2/3

Analysis:
  analyze_stability_comparison
```

実装候補:

```text
mdclaw/study_server.py
```

最初に置く高レベル tool の例:

```python
def run_mutation_stability_study(
    uniprot_id: str | None = None,
    pdb_id: str | None = None,
    mutation: str | None = None,
    simulation_time_ns: float = 20.0,
    replicates: int = 3,
    job_dir: str | None = None,
) -> dict:
    ...
```

Study Layer は OpenMM を直接触らず、既存の execution tools を組み合わせる。

優先 study type:

- `stability_screen`
- `mutation_effect_study`
- `binding_site_flexibility_study`
- `protein_interface_stability_study`
- `apo_vs_holo_study`
- `wt_vs_mutant_comparison`

### 3. Evidence Layer

外部連携の本命。MD の raw output を、ScienceClaw や細胞モデルが使える evidence report に変換する。

Evidence report は、自然言語 summary と型付き metrics の両方を持つ。自然言語だけでは、人間には読めても、外部エージェントが比較・ランキング・モデル更新に使いにくい。

例:

```json
{
  "evidence_type": "mutation_stability_effect",
  "target": {
    "uniprot_id": "P12345",
    "mutation": "V148A"
  },
  "summary": "V148A shows moderately increased flexibility near the active-site loop compared with WT.",
  "effect": {
    "direction": "destabilizing",
    "magnitude": "moderate",
    "confidence": "medium"
  },
  "metrics": {
    "wt_rmsd_mean_nm": 0.31,
    "mutant_rmsd_mean_nm": 0.44,
    "delta_rmsd_nm": 0.13,
    "active_site_delta_rmsf_nm": 0.06,
    "replicates": 3,
    "simulation_time_ns_per_replica": 20
  },
  "model_parameter_hints": {
    "active_fraction_change": "decrease",
    "protein_stability_effect": "lower",
    "suggested_cell_model_update": {
      "parameter": "degradation_rate",
      "direction": "increase",
      "confidence": "low_to_medium"
    }
  },
  "limitations": [
    "Short MD screen; not a free-energy calculation",
    "Structure source was predicted, not experimental"
  ],
  "provenance": {
    "mdclaw_job_dir": "...",
    "nodes": ["prod_001", "prod_002", "analyze_001"]
  }
}
```

実装候補:

```text
mdclaw/evidence_server.py
mdclaw/evidence_schema.py
```

tool 例:

```python
def generate_md_evidence_report(
    job_dir: str,
    study_node_id: str | None = None,
    report_type: str = "mutation_stability",
) -> dict:
    ...
```

## 外部エージェントから見た MDClaw

MDClaw は、細胞シミュレータ foundation model エージェントにとって、次のような質問に答える専門ツールになる。

- この変異はタンパク質を安定化/不安定化するか
- 活性型/不活性型の population はどう変わるか
- ligand binding は強まりそうか弱まりそうか
- protein-protein interface は壊れそうか
- membrane protein の gating / transport に影響しそうか
- pathway model の rate / affinity / state transition parameter に反映できるか

細胞モデルが欲しいのは trajectory そのものではなく、MD から抽出された粗視化された evidence や parameter hint である。

## 実装順序

### Step 1: Evidence Report Schema を決める

まず外部に返す contract を固定する。最小フィールド:

- `evidence_type`
- `target`
- `question`
- `summary`
- `status`
- `effect`
- `confidence`
- `metrics`
- `model_parameter_hints`
- `limitations`
- `artifacts`
- `provenance`

### Step 2: 既存 DAG から evidence report を作る tool を追加する

Study Layer はまだ作らず、既存の `prod` / `analyze` node から report を生成する。

最初の tool:

```python
generate_md_evidence_report(job_dir, report_type="stability")
```

### Step 3: 最小 Study Layer を1つ作る

最初は `mutation_stability_study` がよい。

理由:

- FASPR mutation workflow と接続しやすい
- WT vs mutant comparison が細胞モデルに意味を持ちやすい
- RMSD/RMSF/replicate consistency で evidence report を作りやすい

### Step 4: ScienceClaw / 細胞エージェント向け request schema を追加する

外部エージェントは、MDClaw の低レベル node を直接指定せず、次のような request を投げる。

```json
{
  "target": {
    "gene": "GENE_X",
    "uniprot_id": "P12345",
    "mutation": "V148A"
  },
  "question": "stability_effect",
  "context": {
    "cell_type": "hepatocyte",
    "pathway": "MAPK"
  },
  "budget": {
    "simulation_time_ns": 20,
    "replicates": 3
  },
  "required_confidence": "medium"
}
```

## 重要な設計判断

### MDClaw DAG を外部互換化しすぎない

MDClaw の内部 DAG を ScienceClaw の DAG と無理に統合しない。内部 DAG は MD 実行のための durable execution graph として保持する。

外部に出す contract は次の4つに絞る。

- MD request
- MD evidence report
- artifact references
- provenance

### 自然言語だけにしない

Evidence report には自然言語 summary が必要だが、それだけでは不十分。

ScienceClaw や細胞モデルが次の判断に使うには、少数の型付き指標が必要。

自然言語:

- 人間が読める
- 意味づけに強い
- limitations を説明しやすい

型付き metrics:

- 比較できる
- ranking できる
- threshold 判定できる
- downstream model の parameter hint に使える

### confidence と limitations を必ず返す

細胞 foundation model との接続では、過信が危険。

MDClaw は結論だけでなく、どの程度信用できるか、何が限界かを必ず返す。

例:

- `low`: short single-replicate screen; qualitative only
- `medium`: multiple replicates with consistent RMSD/RMSF shift
- `high`: longer sampling plus free-energy estimate and replicate agreement

## 最小ゴール

最初の完成形:

```text
外部エージェント:
  "P12345 V148A は安定性に影響する？"

MDClaw:
  mutation_stability_study を実行
  WT vs mutant の短時間 replicate MD を流す
  RMSD/RMSF/active-site flexibility を比較
  evidence report を返す
```

この形にすると、MDClaw は細胞シミュレータ foundation model にとって自然な「原子レベル evidence provider」になる。
