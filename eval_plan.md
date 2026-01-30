# MDZen 評価設計計画

## 概要

Anthropicのブログ記事「Demystifying Evals for AI Agents」の原則に基づき、MDZen（MD シミュレーション準備AIエージェント）の評価フレームワークを設計する。

---

## 1. MDZen の評価における特殊性

### 1.1 エージェントの特徴

MDZenは**コーディングエージェント**と**研究エージェント**のハイブリッド：
- **研究エージェント的側面**: Phase 1でPDB/UniProt検索、構造解析
- **コーディングエージェント的側面**: Phase 2で決定論的なワークフロー実行
- **検証可能な成果物**: PDB, parm7, rst7, trajectory等のファイル

### 1.2 評価の難しさ

| 課題 | 詳細 |
|-----|------|
| 長時間実行 | 1タスク完了に5-60分（構造予測、MD実行） |
| 外部依存 | PDB/AlphaFold API、Boltz-2、packmol、tleap、OpenMM |
| 非決定性 | LLMの回答バリエーション、MD結果のゆらぎ |
| ドメイン専門性 | 成果物の品質評価に分子動力学の知識が必要 |

---

## 2. 評価の3層構造

記事の推奨に従い、**Unit → Integration → E2E** の3層で設計。

### 2.1 Unit Evals（ツール単位）

**目的**: 各MCPツールが正しく動作することを確認

#### 2.1.1 Research Server

| タスク | 入力 | 成功基準（コードベースグレーダー） |
|-------|------|--------------------------------|
| PDBダウンロード | `pdb_id="1AKE"` | `success=True && file_exists && num_atoms > 0` |
| 無効PDB処理 | `pdb_id="XXXX"` | `success=False && "not found" in errors` |
| 構造検査 | 有効PDBファイル | `len(chains) > 0 && summary fields exist` |
| UniProt検索 | `query="kinase"` | `len(results) > 0` |

#### 2.1.2 Structure Server

| タスク | 入力 | 成功基準 |
|-------|------|---------|
| タンパク質クリーニング | 1AKE chain A | `output_file exists && "clean" in name` |
| リガンドパラメータ化 | ATP mol2 | `frcmod exists && gaff_mol2 exists` |
| SMILES検証 | 有効/無効SMILES | 有効→canonical返却、無効→エラー |
| テンプレートマッチング失敗 | 不一致SMILES | `warnings contain "template"` |

#### 2.1.3 Solvation Server

| タスク | 入力 | 成功基準 |
|-------|------|---------|
| 水和（立方体ボックス） | merged.pdb, cubic=True | `box_dimensions.is_cubic=True && α=β=γ=90°` |
| 塩添加 | saltcon=0.15 | `statistics.num_ions > 0` |
| 膜埋め込み | lipids="POPC" | `output_file exists && lipid atoms present` |

#### 2.1.4 Amber Server

| タスク | 入力 | 成功基準 |
|-------|------|---------|
| トポロジー生成 | solvated.pdb + box_dims | `parm7 exists && rst7 exists` |
| box_dimensions欠損 | solvated.pdb only | `solvent_type="implicit"` (警告) |
| リガンドパラメータ統合 | mol2 + frcmod | `no "Unknown residue" in leap_log` |

#### 2.1.5 MD Simulation Server

| タスク | 入力 | 成功基準 |
|-------|------|---------|
| NVTシミュレーション | pressure=None | `ensemble="NVT"` |
| NPTシミュレーション | pressure=1.0 | `ensemble="NPT"` |
| RMSD計算 | trajectory + topology | `len(rmsd_values) == num_frames` |
| エネルギー時系列 | energy.log | `final_energy < initial_energy` (弛緩) |

### 2.2 Integration Evals（フェーズ単位）

**目的**: 各フェーズが正しく状態を管理し、次フェーズに引き継げることを確認

#### 2.2.1 Phase 1: Clarification

