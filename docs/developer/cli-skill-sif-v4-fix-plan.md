# MDDataBench v4 の cli_skill_sif 失敗4件：原因と MDClaw（CLI・skill）の修正計画

作成: 2026-09-28。実装前の計画（引き継ぎ用）。checkout `53f2595`。下で参照する
`mdclaw/node/lifecycle.py`、`mdclaw/node/constants.py`、`mdclaw/slurm/submit.py`、
`mdclaw/slurm/preflight.py`、`mdclaw/simulation/production.py`、`mdclaw/_envelope.py`、
`skills/hpc-run/submit-single.md`、`skills/md-prepare/SKILL.md` は、キャンペーンで使ったイメージの
`0528df5` から変わっていない（行番号はどちらでも同じ）。

根拠: MDDataBench のキャンペーン `runs/glm-5.3-flash-3cond-full-v4`（pi + rikyu/glm-5.3-flash、
98 タスク × 3 条件 × 3 反復 = 882 試行、エージェント隔離あり、イメージ membranecache-3dd0abe30156 =
mdclaw `0528df5`、pi の skill は `87050da`、2026-09-26 21:30 〜 09-28 01:43 JST）。
cli_skill_sif は 290/294 合格。失敗した4件をすべて transcript・ジョブ・コードまでさかのぼって調べた。

範囲: MDClaw の CLI と skill だけ。ハーネス（MDDataBench）側の対応（採点ジョブ投入の再試行、
010 r3 の採点し直しなど）は含めない。

## 要約

| 試行 | 失敗コード | 直接の原因 | 主な修正箇所 |
|---|---|---|---|
| 004_membrane_5zkb r3 | `node_execution_context_invalid` | prod を topo の子として作れた | `create_node` の親の型検査、`submit_job` の事前検査 |
| 010_membrane_6kux r3 | `scorer_submit_failed`（ハーネス側） | Slurm のタイムアウト後、同じ prod ノードに2本目のジョブ | `submit_job` の結果不明時の扱い、`begin_node` の二重実行防止 |
| 015_antibody_1ahw r1 | `agent_no_submission` | 鎖ごとの残基範囲を知るため自作した python が無限ループ | `inspect_molecules` の既定出力、source 完了時の `next`、`prepare_complex` |
| 015_antibody_1ahw r2 | `production_incomplete` | 3 ns を 20 分のジョブ1本に入れ、時間切れで打ち切り | `run_production` の時間制限の扱い、`submit_job` の見積もり |

4件とも、skill に書かれた手順自体は正しかった。エージェントが skill から外れたとき、CLI がそれを
受け付けたか、必要な情報を既定では出していなかった。設計原則（skill は意図を述べ、ツールが拒否する）に
従い、直すのは CLI で、skill は短くするだけにする。

共通する構造（MDDataBench 側のセッションでも独立に同じ結論に達した）: **実行時にしか行われない検査は、
エージェントには見えない。** 拒否や打ち切りが Slurm ジョブの中で起きるころには、エージェントは
セッションを終えている（004 r3 は 1800 秒中 462 秒、015 r2 は 669 秒で終了）。`create_node` や
`submit_job` のように、エージェントが結果を受け取る同期的な呼び出しで拒否していれば、残り時間で
直せた。したがって、実行時の検査をなくすのではなく、同じ検査を作成時・投入時にも行う。

## 1. `create_node` が親の型を検査しない（004_membrane_5zkb r3）

### 経過

- 37 回の LLM 呼び出し、462 秒。skill を 16 ページ読んでいる（`hpc-run/submit-single.md` を含む）。
- min・eq・prod を一度に、すべて `topo_001` の子として作った（transcript の原文）:

  ```bash
  ./mdc.sh --job-dir $JD create_node --node-type min --parent-node-ids topo_001 \
    --conditions '{"max_iterations": 5000, "restraint_atoms": "solute_heavy", "restraint_force_constant": 100.0}'
  ./mdc.sh --job-dir $JD create_node --node-type eq --parent-node-ids topo_001 \
    --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0, "nvt_time_ns": 0.5, "npt_time_ns": 1.0}'
  ./mdc.sh --job-dir $JD create_node --node-type prod --parent-node-ids topo_001 \
    --conditions '{"simulation_time_ns": 1.0}'
  ```

