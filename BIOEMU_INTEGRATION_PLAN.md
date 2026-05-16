# MD surrogate source を mdclaw に統合する改善案

## 結論

BioEmu 統合は、最初から「surrogate ensemble → sidechain 復元 → 自動選択 → k 並列 MD → 集約解析」まで一気通貫に作ると複雑になりすぎる。MVP は **MD surrogate 由来の複数構造を mdclaw の `source_bundle` として、side-chain 込みの all-atom PDB として作れること**に絞る。

> **更新 (2026-05-16)**: Phase 1 の sidechain 復元 backend は HPacker に置き換えた。OpenMM minimize / energy ranking は引き続き後フェーズ。詳細は §改善後の Phase 設計を参照。

ただし、将来的に BioEmu 以外の MD surrogate モデルも使う前提にする。公開 CLI は BioEmu 固有名ではなく、surrogate 共通 CLI に `--model bioemu` を渡す形にする。BioEmu は最初の backend として実装する。

```bash
mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 100 \
  --max-candidates 100 \
  --job-dir <job_dir> \
  --node-id source_001
```

このコマンドの責務は、指定された surrogate model を backend ごとの実行環境で動かし、出力 trajectory / 構造群を `candidate_NNN.pdb` 群に正規化し、`source_bundle.json` を作るところまで。下流の `prepare_complex --source-candidate-id candidate_NNN` は既存実装を使う。

## モデル差し替え設計

MD surrogate は `surrogate_server.py` の中で backend registry として扱う。

```python
SURROGATE_BACKENDS = {
    "bioemu": BioEmuBackend(),
    # future: "alphaflow": AlphaFlowBackend(),
    # future: "esmflow": ESMFlowBackend(),
}
```

候補生成の公開 CLI は原則 1 つにする。

```python
generate_surrogate_candidates(
    model: str,
    amino_acid_sequence: str,
    num_samples: int = 100,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
    output_dir: str | None = None,
    job_dir: str | None = None,
    node_id: str | None = None,
    **model_options,
) -> dict
```

backend の共通契約:

1. 入力 validation を行う。
2. モデル固有 env / subprocess / local Python API を呼ぶ。
3. MDTraj-readable な topology + trajectory、または PDB/mmCIF の構造群を返す。
4. 共通 helper で `candidate_NNN.pdb` に正規化する。
5. `source_bundle.json` に `source_type="surrogate"`、`origin.kind=<model>` を書く。

BioEmu 固有の `msa_path`、`checkpoint`、`filter_samples` などは `model_options` として BioEmu backend だけが解釈する。将来の backend を足すときは、CLI 名や下流 DAG を増やさず、backend registry に 1 件追加する。

## Deploy を簡単にする

BioEmu backend の導入は、ユーザに conda env 手作成を要求しない。mdclaw が backend runtime を管理する。ただし、コンテナは分けない。Docker/Singularity を使う場合は、既存の mdclaw container image に BioEmu backend を同梱する。

**決定**: local でも container でも、BioEmu は conda の `mdclaw` environment には入れない。常に mdclaw 本体 Python から隔離された venv に入れる。`generate_surrogate_candidates --model bioemu` は、その venv の Python を subprocess で呼ぶ。

```bash
mdclaw setup_surrogate_backend --model bioemu --device cuda
mdclaw check_surrogate_backend --model bioemu
```

方針:

- mdclaw 本体 env には BioEmu を入れない。
- local workstation / conda install では managed venv を使う。BioEmu backend は `~/.cache/mdclaw/surrogates/bioemu/venv` のような mdclaw 管理 venv に隔離する。
- managed venv では、`uv` があれば `uv venv` / `uv pip install` を使い、なければ標準の `python -m venv` + `pip` に fallback する。
- `--device cpu` は `pip install bioemu`、`--device cuda` は `pip install "bioemu[cuda]"` を実行する。
- Docker/Singularity image では、build 時に BioEmu backend を image 内の隔離 venv に作っておく。conda の `mdclaw` env には入れない。実行時の `setup_surrogate_backend` は原則不要で、`check_surrogate_backend` だけでよい。
- 初回実行時に必要な AF2 / BioEmu model weights は upstream に任せて cache させる。mdclaw は cache path と download failure をわかりやすく報告するだけにする。
- クラスタ運用では cache directory だけ shared filesystem に置けるようにする。

`generate_surrogate_candidates --model bioemu` は、backend が未導入なら勝手に install しない。かわりに `setup_surrogate_backend` の再現可能なコマンドを error に出す。自動 install は便利だが、HPC や proxy 環境で予期せぬ network access を起こすので、明示コマンドに分ける。