```yaml
task: "Setup MD for PDB 1AKE"
graders:
  - type: code
    check: session.state["simulation_brief"] is not None
  - type: code
    check: brief["pdb_id"] == "1AKE"
  - type: llm_rubric
    rubric: |
      - Agent asked relevant clarification questions (1-3 questions)
      - Agent used inspect_molecules to understand structure
      - SimulationBrief contains reasonable defaults
    scale: 1-5
```

**評価ポイント**:
- `generate_simulation_brief()` が呼ばれたか
- 必須パラメータ（pdb_id or fasta_sequence）が設定されたか
- 会話ターン数（少なすぎ＝確認不足、多すぎ＝非効率）

#### 2.2.2 Phase 2: Setup

```yaml
task: "Execute 4-step workflow from SimulationBrief"
input:
  simulation_brief: {pdb_id: "1AKE", ...}
graders:
  - type: code
    check: |
      completed_steps == ["prepare_complex", "solvate", "build_topology", "run_simulation"]
  - type: code
    check: |
      all([
        Path(outputs["merged_pdb"]).exists(),
        Path(outputs["solvated_pdb"]).exists(),
        Path(outputs["parm7"]).exists(),
        Path(outputs["rst7"]).exists(),
      ])
  - type: code
    check: outputs["box_dimensions"] is not None  # Critical handoff
```

**評価ポイント**:
- ステップ順序の遵守（1→2→3→4）
- `mark_step_complete()` の適切な呼び出し
- `box_dimensions` の Phase 2→3 引き継ぎ（最重要）
- 出力ファイルの存在と妥当なサイズ

#### 2.2.3 Phase 3: Validation

```yaml
task: "Generate validation report"
graders:
  - type: code
    check: validation_result["success"] == True
  - type: code
    check: |
      "parm7" in validation_result["required_files"]
      and validation_result["required_files"]["parm7"]["exists"]
  - type: llm_rubric
    rubric: |
      Report includes:
      - Configuration summary matching SimulationBrief
      - File status for all critical outputs
      - No false positives (claiming success when files missing)
```

### 2.3 End-to-End Evals

**目的**: ユーザーリクエストから最終成果物まで一貫して動作することを確認

#### 2.3.1 タスクセット設計

記事の推奨「20-50個の実タスク」に従い、以下のカテゴリでバランス：

| カテゴリ | タスク数 | 例 |
|---------|--------|-----|
| 単純タンパク質 | 10 | "Setup MD for PDB 1AKE" |
| タンパク質-リガンド | 10 | "Setup MD for 1AKE with ATP" |
| 膜タンパク質 | 5 | "Setup MD for GPCR in POPC membrane" |
| Boltz-2予測 | 5 | "Predict structure from FASTA and run MD" |
| エッジケース | 10 | マルチチェーン、ジスルフィド、金属イオン |
| 失敗ケース | 10 | 無効PDB、不正SMILES、タイムアウト |

#### 2.3.2 E2Eタスク例

```yaml
name: "protein_ligand_standard"
description: "Standard protein-ligand MD setup"
input: "Setup a 10ns MD simulation for PDB 3HTB with the bound ligand at 300K"
expected_outputs:
  - parm7: exists, size > 100KB
  - rst7: exists, size > 1KB
  - trajectory: exists, size > 1MB
graders:
  - type: deterministic
    check: all_required_files_exist()
  - type: code
    check: |
      # Ligand was parameterized
      len(outputs.get("ligand_files", [])) > 0
  - type: llm_rubric
    rubric: |
      - Ligand was correctly identified from PDB
      - GAFF2 parameters were generated
      - Simulation parameters match user request (10ns, 300K)
  - type: domain_expert  # 人間グレーダー（サンプリング）
    criteria: |
      - parm7 opens correctly in PyMOL/VMD
      - rst7 coordinates are chemically reasonable
      - Trajectory shows expected dynamics
```

---

## 3. グレーダー設計

### 3.1 コードベースグレーダー（高速・決定論的）