- 3 つとも `success: true`。prod_001 の `submit_job` も成功し、`condition_preflight` は `code: ok` だった。
- ジョブ: 141574（min）COMPLETED、141575（eq）COMPLETED、141576（prod）FAILED。
  - eq は互換用の経路で topo_001 から走り、min_001 の出力は使われなかった。
  - prod は実行時に `Node 'prod_001' cannot run with parent 'topo_001' of type 'topo'; expected one of ['eq', 'prod']`
    で拒否された。
- skill の例（`skills/hpc-run/submit-single.md:8`、`:19`）は min ← topo、eq ← min と正しい。エージェントが外れた。

### 原因（コード）

- `create_node` は、親ノードが存在するかどうかしか見ていない（`mdclaw/node/lifecycle.py:544-551`）。
- 親の型の表 `_ALLOWED_PARENT_TYPES`（`mdclaw/node/constants.py:168`）は実行時の検査
  （`mdclaw/node/lifecycle.py:1453-1469`）でだけ使われる。この表では prod の親は `{"eq", "prod"}`。
  eq の親には互換用に `topo` も入っている。
- `submit_job` の事前検査（`mdclaw/slurm/preflight.py:60-108`）は、run_production の宣言した条件値と
  CLI の引数を比べるだけで、ノードの親の型は見ない。
- 拒否したのは、ジョブが計算ノードで動き始めてからの `run_production` だけだった。その時点で
  エージェントはもう終了している（462 秒で終了、持ち時間は 1800 秒）。作成時か投入時に拒否されて
  いれば、時間内に直せた。

### 修正

1. **`create_node` で親の型を検査する。**
   - 明示された各親について、`nodes_index[parent]["type"] in _ALLOWED_PARENT_TYPES[node_type]` を確かめる。
   - 合わなければ `success: false`、`code: parent_type_invalid`（実行時の検査がすでに使っているコード）で拒否する。
   - `next_action` には `_auto_resolve_parent` で求めた正しい親を入れる
     （例: `mdclaw create_node --job-dir J --node-type prod --parent-node-ids eq_001`）。
     「`--parent-node-ids` を省けば自動で解決する」とも添える。
   - fep や ABFE の topo ← eq など、表に書かれた特別な組み合わせは表のとおりに通す。
   - `parent_type_invalid` が `mdclaw/guardrail_codes.py` と `tests/data/guardrail_codes.json` に
     登録されているか確かめ、なければ登録する（`tests/test_guardrail_code_registry.py`）。
2. **canonical な study ジョブでは、topo の下に eq を作らせない。**
   - 同じ topo の下に min がある（またはジョブが study 文脈を持つ）のに eq の親に topo を指定したら、
     拒否するか警告する（新しいコード、例: `eq_parent_should_be_min`）。
   - topo → eq は、min のない古い DAG のためだけに残す。
3. **`submit_job` の事前検査で、構造の検査をすべての run_* に広げる。**
   - 実行時の検査（`lifecycle.py` の context 検査）のうち、ノードの型とツールの対応、親の型、終了済みかどうか
     だけを投入前にも行う。親が pending なのはチェーン投入のために許す。
   - 引っかかれば sbatch を呼ぶ前に `node_execution_context_invalid` で拒否する。

### テスト・受け入れ条件

- `create_node` が prod ← topo、min ← eq などを `parent_type_invalid` で拒否し、`next_action` に正しい親を出す。
- prod ← eq、prod ← prod、eq ← min、eq ← eq、ABFE の topo ← eq はこれまでどおり通る。
- min のない古い DAG での eq ← topo の既存テストが通る。
- 親が topo の prod ノードに対する `submit_job` を、sbatch を呼ぶ前に拒否する。

### 同じ修正で防げる他の失敗

- 039_ligand_3ikd cli_sif r3、027_complex_1b6c cli_sif r1（同じパターン）。

### skill

変更は不要。

## 2. `submit_job` の「結果不明の投入」と `begin_node` の二重実行（010_membrane_6kux r3）

試行としての最終的な失敗は、ハーネスの採点ジョブ投入が同じ Slurm のタイムアウトで落ちたもので、
今回の範囲外。MDClaw 側の問題は、その手前で同じ prod ノードに2本のジョブが走ったこと。

### 経過（時刻は UTC）

- 17:03:11 min（141977）、17:03:17 eq（141978）を投入。
- 17:03:33 prod の投入で sbatch が
  `sbatch: error: Batch job submission failed: Socket timed out on send/recv operation` を返した。
  実際には Slurm がジョブ 141979（prod_6kux）を作っていた。