最小実装では以下の 2 つを utility CLI として追加する。

```python
setup_surrogate_backend(
    model: str,
    device: str = "cpu",        # "cpu" | "cuda"
    prefix: str | None = None,
    reinstall: bool = False,
) -> dict

check_surrogate_backend(
    model: str,
    prefix: str | None = None,
) -> dict
```

`check_surrogate_backend` は venv path または container 内 backend path、import check、version、device hint、cache path を返す。実際の sampling はしない。

Docker/Singularity を使う場合の注意:

- コンテナを分けない。既存 `mdclaw:latest` image に BioEmu backend 用 venv を同梱する。
- Singularity は Docker image から作る既存方針を維持する。
- Singularity では `--nv` が必須。GPU なしなら CPU runtime として動かすが、実用速度は期待しない。
- container 内外で cache path が変わると重みを何度も download するので、BioEmu / ColabFold 相当の cache を mdclaw cache 配下に bind mount する。

## 直すべき点

### 1. MVP が大きすぎる

元プランは Phase 1 に以下が入りかけていた。

- BioEmu sampling
- trajectory から候補 PDB への分割
- 全 candidate の sidechain 復元
- OpenMM minimize と energy scoring
- source bundle 更新
- skill/docs/tests

これは失敗点が多い。BioEmu は backend 専用 runtime、backbone-only 出力、model weight cache、GPU/Linux 要件を持つので、MVP では BioEmu 固有の不確実性だけを閉じ込めるべき。

改善後の Phase 1 は **共通候補生成 CLI 一発 + source_bundle 化 + HPacker repack** にする。OpenMM minimize / energy scoring / 候補選択 / 多分岐 MD は Phase 2 以降。HPacker は mutation workflow と同じ side-chain backend なので、ユーザが期待する「1 CLI で side-chain 込みアンサンブル」を満たせる。

### 2. 全 candidate の sidechain 復元は HPacker repack に限定する

BioEmu の出力は backbone-only で、そのままでは `prepare_complex` の前提を満たさない。当初はこれを Phase 3 に先送りしていたが、

- HPacker repack は mutation workflow と同じ backend で扱える
- HPacker は `environment.yml` 経由でコンテナに同梱する
- ユーザが「1 CLI で side-chain 込みアンサンブル」を期待する

ので Phase 1 に取り込み、`generate_surrogate_candidates` の中で各 frame に対し HPacker を呼ぶ。OpenMM minimize / energy ranking / clustering は引き続き Phase 2 以降。

実装上は次の通り。

1. `generate_surrogate_candidates --model bioemu` が backbone candidates を抽出した後、HPacker で in-place repack する。
2. backbone-only PDB は `<artifacts>/candidates_backbone/` に provenance として保管する。
3. candidate の `tags` は `hpacker_repacked`（または disable 時 `backbone_only`）。
4. opt-out したい場合は `--reconstruct-sidechains false`。

### 3. `selection.<tag>` は既存 API では見えにくい

`source_bundle.json` は extra metadata を許すが、`list_source_candidates` は任意の bundle-level `selection.<tag>` を一級 API として扱わない。選択結果を既存 API で見せたいなら、bundle-level 独自スキーマよりも、各 candidate record の `tags` / `metrics` / `rank` / `is_primary` に寄せるほうが堅い。

Phase 2 の選択 CLI は、まず次の程度に留める。

```bash
mdclaw select_source_candidates \
  --job-dir <job_dir> \
  --node-id source_001 \
  --strategy evenly_spaced \
  --k 10 \
  --tag bioemu_k10
```

出力は各 candidate に以下を追記する。

- `tags`: `["selected:bioemu_k10"]`
- `metrics.selection_rank`: 1, 2, ...
- `metrics.selection_strategy`: `"evenly_spaced"` など

これなら既存の source bundle の自由度の範囲で済み、後から `list_source_candidates` の表示拡張もしやすい。

### 4. multi-prod concat は topology 互換性を前提にする

同一 job 内で `source_001 -> prep_001..prep_K -> prod_001..prod_K -> analyze_001` とする設計は、既存 DAG と整合する。ただし、multi-prod `concat_trajectory` は共有 topology を前提に動くため、全分岐の atom order / atom count が互換であることを運用上の invariant として明記する必要がある。

BioEmu 候補は同じ配列の monomer に限定し、`prepare_complex` の設定も全分岐で同一にする。枝ごとに protonation、欠損補完、ligand、chain selection が変わるような使い方は MVP では非対応にする。

### 5. `branch_prep_for_candidates` は急がない