```python
# graders/code_graders.py

def check_file_exists(path: str) -> bool:
    return Path(path).exists() and Path(path).stat().st_size > 0

def check_workflow_order(completed_steps: list) -> bool:
    expected = ["prepare_complex", "solvate", "build_topology", "run_simulation"]
    return completed_steps == expected[:len(completed_steps)]

def check_box_dimensions_preserved(outputs: dict) -> bool:
    """Critical: box_dimensions must flow from solvate to build_topology"""
    return outputs.get("box_dimensions") is not None

def check_parm7_validity(parm7_path: str) -> dict:
    """Validate Amber topology file"""
    from parmed import load_file
    try:
        parm = load_file(parm7_path)
        return {
            "valid": True,
            "num_atoms": len(parm.atoms),
            "num_residues": len(parm.residues),
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}

def check_trajectory_frames(traj_path: str, top_path: str) -> dict:
    """Validate trajectory file"""
    import mdtraj as mdt
    try:
        traj = mdt.load(traj_path, top=top_path)
        return {
            "valid": True,
            "num_frames": traj.n_frames,
            "num_atoms": traj.n_atoms,
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}
```

### 3.2 LLMルーブリックグレーダー（柔軟・主観的）

```python
# graders/llm_graders.py

CLARIFICATION_RUBRIC = """
Evaluate the clarification agent's conversation quality:

1. **Information Gathering** (1-5)
   - Did the agent use inspect_molecules to understand the structure?
   - Did it ask relevant questions about simulation parameters?
   - Did it avoid unnecessary questions?

2. **SimulationBrief Quality** (1-5)
   - Are all required fields populated?
   - Do defaults make scientific sense?
   - Does the brief match user's stated intent?

3. **Conversation Efficiency** (1-5)
   - Was the conversation concise?
   - Did the agent avoid repetition?
   - Did it reach conclusion in reasonable turns (3-7)?

Overall score: Average of above
"""

SETUP_EXECUTION_RUBRIC = """
Evaluate the setup agent's workflow execution:

1. **Step Ordering** (Pass/Fail)
   - Steps executed in correct order?
   - No skipped steps?

2. **File Path Handling** (1-5)
   - Actual file paths used (not placeholders)?
   - output_dir parameter always provided?
   - Correct files passed between steps?

3. **State Management** (1-5)
   - mark_step_complete() called after each step?
   - box_dimensions preserved through workflow?
   - outputs dict correctly populated?

4. **Error Recovery** (1-5)
   - Appropriate response to tool errors?
   - Meaningful error messages propagated?
"""
```

### 3.3 ドメイン専門家グレーダー（サンプリング）

記事の推奨に従い、**自動評価の検証用**として人間評価を設計：

```yaml
human_grading_protocol:
  frequency: "10% of E2E trials, randomly sampled"
  evaluators: "MD simulation practitioners"

  checklist:
    structure_quality:
      - "Protonation states correct for pH 7.4?"
      - "Disulfide bonds correctly identified?"
      - "Ligand geometry chemically reasonable?"

    topology_quality:
      - "Force field appropriate for system?"
      - "Water model consistent with force field?"
      - "Box size adequate (no self-interaction)?"

    simulation_quality:
      - "Trajectory shows expected behavior?"
      - "No unphysical energy spikes?"
      - "System equilibrated before production?"
```

---

## 4. メトリクス設計

### 4.1 pass@k と pass^k

```python
def calculate_pass_at_k(results: list[bool], k: int) -> float:
    """k回の試行で少なくとも1回成功する確率"""
    n = len(results)
    c = sum(results)  # 成功数
    if n < k:
        return None
    # 組み合わせ計算
    from math import comb
    return 1 - comb(n - c, k) / comb(n, k)

def calculate_pass_power_k(results: list[bool], k: int) -> float:
    """k回の試行がすべて成功する確率"""
    success_rate = sum(results) / len(results)
    return success_rate ** k
```

**MDZenでの使い分け**:
- **pass@3**: 「ユーザーが3回試行して少なくとも1回成功するか」→ ユーザー体験指標
- **pass^3**: 「3回連続で成功するか」→ 信頼性指標（本番デプロイ判断）

### 4.2 フェーズ別メトリクス

