# prep 段階の 4 欠陥（ジスルフィド検証・CLI 型・多残基リガンド・ループ補完）の修正計画

作成: 2026-09-10、checkout `a4ad51c`。同日 `6168c15`（7 コミット追加）で再確認: 4 件に関わる
`genesis/modeller.py` / `structure/*` / `guardrail_codes.py` に変更なし、`_cli.py` は受領票（C9）の
追加のみで例外経路と型判定は同じ。実装前の計画。

**状態（2026-09-10 夜）: 5 項目すべて実装済み。** 単体テスト追加、golden 再生成、実 3 系を deposit から
作り直して `studies/wang/check_hum_builds.py` が PASS（詳細は `docs/memo.md` の同日エントリ）。
実装中に見つかった追加の欠陥 1 件（テンプレート内のリガンド塊で exact-site 番号復元が
全放棄する）も `_restore_template_frame` で修正済み。
根拠: Wang 2025 の human/mouse 甘味受容体 ECD 3 状態（9OPW / 9OPZ / 9OQ1）を
`studies/hum-ecd-*` として構築した際の記録
（[hum-systems.md](../research/t1r_ecd_campaign/hum-systems.md)）。
行番号は `6168c15` 時点。

## 判定の整理

| # | 現象 | 原因 | 評価 |
|---|---|---|---|
| 1 | 明示したジスルフィドが deposit の S–S 1.30 Å で拒否される。自動検出なら同じ対が採用される | 検証窓 1.8–2.3 Å は明示対のみに適用。自動検出は下限なし | 一貫性の欠陥 |
| 2 | `modeller_from_alignment --disulfide-patches` が落ち、ノードが running のまま残る | 素の `list` 型が文字列扱い。例外が `begin_node` の後・`try` の前で発生し、CLI も非 node 必須ツールを失敗にしない | 欠陥。失敗後の封印は仕様 |
| 3 | MODELLER 出力 PDB を source にすると sucralose が 2 残基に分かれ prep が失敗 | リガンド単位が gemmi subchain。PDB は hetero 残基ごとに subchain が自動生成される | 欠陥（PDB 経由の多残基リガンド全般） |
| 4 | 欠損ループがリガンド不在で構築され、9OPZ で sucralose と 0.25 Å 衝突。警告なし | 修復前に非ポリマー鎖をテンプレートから削除 | 既知の制限（`docs/memo.md` 2026-09 の 9UTC 記述）。自動検査の欠如が改善点 |

## 前提と影響範囲

- ログイン側 CLI は `bin/mdclaw` が checkout を `PYTHONPATH` で重ねて実行する
  （`bin/mdclaw:154`）。4 件はすべて prep 段階なので、修正は SIF を再構築せずに検証できる。
- 計算ノード側（min/eq/prod）は `.mdclaw_cluster.json` の `source_mode=image` で
  SIF 内蔵パッケージを使う。4 件とも計算ノード側には関係しない。
- closed campaign の実行中 24 job（旧 SIF）には触れない。
- 既存の終端ノードや過去の prep 結果は書き換えない。

## 1. ジスルフィド検証の対称化

主対象: `mdclaw/structure/clean_protein.py`（`DISULFIDE_BOND_MIN/MAX_ANGSTROM` 1259–1260 行、
`_validate_declared_disulfides` 1263–1316 行、呼び出し 1965 行）、
`mdclaw/structure/prepare_complex.py`（2444 行、`_merge_disulfide_pairs` 1450–1459 行、
`sa_disulfide_pairs` 1776 行）、`mdclaw/structure/disulfide.py`
（`_detect_disulfide_candidates` 326 行以降、high 判定 400 行）。

- 検証結果を「結合が形成されていない（長すぎる）」と「原子が重なっている（短すぎる）」に分ける。
  長すぎる: 現行どおり error、`modeller_disulfide_not_formed`。
  短すぎる: warning、新 code `disulfide_sg_overlap`。`distances` に距離と
  `geometry: "overlap"` を記録し、`confirmation_needed.disulfide_bonds` にも載せる。
  理由: ループ再構築は観測残基を固定するので、deposit 由来の重なりは MODELLER では直らず、
  最小化で解消する種類の欠陥である。
