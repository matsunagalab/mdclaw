# Weighted Ensemble Phase 3 — テスト依頼（GPU 実測と文献比較）

作成: 2026-09-21。対象は 2026-09-21 に実装した round 駆動サンプリング（`mdclaw/rounds/`）と
weighted ensemble（`mdclaw/we/`）。設計は `docs/research/weighted-ensemble-plan.md`、実装の記録は
`docs/memo.md` の 2026-09-21 エントリ。

## 0. 依頼の目的

1. 新実装を GPU で実系に適用し、**文献値と比較できる速度定数**（論文に載せられる品質: 同じ力場の
   brute force との一致、文献 MD・実験との差を力場の違いも含めて説明できる）を出す。
2. 実装側の数値を測る: segment 1 本のオーバーヘッド、round あたりの DAG 操作の時間、ノード数 10³ 級での
   `progress.json` / `events/` / ディスク。
3. **実行して気づいたことを全部フィードバックする**（§8）。スキルの記述、CLI の出力、待ち時間、迷った箇所、
   回避策。うまく動いた場合も「探すのに時間がかかった点」を書いてほしい。

## 1. 先に読むもの

- `skills/md-we/SKILL.md` → `pcoord-and-bins.md` → `kinetics.md`（WE の手順・pcoord の選び方・結果の読み方）
- `skills/md-production/rounds.md`（replica の round 駆動。WE と同じループ）
- `skills/hpc-run/SKILL.md`, `submit-single.md`（`run_rounds` を 1 GPU ジョブで回す形）
- `docs/developer/tool-reference.md` の `rounds/` と `we/` の節（引数と code）

## 2. 環境と約束事（RIKYU）

- checkout `/home/rku00161/mdclaw` は**未コミットの作業を含む。読み取り専用**。git 操作・編集・`pi update`・
  SIF の差し替えは禁止。バグは直さず §8 の形式で報告する（回避策を取ったらそれも書く）。
- 実行は launcher + 共有 SIF（checkout のコードをイメージに重ねる overlay）:

  ```bash
  export MDCLAW_RUNTIME=apptainer
  export MDCLAW_SIF=/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif
  export MDCLAW_OUTPUT=full
  M=/home/rku00161/mdclaw/bin/mdclaw
  "$M" --list-json run_rounds        # rounds / we のツールが見えれば環境は正しい
  ```

- 作業ディレクトリは `/data1/rkp00079/rku00161/we-trials/<系名>/`（study はその下の `study/`）。
  既存の trial の作り方は `/data1/rkp00079/rku00161/seus-trials/ala3-e2e/launch.sh` と
  `cln025-e2e/launch.sh` が手本（`register_local_structure` → prep → solv → topo → min → eq の並び、
  `submit_job` の形、`--output-dir slurm`）。
- Slurm: 各作業ディレクトリに `/data1/rkp00079/rku00161/runs/mps-bench-20260916/.mdclaw_cluster.json` を
  コピーして使う（image、`--nv`、`source_mode: overlay`、partition gpu、account rkp00079）。GPU ジョブは
  必ず `submit_job` から。`run_rounds` は 1 GPU ジョブに `--max-wall-hours (time-limit − 1)` で投げ、
  止まったら同じコマンドを再投入する（状態は DAG にある）:

  ```bash
  "$M" submit_job --job-name we1 --gpus 1 --cpus-per-task 2 --time-limit 24:00:00 --output-dir "$W/slurm" \
    --script "mdclaw run_rounds --job-dir $JD --scheme-id we1 --max-wall-hours 23 --platform CUDA"
  ```

- **予算: 合計 350 GPU 時間を上限**（目安 A ≈ 45、B ≈ 200、C ≤ 100）。長いジョブを投げる前に
  1 round だけ回して ns/day と segment の壁時間を実測し、見積もりを report に書いてから投げる。
  上限を超えそうなら止めて相談。GPU 時間の台帳（`sacct -X --format=JobID,JobName,Elapsed,AllocTRES,State`）を
  report に載せる。
- 速度定数は必ず `analyze_we` の `verdict` つきで報告する。`flux_transient` は下限、`no_target_events` は
  推定なし。verdict を通すために target を近づける・segment を短くする・bootstrap の幅を変える、はしない。

## 3. 全課題で記録する実装側の数値

| 数値 | 取り方 |
|---|---|
| segment 1 本の壁時間と MD 時間の内訳 | `events/` の `tool_started` → `tool_completed` の差（壁時間）と、segment の `energy.dat` / result の `ns_per_day` から MD 時間。差がオーバーヘッド（Simulation 構築 + state 読み書き） |
| round の壁時間と DAG 操作の時間 | `run_rounds` 結果の `elapsed_seconds` と `segments_run`。round 壁時間 − Σ segment 壁時間 = ノード作成・policy・plan の時間 |
| `progress.json` のサイズ、`events/` のファイル数 | ノード数 100 / 1,000 の時点で `ls -la progress.json; ls events | wc -l` |
| segment あたりのディスク | `du -sh nodes/prod_<scheme>_r0010_w0001` |
| `inspect_job` / `inspect_rounds` / `explain_node` の応答時間 | ノード数 1,000 前後で `time` |
| 失敗した segment の再走 | 起きたら node id、code、再走の結果 |