| フェーズ | 主要メトリクス | 目標値 |
|---------|--------------|-------|
| Phase 1 | SimulationBrief生成率 | > 95% |
| Phase 1 | 会話ターン数 | 3-7 turns |
| Phase 2 | ワークフロー完了率 | > 90% |
| Phase 2 | box_dimensions引き継ぎ率 | 100% |
| Phase 3 | 必須ファイル検出率 | 100% |
| E2E | 全体成功率 | > 80% |
| E2E | pass^3 | > 50% |

### 4.3 エラー分類メトリクス

```python
ERROR_CATEGORIES = {
    "api_failure": ["PDB download failed", "UniProt timeout"],
    "tool_error": ["antechamber failed", "tleap unknown residue"],
    "state_error": ["box_dimensions missing", "output_key not found"],
    "llm_error": ["wrong tool called", "step skipped", "infinite loop"],
    "timeout": ["Boltz-2 timeout", "packmol timeout"],
}

def categorize_failure(transcript: dict) -> str:
    """失敗原因を分類"""
    # トランスクリプト解析ロジック
    ...
```

---

## 5. 実装ロードマップ

### Phase 1: 基盤構築（推奨開始点）

1. **テストフィクスチャ作成**
   - 既知のPDB構造（1AKE, 3HTB, 7TM4）をローカルにキャッシュ
   - 期待される出力ファイル（参照parm7, rst7）を準備

2. **コードベースグレーダー実装**
   - ファイル存在チェック
   - ワークフロー順序検証
   - box_dimensions検証

3. **Unit Eval実行環境**
   - 各MCPサーバーの独立テスト
   - `pytest` + `mcp dev` での自動化

### Phase 2: 統合評価

4. **フェーズ間状態検証**
   - Phase 1→2: SimulationBrief検証
   - Phase 2→3: outputs dict検証

5. **LLMグレーダー実装**
   - ルーブリック定義
   - Claude APIでのスコアリング

### Phase 3: E2E評価

6. **タスクセット構築**
   - 50タスクのYAML定義
   - 期待出力の準備

7. **トランスクリプト収集**
   - 全API呼び出しのログ
   - 失敗分析用データ

### Phase 4: 継続的評価

8. **CI/CD統合**
   - Unit evals: PRごと
   - Integration evals: daily
   - E2E evals: weekly

9. **ダッシュボード**
   - pass@k/pass^k推移
   - エラーカテゴリ分布
   - モデル別比較

---

## 6. 重要な設計原則（記事より）

### 6.1 「出力を測定し、プロセスではない」

```yaml
# ❌ BAD: プロセスを測定
check: "agent called inspect_molecules before generate_simulation_brief"

# ✅ GOOD: 出力を測定
check: "SimulationBrief contains valid pdb_id and reasonable defaults"
```

### 6.2 「トランスクリプトを定期的に読む」

- 週次で10件のトランスクリプトを手動レビュー
- 自動評価が見逃しているパターンを発見
- LLMグレーダーの校正

### 6.3 「飽和したら新問題へ移行」

- 特定タスクで100%達成したら、より難しいタスクを追加
- 例: 単純タンパク質 100% → タンパク質-リガンド追加

---

## 7. MDZen固有の考慮事項

### 7.1 長時間実行への対応

```python
# タイムアウト設定（評価環境）
EVAL_TIMEOUTS = {
    "unit_eval": 60,        # 1分
    "integration_eval": 600, # 10分
    "e2e_eval": 3600,       # 1時間
}

# 並列実行で効率化
async def run_e2e_eval_parallel(tasks: list, max_concurrent: int = 5):
    ...
```

### 7.2 外部API依存への対応

```python
# モック戦略
MOCK_RESPONSES = {
    "download_structure:1AKE": {"success": True, "file_path": "fixtures/1ake.cif"},
    "get_alphafold_structure:P12345": {"success": True, "file_path": "fixtures/af_p12345.pdb"},
}

# 実API vs モックの切り替え
USE_MOCKS = os.getenv("MDZEN_EVAL_USE_MOCKS", "false") == "true"
```

### 7.3 ドメイン知識の組み込み