- 自動検出側にも同じ分類を入れる。`_detect_disulfide_candidates` で 1.8 Å 未満の候補に
  `geometry: "overlap"` を付け、prep の warnings に同文で出す。`confidence` /
  `recommendation` は変えない（採用自体は正しい）。
- 明示・自動どちらでも、MODELLER 修復の前に入力構造上で対を測り、deposit 側の値を
  `declared_disulfide_validation.input_distances` として残す。修復後の値と並べれば、
  「MODELLER が壊した」のか「最初から短い」のかを結果 JSON だけで区別できる。
- 受領票（`mdclaw/_receipt.py:_facts_prep` 154–216 行、6168c15 で追加）の `disulfides` に
  重なり対を `A59-A102 (overlap 1.30 A)` の形で出す。
- `mdclaw/guardrail_codes.py` に `disulfide_sg_overlap` を登録し、対処文に
  「最小化で解消される。SG を置き直したい場合は…」を書く。
  `tests/test_guardrail_code_registry.py` の golden を更新する。
- 自動検出モードでは MODELLER に DISU パッチが渡らない（`sa_disulfide_pairs=None`）。
  これは skill 文書の記述どおりの仕様なので今回は変えないが、結果 JSON の
  `disulfide_source` の隣に `modeller_patches: 0` を出して見えるようにする。

テスト: `tests/test_disulfide_contract.py` に、合成 PDB で SG–SG 1.30 / 2.05 / 3.53 Å の
3 ケース（warning / ok / error）。`tests/test_complex_missing_residue_repair.py` の
monkeypatch 方式で、修復結果に 1.30 Å の明示対が含まれても `overall_status=success` かつ
warning が付くこと。`tests/test_disulfide_schema_handoff.py` で新フィールドが additive で
あること。

完了条件: 9OQ1 deposit に 17 対を明示して prep が成功し、A59–A102 / B236–B522 が
`disulfide_sg_overlap` の warning として報告される。自動検出でも同じ 2 対に同じ warning が出る。

## 2. CLI の素の `list` 型と、未処理例外で running のまま残るノード

主対象: `mdclaw/genesis/modeller.py`（シグネチャ 724–725 行、`begin_node` 934 行、
config 生成 940–964 行、`try` 965 行、`fail_node` 1005 行）、`mdclaw/_cli.py`
（`_takes_json` 439 行、else 分岐 690 行、`unhandled_exception` 1728 行以降、
`_record_cli_node_failure` 733 行）、`tests/data/cli_contract.json`。

2a. 型の修正
- `disulfide_patches: Optional[list[list[int]]]`、`target_residue_sites: Optional[list[dict]]`
  に変える。`--list-json` 全走査で素の `list` はこの 2 引数だけ（2026-09-10 監査）。
- `_tool_param_specs` で素の `list` / `List` / `dict` なし添字の引数を検出したら
  起動時に `TypeError` にする（黙って `str` に落とさない）。
  `tests/test_cli_contract.py` に「素の list 注釈を持つツール引数はない」検査を追加。
- golden `tests/data/cli_contract.json` はファイル冒頭の手順で再生成する。

2b. ノード状態
- `modeller_from_alignment`: 引数検証と config 生成（960 行の unpack を含む）を
  `begin_node` より前に移す。`begin_node` 以降は一つの `try/except Exception` で包み、
  `fail_node(job_dir, node_id, errors=[...])` を必ず呼ぶ。現在の `except` は
  `subprocess` 実行部だけを覆っている。
- `_cli.py` の例外経路: `requires_node` に関わらず、`effective_job_dir` /
  `effective_node_id`（1504–1509 行で非 node 必須ツールでも解決済み）が揃い、
  かつ `read_node(...).status == "running"` なら `fail_node` を呼んでから
  `_record_cli_node_failure` を呼ぶ。後者は証跡を保存するだけで状態を変えない
  （733–775 行）。
