## ベンチマーク（成功率評価）: ゴールデン例 + 10回反復

このディレクトリは、MDZenの workflow（Phase1→Phase2→Phase3）を**複数タイプのMDシステム**で反復実行し、成功率と失敗モードを定量化するためのものです。

### 使い方

- **ケース定義**: `benchmarks/cases_v1.yaml`
- **実行**:

```bash
python main.py benchmark run --cases benchmarks/cases_v1.yaml --repeats 10
```

モデルを固定する場合:

```bash
python main.py benchmark run --cases benchmarks/cases_v1.yaml --repeats 10 --model claude-sonnet
```

### 生成されるもの

`benchmarks/runs/<timestamp>/` 配下に、ケース×試行ごとの出力が保存されます。

- **各試行**: `.../<case_id>/attempt_XX/benchmark_metadata.json`
- **全体集計**: `summary.json` と `summary.csv`

### 再現性（ダウンロード凍結）

ベンチでは `MDZEN_CACHE_DIR`（既定: repo直下の `.mdzen_cache/`）を用いて、\nPDB/mmCIFのダウンロード結果を**チェックサム付きでキャッシュ**します。\n同一ケースの繰り返しで外部変動（ネットワーク、PDB更新）の影響を最小化します。

### ゴールデン例（成功run）の保存

「正解例」は、`job_*/validation_result.json` が `success=true` になり、必須成果物（`parm7`,`rst7`）が揃った run を指します。\n必要に応じて、その `job_.../` ディレクトリを丸ごと `benchmarks/golden/<case_id>/` にコピーして固定できます。

```bash
mkdir -p benchmarks/golden/soluble_apo_1ubq
cp -R /path/to/job_xxxxx benchmarks/golden/soluble_apo_1ubq/
```

`validation_result.json` には QC v1（トポロジ/座標整合性 + 組成サマリ）も含まれ、ベンチの機械判定に使われます。