`prepare_complex` は既に `--source-candidate-id` を受け取れる。MVP では branch 作成 CLI を増やさず、まず 1 candidate を選んで既存 MD workflow に流す。

多分岐が必要になった段階で、薄い helper として `branch_prep_for_candidates` を追加する。これは新しい DAG 規約ではなく、`create_node(job_dir, "prep", parent_node_ids=["source_001"], conditions={"source_candidate_id": ...})` を繰り返すだけにする。

## 改善後アーキテクチャ

### Phase 1: surrogate source bundle 生成だけ

候補生成の新規 CLI は 1 つ。これとは別に deploy 確認用の utility CLI として `setup_surrogate_backend` / `check_surrogate_backend` を置く。

```python
generate_surrogate_candidates(
    amino_acid_sequence: str,
    model: str = "bioemu",
    num_samples: int = 100,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
    output_dir: str | None = None,
    job_dir: str | None = None,
    node_id: str | None = None,
    **model_options,
) -> dict
```

責務:

1. 入力 validation を行う。
2. `model` で backend を選ぶ。
3. backend 固有の managed venv 内で surrogate model を subprocess 実行する。
4. trajectory/topology 出力、または構造群を受け取る。
5. `candidate_NNN.pdb` に分割する。
6. `source_bundle.json` を `source_type="surrogate"` で作る。
7. node mode なら source node を complete する。

Phase 1 で作るファイル:

- `mdclaw/surrogate_server.py`
- `tests/test_surrogate_server.py`
- `skills/bioemu-sample/SKILL.md`
- `docs/research/md_surrogate_integration.md`
- `docs/developer/configuration.md` の surrogate backend deploy 説明

Phase 1 で既存改修するファイル:

- `mdclaw/source_bundle.py`: trajectory を candidate PDB に分割する helper
- `mdclaw/_registry.py`: surrogate server 登録
- `mdclaw/__init__.py`: export 更新
- `README.md`: MD surrogate source の位置づけを短く追記

Phase 1 でやること:

- BioEmu sampling と source_bundle 化
- HPacker による side-chain repack（in-process、`--reconstruct-sidechains false` で無効化可）
- backbone-only PDB の provenance 保管

Phase 1 でやらないこと:

- OpenMM minimize / 制約付き relax
- energy ranking
- clustering
- prep fan-out
- multi-prod 解析拡張
- reweighting

### Phase 2: 少数候補の選択

候補選択 CLI を 1 つ追加する。

```bash
mdclaw select_source_candidates \
  --job-dir <job_dir> \
  --node-id source_001 \
  --strategy evenly_spaced \
  --k 10 \
  --tag bioemu_k10
```

最初の strategy は軽くする。

- `evenly_spaced`: source bundle の順序から等間隔に取る
- `random_seeded`: seed 固定でランダムに取る
- `rmsd_farthest`: Cα RMSD の farthest-first sampling

`cluster_kmedoid`、energy ranking、clash score は後でよい。最初から sklearn や独自 k-medoid を入れない。

### Phase 3: 選ばれた候補の補正と relax

HPacker repack は Phase 1 で済ませてあるので、ここでは relax / minimize 系の補正を加える。

候補:

1. 選択済み k 個に対する OpenMM local minimize（clash 解消）。
2. `bioemu.sidechain_relax` 相当の制約付き短時間 MD equilibration（任意）。
3. HPacker helper を `create_mutated_structure` と共有する。

この Phase のゴールは「選択済み k 個を安定して prep に渡す」ことであり、全 BioEmu sample を minimize することではない。

### Phase 4: 多分岐 MD

必要になったら `branch_prep_for_candidates` を追加する。

```bash
mdclaw branch_prep_for_candidates \
  --job-dir <job_dir> \
  --source-node-id source_001 \
  --tag bioemu_k10 \
  --dry-run
```

この CLI は dry-run を標準導線にする。作る node の一覧、candidate ID、label、予想される `prepare_complex` 引数を先に表示し、実行時は `prep` node を作るだけにする。

### Phase 5: 集約解析

既存の analyze multi-parent を使う。ただし、以下の invariant を満たすときだけ対応する。

- 全 prod parent が同じ配列由来
- 全 branch の topology が atom-order-compatible
- 同一 solvent/modeling options で作られている
- analyze parent は prod のみで mixed parent にしない

`frame_provenance.json` などの provenance 拡張はこの段階で追加する。MVP には入れない。

## Source bundle 設計

`source_bundle` の schema は変えない。source type は backend 固有名ではなく `"surrogate"` にし、実際のモデル名は `origin.kind` に入れる。