- 「失敗ノードは終端で不変」は仕様として維持する。封印されたノードに再実行を
  かけたときの `NodeSealedError` は、`create_node --parent-node-ids` で新ノードを作る
  案内文つきの構造化エラーにする（現状は `unhandled_exception`）。

テスト: `tests/test_modeller_runner_contract.py` に CLI 経由で
`--disulfide-patches '[[3,40]]'` が config に整数対として届くこと。
`tests/test_failure_trace.py` に、`begin_node` 後に例外を投げる偽ツールを CLI から呼び、
終了後に `status=failed` と `tool_failed` イベントが残ること。

完了条件: 2026-09-10 の 1 回目の失敗と同じ引数で再実行すると、素の文字列ではなく整数対が
渡り、実行が失敗した場合もノードは `failed` になる。

## 3. PDB 経由の多残基リガンド

主対象: `mdclaw/structure/split.py`（subchain 走査 338 行・1668 行、自動 subchain の説明
1295 行、出力名 1926–1930 行）、`mdclaw/structure/clean_ligand.py`（多残基統合 393 行以降）、
`mdclaw/genesis/modeller.py`（`_stage_template_as_pdb` 328 行、`_restore_template_frame` 189 行）。

3-i. split 側で共有結合単位にまとめる（主修正）
- subchain 分類の後に、ligand 種の単位どうしを次の順で連結成分にまとめる:
  (a) `structure.connections` の Covale（mmCIF `struct_conn`、PDB `LINK`）、
  (b) 記録がなければ、同じ author chain 内で重原子間距離 1.9 Å 以下の対。
  イオン・水・glycan 種は対象外。author chain をまたぐ結合、タンパク質との共有結合は
  まとめずに `covalent_partner_outside_unit` として報告する。
- まとまった単位は 1 ファイルに書き、`unique_id` は先頭残基のもの、`residue_names` に
  全残基名、split メタデータに `merged_from` を残す。`include_ligand_resnames` は
  いずれかの残基名が一致すれば単位全体を選ぶ。
- `clean_ligand` の統合処理はそのまま使える（deposit mmCIF では既にこの経路で 23 原子の
  sucralose になっている）。

3-ii. MODELLER ツール側（任意）
- `hetatm=True` でテンプレートが mmCIF のとき、`struct_conn` の Covale と entity 種別を
  保持し、`template_frame` 適用後のモデルを `<model>.cif` としても書く。
  3-i があれば必須ではない。

テスト: `tests/test_ligand_pathway.py` に、9OQ1 の sucralose 2 残基だけを PDB にした小さな
fixture（`tests/data/sucralose_rry_rrj.pdb`、タンパク質不要）で単位が 1 つ・23 原子になる
こと、`LINK` 行を消しても距離で同じ結果になること、結合していない 2 リガンドが同じ chain に
あっても分かれたままであること。mmCIF の branched entity の既存経路が変わらないこと。

完了条件: `studies/wang/models/9OPZ` の MODELLER 出力 PDB を source にした prep が、
完全 SMILES 指定で成功し、`ligand_chemistry` が 23 重原子の 1 分子になる。

## 4. ループ補完のリガンド不在

主対象: `mdclaw/structure/clean_protein.py`
（`_repair_missing_residues_with_modeller` 1561 行、非ポリマー削除 1655–1685 行、
`_write_repair_alignment` 805 行、再結合 1976 行以降、`segments`/`sites` 1868 行・2017 行）、
`mdclaw/guardrail_codes.py`。

4a. 距離検査（先に入れる、小）
- 修復モデルの検証（1965 行）の後、再構築した各セグメントの重原子と
  `nonpolymer_context`（リガンド・イオン）の重原子との最短距離を測り、
  `complex_missing_residue_repair.segments[i].min_distance_to_nonpolymer_angstrom` と
  最近接の残基・原子を記録する。