```python
# 化学的妥当性チェック
def check_chemical_validity(pdb_path: str) -> dict:
    """
    - 結合長が妥当か
    - クラッシュがないか
    - 電荷がニュートラルに近いか
    """
    from rdkit import Chem
    mol = Chem.MolFromPDBFile(pdb_path)
    ...
```

---

## 8. Google ADK 評価機能の活用

MDZenはGoogle ADKを使用しているため、**ADKネイティブの評価フレームワーク**を最大限に活用できる。

### 8.1 ADK評価フレームワーク概要

ADKは3つの評価実行方法を提供：

| 方法 | コマンド | 用途 |
|-----|---------|-----|
| Web UI | `adk web` | 対話的なゴールデンデータセット作成、視覚的デバッグ |
| CLI | `adk eval` | コマンドラインからの直接評価実行 |
| pytest | `pytest tests/` | CI/CDパイプライン統合 |

### 8.2 評価ファイル形式

#### テストファイル（`.test.json`）- Unit/Integration向け

```json
{
  "eval_set_id": "mdzen_phase2_workflow",
  "eval_cases": [
    {
      "eval_id": "prepare_complex_basic",
      "session_input": {
        "app_name": "mdzen",
        "user_id": "eval_user",
        "state": {
          "simulation_brief": {"pdb_id": "1AKE", "select_chains": ["A"]}
        }
      },
      "conversation": [
        {
          "user_content": {"parts": [{"text": "Execute prepare_complex step"}]},
          "intermediate_data": {
            "tool_uses": [
              {"name": "get_workflow_status_tool", "args": {}},
              {"name": "prepare_complex", "args": {"pdb_id": "1AKE", "select_chains": ["A"]}},
              {"name": "mark_step_complete", "args": {"step_name": "prepare_complex"}}
            ]
          },
          "final_response": {"parts": [{"text": "prepare_complex completed"}]}
        }
      ]
    }
  ]
}
```

#### Evalsetファイル（`.evalset.json`）- E2E向け

複数のマルチターン会話を含む、より複雑な評価セット。`adk web`で対話しながら簡単に作成可能。

### 8.3 ADK評価基準のMDZenへのマッピング

| ADK基準 | MDZen用途 | 設定例 |
|---------|----------|-------|
| `tool_trajectory_avg_score` | Phase 2のワークフロー順序検証 | `1.0`（完全一致必須） |
| `response_match_score` | Phase 3レポートの内容検証 | `0.7`（ROUGE-1） |
| `rubric_based_tool_use_quality_v1` | ツール引数の妥当性評価 | カスタムルーブリック |
| `rubric_based_final_response_quality_v1` | SimulationBriefの品質評価 | カスタムルーブリック |

### 8.4 Phase別のADK評価設定

#### Phase 1: Clarification（User Simulation活用）

**User Simulation**機能を使用して、対話的な要件収集を評価：

```json
{
  "eval_set_id": "mdzen_clarification",
  "eval_cases": [
    {
      "eval_id": "protein_ligand_clarification",
      "conversation_scenario": {
        "starting_prompt": "I want to run MD simulation for PDB 3HTB",
        "conversation_plan": "When asked about simulation parameters, specify 10ns at 300K. When asked about ligand handling, confirm to include the bound ligand with default parameters. Goal: Agent should generate a complete SimulationBrief."
      },
      "criteria": {
        "tool_trajectory_avg_score": {
          "threshold": 0.8,
          "match_type": "IN_ORDER"
        },
        "rubric_based_final_response_quality_v1": {
          "threshold": 0.8,
          "rubrics": [
            {
              "rubric_id": "brief_completeness",
              "rubric_content": {"text_property": "SimulationBrief contains pdb_id, temperature, simulation_time_ns"}
            },
            {
              "rubric_id": "conversation_efficiency",
              "rubric_content": {"text_property": "Conversation completed in 3-7 turns without unnecessary questions"}
            }
          ]
        }
      }
    }
  ]
}
```

#### Phase 2: Setup（Trajectory評価）

ワークフロー順序の**厳密検証**に`tool_trajectory_avg_score`を使用：