- `submit_job` は `success: false`、`code: unhandled_error`、
  `next_action: "Read the message and errors, fix the reported cause, then retry."` を返した。
- 17:03:46 エージェントがそれに従って投入し直し、141980（prod_6kux）ができた。
- 141979 と 141980 は、どちらも 02:05:09 JST に開始し、同じ prod_001 を GPU 2枚で約3分ずつ計算した。
  - 141980 が 02:08:05 に COMPLETED になり、ノードを確定させた。
  - 141979 は 02:08:06 に `node_terminal`（`terminal node.json record is sealed`）で FAILED になった。

### 原因（コード）

- `mdclaw/slurm/submit.py:444` で sbatch を呼ぶ。
  - `CalledProcessError` はすべて `errors.append("sbatch failed: …")` になり、`code` が付かない
    （`submit.py:528-532`）。そのため CLI の外枠が `unhandled_error` を付ける（`mdclaw/_common.py:565`）。
  - `finally` で `_clear_slurm_submission_intent` を呼び、ノードの投入予約を消す（`submit.py:536-541`）。
    ジョブが作られていても、ノードは未投入に見える。
- `begin_node`（`mdclaw/node/lifecycle.py:1187`）は `_apply_status(..., "running")` を呼ぶだけ。
  `_apply_status` は終了済みのノードへの書き込みは拒むが、running → running は通す。
  2本目のジョブも最後まで計算してから、完了の書き込みで初めて落ちる。

### 修正

1. **投入に印を付け、結果が不明なときは Slurm に問い合わせる。**
   - sbatch スクリプトに `#SBATCH --comment=mdclaw:<submission_intent_id>` を入れる
     （`submission_intent_id` は `submit.py:431` ですでに作っている）。
   - 結果が不明な失敗（`Socket timed out on send/recv operation`、`Unable to contact slurm controller`、
     `Zero Bytes were transmitted or received`、`subprocess.TimeoutExpired` など）では、
     `squeue --me -h -o "%i|%k"`（なければ sacct）で印を 30〜60 秒探す。
     - 見つかれば投入できたものとして扱う（`_stamp_slurm_on_node`、追跡記録、`success: true` と警告）。
     - 見つからなければ `code: slurm_submit_uncertain` を返し、投入予約は消さない。
       `next_action` は `mdclaw check_job --job-dir J --node-id N`（印で探して、見つかれば記録し、
       なければ予約を解く）。
   - `_validate_node_ready_for_slurm_submit` は、結果不明の予約が残っているノードへの再投入を拒む。
2. **その他の sbatch の失敗には安定したコードを付ける。**
   - `code: slurm_submit_failed` と sbatch の stderr を返し、`unhandled_error` にしない。
3. **`begin_node` で二重実行を防ぐ。**
   - `node.lock` の中で、実行者（`SLURM_JOB_ID`、ホスト、pid、開始時刻）を metadata に記録する。
   - すでに running で、記録された別の実行者が生きている（Slurm のジョブが RUNNING、または同じホストの
     pid が生きている）なら、計算を始める前に `node_already_running` で終了する。
   - 記録された実行者がもういなければ引き継ぐ（異常終了したジョブの後始末）。
   - `submit_array_job` と `submit_mps_job` の経路も同じ `begin_node` を通るので、あわせて守られる。

### テスト・受け入れ条件

- タイムアウトを出して終了コード 1 で終わるが、ジョブは作られている偽の sbatch（偽の squeue が印を返す）で、
  `submit_job` がそのジョブを採用し、ノードに記録する。
- ジョブが作られていない偽の sbatch では `slurm_submit_uncertain` を返し、予約が残り、2回目の投入は拒否される。
  `check_job` で予約が解ける。
- `begin_node` は、生きている別の実行者がいれば `node_already_running` で拒否し、いなければ引き継ぐ。

### skill

必要なら hpc-run に「`slurm_submit_uncertain` は `next_action` に従い、同じノードに投入し直さない」の1行を足す。
`next_action` が十分なら不要。

### 補足

キャンペーンの監視で「隔離のすり抜け」として検出された 141979 は、この経過で記録の残らなかったジョブ。
ジョブ自体は隔離のラッパー経由で投入されている。

## 3. 鎖ごとの残基範囲が既定の出力に出ない（015_antibody_1ahw r1）