- 2.2 Å 未満: `success=False`、新 code `modeller_loop_nonpolymer_clash`、対処文は
  「4b の `--repair-nonpolymer-context` で再実行するか、`modeller_from_alignment --hetatm`
  でテンプレートにリガンドを含めて source を作り直す」。
  2.2–3.0 Å: warning。
- 閾値は定数として置き、結果 JSON に出す。受領票の prep `facts` に
  `rebuilt_segments: [{chain, range, min_distance_to_nonpolymer}]` を加える。

4b. リガンドを障害物としてテンプレートに残す（根本修正、中）
- 非ポリマー残基（水以外）を `polymer_template` から削除せず、ポリマー鎖の後ろに
  HETATM として残す。
- `_write_repair_alignment`: テンプレート行・ターゲット行の末尾に `/` と hetero 残基数ぶんの
  `.` を付け、ヘッダの `LAST:` を hetero 鎖にする。runner の `hetatm` は既に True。
- `_validate_modeller_repair_model`、`_restore_template_frame`、番号付け直しは hetero 残基を
  読み飛ばす。2026-09-10 の手動実行（`studies/wang/models`）では `template_frame=True` で
  hetero を含むモデルの CA 対応が問題なく取れている。
- MODELLER が書き戻した hetero 座標は捨て、従来どおり `nonpolymer_context` の元座標を
  再結合する（BLK は固定扱いだが、元座標を使えば下流の挙動が変わらない）。
- フラグ `--repair-nonpolymer-context`（既定 on）。BLK を含む MODELLER 実行が失敗した場合は
  従来経路に戻して warning を出す。4a の検査は経路に関係なく常に走らせる。

テスト: 4a は `_nonpolymer_clearance(model_path, context, segments)` を関数に切り出し、
合成モデルとリガンド座標で 1.0 / 2.5 / 5.0 Å の 3 ケース。4b は
`tests/test_modeller_runner_contract.py` にアラインメントの `.` ブロック数と `LAST:` の検査、
`KEY_MODELLER` がある環境だけで走る smoke（`tests/test_gap_local_modeller_smoke.py` の
skipif 方式）を 1 本。

完了条件: 9OPZ deposit を `--missing-residue-method modeller` で prep すると、4a のみの段階では
`modeller_loop_nonpolymer_clash` で止まり、4b 適用後は loop 45–57 が sucralose から 3 Å 以上
離れて成功する。9OQ1（loop 45–57 が観測済み）と 9OPW（リガンドなし）は結果が変わらない。

## 実施順と目安

1. 4a 距離検査（0.5 日）: 壊れた構造が黙って通る唯一の経路を先に塞ぐ。
2. 2a + 2b（0.5 日）: 小さく、他の作業の再実行を安全にする。
3. 1（0.5 日）。
4. 3-i（1 日）。
5. 4b（1–2 日）。3-ii は 3-i の後に必要なら。

各段階で `conda run -n mdclaw ruff check mdclaw/` と
`pytest tests/test_registry.py tests/test_cli.py tests/test_guardrails.py
tests/test_guardrail_code_registry.py tests/test_slurm_server.py tests/test_cli_contract.py
tests/test_disulfide_contract.py tests/test_complex_missing_residue_repair.py
tests/test_modeller_runner_contract.py tests/test_ligand_pathway.py`。
実データでの確認は `studies/_attempts/` と同じ手順を scratch の study で再現する
（deposit 9OPZ / 9OQ1 の mmCIF、`studies/wang/disulfides_hum.json`、完全 SMILES）。

## やらないこと

- 自動検出モードで MODELLER に DISU パッチを渡す仕様変更（別件として記録のみ）。
- deposit の SG 座標を自動で置き直す処理（`studies/wang/build_hum_templates.py` の chi1 探索は
  研究側の手順として残す）。
- 終端ノードの不変性の緩和。
- SIF の再構築（リリース時にまとめる）。