```json
{
  "eval_set_id": "mdzen_setup_workflow",
  "eval_cases": [
    {
      "eval_id": "full_workflow_1ake",
      "session_input": {
        "state": {
          "simulation_brief": {
            "pdb_id": "1AKE",
            "select_chains": ["A"],
            "temperature": 300.0,
            "simulation_time_ns": 1.0
          }
        }
      },
      "conversation": [
        {
          "user_content": {"parts": [{"text": "Execute the MD setup workflow"}]},
          "intermediate_data": {
            "tool_uses": [
              {"name": "get_workflow_status_tool", "args": {}},
              {"name": "prepare_complex", "args": {"pdb_id": "1AKE"}},
              {"name": "mark_step_complete", "args": {"step_name": "prepare_complex"}},
              {"name": "solvate_structure", "args": {}},
              {"name": "mark_step_complete", "args": {"step_name": "solvate"}},
              {"name": "build_amber_system", "args": {}},
              {"name": "mark_step_complete", "args": {"step_name": "build_topology"}},
              {"name": "run_md_simulation", "args": {}},
              {"name": "mark_step_complete", "args": {"step_name": "run_simulation"}}
            ]
          }
        }
      ]
    }
  ],
  "criteria": {
    "tool_trajectory_avg_score": {
      "threshold": 1.0,
      "match_type": "IN_ORDER"
    }
  }
}
```

#### Phase 3: Validation（Rubric評価）

レポート品質を**ルーブリック**で評価：

```json
{
  "criteria": {
    "rubric_based_final_response_quality_v1": {
      "threshold": 0.9,
      "rubrics": [
        {
          "rubric_id": "file_status_accuracy",
          "rubric_content": {"text_property": "Report correctly identifies existence of parm7 and rst7 files"}
        },
        {
          "rubric_id": "config_summary",
          "rubric_content": {"text_property": "Report includes configuration summary matching SimulationBrief"}
        },
        {
          "rubric_id": "no_false_positives",
          "rubric_content": {"text_property": "Report does not claim success when required files are missing"}
        }
      ]
    }
  }
}
```

### 8.5 pytest統合実装

```python
# tests/integration/test_mdzen_eval.py

import pytest
import importlib
from google.adk.evaluation.agent_evaluator import AgentEvaluator
from pathlib import Path


@pytest.mark.asyncio
async def test_phase1_clarification():
    """Phase 1: Clarification agent evaluation"""
    await AgentEvaluator.evaluate(
        agent_module="mdzen.agents.clarification_agent",
        eval_dataset_file_path_or_dir="tests/evals/clarification/",
        config_file_path="tests/evals/config/clarification_config.json",
        num_runs=3,  # pass@3 計算用
    )


@pytest.mark.asyncio
async def test_phase2_setup_workflow():
    """Phase 2: Setup agent workflow evaluation"""
    await AgentEvaluator.evaluate(
        agent_module="mdzen.agents.setup_agent",
        eval_dataset_file_path_or_dir="tests/evals/setup/workflow.evalset.json",
        config_file_path="tests/evals/config/setup_config.json",
        num_runs=1,
    )


@pytest.mark.asyncio
async def test_phase2_box_dimensions_handoff():
    """Critical: box_dimensions must be preserved"""
    await AgentEvaluator.evaluate(
        agent_module="mdzen.agents.setup_agent",
        eval_dataset_file_path_or_dir="tests/evals/setup/box_dimensions.test.json",
        config_file_path="tests/evals/config/strict_trajectory.json",
    )


@pytest.mark.asyncio
async def test_e2e_protein_only():
    """E2E: Simple protein MD setup"""
    await AgentEvaluator.evaluate(
        agent_module="mdzen.agents.full_agent",
        eval_dataset_file_path_or_dir="tests/evals/e2e/protein_only/",
        num_runs=3,
    )


@pytest.mark.asyncio
async def test_e2e_protein_ligand():
    """E2E: Protein-ligand MD setup"""
    await AgentEvaluator.evaluate(
        agent_module="mdzen.agents.full_agent",
        eval_dataset_file_path_or_dir="tests/evals/e2e/protein_ligand/",
        num_runs=3,
    )
```