### 経過

- タスクは「chain A 1–214、chain B 1–214、chain C 4–211」。1AHW には蛋白質の鎖が A〜F の6本ある。
- エージェントは skill を 16 ページ読み、study を作り、source を取得した（source_001 completed。
  `next` は「prep を作る」）。
- `skills/md-prepare/SKILL.md:144` は `inspect_molecules` の実行を指示しているが、エージェントは飛ばした。
  代わりに、鎖ごとの著者番号の範囲と欠損を出す python を自分で書き、鎖 A〜F について回した。
- その python では、内側の `while` の `i += 1` が `if p[idx['label_asym_id']] == ch:` の中にしかなかった。
  別の鎖の最初の原子で `i` が進まなくなり、無限ループになった。
- このツール呼び出しは戻らず、エージェントは 1800 秒の持ち時間を使い切り、ジョブは未投入。

### 原因（出力）

- `inspect_molecules` の既定の出力では `chains` がまるごと省略される
  （`{"_omitted": true, "chars": 104090, "see": "rerun with --output full"}`、`mdclaw/_envelope.py:284`）。
  `summary` と `action_contract` には鎖 ID しかない。
- `--output full` の `chains[].residue_numbering` には `count`、`first`、`last` と全残基の一覧がある。
  1AHW の鎖 C は first 4 THR、last 211 GLY、count 200 で、4–211 の途中に 8 残基の欠損がある。
- つまり、勧められたツールを使っても、エージェントが欲しかった情報は既定では見えない。
- source が完了したときの `next` は、`inspect_molecules` を飛ばして prep の作成を示す。

### 修正

1. **`inspect_molecules` の既定の出力に `chain_ranges` を足す（省略の対象にしない）。**
   - 鎖ごとに1行: `chain_id`、`author_chain`、型、最初と最後の残基（著者番号、挿入コード、残基名）、残基数、
     欠損（著者番号での区間のリスト）、挿入コード付きの残基（例: `1A`）。
   - 6 本の鎖でも数百文字に収まる。
2. **source の完了時の結果に同じ `chain_ranges` を載せ、`next` で prep より先に `inspect_molecules` を示す。**
   - 対象は `fetch_structure` と `register_local_structure`。
3. **`prepare_complex --residue-ranges` の結果に、鎖ごとに実際に残した範囲 `kept_residue_ranges` を返す。**
   - 次の場合は警告を出す: 指定した範囲が欠損をまたぐ（欠けた残基を示す）、境界と同じ番号の挿入コード残基を
     落とす（例: `A:1-79` が `1A` を落とす）、指定した範囲が構造にある残基を超える。

### テスト・受け入れ条件

- 欠損と挿入コードを含む構造で `chain_ranges` が正しく、既定の出力に入っている。
- `prepare_complex` が `kept_residue_ranges` を返し、挿入コードの取りこぼしと欠損をまたぐ範囲に警告を出す。

### 同じ修正で防げる他の失敗

- 036_ligand_1ceb cli_sif r2（`A:1-79` で 1A が落ちた）。
- 028_complex_1dfj cli_sif r1・r2（範囲を指定せず、鎖 I の N 末端の ACE が残った）。
- 024_antibody_5cba cli_sif r2（範囲を指定せず、鎖 B が1残基多くなった）。
- いずれも、残した範囲が結果に出ていれば、エージェントがタスクの指定と照らし合わせられた。

### skill

`skills/md-prepare/SKILL.md` の手順4を「残基番号と欠損は source の結果か `inspect_molecules` の
`chain_ranges` で確かめ、構造ファイルを自分で解析しない」に短くする。

## 4. `run_production` がジョブの時間制限を見ない（015_antibody_1ahw r2）

### 経過

- ハーネスは MD ジョブ1本の上限を 20 分にしている（エージェントの CAPABILITIES.md にも
  「Each MD Slurm job wall limit: 00:20:00」とある）。
- エージェントは次のように投入した（eq への afterok 依存付き）:

  ```bash
  submit_job --job-dir "$JOB" --node-id prod_001 \
    --script "mdclaw --job-dir $JOB --node-id prod_001 run_production --simulation-time-ns 3.0 --output-frequency-ps 10.0 --platform CUDA" \
    --job-name prod_1ahw --partition gpu --gpus 1 --cpus-per-task 4 --time-limit "00:20:00" --memory 64G \
    --dependency afterok:144251
  ```