## 4. 課題 A: alanine dipeptide の φ 反転（αR / PPII → αL）— 同じ力場の brute force と厳密比較

**理由**: 遷移が 10–100 ns 級なので brute force で数十回の事象が安く取れ、WE の推定量を統計的に厳しく検証できる。
標準ベンチマークで文献（力場別の implied timescale）もある。segment が短い小系なので、オーバーヘッドの実測にもなる。

- 系: ACE-ALA-NME。tleap `sequence { ACE ALA NME }` で PDB を作り（`ala3-e2e/build/tleap_dry.in` を手本）、
  `register_local_structure` → prep（キャップ保持）→ solv（OPC、12 Å、0.15 M NaCl）→ topo（ff19SB/OPC/HMR）→
  min → eq（300 K、NVT 0.2 ns + NPT 1 ns、1 bar）。
- pcoord（2 次元）:

  ```json
  [{"type":"dihedral","name":"phi","selections":["resname ACE and name C","resname ALA and name N","resname ALA and name CA","resname ALA and name C"]},
   {"type":"dihedral","name":"psi","selections":["resname ALA and name N","resname ALA and name CA","resname ALA and name C","resname NME and name N"]}]
  ```

  bins: φ は 30° 刻み（edges −150 … 150）、ψ は 60° 刻み（−120 … 120）。target: `{"pcoord_ranges": [[20, 130], null]}`
  （φ が正 = αL 側）。basis: eq ノード（φ < 0）。`walkers_per_bin` 4、τ = 0.5 ns（`simulation_time_ns` 0.5、
  `output_frequency_ps` 25）、seed を変えて 2 scheme（`we1`, `we2`）。定常後（flux の立ち上がりが収まってから）
  30 round 以上。
- brute force（同じ eq から）: `run_production` 4 本 × 500 ns（seed 違い、出力 1 ps）。A(φ < −30°) → B(φ > 30°) の
  初通過時間を、20 ps 以上 B に留まったものだけ事象と数え、MFPT ± SE（事象数 ≥ 20 が目標）。解析は mdtraj の
  小さなスクリプトで構わない（`analysis/` に置き、report から参照）。20 ps の滞在条件は必須: 滞在なしだと φ の
  ±180° wrap が偽の遷移を約 10,000 件数え、WE の判定（segment 終点で target 内）とも一致しない（2026-09-22 注記）。
- 文献: Vitalini, Noé, Keller, *J. Chem. Phys.* 142, 084101 (2015)（力場別の implied timescales。ff19SB は無いので
  ff99SB-ILDN / ff03 の値と並べ、力場差として説明）。他に見つけた文献値も可（出典を必ず書く）。
- 合格の目安: 2 scheme の k_WE が brute force の 95 % 区間と重なり、verdict が `flux_steady`。
- 予算 ≈ 45 GPU 時間（brute force 2 µs + WE 2 scheme × 集計 2 µs）。

## 5. 課題 B: chignolin CLN025 の変性・折り畳み（340 K）— Anton の文献値と実験との比較

**理由**: Lindorff-Larsen et al., *Science* 334, 517 (2011) が CLN025 を 340 K で µs 級の folding / unfolding time
として報告している（CHARMM22*/TIP3P）。同じ温度で走らせれば文献 MD と直接比べられ、実験値も同じ論文の表から
引ける。力場は ff19SB/OPC なので、**同じ力場の brute force（340 K）を一次の参照**にし、文献 MD・実験を二次にする。

- 系: 5AWL 鎖 A。コスト半減のため `we-trials/cln025` に **緩衝 12 Å で作り直す**ことを勧める
  （既存 `seus-trials/cln025-e2e` は 20 Å。その 300 K 無バイアス 4 × 500 ns `prod_024/027/030/033` は 300 K の
  二次確認に使える）。topo ff19SB/OPC/HMR → min → eq 340 K（NPT 1 bar、2 ns）= **折り畳み側の basis**。
- 変性側の basis: eq 鎖で作る。min → eq(500 K、NVT、5 ns) → eq(340 K、NPT、2 ns)。最後の eq の
  `equilibrated.pdb` が変性している（Q < 0.2、RMSD > 0.6 nm）ことを `analyze_q_value` / `analyze_rmsd` で確認してから
  `start.node_ids` に使う。折り畳んだままなら 500 K を伸ばす。
- pcoord: `q`（`native_pdb` = 340 K 折り畳み eq の `equilibrated.pdb`、selection は既定）を主軸に、`rmsd`（同じ参照、
  backbone）を 2 次元目に。bins: q は 0.1 刻み（edges 0.1 … 0.9）、rmsd は 0.1 nm 刻み（0.1 … 0.8）。
  target: 変性 scheme は `[[null, 0.2], null]`（q ≤ 0.2）、折り畳み scheme は `[[0.8, null], null]`（q ≥ 0.8）。