```json
{
  "schema_version": 1,
  "source_type": "surrogate",
  "structures": [
    {
      "candidate_id": "candidate_001",
      "rank": 1,
      "is_primary": true,
      "path": "candidates/candidate_001.pdb",
      "origin": {
        "kind": "bioemu",
        "bioemu_frame_index": 0,
        "bioemu_num_samples_requested": 100,
        "bioemu_filter_passed": true
      },
      "metrics": {},
      "tags": ["backbone_only"]
    }
  ]
}
```

共通情報:

- `origin.kind`: `"bioemu"` などの backend 名
- `origin.surrogate_model`: backend 名または model id
- `origin.surrogate_version`

BioEmu 固有情報は `origin` に寄せる。

- `bioemu_frame_index`
- `bioemu_checkpoint`
- `bioemu_version`
- `msa_path_used`
- `msa_sha256`
- `filter_samples`
- `bioemu_filter_passed`

MVP では `metrics` を無理に埋めない。confidence がないことを正直に表現する。

## エラー方針

Phase 1 のエラーは backend 実行と bundle 生成に限定する。

| 状況 | 挙動 |
|---|---|
| backend が現在の OS/GPU に非対応 | early error。BioEmu 実走は Linux/GPU を推奨 |
| 未対応の `--model` | 利用可能 model 一覧を返して fail |
| backend venv がない | `setup_surrogate_backend --model ...` のコマンドを返して fail |
| backend import 失敗 | `check_surrogate_backend` の結果と reinstall 手順を返して fail |
| multimer/ligand/PTM らしき入力 | early reject。Boltz-2 を案内 |
| sequence 長が短すぎる | early reject |
| backend subprocess 失敗 | stderr と再現コマンドを artifact に保存 |
| trajectory が読めない | fail。source node は complete しない |
| accepted candidate が 0 | fail |
| accepted candidate が要求より少ない | warning 付きで bundle 化 |

自動 retry は最初は入れない。retry は便利だが、失敗原因を隠してテストも複雑にする。

## テスト計画

### Unit

- `generate_surrogate_candidates --model bioemu` は subprocess を完全 mock する。
- `setup_surrogate_backend` は install command construction を mock し、実 network install は unit test で行わない。
- `check_surrogate_backend` は managed venv の Python 実行を mock する。
- mock trajectory から `candidate_NNN.pdb` が生成されることを確認する。
- `source_bundle.json` が `source_type="surrogate"` と `origin.kind="bioemu"` を持つことを確認する。
- CLI discovery で `generate_surrogate_candidates` が出ることを確認する。
- macOS / missing env / invalid sequence の early error を確認する。

### Smoke

GPU/Linux 環境があるときだけ skip 解除する。

```bash
mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 3 \
  --max-candidates 3 \
  --job-dir <job_dir> \
  --node-id source_001

mdclaw list_source_candidates \
  --job-dir <job_dir> \
  --node-id source_001
```

### Integration

Phase 1 の integration は 1 candidate だけを既存 `prepare_complex` に渡すところまで。

```bash
mdclaw prepare_complex \
  --job-dir <job_dir> \
  --node-id prep_001 \
  --source-candidate-id candidate_001
```

候補は HPacker repack 済みなので、prepare 側で side-chain が無いことに起因する破綻は出ない前提。それでも prepare が壊れる場合は Phase 3 で minimize/relax を加える設計に進む。

## 採用する実装順

1. `generate_surrogate_candidates --model bioemu` を作る。
2. `setup_surrogate_backend` / `check_surrogate_backend` で BioEmu deploy を mdclaw から確認できるようにする。
3. backbone candidate を抽出して source bundle 化する。
4. 抽出後に HPacker で in-place repack し、`candidates_backbone/` に raw を保管する。
5. `list_source_candidates` で候補が見えることを確認する。
6. 1 candidate を `prepare_complex` に渡して、どこで壊れるか観測する。
7. 壊れた事実に基づき Phase 3 で minimize / relax を最小実装する。
8. 選択 CLI を追加する。
9. 多分岐 prep helper を追加する。
10. 最後に multi-prod 解析 provenance を追加する。

## 判断基準

この統合の価値は「MD surrogate model を mdclaw の source generator として安全に差し込めること」にある。したがって、最初の成功条件は ensemble MD の完全自動化ではなく、次の 3 点にする。

- BioEmu など backend 固有の重い依存関係が mdclaw env を汚さず、mdclaw の utility CLI で導入確認できる。
- surrogate 出力が既存 `source_bundle` 契約に収まる。
- 既存の `prepare_complex --source-candidate-id` に 1 候補を渡せる。

この 3 点が安定してから、候補選択、多分岐、解析を足す。