### 8.6 評価設定ファイル

```json
// tests/evals/config/setup_config.json
{
  "criteria": {
    "tool_trajectory_avg_score": {
      "threshold": 1.0,
      "match_type": "IN_ORDER"
    }
  }
}

// tests/evals/config/clarification_config.json
{
  "criteria": {
    "tool_trajectory_avg_score": {
      "threshold": 0.8,
      "match_type": "ANY_ORDER"
    },
    "rubric_based_final_response_quality_v1": {
      "threshold": 0.8,
      "judge_model_options": {
        "judge_model": "gemini-2.0-flash"
      },
      "rubrics": [
        {
          "rubric_id": "brief_generated",
          "rubric_content": {"text_property": "Agent called generate_simulation_brief with valid parameters"}
        }
      ]
    }
  }
}
```

### 8.7 adk webでのゴールデンデータセット作成ワークフロー

```bash
# 1. Web UIを起動
adk web src/mdzen/agents/

# 2. ブラウザで http://localhost:8000 を開く

# 3. エージェントと対話して理想的な会話を作成
#    - "Setup MD for PDB 1AKE" と入力
#    - エージェントの質問に回答
#    - 完了まで対話

# 4. Evalタブで "Create Evaluation Set" をクリック
#    - セット名を入力（例: protein_only_1ake）
#    - 自動で .evalset.json ファイルが生成

# 5. 生成されたファイルを tests/evals/ に移動
```

### 8.8 ADK評価の利点（MDZen向け）

| 利点 | 説明 |
|-----|------|
| **ネイティブ統合** | ADKのRunner/Sessionと完全互換 |
| **ツール軌跡評価** | Phase 2のワークフロー順序を自動検証 |
| **User Simulation** | Phase 1の対話評価を自動化 |
| **Web UIでの作成** | ゴールデンデータセットを対話的に作成 |
| **pytest統合** | 既存CI/CDパイプラインに簡単に組み込み |

### 8.9 注意事項

1. **Vertex AI依存**: 一部の評価基準（`safety_v1`等）はVertex AI Gen AI Evaluation Service APIが必要（有料）
2. **モデル選択**: `judge_model`にはGeminiモデルを指定（`gemini-2.0-flash`推奨）
3. **長時間タスク**: MDZenのE2E評価は時間がかかるため、`timeout`設定を適切に調整

---

## 9. 推奨実装順序（ADK活用版）

### Step 1: 環境セットアップ
```bash
# 評価ディレクトリ構造を作成
mkdir -p tests/evals/{clarification,setup,validation,e2e,config}
```

### Step 2: adk webでゴールデンデータセット作成
- Phase 1: 3-5パターンの会話を記録
- Phase 2: 正常ワークフローと異常ケースを記録
- Phase 3: 成功/失敗レポートを記録

### Step 3: pytest統合
- `test_mdzen_eval.py` を作成
- CI/CDに `pytest tests/evals/` を追加

### Step 4: User Simulation導入
- Phase 1の対話評価を自動化
- 複数のユーザーペルソナでテスト

### Step 5: 継続的改善
- 失敗トランスクリプトを分析
- ゴールデンデータセットを拡充

---

## 10. 次のステップ

1. **タスク優先度の確認**: 最初に実装すべき評価カテゴリはどれか？
2. **リソース確認**: 人間評価に使えるドメイン専門家はいるか？
3. **Vertex AI**: 有料サービス（safety_v1等）を使用するか？
4. **CI/CD統合**: 既存のCI/CDパイプラインはあるか？

---

## 参考資料

- [Demystifying Evals for AI Agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [Google ADK Evaluation Guide](https://google.github.io/adk-docs/evaluate/)
- [ADK Evaluation Criteria](https://google.github.io/adk-docs/evaluate/criteria/)
- [User Simulation in ADK](https://developers.googleblog.com/announcing-user-simulation-in-adk-evaluation/)
- [ADK Evaluation Codelab](https://codelabs.developers.google.com/adk-eval/instructions)
- MDZen CLAUDE.md（プロジェクト仕様）