- τ = 0.2 ns（出力 20 ps）、`walkers_per_bin` 5。まず方向ごとに 1 scheme（`unf1`, `fold1`）。予算が残れば 2 本目の seed。
- brute force（340 K、折り畳み eq から）: 4 本 × 1 µs（出力 10 ps）。q の時系列から変性（q < 0.2 に到達）と
  再折り畳み（q > 0.8）の初通過を数え、rate ± SE。300 K の既存 2 µs は 300 K の変性頻度の参考に。
- 文献: 上記 Science 2011 の Table 1（CLN025、340 K の folding / unfolding time と、引用されている実験値）。
  **数値は論文から自分で読み取り、出典と温度・力場を表に書く**（この依頼文には数値を書かない）。
- 合格の目安: WE の k_fold, k_unfold が同じ力場の brute force と因子 2 以内で一致（区間が重なる）、
  文献 MD とは桁で一致（力場差の議論つき）。
- 予算 ≈ 200 GPU 時間（12 Å 箱の場合。20 Å 箱なら倍）。**brute force と最初の 30 round が終わった時点で、
  実測の ns/day・オーバーヘッド・flux の立ち上がりを添えて一度報告し、続行の可否を確認する。**

## 6. 課題 C（任意）: リガンドの解離 — 実験値のある系

準備が MDClaw で素直にできるかを先に 2 時間以内で確かめ、できなければ §8 のフィードバックにして飛ばす。

- 候補 1: β-cyclodextrin + 小分子ゲスト（1-butanol、aspirin、naproxen など）。実験 k_off（超音波緩和法、
  Fukahori らの一連の報告）と MD の文献値（Tang & Chang, *J. Chem. Theory Comput.* 14, 303 (2018);
  SEEKR2, Votapka et al. 2022）がある。k_off は 10⁴–10⁷ s⁻¹ で WE に向く。ただしタンパク質の無い
  host–guest 系の prep（β-CD をリガンドとして扱う）が MDClaw で通るかは未確認。
- 候補 2: FKBP12 + フラグメント（DMSO、DSS、BUT）。Pan, Xu, Shaw, *J. Chem. Theory Comput.* 13, 3372 (2017) の
  Anton brute force の k_off / k_on と、Huang & Caflisch, *PLoS Comput. Biol.* 7, e1002002 (2011) が比較対象。
  タンパク質 + リガンドなので prep は標準ルート。
- pcoord: `distance`（結合部位の残基群 ↔ ゲストの重心、分子間なので最小像）+ `rmsd`（タンパク質 / host で重ね合わせ、
  ゲストで測る `align_selection`）。target: distance ≥ 1.2 nm（箱はその 2 倍 + 余裕。半箱長のガードに注意）。
  k_off = 定常 flux。結合方向（k_on）は別 scheme で、`rate_per_molar_per_s` を濃度の注記つきで報告。
- 予算 ≤ 100 GPU 時間。

## 7. 比較の作法と報告表

- 一次の参照は**同じ力場・同じ温度の brute force**。文献 MD（力場が違う）と実験は二次。差は力場・箱・温度で説明し、
  状態の定義（pcoord の範囲）を WE と brute force で完全に揃える。
- 誤差: WE は `analyze_we` の bootstrap 区間と、独立 scheme（seed 違い）の散らばり。brute force は事象数からの SE。
- 表の列: 系 / 遷移 / T / 力場・水 / 方法（WE: rounds、walker 数、集計 ns、verdict）/ k (s⁻¹) [95 %] / MFPT /
  参照 1: brute force 同力場 / 参照 2: 文献 MD（力場）/ 参照 3: 実験 / 比。

## 8. フィードバック（重要）

実行して気づいたことを**すべて**番号つきで書く（`WE-1`, `WE-2`, …）。対象は限定しない: スキルの記述が足りない・
誤っている、`next_action` や `code` が次の手を教えていない、結果の読み方が分からない、待ち時間が長い・進捗が見えない、
迷った箇所、取った回避策、ドキュメントの間違い、あれば便利な機能。各項目は次の形式:

```
WE-<n>: <一行の要約>
  何が起きた: ...（コマンド、返ってきた code / message を原文で）
  期待: ...
  再現: <コマンド>（job_dir / node_id / scheme_id つき）
  関連ファイル: result.json, node.json, slurm ログ, スキルの該当行
  提案: ...（任意）
```

うまく動いた場合も、「どこを読めば分かるか探すのに時間がかかった点」「最初に間違えた点」を書く。

## 9. 報告物

- `/data1/rkp00079/rku00161/we-trials/report.md`: §3 の数値、§7 の表、GPU 時間台帳、§8 のフィードバック、
  各 study の `we_kinetics.json` / `we_flux.png` / brute force 解析スクリプトへのパス。
- 最終メッセージに、`docs/memo.md` に貼れる形の要約（日付、系、数値、結論、未了）を付ける。checkout には書き込まない。

## 10. やらないこと

segment を手で走らせる（`run_rounds` だけが scheme を進める）、`node.json` / `progress.json` を編集する、
`nodes/` を `rm -rf` する、同じノードを再実行する、`pi update`、SIF の差し替え、checkout の変更、
予算超過、verdict を通すための target・τ・bootstrap の調整。