- eq_001 は 1 ns の NVT と 1 ns の NPT（4 fs、HMR）を 11 分 54 秒で終えていた。約 10 ns/時なので、
  3 ns には約 18 分かかり、準備の時間を足すと 20 分を超える。
- ジョブ 144252 は 1221 秒で「DUE TO TIME LIMIT」で打ち切られた。
  - 残った成果物: trajectory.dcd（1.49 GB）、state.xml と checkpoint.chk（打ち切りの時刻に書かれている）、
    energy.dat。
  - prod_001 は `running` のままで、採点では `production_incomplete` になった。
  - エージェントは 669 秒の時点でもう終了していた。
- 合格した r3 は、1.4 ns の prod を2本（`--continue-from`）に分け、それぞれ約 10 分で通している。

### 原因（コード）

- `mdclaw/simulation/production.py` には、ジョブの時間制限を見る処理がない
  （`SLURM_JOB_END_TIME`、walltime、time_limit、deadline のどれも出てこない）。
- eq の metadata には step 数はあるが、計算速度（ns/day）や所要秒は記録されていない。

### 修正

1. **`run_production` を時間制限の手前で止め、ノードを使える状態で閉じる。**
   - 締め切りはジョブの終了時刻（環境変数 `SLURM_JOB_END_TIME` があればそれ、なければ
     `squeue -h -j $SLURM_JOB_ID -o %e` か `scontrol show job`）。
   - 締め切りから余裕（例: 90 秒と出力間隔2回分の大きい方）を引いた時刻で積分を止める。
     portable な state を書き、trajectory と energy を閉じる。
   - ノードは completed にし、metadata に実際の `simulation_time_ns`、`requested_simulation_time_ns`、
     `stopped_reason: "time_limit"` を記録し、警告を付ける。
   - `next` には残りの長さと延長の手順（`create_node --node-type prod --continue-from <node>`）を示す。
   - completed のノードは変更できないので、延長は既存の prod → prod で行う。
2. **min・eq・prod の metadata に計算速度を記録する。**
   - `ns_per_day` と所要秒。
3. **`submit_job` の事前検査で、本計算の所要時間を見積もる。**
   - 見積もり = `simulation_time_ns` ÷ 直近の完了した祖先の `ns_per_day` + 準備の時間。
   - `--time-limit` から余裕を引いた時間を超えるなら、`production_exceeds_time_limit` で拒否する。
     `next_action` には、1本あたりの長さと `--continue-from` でのつなぎ方（例: 「1.5 ns × 2 本」）を示す。
   - 祖先がまだ完了していない（チェーン投入）ときは、警告に留める。
4. （1 の後はまれだが）ノードの同期で Slurm の TIMEOUT を `slurm_time_limit` として失敗扱いにし、
   最後に書いた state を示す。`trace_failure` が延長を提案できるようにする。

### テスト・受け入れ条件

- `SLURM_JOB_END_TIME` を近い時刻にして `run_production` を動かすと、手前で止まり、実際の長さを記録して
  completed になり、`next` が延長を示す。
- metadata の `ns_per_day` から見積もりが `--time-limit` を超える場合、`submit_job` が
  `production_exceeds_time_limit` で拒否する。

### skill

`skills/md-production/` と `skills/hpc-run/` の説明を、「1本の時間制限を超える本計算は `--continue-from` で
つないだ prod に分ける。MDClaw は制限の手前で止めて、延長の方法を返す」の1行に置き換える。

## 実装の順序

1. **`create_node` の親の型の検査**（1 の修正1〜3）。既存の表を使う小さな変更で、3 件
   （cli_sif の 2 件を含む）を防げる。
2. **`run_production` の時間制限での停止、計算速度の記録、投入前の見積もり**（4）。時間切れでデータが
   宙に浮くのを防ぐ。
3. **`submit_job` の結果不明時の扱いと `begin_node` の二重実行の防止**（2）。二重実行は
   「決して起きてはならないこと」に当たる。
4. **`chain_ranges`、source の `next`、`prepare_complex` の `kept_residue_ranges`**（3）。

変更後は `docs/developer/tool-reference.md` と、影響する skill の例を更新する（`CLAUDE.md`「Adding Or Changing Tools」）。
検証として、この4タスク（004、010、015）と cli_sif で同じ型だった 027・028・036・039 を、
cli_skill_sif と cli_sif で小さく流し直すとよい。

## 証拠の場所

- 試行のディレクトリ:
  `/data1/rkp00079/rku00161/runs/glm-5.3-flash-3cond-full-v4/attempts/<task>/<condition>__pi__rikyu-glm-5.3-flash__rN/`
  - `agent.stdout.jsonl`: pi の transcript（`tool_execution_start` / `tool_execution_end` の行にコマンドと出力）。
  - `result.json`: 失敗コード、指標、採点の各項目（`artifacts.score`）。
  - `events.jsonl`: エージェントの開始・終了、採点の投入。
  - `workspace/.mddatabench/sbatch-events.jsonl`: シムが記録した sbatch の呼び出しと結果。
  - `workspace/study/jobs/main/nodes/<node_id>/node.json`: MDClaw のノードの状態と metadata。
- Slurm のジョブ: 004 r3 = 141574〜141576、010 r3 = 141977〜141980、015 r2 = 144250〜144252、
  015 r3（合格、比較用）= 144255・144257・144259・144260。
- transcript からツール呼び出しを一覧する例:

  ```python
  import json
  starts = {}
  for line in open("agent.stdout.jsonl", errors="replace"):
      if '"tool_execution_start"' in line:
          r = json.loads(line); starts[r["toolCallId"]] = (r.get("args") or {}).get("command") or ""
      elif '"tool_execution_end"' in line:
          r = json.loads(line)
          out = "".join(c.get("text", "") for c in (r.get("result") or {}).get("content", []) if c.get("type") == "text")
          print("CMD:", " ".join(starts.get(r["toolCallId"], "").split())[:200])
          print("OUT:", " ".join(out.split())[:300])
  ```

## 実装状況（2026-09-29）

4 節とも実装し、単体テストを追加した（`docs/memo.md` の同日の項に詳細）。計画との差分:

- §1: `create_node` の `parent_type_invalid` と `eq_parent_should_be_min` は計画どおり。study 文脈で
  min のない topo の下に eq を作るのは拒否ではなく警告（`eq_parent_is_topo`）に留めた。`submit_job` /
  `submit_array_job` / `submit_mps_job` の構造検査は `node_structure_preflight`（親の型・失敗した親・
  ツールと段の対応。pending の親は通す。progress.json のない素のディレクトリは判定しない）。
  同時に、`run_metadynamics` 追加以降 prod 段の推奨ツールが `run_metadynamics` になっていた
  envelope のバグを直した（`_STAGE_PREFERENCE["prod"]`）。
- §2: 印は `#SBATCH --comment=mdclaw:<intent>`。結果不明時は `find_marked_job` が squeue を最大 30 秒
  （`MDCLAW_SLURM_CONFIRM_SECONDS`）探し、見つかれば採用、なければ `slurm_submit_uncertain` で予約を
  残す。解決は `check_job --job-dir J --node-id N`（`settle_uncertain_submission`）。二重実行の防止は
  rounds の owner record（`mdclaw/node/owner.py` に移動、旧パスは shim）を `begin_node` が使う。
  Slurm の終了同期は、別の生きた実行者が持つ running ノードを失敗にしない。
  配列 / MPS 投入には印を付けていない（単発投入のみ）。
- §3: `inspect_molecules.chain_ranges`（`compact_numbering`）、source 完了時の `chain_ranges`
  （metadata と結果、`next.inspect_command`）、`prepare_complex.kept_residue_ranges`
  （`split_molecules.delivered_chain_ranges`）と範囲の警告。
- §4: `DeadlineStepper`（`mdclaw/simulation/deadline.py`）で `run_production` を締め切りの手前で止め、
  実際の長さで completed にし、`next` を延長にした。rounds のセグメントは対象外。eq に
  `md_seconds` / `wall_seconds` / `ns_per_day`、min に `wall_seconds`。`submit_job` の見積もりは
  `production_time_budget`（親の `ns_per_day` と同じプラットフォーム族のときだけ）。修正 4 の
  `slurm_time_limit` への読み替えは、修正 1 で必要性が下がったので見送った。

## 対象外（参考）

- ハーネス側: 010 r3 の採点し直し（MD は COMPLETED なので合格する見込み）、採点ジョブ投入の再試行、
  リセット時の旧ジョブの取り消しなど。キャンペーン全体の結果と sif_only の分析は、MDDataBench 側の memo の
  案（未追記）にまとめてある。
